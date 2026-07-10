from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..schemas import CommandResult, JobStatus, OdooRpcResult
from .store_service import JsonStore

_RESUMABLE_SAFE_DEPLOY_RESULTS = {
    "deploy_failed_or_timeout",
    "deploy_running",
    "module_upgrade_running",
    "module_upgrade_timeout",
    "deploy_code_mismatch",
}
_ACTIVE_JOB_STATUSES = {JobStatus.queued, JobStatus.running, JobStatus.warning}

# If the latest safe deploy failed at or before proving that code reached the
# remote branch, manual Odoo.sh install/upgrade tools must not run. They would
# only upgrade the database against stale source code. Keep this list broad and
# include legacy/current step names used across connector versions.
_PRE_PUSH_FAILED_STEPS = {
    "resolve_project",
    "git_status",
    "git_root_verify",
    "safe_deploy_path_guard",
    "deploy_path_gate",
    "safe_deploy_path_gate",
    "git_diff",
    "git_changed_files",
    "deploy_dirty_path_gate",
    "qa_self_run",
    "verify_against_core_pre_push",
    "git_configure_identity",
    "git_validate_target",
    "git_commit",
    "git_head_sha",
    "git_head_changed_files",
    "git_head_commit_path_gate",
    "git_commit_content_guard",
    "git_push",
    "git_remote_head_verify",
    "git_remote_sha_guard",
}



