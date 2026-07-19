from pathlib import Path
from types import SimpleNamespace

from odoo_agent_server.config import settings
from odoo_agent_server.schemas import CommandResult, JobRecord, JobStatus, OdooModuleRequest
from odoo_agent_server.routers.odoo import _background_job_response
from odoo_agent_server.services import core_sync_service
from odoo_agent_server.services.odoosh_service import OdooShService, _SshMasterPool
from odoo_agent_server.services.store_service import JsonStore


def test_core_sync_negative_cache_avoids_repeating_failed_rsync(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "core_sync_enabled", True)
    monkeypatch.setattr(core_sync_service, "_deployed_sha", lambda client=None: "abc123")
    monkeypatch.setattr(
        core_sync_service,
        "_target_for_sync",
        lambda client: SimpleNamespace(host="example", user="odoo", port=22, key_path=None),
    )
    calls = []

    def failed_run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(core_sync_service.subprocess, "run", failed_run)
    core_sync_service._negative_cache.clear()

    assert core_sync_service.ensure_module_synced(str(tmp_path), "missing_module") is False
    assert core_sync_service.ensure_module_synced(str(tmp_path), "missing_module") is False
    assert len(calls) == 1


def test_ssh_pool_identity_includes_private_key(tmp_path):
    pool = _SshMasterPool()
    first = pool._key("host", 22, "odoo", tmp_path / "first")
    second = pool._key("host", 22, "odoo", tmp_path / "second")
    assert first != second


def test_long_deploy_poll_uses_adaptive_interval(monkeypatch):
    service = OdooShService()
    checks = iter([False, True])

    def check(_sha):
        deployed = next(checks)
        return CommandResult(
            command=["check"],
            cwd="odoosh:ssh",
            exit_code=0 if deployed else 1,
            stdout='{"deployed": true}' if deployed else '{"deployed": false}',
        )

    sleeps = []
    monkeypatch.setattr(service, "check_commit_deployed", check)
    monkeypatch.setattr("odoo_agent_server.services.odoosh_service.time.sleep", sleeps.append)
    result = service.poll_deployed_commit("abc123", timeout_seconds=60, interval_seconds=0.25)

    assert result.exit_code == 0
    assert sleeps == [5.0]


def test_running_job_updates_do_not_rewrite_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="deploy", status=JobStatus.running))
    store.update_job(job)
    assert "job.update" not in {event.action for event in store.list_audit()}

    store.finish_job(job, JobStatus.passed, {"ok": True})
    assert "job.update" in {event.action for event in store.list_audit()}


def test_odoo_background_wait_honors_request_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="odoo.module_update", status=JobStatus.running))
    captured = {}

    def wait_job(job_id, timeout_seconds, poll_interval_seconds):
        captured.update(job_id=job_id, timeout=timeout_seconds, interval=poll_interval_seconds)
        return store.get_job(job_id)

    monkeypatch.setattr(store, "wait_job", wait_job)
    result = _background_job_response(
        store,
        job,
        OdooModuleRequest(module_name="x_module", wait_timeout_seconds=7, poll_interval_seconds=0.5),
        "Upgrade started.",
    )

    assert captured == {"job_id": job.id, "timeout": 7, "interval": 0.5}
    assert result.result["status"] == "running"
    assert result.result["next_call"]["tool"] == "jobs.get"
