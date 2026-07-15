from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.mcp import provider_tool_names_safe

_PROVIDER_TOOLS = {
    "connector_health",
    "client_check",
    "odoo_query",
    "odoo_search_read",
    "odoo_module_deploy",
    "workflow_odoo_code_change",
    "workflow_restore_last_good_commit",
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
        "verify_targets",
        "target_text",
        "style",
        "allow_autopatch",
    ):
        assert field in properties


def test_provider_query_tools_are_public_read_only_and_have_query_schemas():
    by_name = {tool["name"]: tool for tool in _mcp_tools_list()}

    functional = by_name["odoo_query"]
    assert functional["annotations"]["readOnlyHint"] is True
    assert {"client_name", "query", "limit"} <= set(functional["inputSchema"]["properties"])

    exact = by_name["odoo_search_read"]
    assert exact["annotations"]["readOnlyHint"] is True
    assert {"client_name", "model", "domain", "fields", "limit"} <= set(exact["inputSchema"]["properties"])

    client_check = by_name["client_check"]
    assert "user_confirmed" in client_check["inputSchema"]["properties"]


def test_provider_module_deploy_is_the_direct_install_upgrade_workflow():
    tool = next(item for item in _mcp_tools_list() if item["name"] == "odoo_module_deploy")

    assert tool["annotations"]["readOnlyHint"] is False
    assert {"client_name", "module_name", "wait_for_result"} <= set(tool["inputSchema"]["properties"])
    assert "never route an install-only request" in tool["description"]


def test_opencode_profile_excludes_server_checkout_code_workflow():
    client = TestClient(app)
    response = client.post(
        "/opencode/mcp",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )

    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    assert set(by_name) == {
        "connector_health",
        "client_check",
        "odoo_query",
        "odoo_search_read",
        "odoo_module_deploy",
        "artifacts_read",
        "jobs_list",
        "jobs_get",
        "odoosh_error_pack",
    }
    assert "workflow_odoo_code_change" not in by_name
    assert "expected_sha" in by_name["odoo_module_deploy"]["inputSchema"]["properties"]

    health = client.get(
        "/opencode/mcp",
        headers={"Authorization": f"Bearer {settings.api_token}"},
    )
    assert health.json()["custom_module_source"] == "opencode-native-local-filesystem"
    assert health.json()["odoosh_custom_source_reads"] is False
    assert health.json()["git_sync"] == "opencode-native-fetch-and-pull-if-behind"
    assert health.json()["git_push"] == "opencode-native-then-mcp-expected-sha-handoff"
