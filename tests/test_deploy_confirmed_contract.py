from __future__ import annotations

import pytest

from odoo_agent_server.config import settings
from odoo_agent_server.routers import jobs
from odoo_agent_server.schemas import JobRecord, JobStatus, JobStep, WorkflowReport
from odoo_agent_server.services.store_service import JsonStore


def _result(*, deployed: bool = True, upgrade_status: str = "passed", upgrade_requested: bool = True) -> dict:
    return {
        "status": "passed",
        "module_name": "x_module",
        "sha": "abc1234",
        "odoosh_check_commit": {
            "status": "passed" if deployed else "failed",
            "deployed": deployed,
            "expected_sha": "abc1234",
        },
        "module_upgrade": {
            "requested": upgrade_requested,
            "status": upgrade_status,
            "passed": upgrade_status == "passed",
        },
    }


@pytest.mark.parametrize(
    ("status", "result", "expected"),
    [
        (JobStatus.passed, _result(deployed=True, upgrade_status="passed"), True),
        (JobStatus.running, _result(deployed=True, upgrade_status="passed"), False),
        (JobStatus.failed, _result(deployed=True, upgrade_status="passed"), False),
        (JobStatus.warning, _result(deployed=True, upgrade_status="passed"), False),
        (JobStatus.passed, _result(deployed=False, upgrade_status="passed"), False),
        (JobStatus.passed, _result(deployed=True, upgrade_status="failed"), False),
        (JobStatus.passed, {"status": "passed", "module_name": "x_module", "sha": "abc1234"}, False),
        (JobStatus.passed, {"status": "passed", "odoosh_check_commit": {"status": "passed", "deployed": True}}, False),
    ],
)
def test_job_record_deploy_confirmed_requires_deployed_check_and_upgrade_passed(status, result, expected):
    job = JobRecord(
        job_type="safe_deploy_module",
        status=status,
        request={"module_name": "x_module", "upgrade_module": True},
        result=result,
    )

    assert job.deploy_confirmed is expected
    dumped = job.model_dump(mode="json")
    assert dumped["deploy_confirmed"] is expected


def test_job_record_deploy_confirmed_allows_no_upgrade_only_when_not_requested():
    job = JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.passed,
        request={"module_name": "x_module", "upgrade_module": False},
        result={
            "status": "passed",
            "odoosh_check_commit": {"status": "passed", "deployed": True, "expected_sha": "abc1234"},
            "module_upgrade": {"requested": False, "status": "skipped", "passed": True},
        },
    )

    assert job.deploy_confirmed is True


def test_jobs_get_exposes_deploy_confirmed_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    JsonStore._read_cache.clear()
    store = JsonStore()
    stored = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.passed,
        request={"module_name": "x_module", "upgrade_module": True},
        result=_result(deployed=True, upgrade_status="passed"),
    ))

    fetched = jobs.get_job(stored.id, wait_for_result=False)

    assert fetched.deploy_confirmed is True
    assert fetched.model_dump(mode="json")["deploy_confirmed"] is True


def test_workflow_report_carries_deploy_confirmed_top_level():
    report = WorkflowReport(
        status="passed",
        job_id="job-1",
        steps=[JobStep(name="odoosh_check_commit", status="passed")],
        deploy_confirmed=True,
        summary={"status": "passed"},
    )

    assert report.deploy_confirmed is True
    assert report.model_dump(mode="json")["deploy_confirmed"] is True
