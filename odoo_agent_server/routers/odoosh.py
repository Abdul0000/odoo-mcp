from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, HTTPException, status

from ..auth import AuthDependency
from ..schemas import (
    CommandResult,
    JobRecord,
    JobStatus,
    JobStep,
    OdooRpcModuleRequest,
    OdooShCheckCommitRequest,
    OdooShCommandRequest,
    OdooShLogRequest,
    OdooShModuleUpdateRequest,
    OdooShVerifyManifestRequest,
    OdooShWaitForCommitRequest,
)
from ..services.approval_service import ApprovalService
from ..services.odoosh_service import OdooShService
from ..services.store_service import JsonStore
from ..services.background_executor import submit_background

router = APIRouter(prefix="/odoosh", tags=["odoosh"], dependencies=[AuthDependency])


def _run(name: str, callback) -> CommandResult:
    try:
        result = callback()
        JsonStore().log_audit(name, "ok" if result.exit_code == 0 else "failed", "Odoo.sh command", {"exit_code": result.exit_code})
        return result
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _background_command_job(
    name: str,
    request: dict,
    callback: Callable[[], CommandResult],
    approval_id: str | None = None,
    wait_for_result: bool = True,
    wait_timeout_seconds: int = 20,
    poll_interval_seconds: float = 2.0,
) -> CommandResult:
    """Start a long Odoo.sh SSH command in the background and return immediately.

    Render and many MCP HTTP clients can drop long requests at about 30 seconds.
    This wrapper persists the command as a job so ChatGPT can poll jobs.get.
    """
    store = JsonStore()
    approval = ApprovalService()
    job = store.create_job(JobRecord(job_type=name, status=JobStatus.running, request=request))

    def _step(status_: str, result: CommandResult | None = None, error: str | None = None) -> None:
        current = store.get_job(job.id)
        store.add_step(
            current,
            JobStep(
                name=name,
                status=status_,
                finished_at=datetime.now(timezone.utc).isoformat(),
                result=result.model_dump(mode="json") if result else None,
                error=error,
            ),
        )

    def _bg() -> None:
        try:
            result = callback()
            _step("passed" if result.exit_code == 0 else "failed", result)
            current = store.get_job(job.id)
            store.finish_job(current, JobStatus.passed if result.exit_code == 0 else JobStatus.failed, result.model_dump(mode="json"))
            approval.finalize_command(approval_id, result.exit_code, f"{name} succeeded", f"{name} failed")
            store.log_audit(name, "ok" if result.exit_code == 0 else "failed", request.get("module_name", name), {"job_id": job.id})
        except Exception as exc:  # noqa: BLE001
            _step("failed", error=str(exc))
            current = store.get_job(job.id)
            store.finish_job(current, JobStatus.failed, {"error": str(exc)})
            approval.mark_failed(approval_id, f"{name} failed")
            store.log_audit(name, "failed", request.get("module_name", name), {"job_id": job.id, "error": str(exc)})

    submit_background(_bg)
    if wait_for_result:
        waited = store.wait_job(job.id, wait_timeout_seconds, poll_interval_seconds)
        payload = {
            "job_id": waited.id,
            "status": waited.status.value,
            "finished": waited.status not in {JobStatus.queued, JobStatus.running},
            "job": waited.model_dump(mode="json"),
        }
        if not payload["finished"]:
            payload["message"] = (
                f"Command still running after {wait_timeout_seconds}s server-side wait. "
                "Call jobs.get with this job_id; jobs.get also waits by default."
            )
        return CommandResult(
            command=[name, "background"],
            cwd="odoosh:ssh",
            exit_code=0 if waited.status in {JobStatus.queued, JobStatus.running, JobStatus.passed} else 1,
            stdout=json.dumps(payload),
            stderr="" if waited.status != JobStatus.failed else json.dumps(waited.result or {}),
        )
    return CommandResult(
        command=[name, "background"],
        cwd="odoosh:ssh",
        exit_code=0,
        stdout=json.dumps({
            "job_id": job.id,
            "status": "running",
            "finished": False,
            "message": "Command started in background. jobs.get waits by default until done or timeout.",
        }),
    )


@router.post("/health", response_model=CommandResult)
def odoosh_health() -> CommandResult:
    return _run("odoosh.health", lambda: OdooShService().health())


