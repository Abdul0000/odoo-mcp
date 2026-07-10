from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterator
from uuid import uuid4

try:  # pragma: no cover - platform-specific import
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt

from fastapi import HTTPException, status

from ..config import get_odoo_clients, settings
from ..schemas import ApprovalRecord, ApprovalStatus, AuditEvent, JobRecord, JobStatus, JobStep


_RUNTIME_ID = uuid4().hex
_TERMINAL_JOB_STATUSES = {JobStatus.passed.value, JobStatus.failed.value, JobStatus.warning.value}
_ACTIVE_JOB_STATUSES = {JobStatus.queued.value, JobStatus.running.value}


class StateStoreCorruptionError(RuntimeError):
    """Raised when legacy/persistent state is malformed and cannot be trusted."""


class JsonStore:
    """Dependency-light transactional state store backed by SQLite.

    The class name is retained for API compatibility, but approvals, jobs, audit
    events, and deploy leases are stored transactionally in SQLite WAL mode. This
    avoids lost updates across threads/processes while remaining suitable for a
    0.1 CPU / 512 MB Render instance.
    """

    _lock = Lock()
    _init_lock = Lock()
    _initialized_paths: set[str] = set()
    # Compatibility attribute used by older tests/clients. SQLite does not use it.
    _read_cache: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}

    def __init__(self) -> None:
        self.root = settings.state_path
        self.approvals_file = self.root / "approvals.json"  # legacy migration input
        self.jobs_file = self.root / "jobs.json"  # legacy migration input
        self.audit_file = self.root / "audit.json"  # legacy migration input
        self.db_file = self.root / "state.sqlite3"
        self.artifacts_dir = self.root / "artifacts"
        self._initialize()

    # ------------------------------------------------------------------
    # SQLite primitives
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_file,
            timeout=max(settings.sqlite_busy_timeout_ms, 100) / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={max(settings.sqlite_busy_timeout_ms, 100)}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        key = str(self.db_file)
        with self._init_lock:
            if key in self._initialized_paths and self.db_file.exists():
                return
            self.root.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            try:
                # WAL gives safe cross-process reads with one serialized writer and
                # uses little memory. NORMAL sync is appropriate for ephemeral/small
                # containers while SQLite transactions still remain atomic.
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA temp_store=FILE")
                conn.execute("PRAGMA cache_size=-2000")  # about 2 MB per connection
                conn.execute("PRAGMA wal_autocheckpoint=100")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS approvals (
                        id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        job_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        finished_at TEXT,
                        runtime_id TEXT,
                        heartbeat_at REAL NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                        ON jobs(status, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_jobs_type_status
                        ON jobs(job_type, status);
                    CREATE TABLE IF NOT EXISTS audit (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        action TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS deploy_leases (
                        lease_key TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL UNIQUE,
                        runtime_id TEXT NOT NULL,
                        heartbeat_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_deploy_leases_expiry
                        ON deploy_leases(expires_at);
                    """
                )
            finally:
                conn.close()
            self._migrate_legacy_json_if_needed()
            self._initialized_paths.add(key)

    def _quarantine_corrupt(self, path: Path, exc: Exception) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantine = path.with_name(f"{path.name}.corrupt.{stamp}")
        try:
            path.replace(quarantine)
        except OSError:
            quarantine = path
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_type": "state_store_corruption",
                "message": f"Persistent state is corrupt and destructive actions are blocked: {path}",
                "quarantined_path": str(quarantine),
                "cause": str(exc),
            },
        ) from exc

    def _legacy_rows(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "[]")
        except (json.JSONDecodeError, OSError) as exc:
            return self._quarantine_corrupt(path, exc)  # type: ignore[return-value]
        if not isinstance(raw, list) or not all(isinstance(row, dict) for row in raw):
            return self._quarantine_corrupt(path, ValueError("Expected a JSON list of objects"))  # type: ignore[return-value]
        return raw

    def _migrate_legacy_json_if_needed(self) -> None:
        with self._transaction() as conn:
            counts = {
                "approvals": conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0],
                "jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                "audit": conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
            }
            approvals = self._legacy_rows(self.approvals_file) if counts["approvals"] == 0 else []
            jobs = self._legacy_rows(self.jobs_file) if counts["jobs"] == 0 else []
            audit = self._legacy_rows(self.audit_file) if counts["audit"] == 0 else []
            now = time.time()
            for row in approvals:
                record = ApprovalRecord.model_validate(row)
                conn.execute(
                    "INSERT OR IGNORE INTO approvals(id, created_at, payload) VALUES(?, ?, ?)",
                    (record.id, record.created_at, json.dumps(record.model_dump(mode="json"), ensure_ascii=False)),
                )
            for row in jobs:
                record = JobRecord.model_validate(row)
                conn.execute(
                    """INSERT OR IGNORE INTO jobs
                       (id, job_type, status, created_at, updated_at, finished_at, runtime_id, heartbeat_at, payload)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record.id,
                        record.job_type,
                        record.status.value,
                        record.created_at,
                        record.finished_at or record.created_at,
                        record.finished_at,
                        "legacy",
                        now,
                        json.dumps(record.model_dump(mode="json"), ensure_ascii=False),
                    ),
                )
            for row in audit:
                record = AuditEvent.model_validate(row)
                conn.execute(
                    "INSERT INTO audit(created_at, action, status, payload) VALUES(?, ?, ?, ?)",
                    (record.created_at, record.action, record.status, json.dumps(record.model_dump(mode="json"), ensure_ascii=False)),
                )

    # Compatibility helpers used by older code/tests.
    def _read_list(self, path: Path) -> list[dict[str, Any]]:
        if path.name == self.approvals_file.name:
            return [item.model_dump(mode="json") for item in reversed(self.list_approvals(limit=2000))]
        if path.name == self.jobs_file.name:
            return [item.model_dump(mode="json") for item in reversed(self.list_jobs(limit=2000))]
        if path.name == self.audit_file.name:
            return [item.model_dump(mode="json") for item in reversed(self.list_audit(limit=2000))]
        return self._legacy_rows(path)

    def _write_list(self, path: Path, rows: list[dict[str, Any]]) -> None:
        # Deliberately limited to legacy compatibility; normal runtime code uses
        # transactional record methods below.
        if path.name == self.jobs_file.name:
            with self._transaction() as conn:
                conn.execute("DELETE FROM deploy_leases")
                conn.execute("DELETE FROM jobs")
                for row in rows:
                    record = JobRecord.model_validate(row)
                    self._insert_job(conn, record)
            return
        payload = json.dumps(rows, indent=2, sort_keys=True)
        with self._file_lock(path):
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(path)

    # ------------------------------------------------------------------
    # Approvals
    # ------------------------------------------------------------------

    def create_approval(self, record: ApprovalRecord) -> ApprovalRecord:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO approvals(id, created_at, payload) VALUES(?, ?, ?)",
                (record.id, record.created_at, json.dumps(record.model_dump(mode="json"), ensure_ascii=False)),
            )
        self.log_audit("approval.request", "pending", record.summary, {"approval_id": record.id, "action": record.action})
        return record

    def list_approvals(self, limit: int = 100) -> list[ApprovalRecord]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT payload FROM approvals ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
            return [ApprovalRecord.model_validate(json.loads(row["payload"])) for row in rows]
        finally:
            conn.close()

    def get_approval(self, approval_id: str) -> ApprovalRecord:
        conn = self._connect()
        try:
            row = conn.execute("SELECT payload FROM approvals WHERE id=?", (approval_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")
        return ApprovalRecord.model_validate(json.loads(row["payload"]))

    def set_approval_status(self, approval_id: str, target: ApprovalStatus, reason: str | None = None) -> ApprovalRecord:
        with self._transaction() as conn:
            row = conn.execute("SELECT payload FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")
            record = ApprovalRecord.model_validate(json.loads(row["payload"]))
            record.status = target
            record.decided_at = datetime.now(timezone.utc).isoformat()
            record.reason = reason
            conn.execute("UPDATE approvals SET payload=? WHERE id=?", (json.dumps(record.model_dump(mode="json"), ensure_ascii=False), approval_id))
        self.log_audit("approval." + target.value, target.value, record.summary, {"approval_id": record.id, "action": record.action})
        return record

    # ------------------------------------------------------------------
    # Jobs and deploy leases
    # ------------------------------------------------------------------

    def _insert_job(self, conn: sqlite3.Connection, job: JobRecord) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        now = time.time()
        conn.execute(
            """INSERT INTO jobs
               (id, job_type, status, created_at, updated_at, finished_at, runtime_id, heartbeat_at, payload)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job.id,
                job.job_type,
                job.status.value,
                job.created_at,
                now_iso,
                job.finished_at,
                _RUNTIME_ID,
                now,
                json.dumps(job.model_dump(mode="json"), ensure_ascii=False),
            ),
        )

    def create_job(self, job: JobRecord) -> JobRecord:
        with self._transaction() as conn:
            self._insert_job(conn, job)
        self.log_audit("job.create", job.status.value, job.job_type, {"job_id": job.id})
        return job

    def list_jobs(self, limit: int = 100) -> list[JobRecord]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT payload FROM jobs ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
            return [JobRecord.model_validate(json.loads(row["payload"])) for row in rows]
        finally:
            conn.close()

    def get_job(self, job_id: str) -> JobRecord:
        conn = self._connect()
        try:
            row = conn.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        return JobRecord.model_validate(json.loads(row["payload"]))

    def wait_job(self, job_id: str, timeout_seconds: int = 600, poll_interval_seconds: float = 10.0) -> JobRecord:
        deadline = None if timeout_seconds <= 0 else time.monotonic() + max(timeout_seconds, 0)
        interval = max(min(poll_interval_seconds, 10.0), 0.25)
        while True:
            job = self.get_job(job_id)
            if job.status not in {JobStatus.queued, JobStatus.running}:
                return job
            if deadline is not None and time.monotonic() >= deadline:
                return job
            sleep_for = interval if deadline is None else min(interval, max(deadline - time.monotonic(), 0.0))
            time.sleep(max(sleep_for, 0.0))

    async def wait_job_async(self, job_id: str, timeout_seconds: int = 600, poll_interval_seconds: float = 10.0) -> JobRecord:
        """Nonblocking MCP wait; SQLite reads are tiny and sleeps yield the loop."""
        deadline = None if timeout_seconds <= 0 else time.monotonic() + max(timeout_seconds, 0)
        interval = max(min(poll_interval_seconds, 10.0), 0.25)
        while True:
            job = await asyncio.to_thread(self.get_job, job_id)
            if job.status not in {JobStatus.queued, JobStatus.running}:
                return job
            if deadline is not None and time.monotonic() >= deadline:
                return job
            sleep_for = interval if deadline is None else min(interval, max(deadline - time.monotonic(), 0.0))
            await asyncio.sleep(max(sleep_for, 0.0))

    @staticmethod
    def _step_key(step: JobStep) -> tuple[str, str | None, str | None]:
        return (step.name, step.started_at, step.finished_at)

    def _merge_job(self, current: JobRecord, incoming: JobRecord) -> JobRecord:
        existing_keys = {self._step_key(step) for step in incoming.steps}
        merged_steps = list(incoming.steps)
        for step in current.steps:
            if self._step_key(step) not in existing_keys:
                merged_steps.append(step)
        incoming.steps = merged_steps
        incoming.request = {**current.request, **incoming.request}
        return incoming

    def _update_job_row(self, conn: sqlite3.Connection, job: JobRecord) -> JobRecord:
        row = conn.execute("SELECT payload FROM jobs WHERE id=?", (job.id,)).fetchone()
        if row:
            current = JobRecord.model_validate(json.loads(row["payload"]))
            job = self._merge_job(current, job)
        else:
            self._insert_job(conn, job)
            return job
        now_iso = datetime.now(timezone.utc).isoformat()
        now = time.time()
        conn.execute(
            """UPDATE jobs SET job_type=?, status=?, updated_at=?, finished_at=?, runtime_id=?,
               heartbeat_at=?, payload=? WHERE id=?""",
            (
                job.job_type,
                job.status.value,
                now_iso,
                job.finished_at,
                _RUNTIME_ID,
                now,
                json.dumps(job.model_dump(mode="json"), ensure_ascii=False),
                job.id,
            ),
        )
        conn.execute(
            "UPDATE deploy_leases SET runtime_id=?, heartbeat_at=?, expires_at=? WHERE job_id=?",
            (_RUNTIME_ID, now, now + settings.safe_deploy_lease_seconds, job.id),
        )
        return job

    def update_job(self, job: JobRecord) -> JobRecord:
        with self._transaction() as conn:
            updated = self._update_job_row(conn, job)
        self.log_audit("job.update", updated.status.value, updated.job_type, {"job_id": updated.id})
        return updated

    def add_step(self, job: JobRecord, step: JobStep) -> JobRecord:
        step.finished_at = step.finished_at or datetime.now(timezone.utc).isoformat()
        with self._transaction() as conn:
            row = conn.execute("SELECT payload FROM jobs WHERE id=?", (job.id,)).fetchone()
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
            current = JobRecord.model_validate(json.loads(row["payload"]))
            if self._step_key(step) not in {self._step_key(item) for item in current.steps}:
                current.steps.append(step)
            updated = self._update_job_row(conn, current)
        return updated

    def finish_job(self, job: JobRecord, status_: JobStatus, result: Any = None) -> JobRecord:
        with self._transaction() as conn:
            row = conn.execute("SELECT payload FROM jobs WHERE id=?", (job.id,)).fetchone()
            current = JobRecord.model_validate(json.loads(row["payload"])) if row else job
            current.status = status_
            current.finished_at = datetime.now(timezone.utc).isoformat()
            current.result = result
            updated = self._update_job_row(conn, current)
            conn.execute("DELETE FROM deploy_leases WHERE job_id=?", (job.id,))
        self.log_audit("job.update", updated.status.value, updated.job_type, {"job_id": updated.id})
        return updated

    @staticmethod
    def deploy_lease_key(client_name: str | None, project_path: str, branch: str | None, module_name: str) -> str:
        return "|".join((client_name or "", str(Path(project_path).expanduser().resolve()), branch or "", module_name))

    def admit_safe_deploy(
        self,
        job: JobRecord,
        *,
        lease_key: str,
        force_new: bool = False,
    ) -> tuple[JobRecord, bool, str]:
        """Atomically admit one safe deploy or return the existing owner.

        The global capacity defaults to one on small Render instances. A same-key
        race always returns the existing job; a different active deploy receives
        HTTP 429 backpressure instead of an unbounded queued/running job.
        """
        now = time.time()
        with self._transaction() as conn:
            conn.execute("DELETE FROM deploy_leases WHERE expires_at <= ?", (now,))
            existing = conn.execute(
                """SELECT l.job_id, j.payload, j.status FROM deploy_leases l
                   JOIN jobs j ON j.id=l.job_id WHERE l.lease_key=?""",
                (lease_key,),
            ).fetchone()
            if existing and existing["status"] in _ACTIVE_JOB_STATUSES | {JobStatus.warning.value}:
                record = JobRecord.model_validate(json.loads(existing["payload"]))
                return record, False, "existing_owner"

            active = conn.execute(
                """SELECT l.job_id, j.payload FROM deploy_leases l
                   JOIN jobs j ON j.id=l.job_id
                   WHERE l.expires_at > ? AND j.status IN (?, ?)
                   ORDER BY l.heartbeat_at ASC""",
                (now, JobStatus.queued.value, JobStatus.running.value),
            ).fetchall()
            if len(active) >= settings.max_active_deploys:
                owner = JobRecord.model_validate(json.loads(active[0]["payload"]))
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "error_type": "deploy_capacity_full",
                        "message": f"This small instance allows {settings.max_active_deploys} active deploy(s). Poll the current job before starting another.",
                        "active_job_id": owner.id,
                        "next_call": {"tool": "jobs.get", "arguments": {"job_id": owner.id, "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 10.0}},
                    },
                )

            # force_new can replace terminal/resumable ownership but never an active
            # owner; active ownership was handled above.
            self._insert_job(conn, job)
            conn.execute(
                "INSERT OR REPLACE INTO deploy_leases(lease_key, job_id, runtime_id, heartbeat_at, expires_at) VALUES(?, ?, ?, ?, ?)",
                (lease_key, job.id, _RUNTIME_ID, now, now + settings.safe_deploy_lease_seconds),
            )
        self.log_audit("job.create", job.status.value, job.job_type, {"job_id": job.id, "lease_key": lease_key})
        return job, True, "admitted"

    def reconcile_interrupted_jobs(self, grace_seconds: int = 60) -> int:
        """Mark stale jobs from a previous process as resumable warnings.

        This is intentionally deterministic rather than pretending an in-memory
        callback survived a Render restart. Safe-deploy jobs point to resume_job.
        """
        cutoff = time.time() - max(grace_seconds, 1)
        changed = 0
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT id, payload FROM jobs WHERE status IN (?, ?) AND runtime_id != ? AND heartbeat_at < ?",
                (JobStatus.queued.value, JobStatus.running.value, _RUNTIME_ID, cutoff),
            ).fetchall()
            for row in rows:
                job = JobRecord.model_validate(json.loads(row["payload"]))
                job.status = JobStatus.warning
                job.finished_at = datetime.now(timezone.utc).isoformat()
                job.result = {
                    "status": "interrupted_restart",
                    "message": "The service restarted while this job was active. No success is claimed; resume the deterministic pipeline from the last recorded step.",
                    "job_id": job.id,
                    "next_call": {"tool": "workflow.resume_job", "arguments": {"job_id": job.id}},
                    "deploy_confirmed": False,
                    "success_allowed": False,
                }
                conn.execute(
                    "UPDATE jobs SET status=?, updated_at=?, finished_at=?, runtime_id=?, heartbeat_at=?, payload=? WHERE id=?",
                    (
                        JobStatus.warning.value,
                        job.finished_at,
                        job.finished_at,
                        _RUNTIME_ID,
                        time.time(),
                        json.dumps(job.model_dump(mode="json"), ensure_ascii=False),
                        job.id,
                    ),
                )
                conn.execute("DELETE FROM deploy_leases WHERE job_id=?", (job.id,))
                changed += 1
        return changed

    # ------------------------------------------------------------------
    # Audit and artifacts
    # ------------------------------------------------------------------

    def log_audit(self, action: str, status_: str, summary: str = "", metadata: dict[str, Any] | None = None) -> AuditEvent:
        event = AuditEvent(action=action, status=status_, summary=summary, metadata=metadata or {})
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO audit(created_at, action, status, payload) VALUES(?, ?, ?, ?)",
                (event.created_at, event.action, event.status, json.dumps(event.model_dump(mode="json"), ensure_ascii=False)),
            )
            conn.execute(
                "DELETE FROM audit WHERE seq NOT IN (SELECT seq FROM audit ORDER BY seq DESC LIMIT 2000)"
            )
        return event

    def list_audit(self, limit: int = 200) -> list[AuditEvent]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT payload FROM audit ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
            return [AuditEvent.model_validate(json.loads(row["payload"])) for row in rows]
        finally:
            conn.close()

    @contextmanager
    def _file_lock(self, path: Path):
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as lock_file:
            try:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_EX)
                else:  # pragma: no cover - Windows
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
                else:  # pragma: no cover - Windows
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)

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
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
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

    # ------------------------------------------------------------------
    # Session state (small JSON objects; atomic locked merge)
    # ------------------------------------------------------------------

    @property
    def _session_file(self) -> Path:
        return self.root / "session_state.json"

    def _session_file_for_client(self, client_name: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", client_name)
        return self.root / f"session_state__{safe}.json"

    def _read_session_locked(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError) as exc:
            return self._quarantine_corrupt(path, exc)  # type: ignore[return-value]
        if not isinstance(raw, dict):
            return self._quarantine_corrupt(path, ValueError("Expected a JSON object"))  # type: ignore[return-value]
        return raw

    def save_session_state(self, state: dict, client_name: str | None = None) -> dict:
        effective_client = client_name or state.get("client_name") or None
        path = self._session_file_for_client(effective_client) if effective_client else self._session_file
        with self._file_lock(path):
            current = self._read_session_locked(path)
            current.update(state)
            current["updated_at"] = datetime.now(timezone.utc).isoformat()
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            return current

    def load_session_state(self, client_name: str | None = None) -> dict:
        if not client_name and len(get_odoo_clients()) > 1:
            return {
                "requires_client_name": True,
                "message": "Multiple ODOO_CLIENTS entries are configured; pass client_name to load isolated session state.",
            }
        path = self._session_file_for_client(client_name) if client_name else self._session_file
        with self._file_lock(path):
            state = self._read_session_locked(path)
        return self._with_validated_project_path(state)

    def _with_validated_project_path(self, state: dict) -> dict:
        project_path = state.get("project_path")
        if not project_path:
            return state
        checked = dict(state)
        try:
            resolved = Path(str(project_path)).expanduser().resolve()
        except OSError as exc:
            checked.update(project_path=None, project_path_valid=False, stale_project_path=str(project_path), project_path_error=f"Could not resolve project_path: {exc}")
            return checked
        allowed_roots = settings.allowed_roots
        if not allowed_roots or not any(resolved == root or root in resolved.parents for root in allowed_roots):
            checked.update(project_path=None, project_path_valid=False, stale_project_path=str(project_path), project_path_error=f"Project path is outside allowed roots: {resolved}")
            return checked
        if not resolved.exists() or not resolved.is_dir():
            checked.update(project_path=None, project_path_valid=False, stale_project_path=str(project_path), project_path_error=f"Project path is missing or not a directory: {resolved}")
            return checked
        checked["project_path"] = str(resolved)
        checked["project_path_valid"] = True
        checked.pop("stale_project_path", None)
        checked.pop("project_path_error", None)
        return checked

    def clear_session_state(self, client_name: str | None = None) -> None:
        path = self._session_file_for_client(client_name) if client_name else self._session_file
        path.unlink(missing_ok=True)
