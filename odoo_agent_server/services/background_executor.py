from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Any

from ..config import settings


# Bound long-running connector work so one busy flow cannot create unbounded
# daemon threads and make jobs.get/search_read sluggish. The low-resource
# production default is one; larger deployments can opt in to more.
_EXECUTOR = ThreadPoolExecutor(
    max_workers=settings.background_max_workers,
    thread_name_prefix="odoo_agent_job",
)


def submit_background(callback: Callable[[], Any]) -> None:
    _EXECUTOR.submit(callback)
