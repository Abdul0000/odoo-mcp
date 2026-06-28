from __future__ import annotations

import threading
from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import JobRecord, JobStatus, OdooRpcModuleRequest, OdooRpcResult, OdooSearchReadRequest
from ..services.approval_service import ApprovalService
from ..services.odoo_rpc_service import OdooRpcService
from ..services.store_service import JsonStore

router = APIRouter(prefix="/odoo", tags=["odoo"], dependencies=[AuthDependency])


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
    return OdooRpcResult(status="ok", result={"job_id": job.id, "message": "Install started. Poll jobs.get with this job_id until status=passed/failed."})


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
    return OdooRpcResult(status="ok", result={"job_id": job.id, "message": "Upgrade started. Poll jobs.get with this job_id until status=passed/failed."})


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
    return OdooRpcResult(status="ok", result={"job_id": job.id, "message": "Deploy started. Poll jobs.get with this job_id every 10s until status=passed/failed."})


@router.post("/search-read", response_model=OdooRpcResult)
def odoo_search_read(payload: OdooSearchReadRequest) -> OdooRpcResult:
    result = OdooRpcService().search_read(payload.model, payload.domain, payload.fields, payload.limit)
    JsonStore().log_audit("odoo.search_read", result.status, payload.model, {"fields": payload.fields, "limit": payload.limit})
    return result
