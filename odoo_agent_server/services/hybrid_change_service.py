from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from ..config import get_client_names
from .git_service import GitService
from .store_service import JsonStore


class HybridChangeService:
    """Persist and validate provider-owned edits without exposing Git mutation tools."""

    STATE_KEY = "hybrid_changes"
    MAX_CHANGES_PER_CLIENT = 20

    def __init__(self, store: JsonStore | None = None) -> None:
        self.store = store or JsonStore()

    def save(self, change_id: str, payload: dict[str, Any], client_name: str | None = None) -> dict[str, Any]:
        state = self.store.load_session_state(client_name=client_name)
        changes = dict(state.get(self.STATE_KEY) or {})
        changes[change_id] = payload
        if len(changes) > self.MAX_CHANGES_PER_CLIENT:
            changes = dict(list(changes.items())[-self.MAX_CHANGES_PER_CLIENT :])
        self.store.save_session_state({self.STATE_KEY: changes}, client_name=client_name)
        return payload

    def load(self, change_id: str, client_name: str | None = None) -> tuple[dict[str, Any], str | None]:
        candidates: list[str | None] = [client_name] if client_name else []
        candidates.extend(name for name in get_client_names() if name not in candidates)
        candidates.append(None)
        for candidate in candidates:
            state = self.store.load_session_state(client_name=candidate)
            changes = state.get(self.STATE_KEY) if isinstance(state, dict) else None
            if isinstance(changes, dict) and isinstance(changes.get(change_id), dict):
                return dict(changes[change_id]), candidate
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown or expired change_id: {change_id}")

    def latest(self, client_name: str | None = None, project_path: str | None = None) -> dict[str, Any] | None:
        state = self.store.load_session_state(client_name=client_name)
        changes = state.get(self.STATE_KEY) if isinstance(state, dict) else None
        if not isinstance(changes, dict):
            return None
        candidates = [
            dict(value) for value in changes.values()
            if isinstance(value, dict) and not (value.get("deploy_terminal") is True and value.get("deploy_succeeded") is True)
        ]
        if project_path:
            expected = Path(project_path).resolve()
            candidates = [item for item in candidates if Path(str(item.get("project_path") or "")).resolve() == expected]
        if not candidates:
            return None
        return max(candidates, key=lambda item: str(item.get("patched_at") or item.get("prepared_at") or ""))

    def update(self, change_id: str, updates: dict[str, Any], client_name: str | None = None) -> dict[str, Any]:
        context, stored_client = self.load(change_id, client_name)
        context.update(updates)
        return self.save(change_id, context, client_name or stored_client)

    @staticmethod
    def checkout_identity(project_path: str) -> dict[str, str]:
        git = GitService()
        branch = git.current_branch(project_path)
        head = git.head_sha(project_path)
        if branch.exit_code != 0 or head.exit_code != 0:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Unable to read checkout branch/HEAD for prepared change")
        return {"branch": branch.stdout.strip(), "sha": head.stdout.strip()}

    @classmethod
    def validate_checkout_identity(cls, context: dict[str, Any], project_path: str) -> None:
        expected_branch = context.get("prepared_branch")
        expected_sha = context.get("prepared_sha")
        if not expected_branch and not expected_sha:  # backward-compatible contexts
            return
        current = cls.checkout_identity(project_path)
        if expected_branch and current["branch"] != expected_branch:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Checkout branch changed after prepare: expected {expected_branch}, found {current['branch']}",
            )
        if expected_sha and current["sha"] != expected_sha:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Checkout HEAD changed after prepare. Prepare the change again before patch or deploy.",
            )

    @classmethod
    def verification_fingerprint(cls, project_path: str, relative_paths: list[str], run_qa: bool) -> str:
        identity = cls.checkout_identity(project_path)
        diff_sha, _ = cls.diff_fingerprint(project_path)
        snapshot = cls.file_hashes(project_path, sorted(relative_paths), include_missing=True)
        payload = {"identity": identity, "diff_sha256": diff_sha, "files": snapshot, "run_qa": bool(run_qa)}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def cached_verification(self, change_id: str, fingerprint: str, client_name: str | None = None) -> dict[str, Any] | None:
        context, _ = self.load(change_id, client_name)
        cached = context.get("verification_cache")
        if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint and cached.get("passed") is True:
            return dict(cached)
        return None

    def store_verification(self, change_id: str, fingerprint: str, result: dict[str, Any], client_name: str | None = None) -> dict[str, Any]:
        return self.update(change_id, {"verification_cache": {"fingerprint": fingerprint, "passed": True, **result}}, client_name)

    @staticmethod
    def diff_fingerprint(project_path: str) -> tuple[str, str]:
        git = GitService()
        changed = git.changed_files(project_path, timeout_seconds=60)
        if changed.exit_code != 0:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Unable to enumerate current changes: {changed.stderr}")
        paths = sorted({line.strip().replace("\\", "/") for line in changed.stdout.splitlines() if line.strip()})
        snapshot = HybridChangeService.file_hashes(project_path, paths, include_missing=True)
        digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode("utf-8")).hexdigest()
        unstaged = git.diff(project_path, staged=False, timeout_seconds=60)
        staged = git.diff(project_path, staged=True, timeout_seconds=60)
        if unstaged.exit_code != 0 or staged.exit_code != 0:
            detail = unstaged.stderr or staged.stderr or "git diff failed"
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Unable to read current git diff: {detail}")
        preview = unstaged.stdout + staged.stdout
        untracked = [path for path in paths if snapshot.get(path) not in {"<deleted>", "<missing>"} and path not in preview]
        if untracked:
            preview += "\nUntracked/changed files:\n" + "\n".join(untracked) + "\n"
        return digest, preview

    @staticmethod
    def file_hashes(project_path: str, relative_paths: list[str], *, include_missing: bool = False) -> dict[str, str]:
        project = Path(project_path).resolve()
        result: dict[str, str] = {}
        for relative_path in relative_paths:
            path = (project / relative_path).resolve()
            try:
                path.relative_to(project)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Path escapes project: {relative_path}") from exc
            if path.is_file():
                result[relative_path] = hashlib.sha256(path.read_bytes()).hexdigest()
            elif include_missing:
                result[relative_path] = "<deleted>"
        return result

    def validate_for_deploy(
        self,
        *,
        change_id: str,
        client_name: str | None,
        project_path: str,
        module_name: str,
        expected_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        context, _ = self.load(change_id, client_name)
        if Path(str(context.get("project_path") or "")).resolve() != Path(project_path).resolve():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="change_id belongs to a different project_path")
        if str(context.get("module_name") or "") != module_name:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="change_id belongs to a different module")
        self.validate_checkout_identity(context, project_path)
        context_paths = sorted(str(path) for path in context.get("changed_files") or [])
        if expected_paths is not None and sorted(str(path) for path in expected_paths) != context_paths:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Deploy paths do not match the files recorded by repo.patch_change")
        current_hashes = self.file_hashes(project_path, context_paths, include_missing=True)
        if current_hashes != context.get("file_hashes"):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Patched file hashes changed after repo.patch_change")
        expected_diff = context.get("diff_sha256")
        if not expected_diff or not context.get("patched_at"):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="change_id has no completed patch to deploy")
        current_diff, _ = self.diff_fingerprint(project_path)
        if current_diff != expected_diff:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Checkout diff changed after repo.patch_change. Prepare or patch the change again before deploy.",
            )
        return context
