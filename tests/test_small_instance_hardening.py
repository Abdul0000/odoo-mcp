from __future__ import annotations

import asyncio
import multiprocessing as mp
import sqlite3
import subprocess
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import _lifespan
from odoo_agent_server.routers import mcp as mcp_router
from odoo_agent_server.routers import workflow
from odoo_agent_server.schemas import (
    FrontendPatternPreflightRequest,
    FrontendVisualImplementRequest,
    JobRecord,
    JobStatus,
    JobStep,
)
from odoo_agent_server.services.background_executor import BackgroundQueueFull, submit_background
from odoo_agent_server.services.frontend_pattern_service import FrontendPatternService
from odoo_agent_server.services.frontend_visual_service import FrontendVisualService
from odoo_agent_server.services.repo_index_service import RepoIndexService
from odoo_agent_server.services.repo_service import RepoService
from odoo_agent_server.services.store_service import JsonStore


def _lease_worker(state_dir: str, allowed_root: str, start, out) -> None:
    # spawned process gets a separate Python runtime but the same SQLite file.
    settings.state_dir = state_dir
    settings.allowed_roots_raw = allowed_root
    store = JsonStore()
    job = JobRecord(job_type="safe_deploy_module", status=JobStatus.running)
    start.wait(5)
    try:
        record, admitted, reason = store.admit_safe_deploy(
            job,
            lease_key=store.deploy_lease_key("Test", allowed_root, "staging", "x_module"),
        )
        out.put((record.id, admitted, reason))
    except Exception as exc:  # pragma: no cover - evidence in assertion
        out.put(("error", False, repr(exc)))


def _git_init(project: Path) -> None:
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=project, check=True, capture_output=True)


def test_sqlite_jobs_and_steps_survive_thread_contention(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "sqlite_busy_timeout_ms", 10000)

    def create_one(_: int) -> str:
        return JsonStore().create_job(JobRecord(job_type="stress", status=JobStatus.running)).id

    with ThreadPoolExecutor(max_workers=12) as pool:
        ids = list(pool.map(create_one, range(40)))

    store = JsonStore()
    persisted = {job.id for job in store.list_jobs(limit=100)}
    assert len(ids) == len(set(ids)) == 40
    assert set(ids) <= persisted

    target = store.create_job(JobRecord(job_type="step-stress", status=JobStatus.running))

    def add_one(index: int) -> None:
        JsonStore().add_step(target, JobStep(name=f"step_{index}", status="passed"))

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(add_one, range(30)))

    final = store.get_job(target.id)
    assert {step.name for step in final.steps} == {f"step_{i}" for i in range(30)}


def test_cross_process_same_deploy_has_one_sqlite_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "max_active_deploys", 1)
    # Initialize schema before forking.
    JsonStore()
    ctx = mp.get_context("spawn")
    start = ctx.Event()
    out = ctx.Queue()
    processes = [ctx.Process(target=_lease_worker, args=(settings.state_dir, str(tmp_path), start, out)) for _ in range(4)]
    for process in processes:
        process.start()
    start.set()
    results = [out.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    owner_ids = {row[0] for row in results}
    assert len(owner_ids) == 1
    assert sum(1 for _job_id, admitted, _reason in results if admitted) == 1
    assert sum(1 for _job_id, _admitted, reason in results if reason == "existing_owner") == 3
    assert len(JsonStore().list_jobs(limit=20)) == 1



def test_small_instance_rejects_second_different_deploy(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "max_active_deploys", 1)
    store = JsonStore()
    first = JobRecord(job_type="safe_deploy_module", status=JobStatus.running)
    owner, admitted, reason = store.admit_safe_deploy(
        first,
        lease_key=store.deploy_lease_key("Test", str(tmp_path / "repo"), "staging", "module_a"),
    )
    assert admitted is True and reason == "admitted"

    second = JobRecord(job_type="safe_deploy_module", status=JobStatus.running)
    with pytest.raises(HTTPException) as exc:
        store.admit_safe_deploy(
            second,
            lease_key=store.deploy_lease_key("Test", str(tmp_path / "repo"), "staging", "module_b"),
        )
    assert exc.value.status_code == 429
    assert exc.value.detail["error_type"] == "deploy_capacity_full"
    assert exc.value.detail["active_job_id"] == owner.id

def test_small_executor_applies_backpressure_instead_of_unbounded_queue():
    release = Event()
    started = Event()

    def blocking() -> None:
        started.set()
        release.wait(5)

    first = submit_background(blocking)
    assert started.wait(2)
    second = submit_background(blocking)  # one queued slot on the small profile
    with pytest.raises(BackgroundQueueFull):
        submit_background(blocking)
    release.set()
    first.result(timeout=5)
    second.result(timeout=5)


def test_mcp_sync_dispatch_runs_off_event_loop(monkeypatch):
    async def fake_dispatch(_name, _arguments):
        time.sleep(0.35)  # intentionally blocking sync work inside an async wrapper
        return {"ok": True}

    monkeypatch.setattr(mcp_router, "_dispatch", fake_dispatch)

    async def scenario():
        started = time.monotonic()
        call = asyncio.create_task(mcp_router._dispatch_without_blocking_event_loop("repo.read", {}))
        await asyncio.sleep(0.05)
        ticker_delay = time.monotonic() - started
        result = await call
        return ticker_delay, result

    delay, result = asyncio.run(scenario())
    assert delay < 0.2
    assert result == {"ok": True}


@pytest.mark.parametrize("text", [
    "adjust the field position",
    "possible repository cleanup",
    "repository status report",
])
def test_frontend_router_does_not_misclassify_words_containing_pos(text):
    request = FrontendPatternPreflightRequest(task_description=text, target_area="auto")
    assert FrontendPatternService._detect_area(request) == "none"


def test_visual_svg_is_valid_and_full_ui_trolley_css_is_attached(tmp_path):
    project = tmp_path / "repo"
    module = project / "x_theme"
    css = module / "static/src/css/trolley.scss"
    xml = module / "static/src/xml/trolley.xml"
    css.parent.mkdir(parents=True)
    xml.parent.mkdir(parents=True)
    css.write_text("/* existing */\n", encoding="utf-8")
    xml.write_text('<templates><div class="pos-theme-trolley pos-theme-trolley-active"/></templates>', encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'assets': {'point_of_sale._assets_pos': ['x_theme/static/src/css/trolley.scss', 'x_theme/static/src/xml/trolley.xml']}}",
        encoding="utf-8",
    )
    request = FrontendVisualImplementRequest(
        project_path=str(project),
        module_name="x_theme",
        target_area="pos",
        component_type="full_ui",
        design_goal='Trolley "gold" & plum <frame> with pipe',
        allowed_methods=["svg_asset"],
        asset_name="reference_pipe",
        target_files=["x_theme/static/src/css/trolley.scss"],
        auto_deploy=False,
    )
    writes, plan = FrontendVisualService().build_writes(request, "x_theme")
    svg_writes = [item for item in writes if item.relative_path.endswith(".svg")]
    assert svg_writes
    for item in svg_writes:
        ET.fromstring(item.content)
    css_write = next(item for item in writes if item.relative_path.endswith("trolley.scss"))
    assert ".pos-theme-custom .pos-theme-trolley-active" in css_write.content
    assert ".o_frontend_visual_reference_pipe" not in css_write.content
    assert plan["binding_proof"]["template_evidence"]


