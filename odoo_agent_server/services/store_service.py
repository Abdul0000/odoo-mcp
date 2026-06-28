from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
import fcntl
from contextlib import contextmanager
from threading import Lock
from typing import Any

from fastapi import HTTPException, status

from ..config import settings
from ..schemas import ApprovalRecord, ApprovalStatus, AuditEvent, JobRecord, JobStatus, JobStep


class JsonStore:
    """Small JSON-file state store for approvals, jobs, and audit events.

    This keeps the first production-ready version dependency-light. Replace with
    PostgreSQL when multiple workers/servers are used.
    """

    _lock = Lock()  # in-process thread safety

    @contextmanager
    def _file_lock(self, path: Path):
        """Cross-process file lock using fcntl to prevent JSON corruption."""
        lock_path = path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def __init__(self) -> None:
        self.root = settings.state_path
        self.approvals_file = self.root / "approvals.json"
        self.jobs_file = self.root / "jobs.json"
        self.audit_file = self.root / "audit.json"

    def _read_list(self, path: Path) -> list[dict[str, Any]]:
        # file_lock is acquired by callers via _write_list for write ops
        with self._lock:
            if not path.exists():
                return []
            try:
                raw = json.loads(path.read_text(encoding="utf-8") or "[]")
            except json.JSONDecodeError:
                raw = []
            return raw if isinstance(raw, list) else []

    def _write_list(self, path: Path, rows: list[dict[str, Any]]) -> None:
        # Always use _file_lock context when calling _write_list
        # e.g.: with self._file_lock(path): self._write_list(path, rows)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")

    def create_approval(self, record: ApprovalRecord) -> ApprovalRecord:
        rows = self._read_list(self.approvals_file)
        rows.append(record.model_dump(mode="json"))
        self._write_list(self.approvals_file, rows)
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
        rows = self._read_list(self.approvals_file)
        for index, row in enumerate(rows):
            if row.get("id") == approval_id:
                record = ApprovalRecord.model_validate(row)
                record.status = target
                record.decided_at = datetime.now(timezone.utc).isoformat()
                record.reason = reason
                rows[index] = record.model_dump(mode="json")
                self._write_list(self.approvals_file, rows)
                self.log_audit("approval." + target.value, target.value, record.summary, {"approval_id": record.id, "action": record.action})
                return record
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")

    def create_job(self, job: JobRecord) -> JobRecord:
        rows = self._read_list(self.jobs_file)
        rows.append(job.model_dump(mode="json"))
        self._write_list(self.jobs_file, rows)
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

    def wait_job(self, job_id: str, timeout_seconds: int = 20, poll_interval_seconds: float = 2.0) -> JobRecord:
        """Wait for a job to finish without requiring repeated client/tool calls.

        The default timeout is intentionally below common Render HTTP timeouts so
        callers get either the final result or a clean still-running response
        instead of a broken message stream.
        """
        deadline = time.monotonic() + max(timeout_seconds, 0)
        interval = max(min(poll_interval_seconds, 10.0), 0.25)
        while True:
            job = self.get_job(job_id)
            if job.status not in {JobStatus.queued, JobStatus.running}:
                return job
            if timeout_seconds <= 0 or time.monotonic() >= deadline:
                return job
            time.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))

    def update_job(self, job: JobRecord) -> JobRecord:
        rows = self._read_list(self.jobs_file)
        for index, row in enumerate(rows):
            if row.get("id") == job.id:
                rows[index] = job.model_dump(mode="json")
                self._write_list(self.jobs_file, rows)
                self.log_audit("job.update", job.status.value, job.job_type, {"job_id": job.id})
                return job
        return self.create_job(job)

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
        rows = self._read_list(self.audit_file)
        rows.append(event.model_dump(mode="json"))
        self._write_list(self.audit_file, rows[-2000:])
        return event

    def list_audit(self, limit: int = 200) -> list[AuditEvent]:
        rows = self._read_list(self.audit_file)
        return [AuditEvent.model_validate(row) for row in rows[-limit:]][::-1]

    # ---------------------------------------------------------------------------
    # Fix 7: Session state persistence — survives Render/Koyeb restarts.
    # The agent can save and reload: last_sha, module_name, step_history, and
    # any arbitrary context across sessions.
    # ---------------------------------------------------------------------------

    @property
    def _session_file(self) -> Path:
        return self.root / "session_state.json"

    def save_session_state(self, state: dict) -> dict:
        """Persist arbitrary session state (sha, module, step history, etc.).

        Called by workflow.start_session after success and by safe_deploy on
        each milestone so the agent can resume without repeating step 1.
        state dict is shallow-merged into the existing persisted state.
        """
        current: dict = {}
        if self._session_file.exists():
            try:
                current = json.loads(self._session_file.read_text(encoding="utf-8"))
            except Exception:
                current = {}
        from datetime import datetime, timezone
        current.update(state)
        current["updated_at"] = datetime.now(timezone.utc).isoformat()
        with self._file_lock(self._session_file):
            self._session_file.write_text(json.dumps(current, indent=2), encoding="utf-8")
        return current

    def load_session_state(self) -> dict:
        """Return last persisted session state, or empty dict if none."""
        if not self._session_file.exists():
            return {}
        try:
            return json.loads(self._session_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def clear_session_state(self) -> None:
        """Wipe the session state file (e.g. after a successful full deploy)."""
        self._session_file.unlink(missing_ok=True)
