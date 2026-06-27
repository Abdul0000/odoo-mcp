from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import HTTPException, status

from ..config import settings
from ..schemas import CommandResult, CustomModuleItem, FileContent, FileListItem, RepoCleanPycResult, RepoPrepareResult
from .path_service import resolve_allowed_path

_TEXT_LIMIT_HINT = "File is too large for this endpoint. Increase max_bytes or use a narrower file."


class RepoService:
    """Safe local repository operations constrained by configured allowed roots."""

    def project(self, project_path: str) -> Path:
        project = resolve_allowed_path(project_path)
        if not project.exists() or not project.is_dir():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Project path not found: {project}")
        return project

    def target(self, project_path: str, relative_path: str) -> Path:
        project = self.project(project_path)
        return self._safe_join(project, relative_path)


    def prepare_repo(
        self,
        repo_url: str | None = None,
        branch: str | None = None,
        repo_name: str | None = None,
        workspace_root: str | None = None,
        remote: str = "origin",
        timeout_seconds: int = 180,
    ) -> RepoPrepareResult:
        """Clone or refresh the target repository before any coding task.

        This is the repo-first entry point ChatGPT should use for coding work:
        clone if missing; otherwise fetch, checkout, and pull the requested branch.
        """
        resolved_repo_url = repo_url or settings.target_git_repo
        resolved_branch = branch or settings.target_git_branch
        if not resolved_repo_url:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="repo_url is required or TARGET_GIT_REPO must be configured")
        if not resolved_branch:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="branch is required or TARGET_GIT_BRANCH must be configured")
        if not self._safe_branch_name(resolved_branch):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsafe branch name: {resolved_branch}")
        if not self._safe_remote_name(remote):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsafe remote name: {remote}")

        workspace = resolve_allowed_path(workspace_root or str(settings.workspace_path))
        workspace.mkdir(parents=True, exist_ok=True)
        if not repo_name:
            if settings.target_project_path:
                repo_name = Path(settings.target_project_path).name
            else:
                repo_name = self._repo_name_from_url(resolved_repo_url)
        if not self._safe_repo_folder_name(repo_name):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsafe repo_name: {repo_name}")

        project = (workspace / repo_name).resolve()
        if not (project == workspace or workspace in project.parents):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Resolved project path escapes workspace root")

        env = self._git_env()
        steps: list[CommandResult] = []
        already_cloned = project.exists()

        if already_cloned:
            if not (project / ".git").exists():
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Path exists but is not a git repository: {project}")
            remote_result = self._run(["git", "remote", "get-url", remote], project, 30, env=env)
            steps.append(remote_result)
            if remote_result.exit_code != 0:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=remote_result.stderr or remote_result.stdout or "Unable to read git remote")
            configured_remote = remote_result.stdout.strip()
            if settings.target_git_repo and configured_remote != settings.target_git_repo:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Configured remote mismatch: {configured_remote} != {settings.target_git_repo}")
            fetch = self._run(["git", "fetch", remote], project, timeout_seconds, env=env)
            steps.append(fetch)
            if fetch.exit_code != 0:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=fetch.stderr or fetch.stdout or "git fetch failed")
            checkout = self._run(["git", "checkout", resolved_branch], project, timeout_seconds, env=env)
            steps.append(checkout)
            if checkout.exit_code != 0:
                # Create a local tracking branch when it only exists on the remote.
                checkout = self._run(["git", "checkout", "-B", resolved_branch, f"{remote}/{resolved_branch}"], project, timeout_seconds, env=env)
                steps.append(checkout)
                if checkout.exit_code != 0:
                    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=checkout.stderr or checkout.stdout or "git checkout failed")
            pull = self._run(["git", "pull", "--ff-only", remote, resolved_branch], project, timeout_seconds, env=env)
            steps.append(pull)
            if pull.exit_code != 0:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=pull.stderr or pull.stdout or "git pull --ff-only failed")
        else:
            clone = self._run(["git", "clone", "--branch", resolved_branch, "--single-branch", resolved_repo_url, str(project)], workspace, timeout_seconds, env=env)
            steps.append(clone)
            if clone.exit_code != 0:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=clone.stderr or clone.stdout or "git clone failed")

        sha = self._run(["git", "rev-parse", "HEAD"], project, 30, env=env)
        steps.append(sha)
        if sha.exit_code != 0:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=sha.stderr or sha.stdout or "Unable to read HEAD SHA")
        git_status = self._run(["git", "status", "--short", "--branch"], project, 30, env=env)
        steps.append(git_status)

        return RepoPrepareResult(
            project_path=str(project),
            repo_url=resolved_repo_url,
            branch=resolved_branch,
            sha=sha.stdout.strip(),
            status=git_status.stdout,
            already_cloned=already_cloned,
            steps=steps,
        )

    def list_custom_modules(self, project_path: str, max_depth: int = 5, include_uninstallable: bool = False) -> list[CustomModuleItem]:
        """Discover custom Odoo modules from repo files, not from the Odoo database."""
        project = self.project(project_path)
        modules: list[CustomModuleItem] = []
        skip_dirs = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv", "odoo", "enterprise"}
        for manifest in project.rglob("__manifest__.py"):
            rel_parts = manifest.relative_to(project).parts
            if any(part in skip_dirs for part in rel_parts):
                continue
            if len(rel_parts) > max_depth + 1:
                continue
            meta = self._read_manifest_metadata(manifest)
            installable = meta.get("installable")
            if installable is False and not include_uninstallable:
                continue
            module_dir = manifest.parent
            modules.append(
                CustomModuleItem(
                    module_name=module_dir.name,
                    path=module_dir.relative_to(project).as_posix(),
                    manifest_path=manifest.relative_to(project).as_posix(),
                    installable=installable if isinstance(installable, bool) else None,
                    name=meta.get("name") if isinstance(meta.get("name"), str) else None,
                    version=meta.get("version") if isinstance(meta.get("version"), str) else None,
                    author=meta.get("author") if isinstance(meta.get("author"), str) else None,
                    application=meta.get("application") if isinstance(meta.get("application"), bool) else None,
                )
            )
        return sorted(modules, key=lambda item: item.module_name)

    def list_files(self, project_path: str, relative_path: str = ".", max_depth: int = 3, max_items: int = 500) -> list[FileListItem]:
        root = self.target(project_path, relative_path)
        if not root.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Path not found: {root}")
        if root.is_file():
            rel = root.relative_to(self.project(project_path)).as_posix()
            return [FileListItem(path=rel, kind="file", size=root.stat().st_size)]

        items: list[FileListItem] = []
        base_depth = len(root.parts)
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            depth = len(current_path.parts) - base_depth
            if depth >= max_depth:
                dirs[:] = []
            dirs[:] = sorted(d for d in dirs if d not in {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"})
            for dirname in dirs:
                rel = (current_path / dirname).relative_to(self.project(project_path)).as_posix()
                items.append(FileListItem(path=rel, kind="dir"))
                if len(items) >= max_items:
                    return items
            for filename in sorted(files):
                rel_path = current_path / filename
                rel = rel_path.relative_to(self.project(project_path)).as_posix()
                items.append(FileListItem(path=rel, kind="file", size=rel_path.stat().st_size))
                if len(items) >= max_items:
                    return items
        return items

    def read_file(self, project_path: str, relative_path: str, max_bytes: int = 300_000) -> FileContent:
        target = self.target(project_path, relative_path)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"File not found: {target}")
        size = target.stat().st_size
        if size > max_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=_TEXT_LIMIT_HINT)
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Only UTF-8 text files are supported") from exc
        return FileContent(path=target.relative_to(self.project(project_path)).as_posix(), content=content, size=size)

    def write_file(self, project_path: str, relative_path: str, content: str, create_dirs: bool = True) -> FileContent:
        self._reject_git_internal_path(relative_path)
        target = self.target(project_path, relative_path)
        if create_dirs:
            target.parent.mkdir(parents=True, exist_ok=True)
        elif not target.parent.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Parent directory not found: {target.parent}")
        target.write_text(content, encoding="utf-8")
        return FileContent(path=target.relative_to(self.project(project_path)).as_posix(), content=content, size=target.stat().st_size)

    def delete_file(self, project_path: str, relative_path: str) -> dict[str, str]:
        self._reject_git_internal_path(relative_path)
        target = self.target(project_path, relative_path)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"File not found: {target}")
        target.unlink()
        return {"deleted": target.relative_to(self.project(project_path)).as_posix()}

    def apply_patch(self, project_path: str, patch: str, check_only: bool = False, timeout_seconds: int = 60) -> CommandResult:
        project = self.project(project_path)
        self._validate_patch_paths(project, patch)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as handle:
            handle.write(patch)
            patch_path = Path(handle.name)
        try:
            check_cmd = ["git", "apply", "--check", str(patch_path)]
            check = self._run(check_cmd, project, timeout_seconds)
            if check.exit_code != 0 or check_only:
                return check
            return self._run(["git", "apply", str(patch_path)], project, timeout_seconds)
        finally:
            patch_path.unlink(missing_ok=True)

    def clean_pyc(self, project_path: str, relative_path: str = ".") -> RepoCleanPycResult:
        root = self.target(project_path, relative_path)
        if not root.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Path not found: {root}")
        removed_files: list[str] = []
        removed_dirs: list[str] = []
        base = self.project(project_path)
        for pyc in root.rglob("*.pyc") if root.is_dir() else []:
            if pyc.is_file():
                removed_files.append(pyc.relative_to(base).as_posix())
                pyc.unlink(missing_ok=True)
        for cache_dir in sorted((p for p in root.rglob("__pycache__") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            removed_dirs.append(cache_dir.relative_to(base).as_posix())
            shutil.rmtree(cache_dir, ignore_errors=True)
        return RepoCleanPycResult(project_path=str(base), scanned_path=str(root), removed_files=removed_files, removed_dirs=removed_dirs)



    def _git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if settings.git_ssh_key_path:
            env["GIT_SSH_COMMAND"] = f"ssh -i {settings.git_ssh_key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        return env

    def _repo_name_from_url(self, repo_url: str) -> str:
        cleaned = repo_url.rstrip("/")
        name = cleaned.rsplit("/", 1)[-1]
        if ":" in name and not name.endswith(".git"):
            name = name.rsplit(":", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        return name or "repo"

    def _safe_repo_folder_name(self, value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9._-]+", value)) and value not in {".", ".."}

    def _safe_remote_name(self, value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9._/-]+", value)) and not value.startswith(("-", "/")) and ".." not in value

    def _safe_branch_name(self, value: str) -> bool:
        if value.startswith(("-", "/")) or value.endswith("/") or ".." in value or "@{" in value:
            return False
        return bool(re.fullmatch(r"[A-Za-z0-9._/-]+", value))

    def _read_manifest_metadata(self, manifest: Path) -> dict:
        try:
            raw = manifest.read_text(encoding="utf-8")
            data = ast.literal_eval(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _reject_git_internal_path(self, relative_path: str) -> None:
        normalized = relative_path.replace("\\", "/").strip("/")
        if normalized == ".git" or normalized.startswith(".git/"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Writing inside .git is blocked. Use dedicated git tools.",
            )

    def _safe_join(self, project: Path, relative_path: str) -> Path:
        if not relative_path:
            relative_path = "."
        rel = Path(relative_path)
        if rel.is_absolute() or "\x00" in relative_path:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="relative_path must be a relative path")
        resolved = (project / rel).resolve()
        if not (resolved == project or project in resolved.parents):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Path escapes project root: {relative_path}")
        return resolved

    def _validate_patch_paths(self, project: Path, patch: str) -> None:
        if not patch.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Patch is empty")

        def safe_patch_path(raw_path: str) -> bool:
            # Strip optional tab timestamp part from ---/+++ headers.
            candidate = raw_path.split("\t", 1)[0].strip().strip('"')
            if candidate == "/dev/null":
                return True
            if "\x00" in candidate or candidate.startswith(("/", "~")):
                return False
            # Standard git patches use a/ and b/ prefixes. We reject unusual
            # absolute/implicit targets because this connector should only patch
            # files inside project_path.
            if candidate.startswith(("a/", "b/")):
                candidate = candidate[2:]
            parts = [part for part in candidate.replace("\\", "/").split("/") if part]
            if not parts:
                return False
            if parts[0] == ".git":
                return False
            return all(part not in {".", ".."} for part in parts)

        for line in patch.splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                if len(parts) < 4 or not safe_patch_path(parts[2]) or not safe_patch_path(parts[3]):
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsafe path in patch header: {line[:180]}")
            elif line.startswith(("--- ", "+++ ")):
                raw_path = line[4:].split(" ", 1)[0]
                if not safe_patch_path(raw_path):
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsafe path in patch header: {line[:180]}")

    def _run(self, command: list[str], cwd: Path, timeout_seconds: int, env: dict[str, str] | None = None) -> CommandResult:
        try:
            completed = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_seconds, check=False, env=env)
            return CommandResult(command=command, cwd=str(cwd), exit_code=completed.returncode, stdout=completed.stdout[-20000:], stderr=completed.stderr[-20000:])
        except subprocess.TimeoutExpired as exc:
            return CommandResult(command=command, cwd=str(cwd), exit_code=124, stdout=(exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else "", stderr=(exc.stderr or "")[-20000:] if isinstance(exc.stderr, str) else "", timed_out=True)
