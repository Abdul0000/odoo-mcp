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
    provider_flow = instructions["provider_flow"]
    skills = instructions["skills"]

    assert len(instructions["rules"]) <= 8
    assert "workflow.odoo_code_change" in rules
    assert "hidden repo, Git" in rules
    assert "Pydantic code inside Odoo addons" in rules
    assert provider_flow["code_change"] == "workflow.odoo_code_change"
    assert provider_flow["visual_change"] == "workflow.frontend_visual_implement"
    assert provider_flow["continuation"] == ["jobs.get", "workflow.resume_job"]
    assert "deploy_confirmed=true" in provider_flow["terminal_success"]
    assert provider_flow["terminal_action"] == "stop_and_summarize"
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

    forbidden_sections = {
        "small_patch_fast_path",
        "mandatory_dual_read",
        "core_reading_rules",
        "strict_frontend_owl_js_xml_preflight",
        "adaptive_scope_levels",
        "pre_push_verify",
        "large_file_writing",
        "flow",
        "error_guide",
        "new_tools_v14plus",
    }
    assert not (forbidden_sections & instructions.keys())
