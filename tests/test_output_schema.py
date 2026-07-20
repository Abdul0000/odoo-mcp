from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.mcp import _tool_to_mcp, _tools_for_profile
from odoo_agent_server.routers.tools import _tool_output_schemas, tool_output_schema


def test_output_schemas_resolve_for_pydantic_backed_tools():
    schemas = _tool_output_schemas()
    # Real, known pydantic-model-backed tools must resolve.
    for name in ("workflow.coding_task_plan", "git.status", "repo.read", "workflow.capability_check"):
        assert name in schemas
        schema = schemas[name]
        assert schema.get("type") == "object"
        assert "properties" in schema


def test_plain_dict_tools_receive_truthful_dynamic_output_schema():
    schema = tool_output_schema("connector.health")
    assert schema["type"] == "object"
    assert "status" in schema["properties"]
    assert "service" in schema["properties"]
    assert schema["additionalProperties"] is True


def test_tool_output_schema_helper_resolves_aliases_and_returns_deepcopy():
    first = tool_output_schema("workflow.coding_task_plan")
    assert first is not None
    first["properties"]["mutated"] = {"type": "string"}
    second = tool_output_schema("workflow.coding_task_plan")
    assert "mutated" not in second["properties"]


def test_unresolvable_tool_receives_safe_generic_object_schema():
    schema = tool_output_schema("not.a.real.tool")
    assert schema["type"] == "object"
    assert {"status", "message", "error", "next_call"} <= set(schema["properties"])


def test_mcp_tools_list_includes_output_schema_when_available():
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    resp = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert resp.status_code == 200
    tools = resp.json()["result"]["tools"]
    assert tools, "expected at least one tool in tools/list"
    assert all("outputSchema" in tool for tool in tools)
    for tool in tools:
        assert tool["outputSchema"].get("type") == "object"


def test_all_provider_profiles_expose_output_schema_for_every_tool():
    for profile in (None, "provider", "chatgpt", "claude", "grok", "qwen"):
        tools = [_tool_to_mcp(tool, profile) for tool in _tools_for_profile(profile)]
        assert tools and all("outputSchema" in tool for tool in tools)


def test_job_output_schema_uses_serialization_fields():
    schema = tool_output_schema("jobs.get")
    assert {"deploy_confirmed", "completion_required", "next_call", "terminal_action"} <= set(schema["properties"])
