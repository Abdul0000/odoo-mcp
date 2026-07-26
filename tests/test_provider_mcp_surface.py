import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.mcp import provider_tool_names_safe
from odoo_agent_server.schemas import OdooCodeChangeRequest, OdooCodeFileChange

_PROVIDER_TOOLS = {
    "connector_health",
    "skills_list",
    "skills_read",
    "client_check",
    "workflow_agent_instructions",
    "workflow_odoo_code_change",
    "workflow_odoo_debug_and_fix",
    "workflow_module_analyze",
    "workflow_module_install_and_check",
    "workflow_frontend_visual_implement",
    "odoo_query",
    "odoo_search_read",
    "odoo_read_group",
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


def test_provider_debug_workflow_exposes_persisted_replay_contract():
    tools = _mcp_tools_list()
    workflow = next(tool for tool in tools if tool["name"] == "workflow_odoo_debug_and_fix")
    properties = workflow["inputSchema"]["properties"]

    for field in (
        "issue_description",
        "expected_result",
        "debug_session_id",
        "operation_id",
        "correlation_id",
        "runtime_evidence",
        "verification_evidence",
        "verification_assertion",
        "writes",
        "job_id",
        "reproduction_passed",
        "expected_result_verified",
        "max_fix_cycles",
        "max_surgical_files",
        "max_surgical_content_chars",
    ):
        assert field in properties
    assert properties["max_fix_cycles"]["maximum"] == 5
    assert properties["writes"]["maxItems"] == 50
    assertion_properties = properties["verification_assertion"]["properties"]
    assert {
        "model", "domain", "fields", "expected_values",
        "expected_count_min", "expected_count_max", "match_mode",
    } <= set(assertion_properties)

    output = workflow["outputSchema"]["properties"]
    assert "debug_session" in output
    assert "deploy_confirmed" in output
    assert "reproduction_passed" in output
    assert "expected_result_verified" in output
    assert "claim_status" in output
    assert "truth_gate" in output


def test_provider_workflow_accepts_up_to_fifty_atomic_writes():
    tools = _mcp_tools_list()
    workflow = next(tool for tool in tools if tool["name"] == "workflow_odoo_code_change")
    assert workflow["inputSchema"]["properties"]["writes"]["maxItems"] == 50

    writes = [OdooCodeFileChange(relative_path=f"x_module/views/view_{index}.xml", content="<odoo/>") for index in range(50)]
    request = OdooCodeChangeRequest(module_name="x_module", task_description="Update module XML", writes=writes)
    assert len(request.writes) == 50
    with pytest.raises(ValidationError):
        OdooCodeChangeRequest(module_name="x_module", task_description="Update module XML", writes=[*writes, writes[0]])


def test_provider_install_fast_path_dispatches_without_planning(monkeypatch):
    captured = {}

    async def fake_dispatch(name, arguments):
        captured["name"] = name
        captured["arguments"] = arguments
        return {"status": "running", "job_id": "install-job", "next_call": {"tool": "jobs.get"}}

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fake_dispatch)
    client = TestClient(app)
    response = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "workflow_module_install_and_check",
                "arguments": {"client_name": "Test 18", "module_name": "iot_device_management"},
            },
        },
    )

    assert response.status_code == 200
    assert captured["name"] == "workflow.module_install_and_check"
    assert captured["arguments"]["module_name"] == "iot_device_management"


def test_provider_workflow_preserves_long_module_requirements():
    request = OdooCodeChangeRequest(
        module_name="x_module",
        create_module=True,
        task_description="Build a comprehensive module. " + ("Detailed requirement. " * 300),
    )
    assert len(request.task_description) > 2000
