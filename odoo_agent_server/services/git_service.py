from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fastapi import HTTPException, status

from ..config import settings
from ..schemas import CommandResult
from .path_service import resolve_allowed_path
from .repo_service import RepoService
from .lock_service import RepoLockService


class GitService:
    def __init__(self) -> None:
        self.repo = RepoService()

    def _ssh_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # GIT_SSH_COMMAND may already be set by start.sh (base64 key inject).
        # If GIT_SSH_KEY_PATH is also configured, prefer that so the connector
        # config is the single source of truth.
        key = settings.git_ssh_key_path
        if key:
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
            )
        return env

    def _project(self, project_path: str) -> Path:
        project = resolve_allowed_path(project_path)
        if not (project / ".git").exists():
            result = self._run(["git", "rev-parse", "--show-toplevel"], project, 20)
            if result.exit_code != 0:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="project_path is not a git repository")
        return project

    def status(self, project_path: str) -> CommandResult:
        return self._run(["git", "status", "--short", "--branch"], self._project(project_path), 30)

    def current_branch(self, project_path: str) -> CommandResult:
        return self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], self._project(project_path), 20)

    def head_sha(self, project_path: str, timeout_seconds: int = 20) -> CommandResult:
        return self._run(["git", "rev-parse", "HEAD"], self._project(project_path), timeout_seconds)

    def diff(self, project_path: str, staged: bool = False, timeout_seconds: int = 30) -> CommandResult:
        command = ["git", "diff", "--cached"] if staged else ["git", "diff"]
        return self._run(command, self._project(project_path), timeout_seconds)

    def configure_identity(self, project_path: str) -> CommandResult:
        project = self._project(project_path)
        name_result = self._run(["git", "config", "user.name", settings.git_author_name], project, 20)
        if name_result.exit_code != 0:
            return name_result
        return self._run(["git", "config", "user.email", settings.git_author_email], project, 20)

    def validate_target(self, project_path: str, branch: str | None = None, remote: str = "origin") -> CommandResult:
        project = self._project(project_path)
        current = self.current_branch(project_path)
        if current.exit_code != 0:
            return current
        current_branch = current.stdout.strip()
        target_branch = branch or settings.target_git_branch or current_branch

        if settings.target_git_branch and target_branch != settings.target_git_branch:
            return CommandResult(
                command=["git", "validate-target"],
                cwd=str(project),
                exit_code=2,
                stderr=f"Refusing push to {target_branch}; expected {settings.target_git_branch}",
            )
        if settings.target_git_branch and current_branch not in {settings.target_git_branch, "HEAD"}:
            return CommandResult(
                command=["git", "validate-target"],
                cwd=str(project),
                exit_code=2,
                stderr=f"Refusing push from branch {current_branch}; expected {settings.target_git_branch}",
            )

        remote_result = self._run(["git", "remote", "get-url", remote], project, 20)
        if remote_result.exit_code != 0:
            return remote_result
        remote_url = remote_result.stdout.strip()
        if settings.target_git_repo and remote_url != settings.target_git_repo:
            return CommandResult(
                command=["git", "validate-target"],
                cwd=str(project),
                exit_code=2,
                stderr=f"Refusing push to remote {remote_url}; expected {settings.target_git_repo}",
            )
        return CommandResult(command=["git", "validate-target"], cwd=str(project), exit_code=0, stdout="Target git remote/branch validated.\n")

    def pull_rebase(self, project_path: str, remote: str = "origin", branch: str | None = None, timeout_seconds: int = 120) -> CommandResult:
        project = self._project(project_path)
        if not branch:
            branch_result = self.current_branch(str(project))
            if branch_result.exit_code != 0:
                return branch_result
            branch = branch_result.stdout.strip()
        if branch in {"", "HEAD"}:
            return CommandResult(command=["git", "pull", "--rebase"], cwd=str(project), exit_code=0, stdout="Detached HEAD; skipping pull --rebase.\n")
        remote_result = self._run(["git", "remote", "get-url", remote], project, 20)
        if remote_result.exit_code != 0:
            return CommandResult(command=["git", "pull", "--rebase", "--autostash", remote, branch], cwd=str(project), exit_code=0, stdout=f"Remote {remote} not configured; skipping pull --rebase.\n")
        fetch = self._run(["git", "fetch", remote], project, timeout_seconds)
        if fetch.exit_code != 0:
            return fetch
        return self._run(["git", "pull", "--rebase", "--autostash", remote, branch], project, timeout_seconds)

    def commit(self, project_path: str, message: str, paths: list[str], timeout_seconds: int = 60) -> CommandResult:
        project = self._project(project_path)
        branch_result = self.current_branch(str(project))
        branch = branch_result.stdout.strip() if branch_result.exit_code == 0 else None
        # Speed fix: only pull --rebase if remote has commits we don't have.
        # Check behind/ahead status first — skip network call if already ahead-only.
        behind_check = self._run(["git", "rev-list", "--count", f"HEAD..origin/{branch}"], project, 10)
        behind_count = behind_check.stdout.strip()
        if behind_check.exit_code == 0 and behind_count == "0":
            rebase_result = CommandResult(command=["git", "pull", "--rebase", "--skipped"], cwd=str(project), exit_code=0, stdout="Already ahead of remote — rebase skipped.\n")
        else:
            rebase_result = self.pull_rebase(str(project), "origin", branch, min(timeout_seconds, 120))
        if rebase_result.exit_code != 0 and settings.target_git_repo:
            return rebase_result
        identity_result = self.configure_identity(str(project))
        if identity_result.exit_code != 0:
            return identity_result
        if paths:
            add_command = ["git", "add"]
            for rel in paths:
                self.repo.target(str(project), rel)
                add_command.append(rel)
        else:
            add_command = ["git", "add", "-A"]
        with RepoLockService().locked(str(project), owner="git.commit", timeout_seconds=min(timeout_seconds, 120)):
            add_result = self._run(add_command, project, timeout_seconds)
            if add_result.exit_code != 0:
                return add_result
            diff_result = self._run(["git", "diff", "--cached", "--quiet"], project, timeout_seconds)
            if diff_result.exit_code == 0:
                return CommandResult(command=["git", "commit", "-m", message], cwd=str(project), exit_code=1, stderr="No staged changes to commit.")
            return self._run(["git", "commit", "-m", message], project, timeout_seconds)


    def revert_commit(self, project_path: str, sha: str, timeout_seconds: int = 120) -> CommandResult:
        project = self._project(project_path)
        if not sha or not all(c in "0123456789abcdefABCDEF" for c in sha[:40]):
            return CommandResult(command=["git", "revert", sha], cwd=str(project), exit_code=2, stderr="Unsafe or empty SHA for revert.")
        return self._run(["git", "revert", "--no-edit", sha], project, timeout_seconds)

    def push(self, project_path: str, remote: str = "origin", branch: str | None = None, set_upstream: bool = True, timeout_seconds: int = 120) -> CommandResult:
        project = self._project(project_path)
        if not branch:
            branch_result = self.current_branch(project_path)
            if branch_result.exit_code != 0:
                return branch_result
            branch = branch_result.stdout.strip()
        if settings.target_git_branch and not branch:
            branch = settings.target_git_branch
        if branch in {"", "HEAD"}:
            return CommandResult(command=["git", "push"], cwd=str(project), exit_code=2, stderr="Cannot push detached HEAD without explicit branch.")

        validation = self.validate_target(str(project), branch, remote)
        if validation.exit_code != 0:
            return validation

        command = ["git", "push"]
        if set_upstream:
            command.extend(["-u", remote, f"HEAD:{branch}"])
        else:
            command.extend([remote, f"HEAD:{branch}"])
        with RepoLockService().locked(str(project), owner="git.push", timeout_seconds=min(timeout_seconds, 120)):
            result = self._run(command, project, timeout_seconds)
            if result.exit_code != 0 and ("non-fast-forward" in (result.stderr or "") or "fetch first" in (result.stderr or "") or "rejected" in (result.stderr or "")):
                rebase = self.pull_rebase(str(project), remote, branch, min(timeout_seconds, 120))
                if rebase.exit_code != 0:
                    return rebase
                result = self._run(command, project, timeout_seconds)
        if result.exit_code == 0:
            sha = self.head_sha(str(project)).stdout.strip()
            result.stdout = (result.stdout or "") + f"\nPUSH_OK\nbranch={branch}\nsha={sha}\n"
        return result

    def _run(self, command: list[str], cwd: Path, timeout_seconds: int) -> CommandResult:
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=self._ssh_env(),
            )
            return CommandResult(
                command=command,
                cwd=str(cwd),
                exit_code=completed.returncode,
                stdout=completed.stdout[-20000:],
                stderr=completed.stderr[-20000:],
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command=command,
                cwd=str(cwd),
                exit_code=124,
                stdout=(exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "")[-20000:] if isinstance(exc.stderr, str) else "",
                timed_out=True,
            )
