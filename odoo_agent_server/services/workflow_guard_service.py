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


def safe_deploy_command_block(
    action: str,
    *,
    project_path: str | None = None,
    module_name: str | None = None,
    cwd: str | None = None,
) -> CommandResult | None:
    job = find_blocking_safe_deploy(project_path=project_path, module_name=module_name)
    if not job:
        return None
    payload = safe_deploy_block_payload(action, job)
    return CommandResult(
        command=[action],
        cwd=cwd or project_path or "workflow.safe_deploy_module",
        exit_code=2,
        stdout=json.dumps(payload),
        stderr=payload["message"],
    )


def safe_deploy_rpc_block(action: str, *, module_name: str | None = None, project_path: str | None = None) -> OdooRpcResult | None:
    job = find_blocking_safe_deploy(project_path=project_path, module_name=module_name)
    if not job:
        return None
    payload = safe_deploy_block_payload(action, job)
    return OdooRpcResult(
        status="failed",
        error=payload["message"],
        result=payload,
    )
