from __future__ import annotations

import hashlib
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from ..auth import AuthDependency
from ..config import settings
from ..schemas import ApprovalRecord, ApprovalStatus, BaseProjectRequest, CommandResult, GitCommitRequest, GitDiffRequest, GitPushRequest
from ..services.approval_service import ApprovalService
from ..services.git_service import GitService
from ..services.store_service import JsonStore

router = APIRouter(prefix="/git", tags=["git"], dependencies=[AuthDependency])


def _normalise_branch(branch: str | None) -> str | None:
    if not branch:
        return None
    value = branch.strip()
    if value.startswith("refs/heads/"):
        value = value.removeprefix("refs/heads/")
    return value.lower()


def _is_protected_branch(branch: str | None) -> bool:
    normalised = _normalise_branch(branch)
    protected = {"main", "master", "production"} | {_normalise_branch(item) for item in settings.protected_branches}
    return bool(normalised and normalised in protected)


def _push_file_hashes(project_path: str, changed_stdout: str) -> dict[str, str]:
    project = Path(project_path).resolve()
    expected: dict[str, str] = {}
    allowed_suffixes = (".py", ".xml", ".js", ".scss", "__manifest__.py")
    for raw_line in changed_stdout.splitlines():
        rel = raw_line.strip().replace("\\", "/")
        if not rel or rel.startswith(".git/") or not rel.endswith(allowed_suffixes):
            continue
        file_path = (project / rel).resolve()
        if not file_path.is_file() or not (file_path == project or project in file_path.parents):
            continue
        expected[rel] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return expected


def _module_names_from_paths(project_path: str, paths: list[str]) -> list[str]:
    project = Path(project_path).resolve()
    names: set[str] = set()
    for rel in paths:
        first = rel.split("/", 1)[0]
        if first and (project / first / "__manifest__.py").exists():
            names.add(first)
    return sorted(names)


def _ensure_protected_branch_approval(branch: str | None, approval_id: str | None) -> bool:
    if not _is_protected_branch(branch):
        return False
    store = JsonStore()
    if not approval_id:
        record = store.create_approval(ApprovalRecord(action="git.push", summary=f"Push protected branch {branch}", payload_preview={"branch": branch}))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "approval_required": True,
                "approval_id": record.id,
                "action": "git.push",
                "message": "Protected branch push requires explicit git.push approval.",
            },
        )
    record = store.get_approval(approval_id)
    if record.action != "git.push":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Approval {approval_id} is for {record.action}, not git.push")
    if record.status != ApprovalStatus.approved:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Approval {approval_id} is {record.status.value}, not approved")
    store.set_approval_status(approval_id, ApprovalStatus.running, reason="Protected branch push started")
    return True


@router.post("/status", response_model=CommandResult)
def git_status(payload: BaseProjectRequest) -> CommandResult:
    result = GitService().status(payload.project_path)
    JsonStore().log_audit("git.status", "ok" if result.exit_code == 0 else "failed", payload.project_path)
    return result


@router.post("/current-branch", response_model=CommandResult)
def git_current_branch(payload: BaseProjectRequest) -> CommandResult:
    return GitService().current_branch(payload.project_path)


@router.post("/diff", response_model=CommandResult)
def git_diff(payload: GitDiffRequest) -> CommandResult:
    result = GitService().diff(payload.project_path, payload.staged, payload.timeout_seconds)
    JsonStore().log_audit("git.diff", "ok" if result.exit_code == 0 else "failed", payload.project_path, {"staged": payload.staged})
    return result


@router.post("/commit", response_model=CommandResult)
def git_commit(payload: GitCommitRequest) -> CommandResult:
    approval = ApprovalService()
    approval.ensure_allowed("git.commit", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    result = GitService().commit(payload.project_path, payload.message, payload.paths, payload.timeout_seconds)
    approval.finalize_command(payload.approval_id, result.exit_code, "git.commit succeeded", "git.commit failed")
    JsonStore().log_audit("git.commit", "ok" if result.exit_code == 0 else "failed", payload.message, {"paths": payload.paths})
    return result


@router.post("/push", response_model=CommandResult)
def git_push(payload: GitPushRequest) -> CommandResult:
    branch = payload.branch
    if branch is None:
        branch_result = GitService().current_branch(payload.project_path)
        if branch_result.exit_code == 0:
            branch = branch_result.stdout.strip()
    protected_approval = _ensure_protected_branch_approval(branch, payload.approval_id)
    approval = ApprovalService()
    if not protected_approval:
        approval.ensure_allowed("git.push", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    git = GitService()
    push_started_epoch = int(time.time())
    changed_result = git.head_changed_files(payload.project_path)
    result = git.push(payload.project_path, payload.remote, payload.branch, payload.set_upstream, payload.timeout_seconds)
    approval.finalize_command(payload.approval_id, result.exit_code, "git.push succeeded", "git.push failed")
    JsonStore().log_audit("git.push", "ok" if result.exit_code == 0 else "failed", branch or "current", {"remote": payload.remote})
    if result.exit_code == 0:
        sha_result = git.head_sha(payload.project_path)
        pushed_sha = sha_result.stdout.strip() if sha_result.exit_code == 0 else ""
        expected_hashes = _push_file_hashes(payload.project_path, changed_result.stdout if changed_result.exit_code == 0 else "")
        JsonStore().save_session_state({
            "last_git_push": {
                "project_path": str(Path(payload.project_path).resolve()),
                "branch": branch or payload.branch,
                "remote": payload.remote,
                "sha": pushed_sha,
                "started_epoch": push_started_epoch,
                "changed_files": sorted(expected_hashes),
                "module_names": _module_names_from_paths(payload.project_path, sorted(expected_hashes)),
                "expected_user_file_hashes": expected_hashes,
                "verified_for_module_deploy": False,
            },
            "last_sha": pushed_sha,
            "safe_deploy_status": "manual_push_pending_deploy_verification",
        })
    return result
