from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.tools import strict_safe_tool_name, list_tools, _start_code_change_job
from odoo_agent_server.schemas import JobRecord, JobStatus, JobStep, OdooCodeChangeRequest
from odoo_agent_server.services.store_service import JsonStore


def _auth():
    return {"Authorization": f"Bearer {settings.api_token}"}


def _mcp(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


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



def test_mcp_initialize_and_core_read_tools_list():
    client = TestClient(app)
    initialized = client.post("/mcp", headers=_auth(), json=_mcp("initialize"))
    assert initialized.status_code == 200
    assert initialized.json()["result"]["protocolVersion"]

    response = client.post("/mcp", headers=_auth(), json=_mcp("tools/list", request_id=2))
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == _PROVIDER_TOOLS
    assert all("inputSchema" in tool for tool in tools)


def test_provider_routes_release_revert_away_from_generated_write_workflow(monkeypatch):
    captured = {}

    async def fake_dispatch(name, arguments):
        captured["name"] = name
        captured["arguments"] = arguments
        return {"status": "running", "job_id": "restore-job"}

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fake_dispatch)
    monkeypatch.setattr("odoo_agent_server.routers.mcp.ClientGateService.require_validated_client", lambda *args, **kwargs: None)
    monkeypatch.setattr(settings, "mcp_operation_guard_enabled", False)
    client = TestClient(app)

    response = client.post(
        "/chatgpt/mcp",
        headers=_auth(),
        json=_mcp("tools/call", {
            "name": "workflow_odoo_code_change",
            "arguments": {
                "client_name": "odoo 18",
                "module_name": "custom_website_home",
                "task_description": "revert last version and code, keep only a new manifest release version",
            },
        }),
    )

    assert response.status_code == 200
    assert captured["name"] == "workflow.restore_last_good_commit"
    assert captured["arguments"]["revert_latest"] is True
    assert captured["arguments"]["bump_manifest_version"] is True
    assert captured["arguments"]["deploy_module"] is True
    assert captured["arguments"]["bad_sha"] is None


def test_rest_tools_keep_full_admin_surface_but_mcp_is_curated():
    client = TestClient(app)
    rest = client.get("/tools/list", headers=_auth())
    assert rest.status_code == 200
    rest_names = {strict_safe_tool_name(tool["name"]) for tool in rest.json()}
    assert "repo_read" in rest_names
    assert "workflow_safe_deploy_module" in rest_names

    mcp = client.post("/mcp", headers=_auth(), json=_mcp("tools/list"))
    assert mcp.status_code == 200
    mcp_names = {tool["name"] for tool in mcp.json()["result"]["tools"]}
    assert mcp_names == _PROVIDER_TOOLS
    assert mcp_names < rest_names


def test_provider_tool_call_dispatches_composite_tool(monkeypatch):
    captured = {}

    async def fake_dispatch(name, arguments):
        captured["name"] = name
        captured["arguments"] = arguments
        return {"status": "needs_writes", "success_allowed": False}

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fake_dispatch)
    client = TestClient(app)
    response = client.post(
        "/mcp",
        headers=_auth(),
        json=_mcp(
            "tools/call",
            {
                "name": "workflow_odoo_code_change",
                "arguments": {
                    "project_path": "/tmp/repo",
                    "module_name": "x_module",
                    "task_description": "inspect change",
                },
            },
        ),
    )
    assert response.status_code == 200
    assert captured["name"] == "workflow.odoo_code_change"
    assert response.json()["result"]["structuredContent"]["status"] == "needs_writes"


def test_install_only_code_change_request_is_routed_to_module_deploy(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "odoo_agent_server.routers.mcp.ClientGateService.require_validated_client",
        lambda self, client_name, action: None,
    )

    async def fake_dispatch(name, arguments):
        captured["name"] = name
        captured["arguments"] = arguments
        return {
            "status": "running",
            "job_id": "install-job-1",
            "next_call": {
                "tool": "jobs.get",
                "arguments": {"job_id": "install-job-1", "wait_for_result": True},
            },
        }

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fake_dispatch)
    response = TestClient(app).post(
        "/mcp",
        headers=_auth(),
        json=_mcp("tools/call", {
            "name": "workflow_odoo_code_change",
            "arguments": {
                "client_name": "odoo 18",
                "project_path": "/workspace/repos/odoo-18-test",
                "module_name": "karachi_school_management",
                "task_description": "Install the existing karachi_school_management module",
                "complexity": "long",
            },
        }),
    )

    assert response.status_code == 200
    assert captured == {
        "name": "odoo.module_deploy",
        "arguments": {
            "client_name": "odoo 18",
            "project_path": "/workspace/repos/odoo-18-test",
            "module_name": "karachi_school_management",
            "wait_for_result": False,
            "wait_timeout_seconds": 0,
        },
    }
    next_call = response.json()["result"]["structuredContent"]["next_call"]
    assert next_call["tool"] == "jobs_get"
    assert next_call["arguments"]["wait_for_result"] is False
    assert next_call["arguments"]["wait_timeout_seconds"] == 0