@router.post("/git-status", response_model=CommandResult)
def odoosh_git_status() -> CommandResult:
    return _run("odoosh.git_status", lambda: OdooShService().git_status())


@router.post("/deployed-sha", response_model=CommandResult)
def odoosh_deployed_sha() -> CommandResult:
    return _run("odoosh.deployed_sha", lambda: OdooShService().deployed_sha())


@router.post("/wait-for-commit", response_model=CommandResult)
def odoosh_wait_for_commit(payload: OdooShWaitForCommitRequest) -> CommandResult:
    return _run("odoosh.wait_for_commit", lambda: OdooShService().wait_for_commit(payload.expected_sha, payload.timeout_seconds, payload.interval_seconds))


@router.post("/check-commit", response_model=CommandResult)
def odoosh_check_commit(payload: OdooShCheckCommitRequest) -> CommandResult:
    """Non-blocking single check — returns immediately with deployed=true/false in stdout JSON."""
    return _run("odoosh.check_commit", lambda: OdooShService().check_commit_deployed(payload.expected_sha))


@router.post("/verify-module-manifest", response_model=CommandResult)
def odoosh_verify_module_manifest(payload: OdooShVerifyManifestRequest) -> CommandResult:
    return _run("odoosh.verify_module_manifest", lambda: OdooShService().verify_module_manifest(payload.module_name))


@router.post("/log", response_model=CommandResult)
def odoosh_log(payload: OdooShLogRequest) -> CommandResult:
    return _run("odoosh.log", lambda: OdooShService().tail_log(payload.log_name, payload.tail_lines))


@router.post("/grep-errors", response_model=CommandResult)
def odoosh_grep_errors(payload: OdooShCommandRequest) -> CommandResult:
    return _run("odoosh.grep_errors", lambda: OdooShService().grep_errors(payload.tail_lines))


@router.post("/module-install-ssh", response_model=CommandResult)
def odoosh_module_install_ssh(payload: OdooRpcModuleRequest) -> CommandResult:
    approval = ApprovalService()
    approval.ensure_allowed("odoosh.module_install_ssh", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    service = OdooShService()
    return _background_command_job(
        "odoosh.module_install_ssh",
        payload.model_dump(exclude={"approval_id"}, mode="json"),
        lambda: service.module_install_ssh(payload.module_name, 1800),
        payload.approval_id,
        payload.wait_for_result,
        payload.wait_timeout_seconds,
        payload.poll_interval_seconds,
    )


@router.post("/module-deploy-ssh", response_model=CommandResult)
def odoosh_module_deploy_ssh(payload: OdooShModuleUpdateRequest) -> CommandResult:
    """SSH smart install/upgrade. Background job: poll jobs.get."""
    approval = ApprovalService()
    approval.ensure_allowed("odoosh.module_deploy_ssh", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    service = OdooShService()

    def _callback() -> CommandResult:
        if payload.expected_sha:
            wait = service.wait_for_commit(payload.expected_sha, payload.timeout_seconds, None)
            if wait.exit_code != 0:
                return wait
        return service.module_deploy_ssh(payload.module_name, payload.timeout_seconds)

    return _background_command_job(
        "odoosh.module_deploy_ssh",
        payload.model_dump(exclude={"approval_id"}, mode="json"),
        _callback,
        payload.approval_id,
        payload.wait_for_result,
        payload.wait_timeout_seconds,
        payload.poll_interval_seconds,
    )


@router.post("/module-update", response_model=CommandResult)
def odoosh_module_update(payload: OdooShModuleUpdateRequest) -> CommandResult:
    approval = ApprovalService()
    approval.ensure_allowed("odoosh.module_update", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    service = OdooShService()

    def _callback() -> CommandResult:
        if payload.expected_sha:
            wait = service.wait_for_commit(payload.expected_sha, payload.timeout_seconds, None)
            if wait.exit_code != 0:
                return wait
        return service.module_update(payload.module_name, payload.timeout_seconds)

    return _background_command_job(
        "odoosh.module_update",
        payload.model_dump(exclude={"approval_id"}, mode="json"),
        _callback,
        payload.approval_id,
        payload.wait_for_result,
        payload.wait_timeout_seconds,
        payload.poll_interval_seconds,
    )
