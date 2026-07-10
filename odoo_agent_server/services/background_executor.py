from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import BoundedSemaphore, Lock
from typing import Any, Callable

from ..config import settings


class BackgroundQueueFull(RuntimeError):
    """Raised when the small-instance background queue has no free slot."""


_MAX_WORKERS = max(1, int(settings.background_workers))
_QUEUE_LIMIT = max(0, int(settings.background_queue_limit))
_CAPACITY = _MAX_WORKERS + _QUEUE_LIMIT
_EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="odoo_agent_job")
_SLOTS = BoundedSemaphore(_CAPACITY)
_STATE_LOCK = Lock()
_ACTIVE = 0
_QUEUED_OR_ACTIVE = 0


def _mark_started() -> None:
    global _ACTIVE
    with _STATE_LOCK:
        _ACTIVE += 1


def _mark_finished() -> None:
    global _ACTIVE, _QUEUED_OR_ACTIVE
    with _STATE_LOCK:
        _ACTIVE = max(0, _ACTIVE - 1)
        _QUEUED_OR_ACTIVE = max(0, _QUEUED_OR_ACTIVE - 1)
    _SLOTS.release()


def submit_background(callback: Callable[[], Any]) -> Future[Any]:
    """Submit one bounded background job.

    Free Render instances have only a fraction of a CPU and 512 MB RAM. The
    default therefore runs one long job at a time and queues at most one more.
    Admission fails immediately instead of accumulating an unbounded executor
    queue that would consume memory while reporting work as running.
    """
    global _QUEUED_OR_ACTIVE
    if not _SLOTS.acquire(blocking=False):
        raise BackgroundQueueFull(
            f"Background capacity is full ({_MAX_WORKERS} active worker, {_QUEUE_LIMIT} queued slots). "
            "Wait for an existing job to finish and retry."
        )
    with _STATE_LOCK:
        _QUEUED_OR_ACTIVE += 1

    def _run() -> Any:
        _mark_started()
        try:
            return callback()
        finally:
            _mark_finished()

    try:
        return _EXECUTOR.submit(_run)
    except Exception:
        with _STATE_LOCK:
            _QUEUED_OR_ACTIVE = max(0, _QUEUED_OR_ACTIVE - 1)
        _SLOTS.release()
        raise


def background_capacity() -> dict[str, int]:
    with _STATE_LOCK:
        active = _ACTIVE
        in_use = _QUEUED_OR_ACTIVE
    return {
        "workers": _MAX_WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "active": active,
        "queued": max(0, in_use - active),
        "capacity": _CAPACITY,
        "free_slots": max(0, _CAPACITY - in_use),
    }
