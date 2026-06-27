from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import HTTPException, status

from ..schemas import CommandResult, FileContent, FileListItem, RepoCleanPycResult
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
        target = self.target(project_path, relative_path)
        if create_dirs:
            target.parent.mkdir(parents=True, exist_ok=True)
        elif not target.parent.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Parent directory not found: {target.parent}")
        target.write_text(content, encoding="utf-8")
        return FileContent(path=target.relative_to(self.project(project_path)).as_posix(), content=content, size=target.stat().st_size)

    def delete_file(self, project_path: str, relative_path: str) -> dict[str, str]:
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

    def _run(self, command: list[str], cwd: Path, timeout_seconds: int) -> CommandResult:
        try:
            completed = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_seconds, check=False)
            return CommandResult(command=command, cwd=str(cwd), exit_code=completed.returncode, stdout=completed.stdout[-20000:], stderr=completed.stderr[-20000:])
        except subprocess.TimeoutExpired as exc:
            return CommandResult(command=command, cwd=str(cwd), exit_code=124, stdout=(exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else "", stderr=(exc.stderr or "")[-20000:] if isinstance(exc.stderr, str) else "", timed_out=True)
