from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from threading import Lock
from typing import Any
from uuid import uuid4

try:  # pragma: no cover - platform-specific import
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt

from fastapi import HTTPException, status

from ..config import get_odoo_clients, settings
from ..schemas import ApprovalRecord, ApprovalStatus, AuditEvent, JobRecord, JobStatus, JobStep


class JsonStore:
    """Small JSON-file state store for approvals, jobs, and audit events.

    This keeps the first production-ready version dependency-light. Replace with
    PostgreSQL when multiple workers/servers are used.
    """

    _lock = Lock()  # in-process thread safety
    _read_cache: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}

    @contextmanager
    def _file_lock(self, path: Path):
        """Cross-process file lock to prevent JSON corruption."""
        lock_path = path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lf:
            try:
                if fcntl is not None:
                    fcntl.flock(lf, fcntl.LOCK_EX)
                else:
                    msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lf, fcntl.LOCK_UN)
                else:
                    lf.seek(0)
                    msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)

    def __init__(self) -> None:
        self.root = settings.state_path
        self.approvals_file = self.root / "approvals.json"
        self.jobs_file = self.root / "jobs.json"
        self.audit_file = self.root / "audit.json"
        self.artifacts_dir = self.root / "artifacts"

    def _read_list(self, path: Path) -> list[dict[str, Any]]:
        with self._lock:
            if not path.exists():
                self._read_cache.pop(str(path), None)
                return []
            try:
                stat = path.stat()
                cache_key = str(path)
                cached = self._read_cache.get(cache_key)
                if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
                    return [dict(row) for row in cached[2]]
                raw = json.loads(path.read_text(encoding="utf-8") or "[]")
            except json.JSONDecodeError:
                raw = []
            rows = raw if isinstance(raw, list) else []
            try:
                stat = path.stat()
                self._read_cache[str(path)] = (stat.st_mtime, stat.st_size, [dict(row) for row in rows])
            except OSError:
                pass
            return [dict(row) for row in rows]

    def _write_list(self, path: Path, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(rows, indent=2, sort_keys=True)
            with self._file_lock(path):
                tmp_path = path.with_suffix(path.suffix + ".tmp")
                tmp_path.write_text(payload, encoding="utf-8")
                tmp_path.replace(path)
            try:
                stat = path.stat()
                self._read_cache[str(path)] = (stat.st_mtime, stat.st_size, [dict(row) for row in rows])
            except OSError:
                self._read_cache.pop(str(path), None)

    def _update_list(self, path: Path, update) -> Any:
        """Atomically read, modify, and replace one JSON list."""
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._file_lock(path):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8") or "[]") if path.exists() else []
                except json.JSONDecodeError:
                    raw = []
                rows = raw if isinstance(raw, list) else []
                result = update(rows)
                payload = json.dumps(rows, indent=2, sort_keys=True)
                tmp_path = path.with_suffix(path.suffix + ".tmp")
                tmp_path.write_text(payload, encoding="utf-8")
                tmp_path.replace(path)
            stat = path.stat()
            self._read_cache[str(path)] = (stat.st_mtime, stat.st_size, [dict(row) for row in rows])
            return result

    def create_approval(self, record: ApprovalRecord) -> ApprovalRecord:
        self._update_list(self.approvals_file, lambda rows: rows.append(record.model_dump(mode="json")))
        self.log_audit("approval.request", "pending", record.summary, {"approval_id": record.id, "action": record.action})
        return record

    def list_approvals(self, limit: int = 100) -> list[ApprovalRecord]:
        rows = self._read_list(self.approvals_file)
        return [ApprovalRecord.model_validate(row) for row in rows[-limit:]][::-1]

    def get_approval(self, approval_id: str) -> ApprovalRecord:
        for row in self._read_list(self.approvals_file):
            if row.get("id") == approval_id:
                return ApprovalRecord.model_validate(row)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")

    def set_approval_status(self, approval_id: str, target: ApprovalStatus, reason: str | None = None) -> ApprovalRecord:
        def update(rows):
            for index, row in enumerate(rows):
                if row.get("id") == approval_id:
                    record = ApprovalRecord.model_validate(row)
                    record.status = target
                    record.decided_at = datetime.now(timezone.utc).isoformat()
                    record.reason = reason
                    rows[index] = record.model_dump(mode="json")
                    return record
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")

        record = self._update_list(self.approvals_file, update)
        self.log_audit("approval." + target.value, target.value, record.summary, {"approval_id": record.id, "action": record.action})
        return record

    def create_job(self, job: JobRecord) -> JobRecord:
        self._update_list(self.jobs_file, lambda rows: rows.append(job.model_dump(mode="json")))
        self.log_audit("job.create", job.status.value, job.job_type, {"job_id": job.id})
        return job

    def list_jobs(self, limit: int = 100) -> list[JobRecord]:
        rows = self._read_list(self.jobs_file)
        return [JobRecord.model_validate(row) for row in rows[-limit:]][::-1]

    def get_job(self, job_id: str) -> JobRecord:
        for row in self._read_list(self.jobs_file):
            if row.get("id") == job_id:
                return JobRecord.model_validate(row)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    def wait_job(self, job_id: str, timeout_seconds: int = 600, poll_interval_seconds: float = 1.0) -> JobRecord:
        """Wait for a job to finish without requiring repeated client/tool calls.

        Safe deploy must not be treated as complete until its owning job reaches
        a terminal status. Use short positive waits for MCP/browser clients so
        pages stay responsive. A timeout value of 0 means wait indefinitely and
        should be reserved for CLI/internal callers.
        """
        deadline = None if timeout_seconds <= 0 else time.monotonic() + max(timeout_seconds, 0)
        interval = max(min(poll_interval_seconds, 10.0), 0.25)
        while True:
            job = self.get_job(job_id)
            if job.status not in {JobStatus.queued, JobStatus.running}:
                return job
            if deadline is not None and time.monotonic() >= deadline:
                return job
            if deadline is None:
                time.sleep(interval)
            else:
                time.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))

    def update_job(self, job: JobRecord) -> JobRecord:
        def update(rows):
            for index, row in enumerate(rows):
                if row.get("id") == job.id:
                    rows[index] = job.model_dump(mode="json")
                    return
            rows.append(job.model_dump(mode="json"))

        self._update_list(self.jobs_file, update)
        self.log_audit("job.update", job.status.value, job.job_type, {"job_id": job.id})
        return job

    def add_step(self, job: JobRecord, step: JobStep) -> JobRecord:
        step.finished_at = step.finished_at or datetime.now(timezone.utc).isoformat()
        job.steps.append(step)
        return self.update_job(job)

    def finish_job(self, job: JobRecord, status_: JobStatus, result: Any = None) -> JobRecord:
        job.status = status_
        job.finished_at = datetime.now(timezone.utc).isoformat()
        job.result = result
        return self.update_job(job)

    def log_audit(self, action: str, status_: str, summary: str = "", metadata: dict[str, Any] | None = None) -> AuditEvent:
        event = AuditEvent(action=action, status=status_, summary=summary, metadata=metadata or {})
        def update(rows):
            rows.append(event.model_dump(mode="json"))
            if len(rows) > 2000:
                del rows[:-2000]

        self._update_list(self.audit_file, update)
        return event

    def list_audit(self, limit: int = 200) -> list[AuditEvent]:
        rows = self._read_list(self.audit_file)
        return [AuditEvent.model_validate(row) for row in rows[-limit:]][::-1]

    def create_artifact(self, kind: str, source_tool: str, payload: Any) -> dict[str, Any]:
        artifact_id = f"art_{uuid4().hex}"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
        record = {
            "artifact_id": artifact_id,
            "kind": kind,
            "source_tool": source_tool,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "bytes": len(text.encode("utf-8")),
            "text": text,
        }
        path = self.artifacts_dir / f"{artifact_id}.json"
        with self._file_lock(path):
            path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        self.log_audit("artifact.create", "ok", source_tool, {"artifact_id": artifact_id, "bytes": record["bytes"]})
        return {key: record[key] for key in ("artifact_id", "kind", "source_tool", "created_at", "bytes")}

    def read_artifact(self, artifact_id: str, offset: int = 0, max_chars: int = 12000) -> dict[str, Any]:
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", artifact_id)
        if safe_id != artifact_id or not safe_id.startswith("art_"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid artifact_id")
        path = self.artifacts_dir / f"{safe_id}.json"
        if not path.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Artifact is unreadable") from exc
        text = str(record.get("text", ""))
        start = min(max(offset, 0), len(text))
        end = min(start + max(max_chars, 1), len(text))
        return {
            "artifact_id": safe_id,
            "kind": record.get("kind"),
            "source_tool": record.get("source_tool"),
            "created_at": record.get("created_at"),
            "bytes": record.get("bytes", len(text.encode("utf-8"))),
            "offset": start,
            "returned_chars": end - start,
            "next_offset": end if end < len(text) else None,
            "text": text[start:end],
        }

    # ---------------------------------------------------------------------------
    # Fix 7: Session state persistence — survives Render/Koyeb restarts.
    # The agent can save and reload: last_sha, module_name, step_history, and
    # any arbitrary context across sessions.
    #
    # Phase 3: multi-client isolation. The legacy unsuffixed session_state.json
    # keeps being written exactly as before (so session.load_state() with no
    # args behaves identically to today — nothing breaks for the no-client or
    # single-client case). When a client_name is known (passed explicitly, or
    # present inside the saved state dict — workflow.start_session already
    # includes it), an additional per-client file is written/read so two
    # different clients' sessions never silently bleed into each other.
    # ---------------------------------------------------------------------------

    @property
    def _session_file(self) -> Path:
        return self.root / "session_state.json"

    def _session_file_for_client(self, client_name: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", client_name)
        return self.root / f"session_state__{safe}.json"

    def save_session_state(self, state: dict, client_name: str | None = None) -> dict:
        """Persist arbitrary session state (sha, module, step history, etc.).

        Called by workflow.start_session after success and by safe_deploy on
        each milestone so the agent can resume without repeating step 1.
        state dict is shallow-merged into the existing persisted state.

        In multi-client mode, writes only the per-client file when a client is
        known. The legacy global file is used only for no-client/single-client
        fallback state so one client's deploy state cannot bleed into another.
        """
        effective_client = client_name or state.get("client_name") or None
        from datetime import datetime, timezone

        def _write(path: Path) -> dict:
            with self._lock:
                with self._file_lock(path):
                    current: dict = {}
                    if path.exists():
                        try:
                            raw = json.loads(path.read_text(encoding="utf-8"))
                            current = raw if isinstance(raw, dict) else {}
                        except (OSError, json.JSONDecodeError):
                            current = {}
                    current.update(state)
                    current["updated_at"] = datetime.now(timezone.utc).isoformat()
                    tmp_path = path.with_suffix(path.suffix + ".tmp")
                    tmp_path.write_text(json.dumps(current, indent=2), encoding="utf-8")
                    tmp_path.replace(path)
                    return current

        if effective_client:
            return _write(self._session_file_for_client(effective_client))
        return _write(self._session_file)

    def load_session_state(self, client_name: str | None = None) -> dict:
        """Return last persisted session state, or empty dict if none.

        With no client_name, reads the legacy global file (same as before
        this feature existed). With client_name, reads that client's
        isolated state file — recommended once you have more than one client
        configured, so a resumed session can never pick up another client's
        project_path/sha by accident.
        """
        if not client_name and len(get_odoo_clients()) > 1:
            return {
                "requires_client_name": True,
                "message": "Multiple ODOO_CLIENTS entries are configured; pass client_name to load isolated session state.",
            }
        path = self._session_file_for_client(client_name) if client_name else self._session_file
        if not path.exists():
            return {}
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return self._with_validated_project_path(state if isinstance(state, dict) else {})

    def _with_validated_project_path(self, state: dict) -> dict:
        project_path = state.get("project_path")
        if not project_path:
            return state

        checked = dict(state)
        try:
            resolved = Path(str(project_path)).expanduser().resolve()
        except OSError as exc:
            checked["project_path"] = None
            checked["project_path_valid"] = False
            checked["stale_project_path"] = str(project_path)
            checked["project_path_error"] = f"Could not resolve project_path: {exc}"
            return checked

        allowed_roots = settings.allowed_roots
        if not allowed_roots or not any(resolved == root or root in resolved.parents for root in allowed_roots):
            checked["project_path"] = None
            checked["project_path_valid"] = False
            checked["stale_project_path"] = str(project_path)
            checked["project_path_error"] = f"Project path is outside allowed roots: {resolved}"
            return checked

        if not resolved.exists() or not resolved.is_dir():
            checked["project_path"] = None
            checked["project_path_valid"] = False
            checked["stale_project_path"] = str(project_path)
            checked["project_path_error"] = f"Project path is missing or not a directory: {resolved}"
            return checked

        checked["project_path"] = str(resolved)
        checked["project_path_valid"] = True
        checked.pop("stale_project_path", None)
        checked.pop("project_path_error", None)
        return checked

    def clear_session_state(self, client_name: str | None = None) -> None:
        """Wipe the session state file (e.g. after a successful full deploy)."""
        path = self._session_file_for_client(client_name) if client_name else self._session_file
        path.unlink(missing_ok=True)
