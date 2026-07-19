from fastapi.testclient import TestClient

from odoo_agent_server.main import app
from odoo_agent_server.config import settings


def _auth():
    return {"Authorization": f"Bearer {settings.api_token}"}


def test_health_and_root_are_backend_only():
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["service"] == "odoo-mcp-agent-connector"
    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["service"] == "Odoo MCP Agent Connector"
    assert root.json()["ui"] == "not_required"
    assert root.json()["openapi"] == "/openapi.json"
    assert root.json()["mcp"] == "/mcp"
    assert root.json()["compatibility_mcp"]["chatgpt"] == "/chatgpt/mcp"


def test_tools_are_exposed_for_direct_chatgpt():
    client = TestClient(app)
    response = client.get("/tools/list", headers=_auth())
    assert response.status_code == 200
    names = {item["name"] for item in response.json()}
    assert "qa_self_run" in names
    assert "repo_apply_patch_atomic" in names
    assert "git_push" in names
    assert "odoosh_grep_errors" in names
    assert "odoosh_read_user_file" in names
    assert "workflow_verify_deployed_module" in names
    assert "browser_screenshot" in names
    assert "skills_list" in names
    assert "skills_read" in names


def test_capability_check_loads():
    client = TestClient(app)
    response = client.get("/workflow/capability-check", headers=_auth())
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ready", "partial", "failed"}
    assert "git" in payload["checks"]
    assert "allowed_roots" in payload["checks"]


def test_agent_instructions_require_complete_safe_deploy_flow():
    client = TestClient(app)
    response = client.get("/agent-instructions")
    assert response.status_code == 200
    instructions = response.json()["instructions"]
    rules = "\n".join(instructions["rules"])
    small_flow = "\n".join(instructions["small_patch_fast_path"]["steps"])
    do_not = "\n".join(instructions["small_patch_fast_path"]["do_not"])
    skills = instructions["skills"]

    assert "do not stop after writing files" in rules
    assert "printing JSON MCP tool calls" in rules
    assert "ir.model.fields" in rules
    assert "ir.ui.view" in rules
    assert "Never generate Pydantic models/imports inside Odoo addon code" in rules
    assert "If the deploy cycle breaks mid-flow" in rules
    assert "repo.write_many_smart" in small_flow
    assert "workflow.safe_deploy_module" in small_flow
    assert "workflow.resume_job" in small_flow
    assert "override_step=push/install/upgrade" in small_flow
    assert "Do not end with 'next steps'" in do_not
    assert "Do not print sample MCP JSON blocks" in do_not
    assert "Do not use Studio/ORM database schema mutations" in do_not
    assert "Do not put Pydantic code in Odoo addons" in do_not
    assert skills["mode"] == "on_demand"
    assert skills["list_tool"] == "skills.list"
    assert skills["read_tool"] == "skills.read"
    assert {
        "odoo-accounting-development",
        "odoo-manufacturing-development",
        "odoo-inventory-development",
        "odoo-sales-development",
        "odoo-purchase-development",
        "odoo-hr-payroll-development",
        "odoo-crm-pipeline-development",
        "odoo-helpdesk-development",
        "odoo-pos-development",
        "odoo-website-development",
    }.issubset(skills["available"])
