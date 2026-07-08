from __future__ import annotations

import re
import time
from typing import Any

import httpx

from ..config import settings
from ..schemas import OdooResult
from .client_resolver_service import ResolvedClient


class OdooJsonRpcService:
    """Read-only Odoo HTTPS JSON-RPC fallback.

    This is intentionally limited to safe query operations such as
    ``search_read``. Deploy/install/upgrade still go through the safe
    Odoo.sh SSH pipeline so deployment verification remains strict.
    """

    def __init__(self, client: ResolvedClient | None = None) -> None:
        self.client = client

    def _base_url(self) -> str | None:
        return ((self.client.odoo_url if self.client else None) or settings.odoo_base_url or "").rstrip("/") or None

    def _db(self) -> str | None:
        return (self.client.odoo_db if self.client else None) or settings.odoo_db_name

    def _login(self) -> str | None:
        return (self.client.odoo_login if self.client else None) or settings.odoo_login

    def _password(self) -> str | None:
        return (self.client.odoo_password if self.client else None) or settings.odoo_password

    def configured(self) -> bool:
        return bool(self._base_url() and self._db() and self._login() and self._password())

    def _verify_ssl(self) -> bool:
        if self.client is not None and self.client.odoo_verify_ssl is not None:
            return bool(self.client.odoo_verify_ssl)
        return bool(settings.odoo_https_verify_ssl)

    def _call(self, service: str, method: str, args: list[Any], timeout_seconds: int) -> Any:
        base_url = self._base_url()
        if not base_url:
            raise RuntimeError("ODOO_BASE_URL/odoo_url is not configured for HTTPS JSON-RPC fallback.")
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": int(time.time() * 1000),
        }
        with httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds)),
            verify=self._verify_ssl(),
        ) as client:
            response = client.post(f"{base_url}/jsonrpc", json=payload)
            response.raise_for_status()
            data = response.json()
        if data.get("error"):
            error = data["error"]
            message = error.get("message") or error.get("data", {}).get("message") or str(error)
            raise RuntimeError(message)
        return data.get("result")

    def _login_uid(self, timeout_seconds: int) -> int:
        db = self._db()
        login = self._login()
        password = self._password()
        if not (db and login and password):
            raise RuntimeError("Odoo HTTPS credentials are incomplete for JSON-RPC fallback.")
        uid = self._call("common", "login", [db, login, password], timeout_seconds)
        if not uid:
            raise RuntimeError("Odoo HTTPS JSON-RPC login failed.")
        return int(uid)

    def _format_exception(self, exc: Exception) -> str:
        message = f"Odoo HTTPS JSON-RPC fallback failed: {exc}"
        if isinstance(exc, httpx.ConnectError) and "CERTIFICATE_VERIFY_FAILED" in str(exc):
            message += (
                "; TLS certificate verification failed. For Odoo.sh/dev hosts with a known "
                "certificate hostname mismatch, set odoo_verify_ssl=false on that ODOO_CLIENTS "
                "entry or set ODOO_HTTPS_VERIFY_SSL=false for single-client/dev usage. "
                "Do not disable verification for untrusted production hosts."
            )
        return message

    def search_read(
        self,
        model: str,
        domain: list | None = None,
        fields: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        order: str | None = None,
        context: dict[str, Any] | None = None,
        timeout_seconds: int = 60,
    ) -> OdooResult:
        if not self.configured():
            return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error="Odoo HTTPS JSON-RPC fallback is not configured for this client.")
        if not re.fullmatch(r"[a-zA-Z0-9_.]+", model or ""):
            return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error="Invalid model name for JSON-RPC query.")
        safe_fields: list[str] = []
        for field in fields or []:
            if not isinstance(field, str) or not re.fullmatch(r"[a-zA-Z0-9_]+", field):
                return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error=f"Invalid field name for JSON-RPC query: {field!r}")
            safe_fields.append(field)
        if order and not re.fullmatch(r"[a-zA-Z0-9_,.\s]+(?:\s+(?:asc|desc))?", order, flags=re.I):
            return OdooResult(status="failed", base_url=self._base_url(), database=self._db(), error="Invalid order string for JSON-RPC query.")

        kwargs: dict[str, Any] = {
            "fields": safe_fields,
            "limit": max(1, min(int(limit or 20), 200)),
            "offset": max(0, int(offset or 0)),
        }
        if order:
            kwargs["order"] = order
        if context:
            kwargs["context"] = context

        try:
            uid = self._login_uid(timeout_seconds)
            db = self._db()
            password = self._password()
            records = self._call(
                "object",
                "execute_kw",
                [db, uid, password, model, "search_read", [domain or []], kwargs],
                timeout_seconds,
            )
            return OdooResult(
                status="ok",
                base_url=self._base_url(),
                database=self._db(),
                result=records or [],
            )
        except Exception as exc:
            return OdooResult(
                status="failed",
                base_url=self._base_url(),
                database=self._db(),
                error=self._format_exception(exc),
            )