def test_source_update_wording_is_not_misrouted_as_module_lifecycle(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "odoo_agent_server.routers.mcp.ClientGateService.require_validated_client",
        lambda self, client_name, action: None,
    )

    async def fake_dispatch(name, arguments):
        captured["name"] = name
        return {"status": "needs_writes", "success_allowed": False}

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fake_dispatch)
    response = TestClient(app).post(
        "/mcp",
        headers=_auth(),
        json=_mcp("tools/call", {
            "name": "workflow_odoo_code_change",
            "arguments": {
                "client_name": "odoo 18",
                "module_name": "karachi_school_management",
                "task_description": "Update module button label",
            },
        }),
    )

    assert response.status_code == 200
    assert captured["name"] == "workflow.odoo_code_change"


def test_provider_tool_call_dispatches_natural_language_query(monkeypatch):
    captured = {}

    async def fake_dispatch(name, arguments):
        captured["name"] = name
        captured["arguments"] = arguments
        return {"status": "ok", "intent": "customer_invoices", "odoo": {"result": []}}

    monkeypatch.setattr(
        "odoo_agent_server.routers.mcp.ClientGateService.require_validated_client",
        lambda self, client_name, action: (_ for _ in ()).throw(
            AssertionError("exact-named read query must not trigger client validation")
        ),
    )
    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fake_dispatch)
    response = TestClient(app).post(
        "/mcp",
        headers=_auth(),
        json=_mcp("tools/call", {
            "name": "odoo_query",
            "arguments": {"client_name": "odoo 18", "query": "list invoices", "limit": 10},
        }),
    )

    assert response.status_code == 200
    assert captured == {
        "name": "odoo.query",
        "arguments": {"client_name": "odoo 18", "query": "list invoices", "limit": 10},
    }
    assert response.json()["result"]["structuredContent"]["intent"] == "customer_invoices"


def test_provider_query_is_blocked_before_dispatch_until_client_is_confirmed(monkeypatch):
    dispatched = []

    def block_unconfirmed(self, client_name, action):
        raise HTTPException(status_code=409, detail={
            "requires_client_selection": True,
            "requires_validation": True,
            "available_client_names": ["Alpha", "Beta"],
            "message": "Ask the user which client to use.",
        })

    async def fake_dispatch(name, arguments):
        dispatched.append((name, arguments))
        return {"status": "ok"}

    monkeypatch.setattr(
        "odoo_agent_server.routers.mcp.ClientGateService.require_validated_client",
        block_unconfirmed,
    )
    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fake_dispatch)
    response = TestClient(app).post(
        "/mcp",
        headers=_auth(),
        json=_mcp("tools/call", {
            "name": "odoo_query",
            "arguments": {"query": "list invoices", "limit": 10},
        }),
    )

    structured = response.json()["result"]["structuredContent"]
    assert response.status_code == 200
    assert dispatched == []
    assert structured["status"] == "client_selection_required"
    assert structured["must_ask_user"] is True
    assert structured["available_client_names"] == ["Alpha", "Beta"]
    assert structured["next_call"] is None


def test_provider_code_workflow_cannot_bypass_odoo_record_delete_guard(monkeypatch):
    dispatched = []
    monkeypatch.setattr(
        "odoo_agent_server.routers.mcp.ClientGateService.require_validated_client",
        lambda self, client_name, action: None,
    )

    async def fake_dispatch(name, arguments):
        dispatched.append((name, arguments))
        return {"status": "ok"}

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fake_dispatch)
    response = TestClient(app).post(
        "/mcp",
        headers=_auth(),
        json=_mcp("tools/call", {
            "name": "workflow_odoo_code_change",
            "arguments": {
                "client_name": "ClientA",
                "module_name": "hr_contract",
                "task_description": "delete all old employee contract records",
            },
        }),
    )

    structured = response.json()["result"]["structuredContent"]
    assert dispatched == []
    assert structured["status"] == "blocked"
    assert structured["error_type"] == "odoo_record_delete_guard"
    assert structured["next_call"] is None


def test_record_delete_guard_does_not_block_normal_code_surface_removal(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "odoo_agent_server.routers.mcp.ClientGateService.require_validated_client",
        lambda self, client_name, action: None,
    )

    async def fake_dispatch(name, arguments):
        captured["name"] = name
        return {"status": "running", "job_id": "job-1"}

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fake_dispatch)
    response = TestClient(app).post(
        "/mcp",
        headers=_auth(),
        json=_mcp("tools/call", {
            "name": "workflow_odoo_code_change",
            "arguments": {
                "client_name": "ClientA",
                "module_name": "hr",
                "task_description": "remove the obsolete employee form button",
            },
        }),
    )

    assert response.status_code == 200
    assert captured["name"] == "workflow.odoo_code_change"


def test_provider_validation_errors_are_agent_friendly():
    client = TestClient(app)
    response = client.post(
        "/mcp",
        headers=_auth(),
        json=_mcp("tools/call", {"name": "workflow_odoo_code_change", "arguments": {"module_name": "bad name", "task_description": "x"}}),
    )
    assert response.status_code == 200
    payload = response.json()["result"]
    assert payload["isError"] is True
    assert payload["structuredContent"]["error_type"] == "tool_input_invalid"


