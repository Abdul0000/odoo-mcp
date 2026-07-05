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


@router.get("/{job_id}", response_model=JobRecord)
def get_job(
    job_id: str,
    wait_for_result: bool = True,
    wait_timeout_seconds: int = 20,
    poll_interval_seconds: float = 2.0,
) -> JobRecord:
    """Return one job with the top-level deploy_confirmed safety gate.

    Callers must treat deployment as complete only when
    ``deploy_confirmed`` is true; finished/running/warning text alone is not a
    deploy-success signal.
    """
    store = JsonStore()
    if wait_for_result:
        return store.wait_job(job_id, wait_timeout_seconds, poll_interval_seconds)
    return store.get_job(job_id)
