from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.config import Settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.jobs import get_job
from odoo_agent_server.routers.mcp import _arguments_for_profile, _provider_safe_next_calls
from odoo_agent_server.routers.tools import tool_output_schema
from odoo_agent_server.schemas import JobRecord, JobStatus, JobStep
from odoo_agent_server.services.workflow_watchdog_service import WorkflowWatchdogService


def test_watchdog_defaults_to_active_safe_redirect(monkeypatch):
    monkeypatch.delenv("ODOO_AGENT_WATCHDOG_MODE", raising=False)
    assert Settings(_env_file=None).workflow_watchdog_mode == "safe_redirect"


@pytest.mark.asyncio
async def test_protected_coding_tool_is_never_timed_out(monkeypatch):
    monkeypatch.setattr(settings, "workflow_watchdog_mode", "safe_redirect")
    monkeypatch.setattr(settings, "workflow_watchdog_observation_budget_seconds", 0.01)

    async def protected_operation():
        await asyncio.sleep(0.03)
        return {"status": "needs_writes"}

    result = await WorkflowWatchdogService().run_tool_call(
        "workflow.odoo_code_change",
        protected_operation,
    )

    assert result == {"status": "needs_writes"}


@pytest.mark.asyncio
async def test_read_only_tool_redirects_after_runtime_budget(monkeypatch):
    monkeypatch.setattr(settings, "workflow_watchdog_mode", "safe_redirect")
    monkeypatch.setattr(settings, "workflow_watchdog_observation_budget_seconds", 0.01)

    async def stalled_read():
        await asyncio.sleep(0.1)
        return {"status": "ok"}

    result = await WorkflowWatchdogService().run_tool_call("workflow.module_analyze", stalled_read)

    assert result["status"] == "watchdog_redirect"
    assert result["cancellation_requested"] is True
    assert result["force_cancelled"] is False
    assert result["success_allowed"] is False
    assert result["next_action"]["type"] == "use_focused_evidence"


@pytest.mark.asyncio
async def test_shadow_mode_never_changes_tool_result(monkeypatch):
    monkeypatch.setattr(settings, "workflow_watchdog_mode", "shadow")
    monkeypatch.setattr(settings, "workflow_watchdog_observation_budget_seconds", 0.01)

    async def slow_read():
        await asyncio.sleep(0.03)
        return {"status": "ok", "records": [1]}

    result = await WorkflowWatchdogService().run_tool_call("odoo.search_read", slow_read)
    assert result == {"status": "ok", "records": [1]}


def test_deploy_job_remains_protected_when_stale(monkeypatch):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=1)).isoformat()
    monkeypatch.setattr(settings, "workflow_watchdog_observation_budget_seconds", 120)
    job = JobRecord(
        id="deploy-1",
        job_type="safe_deploy_module",
        status=JobStatus.running,
        created_at=old,
        steps=[JobStep(name="git_push", status="passed", started_at=old, finished_at=old)],
    )
    service = WorkflowWatchdogService()

    service.assess_job(job)
    service.assess_job(job)
    assessment = service.assess_job(job)

    assert assessment["stalled"] is True
    assert assessment["protected"] is True
    assert assessment["cancellation_supported"] is False
    assert assessment["action"] == "continue_with_backoff"
    assert assessment["redirect_kind"] == "preserve_protected_work"


def test_stale_read_job_recommends_strategy_switch_without_claiming_cancel(monkeypatch):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(minutes=10)).isoformat()
    monkeypatch.setattr(settings, "workflow_watchdog_observation_budget_seconds", 120)
    job = JobRecord(id="read-1", job_type="module_analysis", status=JobStatus.running, created_at=old)
    service = WorkflowWatchdogService()

    service.assess_job(job)
    service.assess_job(job)
    assessment = service.assess_job(job)

    assert assessment["stalled"] is True
    assert assessment["protected"] is False
    assert assessment["cancellation_supported"] is False
    assert assessment["action"] == "switch_strategy"
    assert assessment["redirect_kind"] == "use_focused_evidence"
    assert assessment["next_action"]["type"] == "use_focused_evidence"