def test_all_rest_tool_names_still_have_safe_mcp_aliases():
    for tool in list_tools(strict_names=False):
        safe = strict_safe_tool_name(tool.name)
        assert safe
        assert len(safe) <= 64


def test_code_change_returns_job_immediately_and_deduplicates_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    submitted = []
    monkeypatch.setattr("odoo_agent_server.routers.tools.submit_background", lambda callback: submitted.append(callback))
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path / "repo"),
        module_name="x_module",
        task_description="fix dark mode borders",
    )

    first = _start_code_change_job(payload)
    duplicate = _start_code_change_job(payload)

    assert first["status"] == "running"
    assert first["completion_required"] is True
    assert first["next_call"]["tool"] == "jobs.get"
    assert duplicate["status"] == "already_in_progress"
    assert duplicate["job_id"] == first["job_id"]
    assert len(submitted) == 1


def test_code_change_retry_does_not_reuse_a_stale_running_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    submitted = []
    monkeypatch.setattr("odoo_agent_server.routers.tools.submit_background", lambda callback: submitted.append(callback))
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path / "repo"),
        module_name="x_module",
        task_description="implement all and push",
    )
    old = JsonStore().create_job(JobRecord(
        job_type="odoo_code_change",
        status=JobStatus.running,
        request=payload.model_dump(mode="json"),
        steps=[JobStep(name="inspect_core_scope", status="running", started_at="2020-01-01T00:00:00+00:00")],
        result={"status": "running", "phase": "inspect_core_scope"},
    ))

    started = _start_code_change_job(payload)

    assert JsonStore().get_job(old.id).status == JobStatus.failed
    assert started["status"] == "running"
    assert started["job_id"] != old.id
    assert len(submitted) == 1


def test_code_change_background_worker_persists_terminal_inspection_result(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    submitted = []
    monkeypatch.setattr("odoo_agent_server.routers.tools.submit_background", lambda callback: submitted.append(callback))

    async def fake_workflow(payload, progress=None):
        progress("inspect_custom_code")
        return {"status": "needs_writes", "message": "inspection complete", "success_allowed": False}

    monkeypatch.setattr("odoo_agent_server.routers.tools._workflow_odoo_code_change", fake_workflow)
    started = _start_code_change_job(OdooCodeChangeRequest(
        project_path=str(tmp_path / "repo"),
        module_name="x_module",
        task_description="inspect change",
    ))
    submitted[0]()
    finished = JsonStore().get_job(started["job_id"])

    assert finished.status == JobStatus.warning
    assert finished.result["status"] == "needs_writes"
    assert finished.completion_required is False
    assert finished.steps[-1].name == "inspect_custom_code"


def test_code_change_parent_mirrors_nested_safe_deploy_terminal_result(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    nested = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.passed,
        request={"module_name": "x_module", "upgrade_module": False},
        result={
            "status": "passed",
            "odoosh_check_commit": {"status": "passed", "deployed": True},
            "module_upgrade": {"requested": False, "status": "skipped"},
        },
    ))
    parent = store.create_job(JobRecord(
        job_type="odoo_code_change",
        status=JobStatus.running,
        request={"module_name": "x_module"},
        steps=[JobStep(name="start_safe_deploy", status="running")],
        result={"status": "running", "nested_job_id": nested.id},
    ))

    refreshed = store.get_job(parent.id)

    assert refreshed.status == JobStatus.passed
    assert refreshed.completion_required is False
    assert refreshed.deploy_confirmed is True
    assert refreshed.success_allowed is True
    assert refreshed.result["nested_job_id"] == nested.id
    assert refreshed.steps[-1].status == "passed"


def test_stale_code_change_phase_fails_with_safe_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    parent = store.create_job(JobRecord(
        job_type="odoo_code_change",
        status=JobStatus.running,
        request={"module_name": "x_module", "task_description": "fix borders"},
        steps=[JobStep(name="inspect_core_scope", status="running", started_at="2020-01-01T00:00:00+00:00")],
        result={"status": "running", "phase": "inspect_core_scope"},
    ))

    refreshed = store.get_job(parent.id)

    assert refreshed.status == JobStatus.failed
    assert refreshed.result["retryable"] is True
    assert refreshed.result["stale_after_seconds"] == 60
    assert refreshed.result["next_call"]["tool"] == "workflow.odoo_code_change"


def test_old_code_change_phase_keeps_running_while_worker_heartbeat_is_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="odoo_code_change",
        status=JobStatus.running,
        request={"module_name": "x_module", "task_description": "implement all"},
        steps=[JobStep(name="inspect_core_scope", status="running", started_at="2020-01-01T00:00:00+00:00")],
        result={
            "status": "running",
            "phase": "inspect_core_scope",
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "worker_alive": True,
        },
    ))

    refreshed = store.get_job(job.id)

    assert refreshed.status == JobStatus.running
    assert refreshed.result["worker_alive"] is True
