from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..auth import AuthDependency
from ..schemas import (
    CommandResult,
    OdooShCommandRequest,
    OdooShLogRequest,
    OdooShModuleUpdateRequest,
    OdooShVerifyManifestRequest,
    OdooShWaitForCommitRequest,
)
from ..services.approval_service import ApprovalService
from ..services.odoosh_service import OdooShService
from ..services.store_service import JsonStore

router = APIRouter(prefix="/odoosh", tags=["odoosh"], dependencies=[AuthDependency])


def _run(name: str, callback) -> CommandResult:
    try:
        result = callback()
        JsonStore().log_audit(name, "ok" if result.exit_code == 0 else "failed", "Odoo.sh command", {"exit_code": result.exit_code})
        return result
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


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


@router.post("/verify-module-manifest", response_model=CommandResult)
def odoosh_verify_module_manifest(payload: OdooShVerifyManifestRequest) -> CommandResult:
    return _run("odoosh.verify_module_manifest", lambda: OdooShService().verify_module_manifest(payload.module_name))


@router.post("/log", response_model=CommandResult)
def odoosh_log(payload: OdooShLogRequest) -> CommandResult:
    return _run("odoosh.log", lambda: OdooShService().tail_log(payload.log_name, payload.tail_lines))


@router.post("/grep-errors", response_model=CommandResult)
def odoosh_grep_errors(payload: OdooShCommandRequest) -> CommandResult:
    return _run("odoosh.grep_errors", lambda: OdooShService().grep_errors(payload.tail_lines))


@router.post("/module-update", response_model=CommandResult)
def odoosh_module_update(payload: OdooShModuleUpdateRequest) -> CommandResult:
    approval = ApprovalService()
    approval.ensure_allowed("odoosh.module_update", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    result = _run("odoosh.module_update", lambda: OdooShService().module_update(payload.module_name, payload.timeout_seconds))
    approval.finalize_command(payload.approval_id, result.exit_code, "odoosh.module_update succeeded", "odoosh.module_update failed")
    return result
