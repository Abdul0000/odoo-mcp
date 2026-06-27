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
        if record.status != ApprovalStatus.approved:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Approval is not approved: {record.status.value}")
        self.store.set_approval_status(approval_id, ApprovalStatus.consumed, reason="Consumed by action execution")
