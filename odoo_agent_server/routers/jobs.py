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
def get_job(job_id: str) -> JobRecord:
    return JsonStore().get_job(job_id)
