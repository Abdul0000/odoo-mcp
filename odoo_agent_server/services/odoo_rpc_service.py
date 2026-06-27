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

    def execute_kw(self, model: str, method: str, args: list[Any] | None = None, kwargs: dict[str, Any] | None = None) -> OdooRpcResult:
        try:
            cfg, _common, models, uid = self._proxies()
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

    def module_install(self, module_name: str) -> OdooRpcResult:
        module_id_result = self._module_id(module_name)
        if module_id_result.status != "ok":
            return module_id_result
        module_id = module_id_result.result
        return self.execute_kw("ir.module.module", "button_immediate_install", [[module_id]])

    def module_update(self, module_name: str) -> OdooRpcResult:
        module_id_result = self._module_id(module_name)
        if module_id_result.status != "ok":
            return module_id_result
        module_id = module_id_result.result
        return self.execute_kw("ir.module.module", "button_immediate_upgrade", [[module_id]])

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
