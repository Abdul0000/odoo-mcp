from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi import HTTPException, status

from ..config import settings
from ..schemas import CommandResult
from .path_service import resolve_allowed_path
from .repo_service import RepoService


class GitService:
    def __init__(self) -> None:
        self.repo = RepoService()

    def _project(self, project_path: str) -> Path:
        project = resolve_allowed_path(project_path)
        if not (project / ".git").exists():
            # Still allow worktrees/subdirectories inside git repos by asking git.
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

    def commit(self, project_path: str, message: str, paths: list[str], timeout_seconds: int = 60) -> CommandResult:
        project = self._project(project_path)
        identity_result = self.configure_identity(str(project))
        if identity_result.exit_code != 0:
            return identity_result
        if paths:
            add_command = ["git", "add"]
            for rel in paths:
                self.repo.target(str(project), rel)  # validates path
                add_command.append(rel)
        else:
            add_command = ["git", "add", "-A"]
        add_result = self._run(add_command, project, timeout_seconds)
        if add_result.exit_code != 0:
            return add_result
        diff_result = self._run(["git", "diff", "--cached", "--quiet"], project, timeout_seconds)
        if diff_result.exit_code == 0:
            return CommandResult(command=["git", "commit", "-m", message], cwd=str(project), exit_code=1, stderr="No staged changes to commit.")
        return self._run(["git", "commit", "-m", message], project, timeout_seconds)

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
        result = self._run(command, project, timeout_seconds)
        if result.exit_code == 0:
            sha = self.head_sha(str(project)).stdout.strip()
            result.stdout = (result.stdout or "") + f"\nPUSH_OK\nbranch={branch}\nsha={sha}\n"
        return result

    def _run(self, command: list[str], cwd: Path, timeout_seconds: int) -> CommandResult:
        try:
            completed = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_seconds, check=False)
            return CommandResult(command=command, cwd=str(cwd), exit_code=completed.returncode, stdout=completed.stdout[-20000:], stderr=completed.stderr[-20000:])
        except subprocess.TimeoutExpired as exc:
            return CommandResult(command=command, cwd=str(cwd), exit_code=124, stdout=(exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else "", stderr=(exc.stderr or "")[-20000:] if isinstance(exc.stderr, str) else "", timed_out=True)
