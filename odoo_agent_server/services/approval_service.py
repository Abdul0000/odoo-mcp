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
        """Validate approval without consuming it before the action runs.

        The old implementation consumed approvals before command execution. If a
        command failed for an environmental reason, for example missing Git user
        identity, the same approval could not be retried. This method now marks
        approved actions as running. Call mark_consumed/mark_failed after the
        command result is known.
        """
        if not settings.require_approval or action not in settings.approval_required_actions:
            self.store.log_audit(action, "allowed", "Approval not required", {"approval_required": False})
            return
        if not approval_id:
            approval = self.store.create_approval(
                ApprovalRecord(
                    action=action,
                    summary=f"Approval required for {action}",
                    payload_preview=payload_preview or {},
                )
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "approval_required": True,
                    "approval_id": approval.id,
                    "action": action,
                    "message": f"Approval required before running {action}.",
                },
            )
        record = self.store.get_approval(approval_id)
        if record.action != action:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Approval action does not match requested action")
        if record.status not in {ApprovalStatus.approved, ApprovalStatus.running}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Approval is not approved: {record.status.value}")
        if record.status == ApprovalStatus.approved:
            self.store.set_approval_status(approval_id, ApprovalStatus.running, reason="Action execution started")

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
