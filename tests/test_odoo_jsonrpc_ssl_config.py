from __future__ import annotations

from odoo_agent_server.services.client_resolver_service import ResolvedClient
from odoo_agent_server.services.odoo_jsonrpc_service import OdooJsonRpcService
from odoo_agent_server.config import settings


def test_jsonrpc_respects_per_client_ssl_verify_false(monkeypatch):
    client = ResolvedClient(
        name="SproutBoxes",
        repo="https://github.com/SproutBoxes/SproutBoxes.git",
        branch="22-06-2026",
        path="/workspace/repos/SproutBoxes",
        odoo_url="https://sproutboxes.dev.odoo.com",
        odoo_db="sproutboxes-db",
        odoo_login="admin@example.com",
        odoo_password="secret",
        odoo_verify_ssl=False,
    )

    assert OdooJsonRpcService(client)._verify_ssl() is False


def test_jsonrpc_defaults_to_global_ssl_setting(monkeypatch):
    monkeypatch.setattr(settings, "odoo_https_verify_ssl", True)
    client = ResolvedClient(
        name="SproutBoxes",
        repo="https://github.com/SproutBoxes/SproutBoxes.git",
        branch="22-06-2026",
        path="/workspace/repos/SproutBoxes",
        odoo_url="https://sproutboxes.dev.odoo.com",
        odoo_db="sproutboxes-db",
        odoo_login="admin@example.com",
        odoo_password="secret",
    )

    assert OdooJsonRpcService(client)._verify_ssl() is True
