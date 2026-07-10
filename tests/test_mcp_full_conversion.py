from __future__ import annotations

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.tools import list_tools


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"}


EXPECTED_OPERATIONAL_MCP_TOOLS = {tool.name for tool in list_tools()}


def test_mcp_tools_list_exposes_every_operational_api_tool():
    client = TestClient(app)
    initialized = client.post("/mcp", headers=_auth(), json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    assert initialized.status_code == 200
    assert initialized.json()["result"]["serverInfo"]["name"] == "odoo-mcp-agent-connector"

    response = client.post("/mcp", headers=_auth(), json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == EXPECTED_OPERATIONAL_MCP_TOOLS
    assert len(tools) == len(EXPECTED_OPERATIONAL_MCP_TOOLS)
    assert "qa_self_run" in names
    assert "repo_apply_patch_atomic" in names
    assert "git_push" in names
    assert "odoo_module_deploy" in names
    assert "approvals_approve" not in names
    assert all("." not in name for name in names)
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert "description" in tool
        assert "annotations" in tool


def test_rest_tools_and_mcp_tools_have_same_surface():
    client = TestClient(app)
    rest = client.get("/tools/list", headers=_auth())
    assert rest.status_code == 200
    rest_names = {tool["name"] for tool in rest.json()}

    mcp = client.post("/mcp", headers=_auth(), json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert mcp.status_code == 200
    mcp_names = {tool["name"] for tool in mcp.json()["result"]["tools"]}
    assert mcp_names == rest_names == EXPECTED_OPERATIONAL_MCP_TOOLS


def test_mcp_supports_health_approval_job_and_audit_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    client = TestClient(app)

    health = client.post(
        "/mcp",
        headers=_auth(),
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "connector.health", "arguments": {}}},
    )
    assert health.status_code == 200
    assert health.json()["result"]["structuredContent"]["status"] == "ok"
    assert health.json()["result"]["structuredContent"]["service"] == "odoo-mcp-agent-connector"

    requested = client.post(
        "/mcp",
        headers=_auth(),
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "approvals.request",
                "arguments": {"action": "repo.write", "summary": "test approval", "payload_preview": {"file": "a.txt"}},
            },
        },
    )
    assert requested.status_code == 200
    approval = requested.json()["result"]["structuredContent"]
    assert approval["action"] == "repo.write"
    assert approval["status"] == "pending"

    listed_approvals = client.post(
        "/mcp",
        headers=_auth(),
        json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "approvals.list", "arguments": {"limit": 10}}},
    )
    assert listed_approvals.status_code == 200
    approvals_payload = listed_approvals.json()["result"]["structuredContent"]
    assert any(item["id"] == approval["id"] for item in approvals_payload["items"])

    audit = client.post(
        "/mcp",
        headers=_auth(),
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "audit.list", "arguments": {"limit": 20}}},
    )
    assert audit.status_code == 200
    audit_payload = audit.json()["result"]["structuredContent"]
    assert any(event["action"].startswith("approval.") for event in audit_payload["items"])


def test_mcp_repo_write_many_smart_accepts_path_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    project.mkdir()
    client = TestClient(app)

    response = client.post(
        "/mcp",
        headers=_auth(),
        json={
            "jsonrpc": "2.0",
            "id": 70,
            "method": "tools/call",
            "params": {
                "name": "repo_write_many_smart",
                "arguments": {
                    "project_path": str(project),
                    "files": [
                        {"path": "sale_order_notebook_tab/models/sale_order.py", "content": "from odoo import fields, models\n"}
                    ],
                },
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()["result"]
    assert payload["isError"] is False
    assert payload["structuredContent"]["exit_code"] == 0
    assert "Pydantic" not in payload["content"][0]["text"]
    assert (project / "sale_order_notebook_tab" / "models" / "sale_order.py").read_text(encoding="utf-8") == "from odoo import fields, models\n"


def test_mcp_validation_errors_are_agent_friendly(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    project.mkdir()
    client = TestClient(app)

    response = client.post(
        "/mcp",
        headers=_auth(),
        json={
            "jsonrpc": "2.0",
            "id": 71,
            "method": "tools/call",
            "params": {
                "name": "repo_write_many_smart",
                "arguments": {"project_path": str(project), "files": [{"path": "x.py"}]},
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()["result"]
    assert payload["isError"] is True
    assert payload["structuredContent"]["error_type"] == "tool_input_invalid"
    assert "Tool input invalid" in payload["content"][0]["text"]
    assert "pydantic.dev" not in payload["content"][0]["text"]


def test_mcp_workflow_capability_check_tool():
    client = TestClient(app)
    response = client.post(
        "/mcp",
        headers=_auth(),
        json={"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "workflow.capability_check", "arguments": {}}},
    )
    assert response.status_code == 200
    payload = response.json()["result"]
    assert payload["isError"] is False
    assert payload["structuredContent"]["status"] in {"ready", "partial", "failed"}
    assert "checks" in payload["structuredContent"]
