from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import settings
from ..schemas import JobStatus
from .git_service import GitService
from .path_service import resolve_allowed_path
from .store_service import JsonStore

_SUCCESSFUL_SAFE_DEPLOY_STATUSES = {
    "passed",
    "already_deployed_confirmed",
}


def _normal_path_for_compare(value: str | Path | None) -> str:
    if not value:
        return ""
    return str(Path(value).expanduser().resolve())


def stale_checkout_error(module_name: str, project_path: str | Path, branch: str | None) -> str:
    return (
        f"Module '{module_name}' not found at '{project_path}' even after syncing with remote branch "
        f"'{branch or '<unknown>'}'. This checkout may be stale, wiped, or pointed at the wrong "
        "project_path/branch for this module. Verify project_path and branch are correct before retrying."
    )


def _command_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _resolve_branch(project_path: Path, branch: str | None, git: GitService) -> str | None:
    if branch:
        return branch
    configured = settings.target_git_branch
    if configured:
        return configured
    current_branch = getattr(git, "current_branch", None)
    if callable(current_branch):
        try:
            result = current_branch(str(project_path))
            if getattr(result, "exit_code", 1) == 0:
                value = (getattr(result, "stdout", "") or "").strip()
                if value and value != "HEAD":
                    return value
        except Exception:
            return None
    return None


def module_was_successfully_deployed(
    *,
    project_path: str | Path,
    module_name: str,
    client_name: str | None = None,
    store: JsonStore | None = None,
) -> bool:
    """Return True when state/jobs show this project/module previously deployed.

    The checkout-current guard is intentionally strict for safe deploy, but for
    planning/read helpers we only auto-sync/fail-fast for module checkouts we
    know should already exist from a prior successful workflow job. This avoids
    blocking legitimate first-time/new-module planning.
    """
    if not module_name:
        return False
    store = store or JsonStore()
    target_project = _normal_path_for_compare(project_path)

    states = []
    try:
        states.append(store.load_session_state(client_name=client_name))
    except Exception:
        pass
    if client_name:
        try:
            states.append(store.load_session_state())
        except Exception:
            pass
    for state in states:
        if not isinstance(state, dict):
            continue
        if state.get("module_name") != module_name:
            continue
        if _normal_path_for_compare(state.get("project_path")) != target_project:
            continue
        if str(state.get("safe_deploy_status") or "") in _SUCCESSFUL_SAFE_DEPLOY_STATUSES:
            return True

    try:
        jobs = store.list_jobs(limit=200)
    except Exception:
        return False
    for job in jobs:
        if job.job_type != "safe_deploy_module" or job.status != JobStatus.passed:
            continue
        request = job.request or {}
        result = job.result if isinstance(job.result, dict) else {}
        if request.get("module_name") != module_name and result.get("module_name") != module_name:
            continue
        request_path = request.get("project_path") or result.get("project_path")
        if _normal_path_for_compare(request_path) != target_project:
            continue
        result_status = str(result.get("status") or "")
        if not result_status or result_status in _SUCCESSFUL_SAFE_DEPLOY_STATUSES:
            return True
    return False


def ensure_module_exists_or_sync(
    *,
    project_path: str | Path,
    module_name: str,
    branch: str | None,
    remote: str = "origin",
    git: GitService | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Ensure ``project_path/module_name`` exists, syncing the checkout if needed.

    When the module folder is already present, this performs no git network work.
    When it is missing, the function does exactly one authoritative sync
    (``git fetch`` + ``git reset --hard origin/<branch>`` via GitService) and
    re-checks the module path. It never falls back to broad ``ls -R``/``find``
    exploration; callers can fail immediately with the returned clear error.
    """
    project = resolve_allowed_path(str(project_path))
    module_path = project / module_name
    git = git or GitService()
    resolved_branch = _resolve_branch(project, branch, git)

    result: dict[str, Any] = {
        "ok": True,
        "status": "present",
        "project_path": str(project),
        "module_name": module_name,
        "module_path": str(module_path),
        "branch": resolved_branch,
        "remote": remote,
        "sync_attempted": False,
    }

    if module_path.exists():
        return result

    result.update({"ok": False, "status": "sync_failed", "sync_attempted": True})
    sync_result = None
    try:
        fetch_reset = getattr(git, "fetch_reset_hard", None)
        if callable(fetch_reset):
            sync_result = fetch_reset(str(project), remote=remote, branch=resolved_branch, timeout_seconds=timeout_seconds)
        else:
            pull_rebase = getattr(git, "pull_rebase", None)
            if not callable(pull_rebase):
                raise RuntimeError("GitService has no fetch_reset_hard() or pull_rebase() method")
            sync_result = pull_rebase(str(project), remote=remote, branch=resolved_branch, timeout_seconds=timeout_seconds)
    except Exception as exc:  # noqa: BLE001
        result["error"] = stale_checkout_error(module_name, project, resolved_branch)
        result["sync_error"] = str(exc)
        return result

    result["sync_result"] = _command_dump(sync_result)
    if getattr(sync_result, "exit_code", 1) != 0:
        result["error"] = stale_checkout_error(module_name, project, resolved_branch)
        result["sync_error"] = getattr(sync_result, "stderr", "") or getattr(sync_result, "stdout", "") or "git sync failed"
        return result

    if module_path.exists():
        result.update({"ok": True, "status": "synced", "error": None})
        return result

    result.update({
        "ok": False,
        "status": "missing_after_sync",
        "error": stale_checkout_error(module_name, project, resolved_branch),
    })
    return result


def maybe_ensure_known_module_current(
    *,
    project_path: str | Path,
    module_name: str | None,
    branch: str | None,
    client_name: str | None = None,
    remote: str = "origin",
    git: GitService | None = None,
    timeout_seconds: int = 120,
    require_prior_success: bool = True,
) -> dict[str, Any] | None:
    """Run the checkout guard when there is a concrete module/project target.

    ``None`` means the guard was intentionally skipped: no module was known, or
    ``require_prior_success`` was true and there is no evidence this module was
    previously deployed successfully in this session/job store.
    """
    if not module_name or module_name.startswith("<"):
        return None
    if require_prior_success and not module_was_successfully_deployed(
        project_path=project_path,
        module_name=module_name,
        client_name=client_name,
    ):
        return None
    return ensure_module_exists_or_sync(
        project_path=project_path,
        module_name=module_name,
        branch=branch,
        remote=remote,
        git=git,
        timeout_seconds=timeout_seconds,
    )


def module_name_from_relative_path(relative_path: str) -> str | None:
    value = str(relative_path or "").replace("\\", "/").strip("/")
    if not value or value.startswith("."):
        return None
    first = value.split("/", 1)[0]
    if not first or first in {"..", ".git"}:
        return None
    return first
