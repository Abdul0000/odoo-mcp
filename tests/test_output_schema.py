from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.tools import _tool_output_schemas, tool_output_schema


def test_output_schemas_resolve_for_pydantic_backed_tools():
    schemas = _tool_output_schemas()
    # Real, known pydantic-model-backed tools must resolve.
    for name in ("workflow.coding_task_plan", "git.status", "repo.read", "workflow.capability_check"):
        assert name in schemas
        schema = schemas[name]
        assert schema.get("type") == "object"
        assert "properties" in schema


def test_output_schema_absent_for_plain_dict_tools():
    schemas = _tool_output_schemas()
    # connector.health returns a plain dict literal, not a pydantic model —
    # no outputSchema should be fabricated for it.
    assert "connector.health" not in schemas


def test_tool_output_schema_helper_resolves_aliases_and_returns_deepcopy():
    first = tool_output_schema("workflow.coding_task_plan")
    assert first is not None
    first["properties"]["mutated"] = {"type": "string"}
    second = tool_output_schema("workflow.coding_task_plan")
    assert "mutated" not in second["properties"]


def test_tool_output_schema_returns_none_for_unresolvable_tool():
    assert tool_output_schema("connector.health") is None
    assert tool_output_schema("not.a.real.tool") is None


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
    with_schema = [t for t in tools if "outputSchema" in t]
    for tool in with_schema:
        assert tool["outputSchema"].get("type") == "object"
