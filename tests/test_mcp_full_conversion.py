from __future__ import annotations

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"}


EXPECTED_OPERATIONAL_MCP_TOOLS = {
    # connector/root health
    "connector.health",
    # workflow
    "workflow.capability_check",
    "workflow.full_verify",
    # QA
    "qa.self_run",
    # repo
    "repo.list",
    "repo.read",
    "repo.write",
    "repo.delete",
    "repo.apply_patch",
    "repo.clean_pyc",
    # git
    "git.status",
    "git.current_branch",
    "git.diff",
    "git.commit",
    "git.push",
    # Odoo.sh
    "odoosh.health",
    "odoosh.git_status",
    "odoosh.log",
    "odoosh.grep_errors",
    "odoosh.module_update",
    # Odoo RPC
    "odoo.health",
    "odoo.module_status",
    "odoo.module_install",
    "odoo.module_update",
    "odoo.search_read",
    # browser
    "browser.screenshot",
    # approvals
    "approvals.list",
    "approvals.request",
    "approvals.approve",
    "approvals.reject",
    # jobs/audit
    "jobs.list",
    "jobs.get",
    "audit.list",
}


def test_mcp_tools_list_exposes_every_operational_api_tool():
    client = TestClient(app)
    response = client.post("/mcp", headers=_auth(), json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == EXPECTED_OPERATIONAL_MCP_TOOLS
    assert len(tools) == 33
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
    assert any(item["id"] == approval["id"] for item in listed_approvals.json()["result"]["structuredContent"])

    approved = client.post(
        "/mcp",
        headers=_auth(),
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "approvals.approve", "arguments": {"approval_id": approval["id"], "reason": "ok"}},
        },
    )
    assert approved.status_code == 200
    assert approved.json()["result"]["structuredContent"]["status"] == "approved"

    audit = client.post(
        "/mcp",
        headers=_auth(),
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "audit.list", "arguments": {"limit": 20}}},
    )
    assert audit.status_code == 200
    assert any(event["action"].startswith("approval.") for event in audit.json()["result"]["structuredContent"])


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
