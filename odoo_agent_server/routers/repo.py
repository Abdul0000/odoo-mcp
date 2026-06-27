from __future__ import annotations

from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import (
    CommandResult,
    FileContent,
    FileListItem,
    RepoCleanPycRequest,
    RepoCleanPycResult,
    RepoDeleteRequest,
    RepoListRequest,
    RepoPatchRequest,
    RepoReadRequest,
    RepoWriteRequest,
)
from ..services.approval_service import ApprovalService
from ..services.repo_service import RepoService
from ..services.store_service import JsonStore

router = APIRouter(prefix="/repo", tags=["repo"], dependencies=[AuthDependency])


def _preview(payload: object) -> dict[str, object]:
    data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else {}
    if "content" in data:
        data["content"] = f"<redacted text length={len(str(data['content']))}>"
    if "patch" in data:
        data["patch"] = f"<redacted patch length={len(str(data['patch']))}>"
    return data


@router.post("/list", response_model=list[FileListItem])
def repo_list(payload: RepoListRequest) -> list[FileListItem]:
    JsonStore().log_audit("repo.list", "ok", payload.relative_path, _preview(payload))
    return RepoService().list_files(payload.project_path, payload.relative_path, payload.max_depth, payload.max_items)


@router.post("/read", response_model=FileContent)
def repo_read(payload: RepoReadRequest) -> FileContent:
    JsonStore().log_audit("repo.read", "ok", payload.relative_path, _preview(payload))
    return RepoService().read_file(payload.project_path, payload.relative_path, payload.max_bytes)


@router.post("/write", response_model=FileContent)
def repo_write(payload: RepoWriteRequest) -> FileContent:
    approval = ApprovalService()
    approval.ensure_allowed("repo.write", payload.approval_id, _preview(payload))
    try:
        result = RepoService().write_file(payload.project_path, payload.relative_path, payload.content, payload.create_dirs)
    except Exception:
        approval.mark_failed(payload.approval_id, "repo.write failed")
        raise
    approval.mark_consumed(payload.approval_id, "repo.write succeeded")
    JsonStore().log_audit("repo.write", "ok", payload.relative_path, _preview(payload))
    return result


@router.post("/delete", response_model=dict[str, str])
def repo_delete(payload: RepoDeleteRequest) -> dict[str, str]:
    approval = ApprovalService()
    approval.ensure_allowed("repo.delete", payload.approval_id, _preview(payload))
    try:
        result = RepoService().delete_file(payload.project_path, payload.relative_path)
    except Exception:
        approval.mark_failed(payload.approval_id, "repo.delete failed")
        raise
    approval.mark_consumed(payload.approval_id, "repo.delete succeeded")
    JsonStore().log_audit("repo.delete", "ok", payload.relative_path, _preview(payload))
    return result


@router.post("/apply-patch", response_model=CommandResult)
def repo_apply_patch(payload: RepoPatchRequest) -> CommandResult:
    approval = ApprovalService()
    if not payload.check_only:
        approval.ensure_allowed("repo.apply_patch", payload.approval_id, _preview(payload))
    result = RepoService().apply_patch(payload.project_path, payload.patch, payload.check_only, payload.timeout_seconds)
    if not payload.check_only:
        approval.finalize_command(payload.approval_id, result.exit_code, "repo.apply_patch succeeded", "repo.apply_patch failed")
    JsonStore().log_audit("repo.apply_patch", "ok" if result.exit_code == 0 else "failed", "check_only" if payload.check_only else "apply", _preview(payload))
    return result


@router.post("/clean-pyc", response_model=RepoCleanPycResult)
def repo_clean_pyc(payload: RepoCleanPycRequest) -> RepoCleanPycResult:
    approval = ApprovalService()
    approval.ensure_allowed("repo.clean_pyc", payload.approval_id, _preview(payload))
    try:
        result = RepoService().clean_pyc(payload.project_path, payload.relative_path)
    except Exception:
        approval.mark_failed(payload.approval_id, "repo.clean_pyc failed")
        raise
    approval.mark_consumed(payload.approval_id, "repo.clean_pyc succeeded")
    JsonStore().log_audit("repo.clean_pyc", "ok", payload.relative_path, _preview(payload))
    return result
