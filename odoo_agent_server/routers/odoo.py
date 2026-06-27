from __future__ import annotations

from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import OdooRpcModuleRequest, OdooRpcResult, OdooSearchReadRequest
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
    approval = ApprovalService()
    approval.ensure_allowed("odoo.module_install", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    result = OdooRpcService().module_install(payload.module_name)
    approval.finalize_status(payload.approval_id, result.status == "ok", "odoo.module_install succeeded", "odoo.module_install failed")
    JsonStore().log_audit("odoo.module_install", result.status, payload.module_name)
    return result


@router.post("/module-update", response_model=OdooRpcResult)
def odoo_module_update(payload: OdooRpcModuleRequest) -> OdooRpcResult:
    approval = ApprovalService()
    approval.ensure_allowed("odoo.module_update", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    result = OdooRpcService().module_update(payload.module_name)
    approval.finalize_status(payload.approval_id, result.status == "ok", "odoo.module_update succeeded", "odoo.module_update failed")
    JsonStore().log_audit("odoo.module_update", result.status, payload.module_name)
    return result


@router.post("/search-read", response_model=OdooRpcResult)
def odoo_search_read(payload: OdooSearchReadRequest) -> OdooRpcResult:
    result = OdooRpcService().search_read(payload.model, payload.domain, payload.fields, payload.limit)
    JsonStore().log_audit("odoo.search_read", result.status, payload.model, {"fields": payload.fields, "limit": payload.limit})
    return result
