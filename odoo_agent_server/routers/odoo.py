from __future__ import annotations

import threading
from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import JobRecord, JobStatus, OdooRpcModuleRequest, OdooRpcResult, OdooSearchReadRequest
from ..services.approval_service import ApprovalService
from ..services.odoo_rpc_service import OdooRpcService
from ..services.store_service import JsonStore

router = APIRouter(prefix="/odoo", tags=["odoo"], dependencies=[AuthDependency])


def _background_job_response(store: JsonStore, job: JobRecord, payload: OdooRpcModuleRequest, started_message: str) -> OdooRpcResult:
    """Return a final result when the job finishes quickly, otherwise a clean running state.

    This avoids fragile ChatGPT-side polling loops and keeps every HTTP request
    below Render's practical timeout window.
    """
    if not payload.wait_for_result:
        return OdooRpcResult(status="ok", result={"job_id": job.id, "job_status": job.status.value, "finished": False, "message": started_message})

    waited = store.wait_job(job.id, payload.wait_timeout_seconds, payload.poll_interval_seconds)
    data = waited.model_dump(mode="json")
    if waited.status == JobStatus.passed:
        return OdooRpcResult(status="ok", result={"job_id": waited.id, "job_status": waited.status.value, "finished": True, "job": data})
    if waited.status in {JobStatus.failed, JobStatus.warning}:
        result_error = None
        if isinstance(waited.result, dict):
            result_error = waited.result.get("error")
        return OdooRpcResult(
            status="failed" if waited.status == JobStatus.failed else "ok",
            result={"job_id": waited.id, "job_status": waited.status.value, "finished": True, "job": data},
            error=result_error or f"Job finished with status={waited.status.value}",
        )
    return OdooRpcResult(
        status="ok",
        result={
            "job_id": waited.id,
            "job_status": waited.status.value,
            "finished": False,
            "message": f"{started_message} Still running after {payload.wait_timeout_seconds}s server-side wait. Call jobs.get with this job_id; jobs.get also waits by default.",
            "next_call": {"tool": "jobs.get", "arguments": {"job_id": waited.id, "wait_for_result": True}},
        },
    )


@router.post("/health", response_model=OdooRpcResult)
def odoo_health() -> OdooRpcResult:
    result = OdooRpcService().health()
    JsonStore().log_audit("odoo.health", result.status, "Odoo RPC health")
    return result


@router.post("/module-status", response_model=OdooRpcResult)
def odoo_module_status(payload: OdooRpcModuleRequest) -> OdooRpcResult:
    result = OdooRpcService().module_status(payload.module_name)
    JsonStore().log_audit("odoo.module_status", result.status, payload.module_name)
    return result


@router.post("/module-install", response_model=OdooRpcResult)
def odoo_module_install(payload: OdooRpcModuleRequest) -> OdooRpcResult:
    """Background install — returns job_id immediately, poll jobs.get for result."""
    approval = ApprovalService()
    approval.ensure_allowed("odoo.module_install", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="odoo.module_install", status=JobStatus.running, request={"module_name": payload.module_name}))

    def _bg():
        result = OdooRpcService().module_install(payload.module_name)
        j = store.get_job(job.id)
        store.finish_job(j, JobStatus.passed if result.status == "ok" else JobStatus.failed, result.model_dump(mode="json"))
        approval.finalize_status(payload.approval_id, result.status == "ok", "odoo.module_install succeeded", "odoo.module_install failed")
        store.log_audit("odoo.module_install", result.status, payload.module_name)

    threading.Thread(target=_bg, daemon=True).start()
    return _background_job_response(store, job, payload, "Install started.")


@router.post("/module-update", response_model=OdooRpcResult)
def odoo_module_update(payload: OdooRpcModuleRequest) -> OdooRpcResult:
    """Background upgrade — returns job_id immediately, poll jobs.get for result."""
    approval = ApprovalService()
    approval.ensure_allowed("odoo.module_update", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="odoo.module_update", status=JobStatus.running, request={"module_name": payload.module_name}))

    def _bg():
        result = OdooRpcService().module_update(payload.module_name)
        j = store.get_job(job.id)
        store.finish_job(j, JobStatus.passed if result.status == "ok" else JobStatus.failed, result.model_dump(mode="json"))
        approval.finalize_status(payload.approval_id, result.status == "ok", "odoo.module_update succeeded", "odoo.module_update failed")
        store.log_audit("odoo.module_update", result.status, payload.module_name)

    threading.Thread(target=_bg, daemon=True).start()
    return _background_job_response(store, job, payload, "Upgrade started.")


@router.post("/module-deploy", response_model=OdooRpcResult)
def odoo_module_deploy(payload: OdooRpcModuleRequest) -> OdooRpcResult:
    """Smart deploy: update_list → check state → install/upgrade. Background job."""
    approval = ApprovalService()
    approval.ensure_allowed("odoo.module_deploy", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="odoo.module_deploy", status=JobStatus.running, request={"module_name": payload.module_name}))

    def _bg():
        result = OdooRpcService().module_deploy(payload.module_name)
        j = store.get_job(job.id)
        store.finish_job(j, JobStatus.passed if result.status == "ok" else JobStatus.failed, result.model_dump(mode="json"))
        approval.finalize_status(payload.approval_id, result.status == "ok", "odoo.module_deploy succeeded", "odoo.module_deploy failed")
        store.log_audit("odoo.module_deploy", result.status, payload.module_name)

    threading.Thread(target=_bg, daemon=True).start()
    return _background_job_response(store, job, payload, "Deploy started.")


@router.post("/search-read", response_model=OdooRpcResult)
def odoo_search_read(payload: OdooSearchReadRequest) -> OdooRpcResult:
    result = OdooRpcService().search_read(payload.model, payload.domain, payload.fields, payload.limit)
    JsonStore().log_audit("odoo.search_read", result.status, payload.model, {"fields": payload.fields, "limit": payload.limit})
    return result
