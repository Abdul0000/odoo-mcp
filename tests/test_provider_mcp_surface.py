from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.mcp import provider_tool_names_safe

_PROVIDER_TOOLS = {
    "connector_health",
    "skills_list",
    "skills_read",
    "client_check",
    "workflow_agent_instructions",
    "workflow_odoo_code_change",
    "workflow_frontend_visual_implement",
    "artifacts_read",
    "workflow_resume_job",
    "jobs_list",
    "jobs_get",
    "odoosh_error_pack",
}


def _mcp_tools_list():
    client = TestClient(app)
    response = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 1
    return payload["result"]["tools"]


def test_provider_mcp_tools_list_exposes_core_read_tools_on_single_mcp_endpoint():
    tools = _mcp_tools_list()
    names = {tool["name"] for tool in tools}
    assert _PROVIDER_TOOLS <= names
    assert len(names) == len(_PROVIDER_TOOLS)
    assert set(provider_tool_names_safe()) == names


def test_provider_mcp_workflow_schema_contains_autopatch_fields():
    tools = _mcp_tools_list()
    workflow = next(tool for tool in tools if tool["name"] == "workflow_odoo_code_change")
    properties = workflow["inputSchema"]["properties"]
    for field in (
        "module_hint",
        "task_type",
        "intent",
        "target_hint",
        "change_request",
        "patch_strategy",
        "target_text",
        "style",
        "allow_autopatch",
    ):
        assert field in properties

    descriptions = [
        value.get("description", "")
        for value in properties.values()
        if isinstance(value, dict)
    ]
    assert all(len(description) <= 80 for description in descriptions)
