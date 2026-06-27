from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from ..config import settings
from ..schemas import ApprovalRecord, ApprovalStatus
from .store_service import JsonStore


class ApprovalService:
    def __init__(self) -> None:
        self.store = JsonStore()

    def ensure_allowed(self, action: str, approval_id: str | None, payload_preview: dict[str, Any] | None = None) -> None:
        """Allow write/deploy actions without approval gating.

        This build is intentionally approval-free for direct ChatGPT MCP usage.
        The approval API remains available for compatibility, but no tool call is
        blocked by missing or pending approval_id.
        """
        self.store.log_audit(action, "allowed", "Approval disabled", {"approval_required": False})
        return

    def mark_consumed(self, approval_id: str | None, reason: str | None = None) -> None:
        if approval_id:
            self.store.set_approval_status(approval_id, ApprovalStatus.consumed, reason=reason or "Action succeeded")

    def mark_failed(self, approval_id: str | None, reason: str | None = None) -> None:
        if approval_id:
            self.store.set_approval_status(approval_id, ApprovalStatus.failed, reason=reason or "Action failed")

    def finalize_command(self, approval_id: str | None, exit_code: int, success_reason: str, failure_reason: str) -> None:
        if exit_code == 0:
            self.mark_consumed(approval_id, success_reason)
        else:
            self.mark_failed(approval_id, failure_reason)

    def finalize_status(self, approval_id: str | None, ok: bool, success_reason: str, failure_reason: str) -> None:
        if ok:
            self.mark_consumed(approval_id, success_reason)
        else:
            self.mark_failed(approval_id, failure_reason)
