from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from ..auth import AuthDependency
from ..config import settings
from ..schemas import ApprovalRecord, ApprovalStatus, BaseProjectRequest, CommandResult, GitCommitRequest, GitDiffRequest, GitPushRequest
from ..services.approval_service import ApprovalService
from ..services.git_service import GitService
from ..services.store_service import JsonStore
from ..services.workflow_guard_service import safe_deploy_command_block
from ..services.module_detection_service import detect_affected_modules

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
    return detect_affected_modules(project_path, paths)


def _changed_paths_from_result(result: CommandResult) -> list[str]:
    if result.exit_code != 0:
        return []
    return [line.strip().replace("\\", "/") for line in (result.stdout or "").splitlines() if line.strip()]


def _manual_push_next_calls(project_path: str, modules: list[str]) -> list[dict[str, object]]:
    return [
        {
            "tool": "workflow.safe_deploy_module",
            "arguments": {
                "project_path": str(Path(project_path).expanduser().resolve()),
                "module_name": module,
                "run_qa": False,
                "wait_for_odoosh": True,
                "upgrade_module": True,
            },
        }
        for module in modules
    ]


def _manual_module_push_payload(project_path: str, modules: list[str], *, pushed_sha: str | None = None, acknowledged: bool = False) -> dict[str, object]:
    module_text = ", ".join(modules)
    message = (
        "Code was pushed directly via git.push, bypassing workflow.safe_deploy_module. "
        "No Odoo.sh module upgrade was triggered automatically. The remote Odoo.sh "
        "environment will NOT reflect this change until a module upgrade is run. "
        "Call workflow.safe_deploy_module with run_qa=false because code is already "
        "pushed, or call an Odoo.sh upgrade tool after confirming the pushed SHA."
    )
    next_calls = _manual_push_next_calls(project_path, modules)
    payload: dict[str, object] = {
        "status": "manual_git_push_pending_odoo_upgrade",
        "warning": message,
        "message": message,
        "module_names": modules,
        "module_name": modules[0] if modules else None,
        "project_path": str(Path(project_path).expanduser().resolve()),
        "acknowledge_manual_deploy": acknowledged,
        "next_call": next_calls[0] if next_calls else {"tool": "workflow.safe_deploy_module", "arguments": {"project_path": str(Path(project_path).expanduser().resolve())}},
        "next_calls": next_calls,
    }
    if pushed_sha:
        payload["pushed_sha"] = pushed_sha
    if module_text:
        payload["message"] = f"{message} Module(s): {module_text}."
        payload["warning"] = payload["message"]
    return payload


def _raw_module_push_refusal(project_path: str, modules: list[str]) -> CommandResult:
    module_text = ", ".join(modules)
    message = (
        f"Refusing raw git.push for Odoo module changes (module_name={module_text}). "
        "Use workflow.safe_deploy_module to push AND upgrade in one guaranteed step. "
        "If you intentionally want to push without upgrading (e.g. batching multiple "
        "changes before a single upgrade), retry with acknowledge_manual_deploy=true, "
        "but note the Odoo.sh environment will remain out of sync until you separately "
        f"run an upgrade tool for module(s): {module_text}."
    )
    payload = {
        "status": "raw_git_push_blocked_for_odoo_modules",
        "message": message,
        "module_names": modules,
        "module_name": modules[0] if modules else None,
        "project_path": str(Path(project_path).expanduser().resolve()),
        "next_call": _manual_push_next_calls(project_path, modules)[0] if modules else {"tool": "workflow.safe_deploy_module", "arguments": {"project_path": str(Path(project_path).expanduser().resolve())}},
        "next_calls": _manual_push_next_calls(project_path, modules),
    }
    return CommandResult(
        command=["git", "push", "blocked-odoo-module"],
        cwd=str(Path(project_path).expanduser().resolve()),
        exit_code=2,
        stdout=json.dumps(payload, indent=2, sort_keys=True),
        stderr=message,
    )


def _append_manual_push_warning(result: CommandResult, payload: dict[str, object]) -> CommandResult:
    result.stdout = (result.stdout or "") + "\nMCP_MANUAL_PUSH_PENDING_ODOO_UPGRADE\n" + json.dumps(payload, indent=2, sort_keys=True) + "\n"
    return result


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
    blocked = safe_deploy_command_block("git.commit", project_path=payload.project_path)
    if blocked:
        return blocked
    approval = ApprovalService()
    approval.ensure_allowed("git.commit", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    result = GitService().commit(payload.project_path, payload.message, payload.paths, payload.timeout_seconds)
    approval.finalize_command(payload.approval_id, result.exit_code, "git.commit succeeded", "git.commit failed")
    JsonStore().log_audit("git.commit", "ok" if result.exit_code == 0 else "failed", payload.message, {"paths": payload.paths})
    return result


@router.post("/push", response_model=CommandResult)
def git_push(payload: GitPushRequest) -> CommandResult:
    blocked = safe_deploy_command_block("git.push", project_path=payload.project_path)
    if blocked:
        return blocked

    git = GitService()
    branch = payload.branch
    if branch is None:
        branch_result = git.current_branch(payload.project_path)
        if branch_result.exit_code == 0:
            branch = branch_result.stdout.strip()

    changed_result = git.head_changed_files(payload.project_path)
    changed_paths = _changed_paths_from_result(changed_result)
    affected_modules = detect_affected_modules(payload.project_path, changed_paths)

    if settings.block_raw_push_for_modules and affected_modules and not payload.acknowledge_manual_deploy:
        return _raw_module_push_refusal(payload.project_path, affected_modules)

    protected_approval = _ensure_protected_branch_approval(branch, payload.approval_id)
    approval = ApprovalService()
    if not protected_approval:
        approval.ensure_allowed("git.push", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))

    push_started_epoch = int(time.time())
    result = git.push(payload.project_path, payload.remote, payload.branch, payload.set_upstream, payload.timeout_seconds)
    approval.finalize_command(payload.approval_id, result.exit_code, "git.push succeeded", "git.push failed")
    JsonStore().log_audit("git.push", "ok" if result.exit_code == 0 else "failed", branch or "current", {"remote": payload.remote})
    if result.exit_code == 0:
        sha_result = git.head_sha(payload.project_path)
        pushed_sha = sha_result.stdout.strip() if sha_result.exit_code == 0 else ""
        expected_hashes = _push_file_hashes(payload.project_path, changed_result.stdout if changed_result.exit_code == 0 else "")
        module_names = affected_modules or _module_names_from_paths(payload.project_path, sorted(expected_hashes))
        JsonStore().save_session_state({
            "last_git_push": {
                "project_path": str(Path(payload.project_path).resolve()),
                "branch": branch or payload.branch,
                "remote": payload.remote,
                "sha": pushed_sha,
                "started_epoch": push_started_epoch,
                "changed_files": sorted(set(changed_paths) | set(expected_hashes)),
                "module_names": module_names,
                "expected_user_file_hashes": expected_hashes,
                "verified_for_module_deploy": False,
                "manual_push_requires_upgrade": bool(module_names),
            },
            "last_sha": pushed_sha,
            "safe_deploy_status": "manual_push_pending_deploy_verification" if module_names else "manual_push_done",
        })
        if module_names:
            result = _append_manual_push_warning(
                result,
                _manual_module_push_payload(
                    payload.project_path,
                    module_names,
                    pushed_sha=pushed_sha,
                    acknowledged=payload.acknowledge_manual_deploy,
                ),
            )
    return result
