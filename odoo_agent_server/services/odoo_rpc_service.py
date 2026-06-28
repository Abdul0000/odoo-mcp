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
        # Odoo.sh can use valid certs; this context is default-verified.
        context = ssl.create_default_context()
        common = xmlrpc.client.ServerProxy(urljoin(cfg.base_url + "/", "xmlrpc/2/common"), allow_none=True, context=context)
        uid = common.authenticate(cfg.database, cfg.login, cfg.password, {})
        if not uid:
            raise ValueError("Odoo XML-RPC authentication failed")
        models = xmlrpc.client.ServerProxy(urljoin(cfg.base_url + "/", "xmlrpc/2/object"), allow_none=True, context=context)
        return cfg, common, models, int(uid)

    def execute_kw(self, model: str, method: str, args: list[Any] | None = None, kwargs: dict[str, Any] | None = None, superuser: bool = False) -> OdooRpcResult:
        try:
            cfg, _common, models, uid = self._proxies()
            # For module install/upgrade use superuser (uid=1) to bypass access restrictions
            effective_uid = 1 if superuser else uid
            result = models.execute_kw(cfg.database, effective_uid, cfg.password, model, method, args or [], kwargs or {})
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

    def module_install(self, module_name: str) -> OdooRpcResult:
        module_id_result = self._module_id(module_name)
        if module_id_result.status != "ok":
            return module_id_result
        module_id = module_id_result.result
        result = self.execute_kw("ir.module.module", "button_immediate_install", [[module_id]], superuser=True)
        if result.status == "failed":
            result = OdooRpcResult(status="failed", error=f"button_immediate_install failed: {result.error}\nACTION: Call odoosh.grep_errors → fix code → recommit → push → redeploy.")
        return result

    def module_update(self, module_name: str) -> OdooRpcResult:
        module_id_result = self._module_id(module_name)
        if module_id_result.status != "ok":
            return module_id_result
        module_id = module_id_result.result
        result = self.execute_kw("ir.module.module", "button_immediate_upgrade", [[module_id]], superuser=True)
        if result.status == "failed":
            result = OdooRpcResult(status="failed", error=f"button_immediate_upgrade failed: {result.error}\nACTION: Call odoosh.grep_errors → fix code → recommit → push → redeploy.")
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

        state = records[0].get("state", "unknown")
        module_id = records[0].get("id")

        if state == "installed":
            method = "button_immediate_upgrade"
        else:
            method = "button_immediate_install"

        result = self.execute_kw("ir.module.module", method, [[module_id]], superuser=True)

        if result.status == "failed":
            return OdooRpcResult(
                status="failed",
                error=(
                    f"{method} FAILED on {module_name!r} (was: {state})\n"
                    f"ERROR: {result.error}\n"
                    "ROOT CAUSE: Python/XML error in module code.\n"
                    "REQUIRED STEPS:\n"
                    "1. odoosh.grep_errors — read full traceback\n"
                    "2. repo.read the broken file\n"
                    "3. repo.write with fix\n"
                    "4. git.commit → git.push → odoosh.check_commit → odoo.module_deploy"
                )
            )

        return OdooRpcResult(
            status="ok",
            result={"method": method, "previous_state": state, "module": module_name}
        )

    def search_read(self, model: str, domain: list[Any], fields: list[str], limit: int) -> OdooRpcResult:
        return self.execute_kw(model, "search_read", [domain], {"fields": fields, "limit": limit})

    def _module_id(self, module_name: str) -> OdooRpcResult:
        status_result = self.execute_kw("ir.module.module", "search", [[("name", "=", module_name)]], {"limit": 1})
        if status_result.status != "ok":
            return status_result
        ids = status_result.result or []
        if not ids:
            return OdooRpcResult(status="failed", error=f"Module not found in Odoo apps registry: {module_name}")
        return OdooRpcResult(status="ok", result=ids[0])
