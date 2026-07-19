import json

from odoo_agent_server.routers import tools
from odoo_agent_server.schemas import OdooFunctionalQueryRequest, OdooResult


def _client_env():
    return json.dumps([
        {
            "name": "ClientA",
            "repo": "https://github.com/ClientA/ClientA.git",
            "branch": "22-06-2026",
            "path": "/workspace/repos/ClientA",
            "token": "ghp_test",
            "odoo_url": "https://client-a.example.odoo.com",
            "odoo_db": "client-a-db",
            "odoo_login": "bad@example.com",
            "odoo_password": "bad",
            "odoosh_db_name": "client-a-db",
            "odoosh_ssh_host": "client-a.example.odoo.com",
            "odoosh_ssh_port": 22,
            "odoosh_ssh_user": "34342188",
            "odoosh_ssh_private_key_b64": "ZmFrZQ==",
            "odoosh_odoo_path": "/home/odoo/src/odoo",
            "odoosh_user_addons_path": "/home/odoo/src/user",
        }
    ])


def test_odoo_query_falls_back_to_ssh_when_shell_fails(monkeypatch):
    monkeypatch.setenv("ODOO_CLIENTS", _client_env())

    def fake_shell_search_read(self, model, domain, fields, limit, offset, order, context):
        return OdooResult(status="failed", error="Odoo Odoo shell authentication failed")

    def fake_ssh_search_read(self, model, domain, fields, limit=20, offset=0, order=None, context=None, timeout_seconds=120):
        assert model == "account.move"
        assert ["move_type", "=", "out_invoice"] in [[*item] for item in domain]
        return OdooResult(
            status="ok",
            database="client-a-db",
            result=[{"name": "INV/2026/00001", "amount_total": 100.0, "state": "posted"}],
        )

    monkeypatch.setattr(tools.OdooService, "search_read", fake_shell_search_read)
    monkeypatch.setattr(tools.OdooShService, "search_read", fake_ssh_search_read)

    result = tools._odoo_functional_query(
        OdooFunctionalQueryRequest(client_name="ClientA", query="latest customer invoices", limit=5)
    )

    assert result["status"] == "ok"
    assert result["source"] == "odoosh_ssh_fallback"
    assert result["ssh_fallback_used"] is True
    assert result["odoo"]["result"][0]["name"] == "INV/2026/00001"


def test_odoo_query_can_infer_client_name_from_text(monkeypatch):
    monkeypatch.setenv("ODOO_CLIENTS", _client_env())

    seen = {}

    def fake_shell_search_read(self, model, domain, fields, limit, offset, order, context):
        return OdooResult(status="failed", error="bad credentials")

    def fake_ssh_search_read(self, model, domain, fields, limit=20, offset=0, order=None, context=None, timeout_seconds=120):
        seen["client"] = self.client.name
        return OdooResult(status="ok", database="client-a-db", result=[])

    monkeypatch.setattr(tools.OdooService, "search_read", fake_shell_search_read)
    monkeypatch.setattr(tools.OdooShService, "search_read", fake_ssh_search_read)

    result = tools._odoo_functional_query(
        OdooFunctionalQueryRequest(query="Use ClientA and read users", limit=3)
    )

    assert result["status"] == "ok"
    assert result["client_name"] == "ClientA"
    assert seen["client"] == "ClientA"

from odoo_agent_server.services.client_resolver_service import ResolvedClient
from odoo_agent_server.services.odoosh_service import OdooShService
from odoo_agent_server.schemas import CommandResult


def test_odoosh_search_read_parses_shell_marker(monkeypatch):
    client = ResolvedClient(
        name="ClientA",
        repo="https://github.com/ClientA/ClientA.git",
        branch="22-06-2026",
        path="/workspace/repos/ClientA",
        odoosh_db_name="client-a-db",
        odoosh_ssh_host="host",
        odoosh_ssh_user="34342188",
        odoosh_ssh_private_key_b64="ZmFrZQ==",
        odoosh_odoo_path="/home/odoo/src/odoo",
        odoosh_user_addons_path="/home/odoo/src/user",
    )

    def fake_run_shell(self, remote_script, timeout_seconds=60):
        assert "./odoo-bin shell" in remote_script
        assert "MCP_ODOOSH_QUERY_JSON:" in remote_script
        return CommandResult(
            command=["ssh"],
            cwd="odoosh:ssh",
            exit_code=0,
            stdout='noise\nMCP_ODOOSH_QUERY_JSON:{"status":"ok","result":[{"name":"INV/1"}],"count":1}\n',
        )

    monkeypatch.setattr(OdooShService, "run_shell", fake_run_shell)
    result = OdooShService(client).search_read(
        "account.move",
        [["move_type", "=", "out_invoice"]],
        ["name"],
        limit=1,
    )
    assert result.status == "ok"
    assert result.database == "client-a-db"
    assert result.result == [{"name": "INV/1"}]
