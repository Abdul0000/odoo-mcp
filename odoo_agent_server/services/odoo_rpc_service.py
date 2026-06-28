from __future__ import annotations

import ssl
import xmlrpc.client
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from ..config import settings
from ..schemas import OdooRpcResult


@dataclass(frozen=True)
class OdooRpcConfig:
    base_url: str
    database: str
    login: str
    password: str


class OdooRpcService:
    """Minimal Odoo XML-RPC connector for module installation/update and read checks."""

    def _config(self) -> OdooRpcConfig:
        missing = []
        if not settings.odoo_base_url:
            missing.append("ODOO_BASE_URL")
        if not settings.odoo_db_name:
            missing.append("ODOO_DB_NAME")
        if not settings.odoo_login:
            missing.append("ODOO_LOGIN")
        if not settings.odoo_password:
            missing.append("ODOO_PASSWORD")
        if missing:
            raise ValueError("Missing Odoo RPC settings: " + ", ".join(missing))
        return OdooRpcConfig(
            base_url=settings.odoo_base_url.rstrip("/"),
            database=settings.odoo_db_name,
            login=settings.odoo_login,
            password=settings.odoo_password,
        )

    def _proxies(self) -> tuple[OdooRpcConfig, xmlrpc.client.ServerProxy, xmlrpc.client.ServerProxy, int]:
        cfg = self._config()
        import socket as _socket
        _socket.setdefaulttimeout(30)  # Prevent infinite blocking on hung Odoo server
        context = ssl.create_default_context()
        common = xmlrpc.client.ServerProxy(urljoin(cfg.base_url + "/", "xmlrpc/2/common"), allow_none=True, context=context)
        uid = common.authenticate(cfg.database, cfg.login, cfg.password, {})
        if not uid:
            raise ValueError("Odoo XML-RPC authentication failed — check ODOO_LOGIN and ODOO_PASSWORD")
        models = xmlrpc.client.ServerProxy(urljoin(cfg.base_url + "/", "xmlrpc/2/object"), allow_none=True, context=context)
        return cfg, common, models, int(uid)

    def execute_kw(self, model: str, method: str, args: list[Any] | None = None, kwargs: dict[str, Any] | None = None, superuser: bool = False) -> OdooRpcResult:
        try:
            cfg, _common, models, uid = self._proxies()
            # NOTE: superuser=True is kept for API compatibility but uid=1 does NOT work
            # on Odoo.sh because the user's password != admin password.
            # All operations use the configured ODOO_LOGIN uid.
            # Access Denied → use SSH fallback (_ssh_fallback_result).
            result = models.execute_kw(cfg.database, uid, cfg.password, model, method, args or [], kwargs or {})
            return OdooRpcResult(status="ok", base_url=cfg.base_url, database=cfg.database, result=result)
        except Exception as exc:  # noqa: BLE001 - return details to user/agent
            return OdooRpcResult(status="failed", result=None, error=str(exc))

    def health(self) -> OdooRpcResult:
        try:
            cfg = self._config()
            context = ssl.create_default_context()
            common = xmlrpc.client.ServerProxy(urljoin(cfg.base_url + "/", "xmlrpc/2/common"), allow_none=True, context=context)
            version = common.version()
            uid = common.authenticate(cfg.database, cfg.login, cfg.password, {})
            if not uid:
                return OdooRpcResult(status="failed", base_url=cfg.base_url, database=cfg.database, error="Authentication failed")
            return OdooRpcResult(status="ok", base_url=cfg.base_url, database=cfg.database, result={"uid": uid, "version": version})
        except Exception as exc:  # noqa: BLE001
            return OdooRpcResult(status="failed", error=str(exc))

    def module_status(self, module_name: str) -> OdooRpcResult:
        return self.execute_kw(
            "ir.module.module",
            "search_read",
            [[("name", "=", module_name)]],
            {"fields": ["name", "state", "latest_version", "installed_version"], "limit": 1},
        )


    def module_dependencies_status(self, module_name: str) -> OdooRpcResult:
        """Return dependency states for an Odoo module.

        This catches missing/uninstalled dependencies before long installs/upgrades.
        It uses superuser uid where possible because Settings users often cannot read
        ir.module.module.dependency directly.
        """
        module_id_result = self._module_id(module_name)
        if module_id_result.status != "ok":
            return module_id_result
        deps = self.execute_kw(
            "ir.module.module.dependency",
            "search_read",
            [[("module_id", "=", module_id_result.result)]],
            {"fields": ["name", "state"], "limit": 200},
            superuser=True,
        )
        if deps.status != "ok":
            return deps
        rows = deps.result or []
        missing = [row for row in rows if row.get("state") not in ("installed", "to install", "to upgrade")]
        return OdooRpcResult(status="ok", result={"dependencies": rows, "missing": missing})

    def _dependency_guard(self, module_name: str) -> OdooRpcResult | None:
        """Fix 10: Auto-install missing dependencies instead of just failing.

        When dependencies are uninstalled, install them automatically one by
        one before the main module is touched. Returns an OdooRpcResult error
        only if auto-install itself fails.  Returns None when all deps are met.
        """
        deps = self.module_dependencies_status(module_name)
        if deps.status != "ok":
            # Can't read deps (e.g. module not in registry yet) — let the main
            # install surface the real error.
            return None
        missing = (deps.result or {}).get("missing") or []
        if not missing:
            return None  # all dependencies satisfied
        missing_names = sorted({str(row.get("name")) for row in missing})
        auto_install_results: list[str] = []
        for dep_name in missing_names:
            dep_id_result = self._module_id(dep_name)
            if dep_id_result.status != "ok":
                return OdooRpcResult(
                    status="failed",
                    error=(
                        f"Dependency '{dep_name}' is not registered in Odoo. "
                        f"Add it to your custom addons path or install it first."
                    ),
                )
            install_result = self.execute_kw(
                "ir.module.module",
                "button_immediate_install",
                [[dep_id_result.result]],
                superuser=True,
            )
            if install_result.status != "ok":
                return OdooRpcResult(
                    status="failed",
                    error=(
                        f"Auto-install of dependency '{dep_name}' failed: {install_result.error}. "
                        f"Install it manually then retry."
                    ),
                )
            auto_install_results.append(dep_name)
        # Stash a note so callers can surface it; not an error.
        if auto_install_results:
            import logging
            logging.getLogger(__name__).info(
                "odoo.ensure_dependencies: auto-installed %s for %s",
                auto_install_results,
                module_name,
            )
        return None

    def ensure_dependencies(self, module_name: str) -> OdooRpcResult:
        """Fix 10: Public tool-callable method to auto-install missing dependencies."""
        err = self._dependency_guard(module_name)
        if err:
            return err
        return OdooRpcResult(status="ok", result={"message": f"All dependencies satisfied for {module_name}"})


    def _ssh_fallback_result(self, module_name: str, operation: str, cause: str) -> OdooRpcResult:
        """Fallback to Odoo.sh SSH when XML-RPC is blocked by access rights.

        This keeps odoo.module_deploy as the single tool an agent should call for
        both installs and upgrades. The router already runs this method in a
        background job, so SSH can take several minutes without Render HTTP
        timeouts.
        """
        try:
            from .odoosh_service import OdooShService

            service = OdooShService()
            if operation == "install":
                ssh = service.module_install_ssh(module_name, timeout_seconds=1800)
            elif operation == "update":
                ssh = service.module_update(module_name, timeout_seconds=1800)
            else:
                ssh = service.module_deploy_ssh(module_name, timeout_seconds=1800)
            status = "ok" if ssh.exit_code == 0 else "failed"
            return OdooRpcResult(
                status=status,
                result={
                    "fallback": "odoosh",
                    "operation": operation,
                    "module": module_name,
                    "ssh": ssh.model_dump(mode="json"),
                    "cause": cause,
                } if status == "ok" else None,
                error=None if status == "ok" else (
                    f"XML-RPC failed and SSH fallback also failed. XML-RPC cause: {cause}\n"
                    f"SSH stdout:\n{ssh.stdout}\nSSH stderr:\n{ssh.stderr}"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return OdooRpcResult(status="failed", error=f"XML-RPC failed and SSH fallback could not start: {cause}; fallback_error={exc}")

    @staticmethod
    def _looks_access_denied(result: OdooRpcResult) -> bool:
        return result.status == "failed" and "Access Denied" in (result.error or "")

    def module_install(self, module_name: str) -> OdooRpcResult:
        dep_error = self._dependency_guard(module_name)
        if dep_error:
            return dep_error
        module_id_result = self._module_id(module_name)
        if module_id_result.status != "ok":
            return module_id_result
        module_id = module_id_result.result
        result = self.execute_kw("ir.module.module", "button_immediate_install", [[module_id]], superuser=True)
        if result.status == "failed":
            if self._looks_access_denied(result):
                return self._ssh_fallback_result(module_name, "install", f"button_immediate_install failed: {result.error}")
            result = OdooRpcResult(status="failed", error=f"button_immediate_install failed: {result.error}\nACTION: Call odoosh.error_pack → fix code → recommit → push → redeploy.")
        return result

    def module_update(self, module_name: str) -> OdooRpcResult:
        dep_error = self._dependency_guard(module_name)
        if dep_error:
            return dep_error
        module_id_result = self._module_id(module_name)
        if module_id_result.status != "ok":
            return module_id_result
        module_id = module_id_result.result
        result = self.execute_kw("ir.module.module", "button_immediate_upgrade", [[module_id]], superuser=True)
        if result.status == "failed":
            if self._looks_access_denied(result):
                return self._ssh_fallback_result(module_name, "update", f"button_immediate_upgrade failed: {result.error}")
            result = OdooRpcResult(status="failed", error=f"button_immediate_upgrade failed: {result.error}\nACTION: Call odoosh.error_pack → fix code → recommit → push → redeploy.")
        return result

    def module_deploy(self, module_name: str) -> OdooRpcResult:
        """Smart deploy sequence:
        1. update_list (register new modules)
        2. check state
        3a. uninstalled → button_immediate_install
        3b. installed   → button_immediate_upgrade
        On failure: returns exact error + action steps for agent.
        """
        import time

        update_result = self.execute_kw("ir.module.module", "update_list", [], superuser=True)
        if update_result.status == "failed":
            if self._looks_access_denied(update_result):
                return self._ssh_fallback_result(module_name, "deploy", f"update_list failed: {update_result.error}")
            return OdooRpcResult(status="failed", error=f"update_list failed: {update_result.error}")

        time.sleep(3)

        status_result = self.module_status(module_name)
        if status_result.status != "ok":
            return status_result

        records = status_result.result or []
        if not records:
            return OdooRpcResult(
                status="failed",
                error=(
                    f"Module {module_name!r} not found after update_list.\n"
                    "ACTIONS:\n"
                    "1. Call odoosh.grep_errors — check for Python import errors\n"
                    "2. Verify __manifest__.py has installable=True\n"
                    "3. Call odoosh.module_update (SSH) then retry odoo.module_deploy"
                )
            )

        dep_error = self._dependency_guard(module_name)
        if dep_error:
            return dep_error

        state = records[0].get("state", "unknown")
        module_id = records[0].get("id")

        if state == "installed":
            method = "button_immediate_upgrade"
            result = self.execute_kw("ir.module.module", method, [[module_id]], superuser=True)
        else:
            # For new installs: XML-RPC may be blocked on Odoo.sh
            # Fallback instruction to use odoosh.module_install_ssh
            method = "button_immediate_install"
            result = self.execute_kw("ir.module.module", method, [[module_id]], superuser=True)

        if result.status == "failed":
            if self._looks_access_denied(result):
                return self._ssh_fallback_result(module_name, "deploy", f"{method} failed on {module_name!r}: {result.error}")
            return OdooRpcResult(
                status="failed",
                error=(
                    f"{method} FAILED on {module_name!r} (was: {state})\n"
                    f"ERROR: {result.error}\n"
                    "If error is code error: odoosh.error_pack → fix → recommit → redeploy."
                )
            )

        return OdooRpcResult(
            status="ok",
            result={"method": method, "previous_state": state, "module": module_name}
        )

    def search_read(self, model: str, domain: list[Any], fields: list[str], limit: int) -> OdooRpcResult:
        return self.execute_kw(model, "search_read", [domain], {"fields": fields, "limit": limit})


    def registry_smoke_test(self, module_name: str, model_names: list[str] | None = None, menu_names: list[str] | None = None, strict: bool = True) -> OdooRpcResult:
        """Verify the upgraded Odoo registry actually contains expected models/menus.

        This catches the class of failures where a menu/action points to a model
        like school.student but the server registry did not load that model.
        """
        model_names = list(model_names or [])
        menu_names = list(menu_names or [])
        checks: dict[str, Any] = {"module": module_name, "models": [], "menus": [], "module_status": None}
        failures: list[str] = []

        status_result = self.module_status(module_name)
        checks["module_status"] = status_result.model_dump(mode="json")
        if status_result.status != "ok" or not status_result.result:
            failures.append(f"Module {module_name} not found in ir.module.module")
        else:
            state = (status_result.result[0] or {}).get("state")
            if state != "installed":
                failures.append(f"Module {module_name} state is {state}, expected installed")

        if not model_names:
            prefix = module_name.split("_", 1)[0]
            if prefix and prefix not in {"odoo", "base"}:
                inferred = self.execute_kw("ir.model", "search_read", [[("model", "=ilike", prefix + ".%")]], {"fields": ["model", "name"], "limit": 200})
                if inferred.status == "ok":
                    model_names = sorted({row.get("model") for row in (inferred.result or []) if row.get("model")})

        for model_name in model_names:
            exists = self.execute_kw("ir.model", "search_read", [[("model", "=", model_name)]], {"fields": ["model", "name"], "limit": 1})
            item: dict[str, Any] = {"model": model_name, "exists": False, "search_read_ok": False}
            if exists.status == "ok" and exists.result:
                item["exists"] = True
                sr = self.search_read(model_name, [], ["id"], 1)
                item["search_read_ok"] = sr.status == "ok"
                if sr.status != "ok":
                    item["error"] = sr.error
                    failures.append(f"Model {model_name} exists but search_read failed: {sr.error}")
            else:
                item["error"] = exists.error or "missing in ir.model"
                failures.append(f"Model {model_name} missing from registry")
            checks["models"].append(item)

        for menu_name in menu_names:
            menu = self.execute_kw("ir.ui.menu", "search_read", [[("name", "=", menu_name)]], {"fields": ["name", "complete_name", "action", "active"], "limit": 20})
            ok = menu.status == "ok" and bool(menu.result)
            checks["menus"].append({"name": menu_name, "exists": ok, "records": menu.result if menu.status == "ok" else [], "error": menu.error})
            if not ok:
                failures.append(f"Menu {menu_name} missing or inaccessible")

        checks["failures"] = failures
        if strict and failures:
            return OdooRpcResult(status="failed", result=checks, error="; ".join(failures))
        return OdooRpcResult(status="ok", result=checks)

    def _module_id(self, module_name: str) -> OdooRpcResult:
        status_result = self.execute_kw("ir.module.module", "search", [[("name", "=", module_name)]], {"limit": 1})
        if status_result.status != "ok":
            return status_result
        ids = status_result.result or []
        if not ids:
            return OdooRpcResult(status="failed", error=f"Module not found in Odoo apps registry: {module_name}")
        return OdooRpcResult(status="ok", result=ids[0])
