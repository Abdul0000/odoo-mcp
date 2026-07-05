from __future__ import annotations

import asyncio
import subprocess

from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.schemas import ApprovalRecord, ApprovalStatus, CommandResult, JobRecord, JobStatus, JobStep, OdooRpcResult, SafeDeployModuleRequest, ResumeJobRequest
from odoo_agent_server.services.approval_service import ApprovalService
from odoo_agent_server.services.git_service import GitService
from odoo_agent_server.services.odoosh_service import OdooShService
from odoo_agent_server.services.repo_service import RepoService
from odoo_agent_server.services.store_service import JsonStore
from odoo_agent_server.routers.workflow import _pre_push_verify_files
from odoo_agent_server.routers import tools, workflow


def _init_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "test_stagging_18"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:Abdul0000/test_18.git"], cwd=path, check=True)
    (path / "README.md").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.local", "commit", "-m", "init"], cwd=path, check=True, capture_output=True, text=True)


def test_safe_deploy_starter_failure_returns_report(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))

    class FakeApproval:
        def ensure_allowed(self, *args, **kwargs):
            return None

    class BrokenStore:
        def create_job(self, job):
            raise OSError("state store unavailable")

    monkeypatch.setattr(workflow, "ApprovalService", lambda: FakeApproval())
    monkeypatch.setattr(workflow, "JsonStore", lambda: BrokenStore())

    result = asyncio.run(workflow.safe_deploy_module(SafeDeployModuleRequest(
        project_path=str(tmp_path),
        module_name="sale_order_notebook_tab",
        commit_message="[ADD] field",
        branch="test_stagging_18",
        run_qa=False,
        wait_for_odoosh=True,
        upgrade_module=True,
        timeout_seconds=900,
    )))

    assert result.status == "failed"
    assert result.job_id == ""
    assert result.steps[0].name == "safe_deploy_start"
    assert "state store unavailable" in result.summary["error"]


def test_safe_deploy_endpoint_starts_job_for_grok_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "sale_order_notebook_tab").mkdir(parents=True)

    def fake_submit(callback):
        return None

    monkeypatch.setattr(workflow, "submit_background", fake_submit)

    result = asyncio.run(workflow.safe_deploy_module(SafeDeployModuleRequest(
        project_path=str(project),
        module_name="sale_order_notebook_tab",
        commit_message="[ADD] test_qwen field before quotation template in sale order form",
        branch="test_stagging_18",
        run_qa=False,
        wait_for_odoosh=True,
        upgrade_module=True,
        timeout_seconds=900,
    )))

    assert result.status == "running"
    assert result.job_id
    assert result.summary["module_name"] == "sale_order_notebook_tab"


def test_git_commit_configures_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "git_author_name", "MCP Bot")
    monkeypatch.setattr(settings, "git_author_email", "mcp@example.local")
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "config", "--unset", "user.name"], cwd=repo, check=False)
    subprocess.run(["git", "config", "--unset", "user.email"], cwd=repo, check=False)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")

    result = GitService().commit(str(repo), "change readme", ["README.md"])

    assert result.exit_code == 0
    assert subprocess.check_output(["git", "config", "user.name"], cwd=repo, text=True).strip() == "MCP Bot"
    assert subprocess.check_output(["git", "config", "user.email"], cwd=repo, text=True).strip() == "mcp@example.local"


def test_git_push_target_validation_rejects_wrong_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "target_git_branch", "test_stagging_18")
    monkeypatch.setattr(settings, "target_git_repo", "git@github.com:Abdul0000/test_18.git")
    repo = tmp_path / "repo"
    _init_repo(repo)

    result = GitService().validate_target(str(repo), branch="main", remote="origin")

    assert result.exit_code == 2
    assert "expected test_stagging_18" in result.stderr


def test_repo_write_blocks_git_internal_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo)

    try:
        RepoService().write_file(str(repo), ".git/config", "bad")
    except HTTPException as exc:
        assert exc.status_code == 403
    else:  # pragma: no cover
        raise AssertionError("Expected .git write to be blocked")


def test_odoosh_module_update_parser_fails_on_skipped_module():
    result = CommandResult(command=["odoo-bin"], cwd="odoosh:ssh", exit_code=0, stderr="module dynamic_ai_report_builder: not installable, skipped")

    classified = OdooShService()._classify_module_update_result(result)

    assert classified.exit_code == 3
    assert "MCP_CLASSIFIED_FAILURE" in classified.stderr


