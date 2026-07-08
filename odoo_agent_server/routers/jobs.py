from __future__ import annotations

from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import AuditEvent, JobRecord
from ..services.store_service import JsonStore

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[AuthDependency])


@router.get("", response_model=list[JobRecord])
def list_jobs(limit: int = 100) -> list[JobRecord]:
    return JsonStore().list_jobs(limit=limit)


@router.get("/audit/events", response_model=list[AuditEvent])
def list_audit(limit: int = 200) -> list[AuditEvent]:
    return JsonStore().list_audit(limit=limit)


def _compact_job_payload(job: JobRecord) -> dict:
    last_step = job.steps[-1] if job.steps else None
    result = job.result if isinstance(job.result, dict) else {}
    return {
        "id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "current_step": last_step.name if last_step else None,
        "current_step_status": last_step.status if last_step else None,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "deploy_confirmed": bool(result.get("deploy_confirmed") or result.get("already_deployed_confirmed") or result.get("verified_already_deployed")),
        "source_pushed": bool(result.get("source_pushed")),
        "source_deployed": bool(result.get("source_deployed") or result.get("source_on_odoosh")),
        "module_upgraded": bool(result.get("module_upgraded")),
        "ui_checked": bool(result.get("ui_checked")),
        "visible_change_confirmed": bool(result.get("visible_change_confirmed")),
        "summary_status": result.get("status") if isinstance(result, dict) else None,
        "message": result.get("message") if isinstance(result, dict) else None,
        "next_call": result.get("next_call") if isinstance(result, dict) else None,
    }


@router.get("/{job_id}/compact")
def get_job_compact(job_id: str) -> dict:
    return _compact_job_payload(JsonStore().get_job(job_id))


@router.get("/{job_id}", response_model=JobRecord)
def get_job(
    job_id: str,
    wait_for_result: bool = True,
    wait_timeout_seconds: int = 600,
    poll_interval_seconds: float = 10.0,
) -> JobRecord:
    """Return one job with explicit deploy-success safety gates.

    This endpoint intentionally uses a short bounded wait by default so MCP/UI
    clients stay responsive. Callers must poll again while the job is running;
    running is never a deploy-success signal. For no-local-changes /
    already-existing claims, only say "already added", "already deployed", or
    "already live" when ``verified_already_deployed`` /
    ``already_deployed_confirmed`` is true.
    """
    store = JsonStore()
    if wait_for_result:
        return store.wait_job(job_id, wait_timeout_seconds, poll_interval_seconds)
    return store.get_job(job_id)
