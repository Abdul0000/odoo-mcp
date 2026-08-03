import pytest

from odoo_agent_server.routers.tools import _verification_override_decision
from odoo_agent_server.schemas import OdooCodeChangeRequest, SyntaxVerifyResult, VerifyFinding


def _finding(code: str) -> VerifyFinding:
    return VerifyFinding(
        file="/tmp/custom/views.xml",
        severity="CRITICAL",
        check=code,
        message="static verifier finding",
        fix_hint="inspect source",
    )


def _verify(*codes: str) -> SyntaxVerifyResult:
    findings = [_finding(code) for code in codes]
    return SyntaxVerifyResult(
        push_blocked=bool(findings),
        status="blocked" if findings else "passed",
        findings=findings,
        critical_count=len(findings),
    )


def test_force_override_requires_exact_confirmation():
    with pytest.raises(ValueError, match="I AUTHORIZE FORCE PUSH AND DEPLOY"):
        OdooCodeChangeRequest(
            module_name="custom_sale",
            task_description="Deploy this verified change",
            verification_override="force",
            verification_override_reason="User explicitly accepts verifier risk.",
            verification_override_confirmation="force it",
        )


def test_irrelevant_only_accepts_known_static_ambiguities():
    payload = OdooCodeChangeRequest(
        module_name="custom_sale",
        task_description="Deploy this verified change",
        verification_override="irrelevant_only",
        verification_override_reason="Custom multi-model XML causes known static ambiguity.",
    )
    decision = _verification_override_decision(
        payload,
        _verify("view_field_not_in_core_model", "related_field_exists"),
    )
    assert decision["accepted"] is True
    assert decision["critical_count"] == 2


def test_irrelevant_only_rejects_xpath_or_other_material_findings():
    payload = OdooCodeChangeRequest(
        module_name="custom_sale",
        task_description="Deploy this verified change",
        verification_override="irrelevant_only",
        verification_override_reason="Attempt narrow override.",
    )
    decision = _verification_override_decision(payload, _verify("xpath_match_count"))
    assert decision["accepted"] is False
    assert decision["rejected_codes"] == ["xpath_match_count"]


def test_force_override_accepts_verifier_findings_with_audit_data():
    payload = OdooCodeChangeRequest(
        module_name="custom_sale",
        task_description="Deploy this verified change",
        verification_override="force",
        verification_override_reason="User explicitly requested force push and deploy after reviewing findings.",
        verification_override_confirmation="I AUTHORIZE FORCE PUSH AND DEPLOY",
    )
    decision = _verification_override_decision(payload, _verify("xpath_match_count"))
    assert decision["accepted"] is True
    assert decision["mode"] == "force"
    assert decision["reason"]