def test_approval_failed_command_is_not_consumed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "require_approval", True)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "git.commit")
    store = JsonStore()
    approval = store.create_approval(ApprovalRecord(action="git.commit", summary="commit", status=ApprovalStatus.approved))

    service = ApprovalService()
    service.ensure_allowed("git.commit", approval.id, {})
    service.finalize_command(approval.id, exit_code=1, success_reason="ok", failure_reason="failed")

    assert JsonStore().get_approval(approval.id).status == ApprovalStatus.failed


def test_pre_push_verify_files_expands_directory_paths_after_write(tmp_path):
    repo = tmp_path / "repo"
    module = repo / "custom_mod"
    (module / "models").mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'Custom'}\n", encoding="utf-8")
    (module / "models" / "x.py").write_text("from odoo import models\n", encoding="utf-8")
    (module / "static").mkdir()
    (module / "static" / "ignored.png").write_bytes(b"png")

    files = _pre_push_verify_files(repo, "custom_mod", ["custom_mod"], "")

    assert str(module / "__manifest__.py") in files
    assert str(module / "models" / "x.py") in files
    assert all(not path.endswith("ignored.png") for path in files)


def test_pre_push_verify_files_falls_back_to_git_status_when_paths_are_not_files(tmp_path):
    repo = tmp_path / "repo"
    module = repo / "custom_mod"
    (module / "views").mkdir(parents=True)
    changed = module / "views" / "view.xml"
    changed.write_text("<odoo/>\n", encoding="utf-8")
    status = "## branch\n M custom_mod/views/view.xml\n M README.md\n"

    files = _pre_push_verify_files(repo, "custom_mod", ["README.md"], status)

    assert files == [str(changed)]


def test_safe_deploy_returns_retry_later_when_sha_is_not_deployed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "x_module").mkdir(parents=True)
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout="")

        def diff(self, project_path, staged=False, timeout_seconds=30):
            return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="")

        def configure_identity(self, project_path):
            return CommandResult(command=["git", "config"], cwd=project_path, exit_code=0)

        def validate_target(self, project_path, branch=None, remote="origin"):
            return CommandResult(command=["git", "validate-target"], cwd=project_path, exit_code=0)

        def commit(self, project_path, message, paths, timeout_seconds=60):
            return CommandResult(command=["git", "commit"], cwd=project_path, exit_code=0)

        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")

        def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
            return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="PUSH_OK\nsha=abc1234\n")

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": false}')

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=False,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.warning
    assert finished.result["status"] == "retry_later"
    assert finished.result["next_call"] == {"tool": "workflow.resume_job", "arguments": {"job_id": job.id}}
    assert "odoosh_wait_for_commit" not in {step.name for step in finished.steps}
    assert any(step.name == "odoosh_check_commit" and step.status == "warning" for step in finished.steps)


def test_resume_job_rechecks_deploy_without_replaying_commit_or_push(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "x_module").mkdir(parents=True)
    calls = {"commit": 0, "push": 0}

    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.warning,
        request={
            "project_path": str(project),
            "module_name": "x_module",
            "commit_message": "Deploy x_module",
            "remote": "origin",
            "branch": "main",
            "run_qa": False,
            "upgrade_module": False,
            "verify_ui_paths": [],
        },
        steps=[
            JobStep(name="qa_self_run", status="skipped"),
            JobStep(name="git_configure_identity", status="passed"),
            JobStep(name="git_validate_target", status="passed"),
            JobStep(name="git_commit", status="passed"),
            JobStep(name="git_head_sha", status="passed", result={"stdout": "abc1234\n"}),
            JobStep(name="git_push", status="passed", result={"stdout": "PUSH_OK\nsha=abc1234\n"}),
            JobStep(name="odoosh_check_commit", status="warning"),
        ],
    ))

    class FakeGit:
        def commit(self, *args, **kwargs):
            calls["commit"] += 1
            raise AssertionError("resume must not replay git commit")

        def push(self, *args, **kwargs):
            calls["push"] += 1
            raise AssertionError("resume must not replay git push")

        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

        def verify_module_manifest(self, module_name):
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    class FakeOdooRpc:
        def module_status(self, module_name):
            return OdooRpcResult(status="ok", result=[{"name": module_name, "state": "installed"}])

        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooRpcResult(status="ok", result={"module": module_name})

    monkeypatch.setattr(tools, "GitService", lambda: FakeGit())
    monkeypatch.setattr(tools, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(tools, "OdooRpcService", lambda: FakeOdooRpc())

    result = asyncio.run(tools._resume_job(ResumeJobRequest(job_id=job.id)))

    assert result["status"] == "ok"
    assert result["resumed_from"] == "odoosh_check_commit"
    assert calls == {"commit": 0, "push": 0}
    assert "git_commit" not in result["steps_run"]
    assert "git_push" not in result["steps_run"]