def test_repo_index_detects_second_edit_while_file_remains_dirty(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    xml = project / "x_module/static/src/xml/a.xml"
    xml.parent.mkdir(parents=True)
    (project / "x_module/__manifest__.py").write_text("{}", encoding="utf-8")
    xml.write_text('<templates><t t-name="Template.A"/></templates>', encoding="utf-8")
    _git_init(project)

    service = RepoIndexService()
    xml.write_text('<templates><t t-name="Template.B"/></templates>', encoding="utf-8")
    first = service.index(str(project))
    assert "Template.B" in first.template_names
    xml.write_text('<templates><t t-name="Template.C"/></templates>', encoding="utf-8")
    second = service.index(str(project))
    assert "Template.C" in second.template_names
    assert "Template.B" not in second.template_names


def test_core_and_enterprise_paths_are_immutable_for_all_repo_writes(tmp_path, monkeypatch):
    root = tmp_path / "home/odoo/src"
    core = root / "odoo/addons/base/models/a.py"
    core.parent.mkdir(parents=True)
    core.write_text("ORIGINAL\n", encoding="utf-8")
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "immutable_roots_raw", f"{root / 'odoo'},{root / 'enterprise'}")

    with pytest.raises(HTTPException) as exc:
        RepoService().write_file(str(root / "odoo"), "addons/base/models/a.py", "BROKEN\n")
    assert exc.value.status_code == 403
    assert core.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_corrupt_legacy_job_state_is_quarantined_and_fails_closed(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "jobs.json").write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(settings, "state_dir", str(state))
    with pytest.raises(HTTPException) as exc:
        JsonStore()
    assert exc.value.status_code == 503
    assert list(state.glob("jobs.json.corrupt.*"))


def test_restart_reconciliation_marks_orphan_job_resumable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))
    with sqlite3.connect(store.db_file) as conn:
        conn.execute("UPDATE jobs SET runtime_id='old-runtime', heartbeat_at=0 WHERE id=?", (job.id,))
        conn.commit()
    assert store.reconcile_interrupted_jobs(grace_seconds=1) == 1
    reconciled = store.get_job(job.id)
    assert reconciled.status == JobStatus.warning
    assert reconciled.result["status"] == "interrupted_restart"
    assert reconciled.result["next_call"]["tool"] == "workflow.resume_job"


def test_destructive_detection_cannot_be_disabled_by_false_hint(tmp_path):
    project = tmp_path / "repo"
    migration = project / "x_module/migrations/18.0.1.0/post.py"
    migration.parent.mkdir(parents=True)
    migration.write_text("env.cr.execute('DELETE FROM account_move')\n", encoding="utf-8")
    destructive, reasons = workflow._detect_destructive_upgrade(
        project,
        "x_module",
        ["x_module/migrations/18.0.1.0/post.py"],
        explicit=False,
    )
    assert destructive is True
    assert reasons


def test_production_lifespan_rejects_default_token(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_token", "change-me")
    monkeypatch.setattr(settings, "allow_insecure_default_token", False)

    async def run():
        async with _lifespan(None):
            pass

    with pytest.raises(RuntimeError, match="Refusing production startup"):
        asyncio.run(run())


def test_production_lifespan_rejects_long_placeholder_token(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_token", "replace-with-a-long-random-secret")
    monkeypatch.setattr(settings, "allow_insecure_default_token", False)

    async def run():
        async with _lifespan(None):
            pass

    with pytest.raises(RuntimeError, match="Refusing production startup"):
        asyncio.run(run())


def test_browser_api_is_explicitly_disabled_on_small_profile(monkeypatch):
    from odoo_agent_server.main import app

    monkeypatch.setattr(settings, "browser_tools_enabled", False)
    client = TestClient(app)
    response = client.post(
        "/browser/screenshot",
        headers={"Authorization": f"Bearer {settings.api_token}"},
        json={"base_url": "https://example.test"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["error_type"] == "browser_tools_disabled"
