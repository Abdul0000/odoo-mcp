from __future__ import annotations

import pytest
from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.services.approval_service import ApprovalService


def test_action_family_approval_rule_requires_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path))
    monkeypatch.setattr(settings, "require_approval", True)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "odoo.record_action.*")

    with pytest.raises(HTTPException) as exc:
        ApprovalService().ensure_allowed("odoo.record_action.sale.order.action_confirm", None, {"ids": [1]})

    assert exc.value.status_code == 409
    assert exc.value.detail["approval_required"] is True


def test_read_only_action_is_not_covered_by_mutation_approval_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path))
    monkeypatch.setattr(settings, "require_approval", True)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "git.push,odoo.record_action.*")

    ApprovalService().ensure_allowed("odoo.search_read", None, {"model": "res.partner"})
