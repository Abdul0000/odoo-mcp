from odoo_agent_server.config import settings
from odoo_agent_server.schemas import JobRecord, JobStatus
from odoo_agent_server.services.store_service import JsonStore
from odoo_agent_server.services.failure_policy_service import FailurePolicyService
from odoo_agent_server.services.workflow_guard_service import (
    code_change_issue_fingerprint,
    record_code_change_deploy_attempt,
    repeated_failure_guard_status,
)


def _passed_job(store: JsonStore) -> JobRecord:
    job = JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.passed,
        result={"deploy_confirmed": True},
    )
    return store.create_job(job)


def test_repeated_failure_blocks_third_write_until_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path))
    store = JsonStore()
    fingerprint = code_change_issue_fingerprint(
        project_path="/workspace/repos/example",
        module_name="example_module",
        task_description="Correct the displayed result",
        task_type="controller",
        intent="fix_bug",
        target_hint="final output",
    )
    first = _passed_job(store)
    second = _passed_job(store)
    record_code_change_deploy_attempt(client_name="Example", fingerprint=fingerprint, job_id=first.id)
    record_code_change_deploy_attempt(client_name="Example", fingerprint=fingerprint, job_id=second.id)

    blocked = repeated_failure_guard_status(
        client_name="Example",
        fingerprint=fingerprint,
        task_description="The same issue is still not resolved",
    )
    assert blocked["blocked"] is True
    assert blocked["passed_attempts"] == 2

    allowed = repeated_failure_guard_status(
        client_name="Example",
        fingerprint=fingerprint,
        task_description="The same issue is still not resolved",
        investigation_evidence=["Runtime state diverges after the framework compute step."],
    )
    assert allowed["blocked"] is False
    assert allowed["investigation_evidence_present"] is True


def test_guard_does_not_change_normal_workflow_and_can_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path))
    fingerprint = code_change_issue_fingerprint(
        project_path="/workspace/repos/example",
        module_name="example_module",
        task_description="Add a new field",
    )
    normal = repeated_failure_guard_status(
        client_name="Example",
        fingerprint=fingerprint,
        task_description="Add a new field",
    )
    assert normal["blocked"] is False

    reset = repeated_failure_guard_status(
        client_name="Example",
        fingerprint=fingerprint,
        task_description="The issue is fixed",
        functional_verification_passed=True,
    )
    assert reset == {"blocked": False, "passed_attempts": 0, "reset": True}


def test_failure_policy_retries_transient_errors_but_not_code_errors():
    service = FailurePolicyService()

    transient = service.decide(
        stage="odoosh_poll",
        error="502 Bad Gateway: connection reset by peer",
        attempt=1,
    )
    code = service.decide(
        stage="module_upgrade",
        error="ParseError: xpath cannot be located in parent view",
        attempt=1,
    )

    assert transient.failure_class == "transient"
    assert transient.retryable is True
    assert transient.retry_after_seconds == 2
    assert code.failure_class == "compatibility"
    assert code.retryable is False
    assert code.correction_required is True


def test_failure_policy_opens_circuit_on_repeated_same_class():
    decision = FailurePolicyService().decide(
        stage="pre_deploy_validation",
        error="SyntaxError: invalid syntax at line 42",
        attempt=2,
    )

    assert decision.circuit_open is True
    assert decision.action == "stop_and_report_evidence"
