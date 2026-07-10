from __future__ import annotations

from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import (
    CommandResult,
    FileContent,
    FileListItem,
    CustomModuleItem,
    RepoCleanPycRequest,
    RepoCleanPycResult,
    RepoCustomModulesRequest,
    RepoDeleteRequest,
    RepoListRequest,
    RepoAtomicPatchRequest,
    RepoPatchRequest,
    RepoPrepareRequest,
    RepoPrepareResult,
    RepoReadRangeRequest,
    RepoReadRequest,
    RepoSearchRequest,
    RepoWriteManyAtomicRequest,
    RepoWriteManyFastSafeRequest,
    RepoWriteManySmartRequest,
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
    if "files" in data and isinstance(data["files"], list):
        redacted = []
        for item in data["files"]:
            if isinstance(item, dict):
                copy = dict(item)
                if "content" in copy:
                    copy["content"] = f"<redacted text length={len(str(copy['content']))}>"
                redacted.append(copy)
            else:
                redacted.append(item)
        data["files"] = redacted
    return data


@router.post("/prepare", response_model=RepoPrepareResult)
def repo_prepare(payload: RepoPrepareRequest) -> RepoPrepareResult:
    approval = ApprovalService()
    approval.ensure_allowed("repo.prepare", payload.approval_id, _preview(payload))
    try:
        result = RepoService().prepare_repo(
            payload.repo_url,
            payload.branch,
            payload.repo_name,
            payload.workspace_root,
            payload.remote,
            payload.timeout_seconds,
            payload.project_path,
            payload.client_name,
        )
    except Exception:
        approval.mark_failed(payload.approval_id, "repo.prepare failed")
        raise
    approval.mark_consumed(payload.approval_id, "repo.prepare succeeded")
    JsonStore().log_audit("repo.prepare", "ok", result.project_path, _preview(payload))
    return result


@router.post("/custom-modules", response_model=list[CustomModuleItem])
def repo_custom_modules(payload: RepoCustomModulesRequest) -> list[CustomModuleItem]:
    JsonStore().log_audit("repo.list_custom_modules", "ok", payload.project_path, _preview(payload))
    return RepoService().list_custom_modules(payload.project_path, payload.max_depth, payload.include_uninstallable)


@router.post("/list", response_model=list[FileListItem])
def repo_list(payload: RepoListRequest) -> list[FileListItem]:
    JsonStore().log_audit("repo.list", "ok", payload.relative_path, _preview(payload))
    return RepoService().list_files(payload.project_path, payload.relative_path, payload.max_depth, payload.max_items)


@router.post("/read", response_model=FileContent)
def repo_read(payload: RepoReadRequest) -> FileContent:
    JsonStore().log_audit("repo.read", "ok", payload.relative_path, _preview(payload))
    return RepoService().read_file(payload.project_path, payload.relative_path, payload.max_bytes)


@router.post("/read-range", response_model=dict)
def repo_read_range(payload: RepoReadRangeRequest) -> dict:
    JsonStore().log_audit("repo.read_range", "ok", payload.relative_path, _preview(payload))
    return RepoService().read_range(payload.project_path, payload.relative_path, payload.start_line, payload.max_lines)


@router.post("/search", response_model=dict)
def repo_search(payload: RepoSearchRequest) -> dict:
    JsonStore().log_audit("repo.search", "ok", payload.pattern, _preview(payload))
    return RepoService().search(payload.project_path, payload.pattern, payload.relative_path, payload.max_matches, payload.context_lines, payload.regex)


@router.post("/write", response_model=FileContent)
def repo_write(payload: RepoWriteRequest) -> FileContent:
    approval = ApprovalService()
    approval.ensure_allowed("repo.write", payload.approval_id, _preview(payload))
    try:
        result = RepoService().write_file(payload.project_path, payload.relative_path, payload.content, payload.create_dirs, chunk_index=payload.chunk_index, total_chunks=payload.total_chunks)
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






@router.post("/write-many-fast-safe", response_model=CommandResult)
def repo_write_many_fast_safe(payload: RepoWriteManyFastSafeRequest) -> CommandResult:
    approval = ApprovalService()
    if not payload.check_only:
        approval.ensure_allowed("repo.write_many_fast_safe", payload.approval_id, _preview(payload))
    result = RepoService().write_many_fast_safe(payload.project_path, payload.files, payload.check_only, payload.timeout_seconds)
    if not payload.check_only:
        approval.finalize_command(payload.approval_id, result.exit_code, "repo.write_many_fast_safe succeeded", "repo.write_many_fast_safe failed")
    JsonStore().log_audit("repo.write_many_fast_safe", "ok" if result.exit_code == 0 else "failed", "check_only" if payload.check_only else "apply", _preview(payload))
    return result


@router.post("/write-many-smart", response_model=CommandResult)
def repo_write_many_smart(payload: RepoWriteManySmartRequest) -> CommandResult:
    approval = ApprovalService()
    if not payload.check_only:
        approval.ensure_allowed("repo.write_many_smart", payload.approval_id, _preview(payload))
    result = RepoService().write_many_smart(payload.project_path, payload.files, payload.check_only, payload.timeout_seconds, payload.strategy)
    if not payload.check_only:
        approval.finalize_command(payload.approval_id, result.exit_code, "repo.write_many_smart succeeded", "repo.write_many_smart failed")
    JsonStore().log_audit("repo.write_many_smart", "ok" if result.exit_code == 0 else "failed", payload.strategy, _preview(payload))
    return result


@router.post("/write-many-atomic", response_model=CommandResult)
def repo_write_many_atomic(payload: RepoWriteManyAtomicRequest) -> CommandResult:
    approval = ApprovalService()
    if not payload.check_only:
        approval.ensure_allowed("repo.write_many_atomic", payload.approval_id, _preview(payload))
    result = RepoService().write_many_atomic(payload.project_path, payload.files, payload.check_only, payload.timeout_seconds)
    if not payload.check_only:
        approval.finalize_command(payload.approval_id, result.exit_code, "repo.write_many_atomic succeeded", "repo.write_many_atomic failed")
    JsonStore().log_audit("repo.write_many_atomic", "ok" if result.exit_code == 0 else "failed", "check_only" if payload.check_only else "apply", _preview(payload))
    return result


@router.post("/apply-patch-atomic", response_model=CommandResult)
def repo_apply_patch_atomic(payload: RepoAtomicPatchRequest) -> CommandResult:
    approval = ApprovalService()
    if not payload.check_only:
        approval.ensure_allowed("repo.apply_patch_atomic", payload.approval_id, _preview(payload))
    result = RepoService().apply_patch_atomic(payload.project_path, payload.patch, payload.check_only, payload.timeout_seconds)
    if not payload.check_only:
        approval.finalize_command(payload.approval_id, result.exit_code, "repo.apply_patch_atomic succeeded", "repo.apply_patch_atomic failed")
    JsonStore().log_audit("repo.apply_patch_atomic", "ok" if result.exit_code == 0 else "failed", "check_only" if payload.check_only else "apply", _preview(payload))
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
