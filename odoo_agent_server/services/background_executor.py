from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Literal, TypeVar

from ..config import settings


BackgroundWorkload = Literal["orchestration", "deploy"]
Callback = TypeVar("Callback", bound=Callable[[], Any])


# Keep workflow orchestration separate from slow SSH/Odoo module operations.
# A safe-deploy parent can synchronously wait for a nested module-deploy job;
# putting both on one bounded pool recreates starvation when the pool is busy.
_ORCHESTRATOR_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, settings.background_orchestrator_workers or settings.background_max_workers),
    thread_name_prefix="odoo_agent_orchestrator",
)
_DEPLOY_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, settings.background_deploy_workers or settings.background_max_workers),
    thread_name_prefix="odoo_agent_deploy",
)


def background_task(callback: Callback, workload: BackgroundWorkload) -> Callback:
    """Mark a callback for the correct bounded worker pool.

    The marker keeps ``submit_background(callback)`` backward compatible with
    existing tests and integrations that monkeypatch the one-argument submitter.
    """
    setattr(callback, "__odoo_agent_workload__", workload)
    return callback


def submit_background(callback: Callable[[], Any]) -> None:
    workload = getattr(callback, "__odoo_agent_workload__", "orchestration")
    executor = _DEPLOY_EXECUTOR if workload == "deploy" else _ORCHESTRATOR_EXECUTOR
    executor.submit(callback)