def _same_project(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return True
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except Exception:
        return str(left).rstrip("/") == str(right).rstrip("/")


def _job_result_status(job: Any) -> str | None:
    result = getattr(job, "result", None)
    if isinstance(result, dict):
        value = result.get("status")
        return str(value) if value else None
    return None


def find_blocking_safe_deploy(
    *,
    project_path: str | None = None,
    module_name: str | None = None,
    client_name: str | None = None,
    limit: int = 100,
):
    """Return an active/resumable safe_deploy_module job that owns deploy flow.

    Agents were able to start workflow.safe_deploy_module and then bypass it with
    raw git.commit/git.push/odoosh.module_update calls.  This guard makes the
    workflow state authoritative: once a safe deploy exists and is running or
    resumable, lower-level deploy tools must fail closed and point to resume.
    """
    store = JsonStore()
    candidates = []
    state = store.load_session_state(client_name)
    saved_job_id = state.get("last_safe_deploy_job_id")
    if isinstance(saved_job_id, str) and saved_job_id.strip():
        try:
            candidates.append(store.get_job(saved_job_id.strip()))
        except Exception:
            pass
    candidates.extend(job for job in store.list_jobs(limit=limit) if job.job_type == "safe_deploy_module")

    seen: set[str] = set()
    for job in candidates:
        if job.id in seen or job.job_type != "safe_deploy_module":
            continue
        seen.add(job.id)
        request = job.request or {}
        req_project = request.get("project_path") or state.get("project_path")
        req_module = request.get("module_name") or state.get("module_name")
        if project_path and req_project and not _same_project(project_path, str(req_project)):
            continue
        if module_name and req_module and str(req_module) != module_name:
            continue
        result_status = _job_result_status(job)
        if job.status in _ACTIVE_JOB_STATUSES or result_status in _RESUMABLE_SAFE_DEPLOY_RESULTS:
            return job
    return None


def safe_deploy_block_payload(action: str, job: Any) -> dict[str, Any]:
    request = job.request or {}
    return {
        "status": "safe_deploy_active",
        "message": (
            f"Manual {action} is blocked because workflow.safe_deploy_module already owns "
            "commit → push → Odoo.sh polling → module upgrade for this module. "
            "Use workflow.resume_job or jobs.get; do not replay commit/push/update."
        ),
        "job_id": job.id,
        "job_status": getattr(job.status, "value", str(job.status)),
        "module_name": request.get("module_name"),
        "project_path": request.get("project_path"),
        "next_call": {"tool": "workflow.resume_job", "arguments": {"job_id": job.id}},
    }


def _job_failed_step(job: Any) -> str | None:
    result = getattr(job, "result", None)
    if isinstance(result, dict):
        value = result.get("failed_step")
        return str(value) if value else None
    return None


def find_latest_pre_push_failed_safe_deploy(
    *,
    project_path: str | None = None,
    module_name: str | None = None,
    client_name: str | None = None,
    limit: int = 250,
):
    """Return the newest safe_deploy job that failed before code was pushed.

    This deliberately checks terminal failed jobs too, not just active or
    resumable jobs. A later manual Odoo.sh upgrade after such a failed safe
    deploy is unsafe because the intended local changes were never proven to be
    committed/pushed to the remote branch.
    """
    store = JsonStore()
    state = store.load_session_state(client_name)
    candidates = [job for job in store.list_jobs(limit=limit) if job.job_type == "safe_deploy_module"]
    for job in candidates:
        request = job.request or {}
        req_project = request.get("project_path") or state.get("project_path")
        req_module = request.get("module_name") or state.get("module_name")
        if project_path and req_project and not _same_project(project_path, str(req_project)):
            continue
        if module_name and req_module and str(req_module) != module_name:
            continue
        failed_step = _job_failed_step(job)
        if job.status == JobStatus.failed and failed_step in _PRE_PUSH_FAILED_STEPS:
            return job
        # The most recent job for this project/module is authoritative. If it
        # does not match a pre-push failure, do not scan older failures and block
        # a newer successful/resumable deploy attempt.
        return None
    return None


def safe_deploy_pre_push_failure_payload(action: str, job: Any) -> dict[str, Any]:
    request = job.request or {}
    failed_step = _job_failed_step(job) or "unknown"
    message = (
        "The last safe_deploy_module attempt failed before code was pushed "
        f"(failed_step={failed_step}). No new code exists on the remote branch "
        "beyond what was already deployed. Fix the underlying issue and re-run "
        "workflow.safe_deploy_module; do not call the manual upgrade tool directly."
    )
    return {
        "status": "safe_deploy_failed_before_push",
        "message": message,
        "failed_step": failed_step,
        "job_id": job.id,
        "job_status": getattr(job.status, "value", str(job.status)),
        "module_name": request.get("module_name"),
        "project_path": request.get("project_path"),
        "next_call": {"tool": "workflow.safe_deploy_module", "arguments": {"project_path": request.get("project_path"), "module_name": request.get("module_name")}},
    }


def safe_deploy_command_block(
    action: str,
    *,
    project_path: str | None = None,
    module_name: str | None = None,
    cwd: str | None = None,
) -> CommandResult | None:
    job = find_blocking_safe_deploy(project_path=project_path, module_name=module_name)
    if job:
        payload = safe_deploy_block_payload(action, job)
        return CommandResult(
            command=[action],
            cwd=cwd or project_path or "workflow.safe_deploy_module",
            exit_code=2,
            stdout=json.dumps(payload),
            stderr=payload["message"],
        )
    failed_job = find_latest_pre_push_failed_safe_deploy(project_path=project_path, module_name=module_name)
    if failed_job:
        payload = safe_deploy_pre_push_failure_payload(action, failed_job)
        return CommandResult(
            command=[action],
            cwd=cwd or project_path or "workflow.safe_deploy_module",
            exit_code=2,
            stdout=json.dumps(payload),
            stderr=payload["message"],
        )
    return None


def safe_deploy_rpc_block(action: str, *, module_name: str | None = None, project_path: str | None = None) -> OdooRpcResult | None:
    job = find_blocking_safe_deploy(project_path=project_path, module_name=module_name)
    if job:
        payload = safe_deploy_block_payload(action, job)
        return OdooRpcResult(
            status="failed",
            error=payload["message"],
            result=payload,
        )
    failed_job = find_latest_pre_push_failed_safe_deploy(project_path=project_path, module_name=module_name)
    if failed_job:
        payload = safe_deploy_pre_push_failure_payload(action, failed_job)
        return OdooRpcResult(
            status="failed",
            error=payload["message"],
            result=payload,
        )
    return None