def test_stale_read_job_redirects_by_server_failure_origin(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    monkeypatch.setattr(settings, "workflow_watchdog_observation_budget_seconds", 120)
    job = JobRecord(
        id="context-1",
        job_type="module_analysis",
        status=JobStatus.running,
        created_at=old,
        result={"diagnosis": {"origin": "context_degradation"}},
    )
    service = WorkflowWatchdogService()

    service.assess_job(job)
    service.assess_job(job)
    assessment = service.assess_job(job)

    assert assessment["redirect_kind"] == "reload_handoff"
    assert assessment["next_action"]["type"] == "reload_handoff"


def test_unknown_job_is_conservatively_protected():
    job = JobRecord(id="custom-1", job_type="custom_worker", status=JobStatus.running)
    assessment = WorkflowWatchdogService().assess_job(job)
    assert assessment["protected"] is True
    assert assessment["cancellation_supported"] is False


def test_watchdog_is_read_only_response_metadata():
    job = JobRecord(id="job-1", job_type="safe_deploy_module", status=JobStatus.running)
    annotated = WorkflowWatchdogService().annotate_job(job)

    assert annotated.watchdog.protected is True
    assert "watchdog" not in job.model_dump()
    assert "watchdog" in annotated.model_dump()


def test_wait_cap_is_provider_only_and_explicit(monkeypatch):
    monkeypatch.setattr(settings, "workflow_watchdog_wait_cap_seconds", 7)
    monkeypatch.setattr(settings, "workflow_watchdog_mode", "safe_redirect")
    service = WorkflowWatchdogService()

    # REST/internal callers retain exactly what they requested.
    assert service.provider_wait_timeout(600) == 7
    provider_arguments = _arguments_for_profile(
        "jobs.get",
        {"job_id": "x", "wait_for_result": True, "wait_timeout_seconds": 600},
        "provider",
    )
    assert provider_arguments["wait_timeout_seconds"] == 7
    next_call = _provider_safe_next_calls(
        {
            "next_call": {
                "tool": "jobs.get",
                "arguments": {"job_id": "x", "wait_for_result": True, "wait_timeout_seconds": 600},
            }
        }
    )
    assert next_call["next_call"]["arguments"]["wait_timeout_seconds"] == 7


def test_shadow_mode_preserves_provider_wait(monkeypatch):
    monkeypatch.setattr(settings, "workflow_watchdog_mode", "shadow")
    monkeypatch.setattr(settings, "workflow_watchdog_wait_cap_seconds", 7)

    arguments = _arguments_for_profile(
        "jobs.get",
        {"job_id": "x", "wait_for_result": True, "wait_timeout_seconds": 600},
        "provider",
    )

    assert arguments["wait_timeout_seconds"] == 600


def test_rest_jobs_get_preserves_requested_wait(monkeypatch):
    captured = {}
    running = JobRecord(id="rest-job", job_type="safe_deploy_module", status=JobStatus.running)

    def fake_wait(self, job_id, timeout_seconds, poll_interval_seconds):
        captured.update(
            job_id=job_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        return running

    monkeypatch.setattr("odoo_agent_server.routers.jobs.JsonStore.wait_job", fake_wait)
    response = get_job("rest-job", True, 600, 2.0)

    assert captured == {
        "job_id": "rest-job",
        "timeout_seconds": 600,
        "poll_interval_seconds": 2.0,
    }
    assert response.watchdog.protected is True


def test_jobs_get_output_schema_advertises_watchdog_contract():
    watchdog = tool_output_schema("jobs.get")["properties"]["watchdog"]
    assert watchdog["additionalProperties"] is False
    assert {"status", "protected", "stalled", "action", "reason"} <= set(watchdog["properties"])
    assert {"redirect_kind", "next_action"} <= set(watchdog["properties"])
    next_action = watchdog["properties"]["next_action"]
    assert next_action["additionalProperties"] is False
    assert set(next_action["required"]) == {"type", "instruction"}


def test_mcp_watchdog_coexists_with_operation_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path))
    monkeypatch.setattr(settings, "mcp_operation_guard_enabled", True)
    monkeypatch.setattr(settings, "workflow_watchdog_mode", "safe_redirect")
    monkeypatch.setattr(settings, "workflow_watchdog_observation_budget_seconds", 0.01)

    async def slow_read(name, arguments):
        await asyncio.sleep(0.1)
        return {"status": "ok"}

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", slow_read)
    response = TestClient(app).post(
        "/mcp",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "workflow_module_analyze",
                "arguments": {"client_name": "Audit", "module_name": "x_module"},
            },
        },
    )

    structured = response.json()["result"]["structuredContent"]
    assert structured["status"] == "watchdog_redirect"
    assert structured["cancellation_requested"] is True


def test_mcp_watchdog_never_times_out_protected_coding(monkeypatch):
    monkeypatch.setattr(settings, "mcp_operation_guard_enabled", False)
    monkeypatch.setattr(settings, "workflow_watchdog_mode", "safe_redirect")
    monkeypatch.setattr(settings, "workflow_watchdog_observation_budget_seconds", 0.01)

    async def slow_coding(name, arguments):
        await asyncio.sleep(0.03)
        return {"status": "needs_writes", "success_allowed": False}

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", slow_coding)
    response = TestClient(app).post(
        "/mcp",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "workflow_odoo_code_change",
                "arguments": {"client_name": "Audit", "module_name": "x_module", "task_description": "Change field"},
            },
        },
    )

    assert response.json()["result"]["structuredContent"]["status"] == "needs_writes"
