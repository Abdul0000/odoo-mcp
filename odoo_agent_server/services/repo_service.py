from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from threading import RLock
from uuid import uuid4

from fastapi import HTTPException, status

from ..config import mask_repo_url, settings
from ..schemas import CommandResult, CustomModuleItem, FileContent, FileListItem, RepoCleanPycResult, RepoPrepareResult
from .client_resolver_service import ClientResolverService
from .path_service import resolve_allowed_path
from .lock_service import RepoLockService

_TEXT_LIMIT_HINT = "File is too large for this endpoint. Increase max_bytes or use a narrower file."


class RepoService:
    """Safe local repository operations constrained by configured allowed roots."""

    _module_cache_lock = RLock()
    _module_cache: dict[tuple[str, int, bool, int, float], tuple[float, list[CustomModuleItem]]] = {}
    _module_cache_ttl_seconds = 30.0

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
        project_path: str | None = None,
        client_name: str | None = None,
    ) -> RepoPrepareResult:
        """Clone or refresh the target repository before any coding task.

        This is the repo-first entry point ChatGPT should use for coding work:
        clone if missing; otherwise fetch, checkout, and pull the requested branch.
        """
        resolver = ClientResolverService()
        client = resolver.resolve_client(
            client_name=client_name,
            project_path=project_path or settings.target_project_path,
            require_complete=False,
        )
        use_client_repo = bool(client and (not repo_url or repo_url == client.repo))
        resolved_repo_url = (client.authenticated_repo if use_client_repo and client else None) or repo_url or settings.target_git_repo
        plain_repo_url = repo_url or (client.repo if client else None) or settings.target_git_repo
        resolved_branch = branch or (client.branch if client else None) or settings.target_git_branch
        resolved_project_path = project_path or (client.path if client else None) or settings.target_project_path
        if not resolved_repo_url:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="repo_url is required or TARGET_GIT_REPO must be configured")
        if not resolved_branch:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="branch is required or TARGET_GIT_BRANCH must be configured")
        if not self._safe_branch_name(resolved_branch):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsafe branch name: {resolved_branch}")
        if not self._safe_remote_name(remote):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsafe remote name: {remote}")

        if resolved_project_path:
            project = resolve_allowed_path(resolved_project_path)
            workspace = project.parent
            workspace.mkdir(parents=True, exist_ok=True)
            repo_name = project.name
        else:
            workspace = resolve_allowed_path(workspace_root or str(settings.workspace_path))
            workspace.mkdir(parents=True, exist_ok=True)
            if not repo_name:
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
                if any(project.iterdir()):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            f"Selected client path exists but is not a git repository and is not empty: {project}. "
                            "Refusing to clone over existing files."
                        ),
                    )
                clone = self._run(["git", "clone", "--branch", resolved_branch, "--single-branch", resolved_repo_url, str(project)], workspace, timeout_seconds, env=env)
                steps.append(clone)
                if clone.exit_code != 0:
                    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=clone.stderr or clone.stdout or "git clone failed")
            remote_result = self._run(["git", "remote", "get-url", remote], project, 30, env=env)
            steps.append(remote_result)
            if remote_result.exit_code != 0:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=remote_result.stderr or remote_result.stdout or "Unable to read git remote")
            configured_remote = remote_result.stdout.strip()
            expected_remote = plain_repo_url
            if expected_remote and not self._remote_matches(configured_remote, expected_remote):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Configured remote mismatch: {configured_remote} != {expected_remote}")
            # Speed: dry-run fetch first — skip full fetch if nothing changed
            dry = self._run(["git", "fetch", "--dry-run", remote], project, 10, env=env)
            if dry.stdout.strip() or dry.stderr.strip():
                fetch = self._run(["git", "fetch", remote], project, timeout_seconds, env=env)
            else:
                fetch = CommandResult(command=["git", "fetch", "--skipped"], cwd=str(project), exit_code=0, stdout="Already up to date — fetch skipped.\n")
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
            project.parent.mkdir(parents=True, exist_ok=True)
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
            repo_url=plain_repo_url or resolved_repo_url,
            branch=resolved_branch,
            sha=sha.stdout.strip(),
            status=git_status.stdout,
            already_cloned=already_cloned,
            steps=steps,
        )

    def list_custom_modules(self, project_path: str, max_depth: int = 5, include_uninstallable: bool = False) -> list[CustomModuleItem]:
        """Discover custom Odoo modules from repo files, not from the Odoo database.

        Uses a short in-memory cache keyed by manifest count/latest mtime. This
        keeps repo.prepare → repo.list_custom_modules fast in agent loops while
        still invalidating as soon as manifests are added or edited.
        """
        project = self.project(project_path)
        manifest_count, latest_mtime = self._manifest_cache_stamp(project, max_depth)
        cache_key = (str(project), max_depth, include_uninstallable, manifest_count, latest_mtime)
        now = time.monotonic()
        with self._module_cache_lock:
            cached = self._module_cache.get(cache_key)
            if cached and cached[0] > now:
                return list(cached[1])

        modules: list[CustomModuleItem] = []
        skip_dirs = {
            ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
            "node_modules", ".venv", "venv", "env", "odoo", "enterprise",
            "src/odoo", "src/enterprise", "static/lib",
        }
        for manifest in self._iter_manifest_files(project, max_depth, skip_dirs):
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
        result = sorted(modules, key=lambda item: item.module_name)
        with self._module_cache_lock:
            # Keep the cache bounded so long-running connector processes do not grow unbounded.
            if len(self._module_cache) > 32:
                self._module_cache.clear()
            self._module_cache[cache_key] = (time.monotonic() + self._module_cache_ttl_seconds, result)
        return list(result)

    def _iter_manifest_files(self, project: Path, max_depth: int, skip_dirs: set[str]):
        base_depth = len(project.parts)
        for current, dirs, files in os.walk(project):
            current_path = Path(current)
            rel_parts = current_path.relative_to(project).parts if current_path != project else ()
            depth = len(current_path.parts) - base_depth
            dirs[:] = [
                d for d in dirs
                if d not in skip_dirs
                and "/".join((*rel_parts, d)) not in skip_dirs
                and depth < max_depth
            ]
            if "__manifest__.py" in files and depth <= max_depth:
                yield current_path / "__manifest__.py"

    def _manifest_cache_stamp(self, project: Path, max_depth: int) -> tuple[int, float]:
        skip_dirs = {
            ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
            "node_modules", ".venv", "venv", "env", "odoo", "enterprise",
            "src/odoo", "src/enterprise", "static/lib",
        }
        count = 0
        latest = 0.0
        for manifest in self._iter_manifest_files(project, max_depth, skip_dirs):
            count += 1
            try:
                latest = max(latest, manifest.stat().st_mtime)
            except OSError:
                pass
        return count, latest

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

    # Fix 2/6: line-count threshold above which chunked mode is recommended.
    _LARGE_FILE_LINE_THRESHOLD = 1000
    # Fix 6: unified-diff line limit to include in response (keeps payload small)
    _DIFF_PREVIEW_LINES = 60

    def write_file(
        self,
        project_path: str,
        relative_path: str,
        content: str,
        create_dirs: bool = True,
        chunk_index: int = 0,
        total_chunks: int = 1,
    ) -> FileContent:
        """Write a file, with optional chunked mode for large files (>1000 lines).

        Chunked mode (total_chunks > 1):
          chunk_index=0 → create/overwrite file with first chunk
          chunk_index=1..N-1 → append to existing partial file
          chunk_index=N-1 (last) → append final chunk, return complete file info

        Fix 2: Files written in a single call (total_chunks==1) that exceed
        _LARGE_FILE_LINE_THRESHOLD lines will carry a 'large_file_warning' in
        the response extra — ChatGPT must use chunked mode next time.

        Fix 6: When overwriting an existing file in single-write mode (chunk_index==0,
        total_chunks==1), the response always includes a short unified diff showing
        what changed, allowing the agent to verify correctness before committing.
        """
        self._reject_git_internal_path(relative_path)
        with RepoLockService().locked(project_path, owner="repo.write", timeout_seconds=60):
            target = self.target(project_path, relative_path)
            if create_dirs:
                target.parent.mkdir(parents=True, exist_ok=True)
            elif not target.parent.exists():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Parent directory not found: {target.parent}"
                )

            # Fix 6: capture old content before overwrite for diff preview
            old_content: str | None = None
            if total_chunks == 1 and target.exists() and target.is_file():
                try:
                    old_content = target.read_text(encoding="utf-8")
                except Exception:
                    old_content = None

            if total_chunks == 1:
                # Normal single-write: atomic replace
                tmp = target.with_name(f".{target.name}.tmp")
                tmp.write_text(content, encoding="utf-8")
                tmp.replace(target)
            elif chunk_index == 0:
                # First chunk: create/overwrite
                tmp = target.with_name(f".{target.name}.chunk.tmp")
                tmp.write_text(content, encoding="utf-8")
                tmp.replace(target)
            else:
                # Subsequent chunks: append
                if not target.exists():
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Chunk {chunk_index}: base file missing — send chunk_index=0 first"
                    )
                with open(target, "a", encoding="utf-8") as f:
                    f.write(content)

            size = target.stat().st_size
            written_text = target.read_text(encoding="utf-8") if target.exists() else ""
            lines = written_text.count("\n")
            is_complete = (chunk_index == total_chunks - 1) or total_chunks == 1

            # Build extra metadata
            extra: dict = {}
            if total_chunks > 1:
                extra = {
                    "chunk_index": chunk_index,
                    "total_chunks": total_chunks,
                    "complete": is_complete,
                    "total_lines": lines,
                    "message": (
                        f"File complete ({lines} lines, {size} bytes)"
                        if is_complete
                        else f"Chunk {chunk_index+1}/{total_chunks} written. Send next chunk with chunk_index={chunk_index+1}"
                    ),
                }
            else:
                # Fix 2: large file warning
                if lines > self._LARGE_FILE_LINE_THRESHOLD:
                    extra["large_file_warning"] = (
                        f"File has {lines} lines. Single-pass repo.write is supported up to "
                        f"{self._LARGE_FILE_LINE_THRESHOLD} lines. For larger files use chunked "
                        f"mode (total_chunks>1); follow-up chunks are append-only and do not have "
                        f"this single-pass warning limit."
                    )
                # Fix 6: diff preview on overwrite
                if old_content is not None:
                    diff_preview = self._unified_diff_preview(
                        old_content,
                        content,
                        str(target.relative_to(self.project(project_path)).as_posix()),
                        max_lines=self._DIFF_PREVIEW_LINES,
                    )
                    extra["diff_preview"] = diff_preview if diff_preview else "[no changes — content identical]"
                else:
                    extra["diff_preview"] = "[new file created]"

            return FileContent(
                path=target.relative_to(self.project(project_path)).as_posix(),
                content=content if is_complete else f"[chunk {chunk_index+1}/{total_chunks} appended]",
                size=size,
                extra=extra,
            )

    @staticmethod
    def _unified_diff_preview(old: str, new: str, filename: str, max_lines: int = 60) -> str:
        """Return a short unified diff string (truncated to max_lines)."""
        import difflib
        diff_lines = list(difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            n=3,
        ))
        if not diff_lines:
            return ""
        if len(diff_lines) > max_lines:
            diff_lines = diff_lines[:max_lines]
            diff_lines.append(f"\n... (diff truncated at {max_lines} lines)\n")
        return "".join(diff_lines)

    def delete_file(self, project_path: str, relative_path: str) -> dict[str, str]:
        self._reject_git_internal_path(relative_path)
        with RepoLockService().locked(project_path, owner="repo.delete", timeout_seconds=60):
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
            with RepoLockService().locked(project_path, owner="repo.apply_patch", timeout_seconds=timeout_seconds):
                return self._run(["git", "apply", str(patch_path)], project, timeout_seconds)
        finally:
            patch_path.unlink(missing_ok=True)


    def apply_patch_atomic(self, project_path: str, patch: str, check_only: bool = False, timeout_seconds: int = 60) -> CommandResult:
        """Apply a patch through an isolated temporary git worktree/branch.

        The real checkout remains untouched until:
        1) patch paths are validated,
        2) the patch applies cleanly in the isolated worktree,
        3) the resulting whole-repo diff applies cleanly to the real checkout.
        """
        project = self.project(project_path)
        self._validate_patch_paths(project, patch)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as handle:
            handle.write(patch)
            patch_path = Path(handle.name)
        try:
            return self._run_atomic_worktree_transaction(
                project=project,
                transaction_name="repo.apply_patch_atomic",
                timeout_seconds=timeout_seconds,
                check_only=check_only,
                mutate_worktree=lambda worktree: self._apply_patch_inside_worktree(worktree, patch_path, timeout_seconds),
            )
        finally:
            patch_path.unlink(missing_ok=True)

    def write_many_atomic(self, project_path: str, files: list[object], check_only: bool = False, timeout_seconds: int = 60) -> CommandResult:
        """Write many files as one atomic transaction via temp worktree/branch."""
        project = self.project(project_path)
        seen: set[str] = set()
        normalized_files: list[dict[str, object]] = []
        for item in files:
            raw_path = str(getattr(item, "relative_path", ""))
            content = str(getattr(item, "content", ""))
            create_dirs = bool(getattr(item, "create_dirs", True))
            self._reject_git_internal_path(raw_path)
            # Validate against the real project root first.
            resolved = self._safe_join(project, raw_path)
            rel = resolved.relative_to(project).as_posix()
            if rel in seen:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Duplicate file in atomic write: {rel}")
            seen.add(rel)
            normalized_files.append({"relative_path": rel, "content": content, "create_dirs": create_dirs})
        if not normalized_files:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="files must not be empty")

        return self._run_atomic_worktree_transaction(
            project=project,
            transaction_name="repo.write_many_atomic",
            timeout_seconds=timeout_seconds,
            check_only=check_only,
            mutate_worktree=lambda worktree: self._write_many_inside_worktree(worktree, normalized_files),
        )

    def _apply_patch_inside_worktree(self, worktree: Path, patch_path: Path, timeout_seconds: int) -> CommandResult:
        check = self._run(["git", "apply", "--check", str(patch_path)], worktree, timeout_seconds)
        if check.exit_code != 0:
            return check
        return self._run(["git", "apply", str(patch_path)], worktree, timeout_seconds)

    def _write_many_inside_worktree(self, worktree: Path, files: list[dict[str, object]]) -> CommandResult:
        changed: list[str] = []
        for item in files:
            rel = str(item["relative_path"])
            target = self._safe_join(worktree, rel)
            if bool(item.get("create_dirs", True)):
                target.parent.mkdir(parents=True, exist_ok=True)
            elif not target.parent.exists():
                return CommandResult(
                    command=["repo", "write_many_atomic"],
                    cwd=str(worktree),
                    exit_code=1,
                    stderr=f"Parent directory not found: {target.parent}",
                )
            tmp = target.with_name(f".{target.name}.tmp")
            tmp.write_text(str(item["content"]), encoding="utf-8")
            tmp.replace(target)
            changed.append(rel)
        return CommandResult(command=["repo", "write_many_atomic"], cwd=str(worktree), exit_code=0, stdout="Prepared files: " + ", ".join(changed))

    def _run_atomic_worktree_transaction(self, project: Path, transaction_name: str, timeout_seconds: int, check_only: bool, mutate_worktree) -> CommandResult:
        if not (project / ".git").exists():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Atomic transactions require a git repository")
        env = self._git_env()
        temp_root = Path(tempfile.mkdtemp(prefix="repo_atomic_"))
        worktree = temp_root / "worktree"
        branch = f"atomic-edit-{uuid4().hex[:12]}"
        steps: list[CommandResult] = []

        def combined(exit_code: int, stdout: str = "", stderr: str = "", timed_out: bool = False) -> CommandResult:
            step_text = []
            for step in steps:
                label = " ".join(step.command)
                if step.exit_code != 0 or step.stdout or step.stderr:
                    step_text.append(f"$ {label}\nexit={step.exit_code}\nstdout={step.stdout}\nstderr={step.stderr}".strip())
            if stdout:
                step_text.append(stdout)
            return CommandResult(
                command=["repo", transaction_name],
                cwd=str(project),
                exit_code=exit_code,
                stdout="\n\n".join(step_text)[-20000:],
                stderr=stderr[-20000:],
                timed_out=timed_out,
            )

        try:
            add = self._run(["git", "worktree", "add", "-b", branch, str(worktree), "HEAD"], project, timeout_seconds, env=env)
            steps.append(add)
            if add.exit_code != 0:
                return combined(add.exit_code, stderr=add.stderr or add.stdout, timed_out=add.timed_out)

            mutate = mutate_worktree(worktree)
            steps.append(mutate)
            if mutate.exit_code != 0:
                return combined(mutate.exit_code, stderr=mutate.stderr or mutate.stdout, timed_out=mutate.timed_out)

            # Make new files visible in git diff without staging content.
            add_intent = self._run(["git", "add", "-N", "--", "."], worktree, timeout_seconds, env=env)
            steps.append(add_intent)
            if add_intent.exit_code != 0:
                return combined(add_intent.exit_code, stderr=add_intent.stderr or add_intent.stdout, timed_out=add_intent.timed_out)

            diff = self._run(["git", "diff", "--binary", "--no-ext-diff"], worktree, timeout_seconds, env=env)
            steps.append(diff)
            if diff.exit_code != 0:
                return combined(diff.exit_code, stderr=diff.stderr or diff.stdout, timed_out=diff.timed_out)
            if not diff.stdout.strip():
                return combined(0, stdout="No changes produced by atomic transaction.")

            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as handle:
                handle.write(diff.stdout)
                final_patch = Path(handle.name)
            try:
                final_check = self._run(["git", "apply", "--check", str(final_patch)], project, timeout_seconds, env=env)
                steps.append(final_check)
                if final_check.exit_code != 0 or check_only:
                    return combined(final_check.exit_code, stdout="Atomic check-only completed; real checkout unchanged." if check_only and final_check.exit_code == 0 else "", stderr=final_check.stderr or final_check.stdout, timed_out=final_check.timed_out)

                with RepoLockService().locked(str(project), owner=transaction_name, timeout_seconds=timeout_seconds):
                    final_apply = self._run(["git", "apply", str(final_patch)], project, timeout_seconds, env=env)
                steps.append(final_apply)
                return combined(final_apply.exit_code, stdout="Atomic transaction applied to real checkout." if final_apply.exit_code == 0 else "", stderr=final_apply.stderr or final_apply.stdout, timed_out=final_apply.timed_out)
            finally:
                final_patch.unlink(missing_ok=True)
        finally:
            remove = self._run(["git", "worktree", "remove", "--force", str(worktree)], project, timeout_seconds, env=env) if worktree.exists() else None
            if remove:
                steps.append(remove)
            delete_branch = self._run(["git", "branch", "-D", branch], project, timeout_seconds, env=env)
            # Do not fail a successful transaction only because cleanup sees no branch.
            if delete_branch.exit_code != 0 and "not found" not in (delete_branch.stderr + delete_branch.stdout).lower():
                steps.append(delete_branch)
            shutil.rmtree(temp_root, ignore_errors=True)

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

    def _remote_matches(self, configured: str, expected: str) -> bool:
        if configured == expected:
            return True
        return self._repo_name_from_url(configured) == self._repo_name_from_url(expected)

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
            return CommandResult(
                command=[mask_repo_url(part) for part in command],
                cwd=str(cwd),
                exit_code=completed.returncode,
                stdout=mask_repo_url(completed.stdout[-20000:]),
                stderr=mask_repo_url(completed.stderr[-20000:]),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "")[-20000:] if isinstance(exc.stderr, str) else ""
            return CommandResult(
                command=[mask_repo_url(part) for part in command],
                cwd=str(cwd),
                exit_code=124,
                stdout=mask_repo_url(stdout),
                stderr=mask_repo_url(stderr),
                timed_out=True,
            )
