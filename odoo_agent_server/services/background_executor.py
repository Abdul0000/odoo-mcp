from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Any


# Bound long-running connector work so one busy flow cannot create unbounded
# daemon threads and make jobs.get/search_read sluggish. Four workers keeps
# normal agent deploy loops responsive without changing job semantics.
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="odoo_agent_job")


def submit_background(callback: Callable[[], Any]) -> None:
    _EXECUTOR.submit(callback)
