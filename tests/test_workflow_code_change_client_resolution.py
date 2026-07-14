import asyncio
from types import SimpleNamespace

from odoo_agent_server.routers import tools
from odoo_agent_server.schemas import IntentPlanRequest, OdooCodeChangeRequest, OdooCodeFileChange, WorkflowReport


def test_resolve_code_change_project_returns_resolved_client_name(monkeypatch, tmp_path):
    resolved = SimpleNamespace(name="Client A", path=str(tmp_path))
    monkeypatch.setattr(
        tools.ClientResolverService,
        "resolve_client",
        lambda self, **kwargs: resolved,
    )

    payload = OdooCodeChangeRequest(
        client_name="Client A",
        module_name="sample_frontend_module",
        task_description="Inspect themes",
    )

    client_name, project_path = tools._resolve_code_change_project(payload)

    assert client_name == "Client A"
    assert project_path == str(tmp_path)


def test_resolve_code_change_project_uses_explicit_path_without_client(monkeypatch, tmp_path):
    monkeypatch.setattr(
        tools.ClientResolverService,
        "resolve_client",
        lambda self, **kwargs: None,
    )

    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="sample_frontend_module",
        task_description="Inspect themes",
    )

    client_name, project_path = tools._resolve_code_change_project(payload)

    assert client_name is None
    assert project_path == str(tmp_path)


def test_intent_plan_explicit_operation_wins_over_keywords():
    result = tools._workflow_intent_plan(IntentPlanRequest(
        task_description="inspect the deploy and push implementation",
        operation="read",
        module_hint="x_module",
    ))

    assert result["normalized_intent"] == "read"
    assert result["confidence"] == "high"
    assert result["steps"] == ["workflow.prepare_change_context"]


def test_low_confidence_intent_only_allows_read_only_resume():
    result = tools._workflow_intent_plan(IntentPlanRequest(task_description="please handle this somehow"))

    assert result["normalized_intent"] == "ambiguous"
    assert result["clarification_required"] is True
    assert result["steps"] == ["workflow.resume_context"]


def test_compatibility_code_change_uses_hybrid_primitives(monkeypatch, tmp_path):
    calls = []

    async def prepare(request):
        calls.append("prepare")
        return {
            "status": "prepared",
            "change_id": "chg_" + "a" * 32,
            "client_name": "Alpha",
            "project_path": str(tmp_path),
            "module_name": "x_module",
            "module_path": "addons/custom/x_module",
        }

    def patch(request):
        calls.append("patch")
        return {
            "status": "patched",
            "changed_files": ["addons/custom/x_module/models/a.py"],
            "diff_sha256": "abc",
            "deploy_call": {
                "arguments": {
                    "change_id": request.change_id,
                    "client_name": "Alpha",
                    "project_path": str(tmp_path),
                    "module_name": "x_module",
                    "module_path": "addons/custom/x_module",
                    "commit_message": "Update module",
                    "paths": ["addons/custom/x_module/models/a.py"],
                }
            },
        }

    async def deploy(request):
        calls.append("deploy")
        assert request.change_id == "chg_" + "a" * 32
        return WorkflowReport(
            status="running",
            job_id="job-1",
            steps=[],
            summary={"next_call": {"tool": "jobs.get", "arguments": {"job_id": "job-1"}}},
        )

    monkeypatch.setattr(tools, "_workflow_prepare_change_context", prepare)
    monkeypatch.setattr(tools, "_repo_patch_change", patch)
    monkeypatch.setattr(tools, "safe_deploy_module", deploy)
    result = asyncio.run(tools._workflow_odoo_code_change(OdooCodeChangeRequest(
        client_name="Alpha",
        project_path=str(tmp_path),
        module_name="x_module",
        task_description="Change one model line",
        writes=[OdooCodeFileChange(relative_path="models/a.py", content="from odoo import models\n")],
    )))

    assert calls == ["prepare", "patch", "deploy"]
    assert result["status"] == "deploy_started"
    assert result["job_id"] == "job-1"
