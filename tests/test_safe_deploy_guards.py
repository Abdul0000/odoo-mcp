from __future__ import annotations

import asyncio
import subprocess
import time

from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.schemas import ApprovalRecord, ApprovalStatus, CommandResult, DeployedModuleVerifyRequest, JobRecord, JobStatus, JobStep, OdooRpcResult, SafeDeployModuleRequest, ResumeJobRequest
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
        wait_for_result=False,
        timeout_seconds=900,
    )))

    assert result.status == "running"
    assert result.job_id
    assert result.summary["module_name"] == "sale_order_notebook_tab"
    assert result.summary["next_call"]["tool"] == "jobs.get"
    assert JsonStore().load_session_state()["last_safe_deploy_job_id"] == result.job_id


def test_safe_deploy_waits_and_returns_finished_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "sale_order_notebook_tab").mkdir(parents=True)

    def fake_run(payload, job_id):
        store = JsonStore()
        job = store.get_job(job_id)
        store.add_step(job, JobStep(name="git_push", status="passed"))
        job = store.get_job(job_id)
        store.finish_job(job, JobStatus.passed, {"module_name": payload.module_name, "sha": "abc1234"})

    def fake_submit(callback):
        callback()

    monkeypatch.setattr(workflow, "_run_safe_deploy", fake_run)
    monkeypatch.setattr(workflow, "submit_background", fake_submit)

    result = asyncio.run(workflow.safe_deploy_module(SafeDeployModuleRequest(
        project_path=str(project),
        module_name="sale_order_notebook_tab",
        commit_message="[ADD] field",
        branch="test_stagging_18",
        run_qa=False,
        wait_for_odoosh=True,
        upgrade_module=True,
        wait_for_result=True,
        wait_timeout_seconds=5,
    )))

    assert result.status == "passed"
    assert result.summary["module_name"] == "sale_order_notebook_tab"
    assert result.summary["sha"] == "abc1234"
    assert any(step.name == "git_push" for step in result.steps)


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


def test_safe_deploy_fails_without_upgrade_when_sha_is_not_deployed(tmp_path, monkeypatch):
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
            odoosh_deploy_timeout_seconds=0,
            odoosh_deploy_poll_interval_seconds=0.25,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.failed
    assert finished.result["status"] == "deploy_failed_or_timeout"
    assert finished.result["next_call"] == {"tool": "workflow.resume_job", "arguments": {"job_id": job.id}}
    assert "odoosh_wait_for_commit" not in {step.name for step in finished.steps}
    assert any(step.name == "odoosh_check_commit" and step.status == "failed" for step in finished.steps)


def test_safe_deploy_polls_odoosh_until_deployed_before_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "x_module").mkdir(parents=True)
    calls = {"check": 0, "deploy": 0}
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
            calls["check"] += 1
            deployed = calls["check"] >= 3
            return CommandResult(
                command=["odoosh", "check"],
                cwd="odoosh:ssh",
                exit_code=0 if deployed else 1,
                stdout='{"deployed": true}' if deployed else '{"deployed": false}',
            )

        def verify_module_manifest(self, module_name):
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def fake_deploy(req):
        calls["deploy"] += 1
        return OdooRpcResult(status="ok", result={"finished": True, "module_name": req.module_name})

    class FakeOdooRpc:
        def module_status(self, module_name):
            return OdooRpcResult(status="ok", result=[{"name": module_name, "state": "installed"}])

        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooRpcResult(status="ok", result={"module": module_name})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", fake_deploy)
    monkeypatch.setattr(workflow, "OdooRpcService", lambda: FakeOdooRpc())

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=True,
            odoosh_deploy_timeout_seconds=5,
            odoosh_deploy_poll_interval_seconds=0.25,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.passed
    assert calls == {"check": 3, "deploy": 1}
    assert any(step.name == "odoosh_check_commit" and step.status == "passed" for step in finished.steps)


def test_safe_deploy_blocks_upgrade_when_odoosh_build_failed_after_push(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "x_module").mkdir(parents=True)
    calls = {"manifest": 0, "deploy": 0}
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
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="db99b7e7\n")

        def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
            return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="PUSH_OK\nsha=db99b7e7\n")

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true, "current_sha": "db99b7e7"}')

        def recent_build_failure(self, since_epoch):
            return CommandResult(command=["odoosh", "recent_build_failure"], cwd="odoosh:ssh", exit_code=1, stdout='{"failed": true, "message": "Test: Failed"}')

        def verify_module_manifest(self, module_name):
            calls["manifest"] += 1
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def fake_deploy(req):
        calls["deploy"] += 1
        return OdooRpcResult(status="ok", result={"finished": True, "module_name": req.module_name})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", fake_deploy)

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=True,
            odoosh_deploy_timeout_seconds=5,
            odoosh_deploy_poll_interval_seconds=0.25,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.failed
    assert finished.result["status"] == "deploy_failed"
    assert calls == {"manifest": 0, "deploy": 0}
    assert any(step.name == "odoosh_build_failure_guard" and step.status == "failed" for step in finished.steps)


def test_safe_deploy_verifies_changed_user_files_before_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    changed = project / "x_module" / "models" / "a.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("from odoo import fields, models\n", encoding="utf-8")
    calls = {"check": 0, "verify_user": 0, "manifest": 0, "deploy": 0}
    captured = {}
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout=" M x_module/models/a.py\n")

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

    class FakeSyntaxVerifier:
        def verify(self, req):
            class Result:
                push_blocked = False

                def model_dump(self, mode="json"):
                    return {"push_blocked": False}

            return Result()

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            calls["check"] += 1
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

        def verify_user_file_hashes(self, expected_hashes):
            calls["verify_user"] += 1
            captured.update(expected_hashes)
            return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=0, stdout="ODOOSH_USER_FILE_VERIFY_OK")

        def verify_module_manifest(self, module_name):
            calls["manifest"] += 1
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def fake_deploy(req):
        calls["deploy"] += 1
        return OdooRpcResult(status="ok", result={"finished": True, "module_name": req.module_name})

    class FakeOdooRpc:
        def module_status(self, module_name):
            return OdooRpcResult(status="ok", result=[{"name": module_name, "state": "installed"}])

        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooRpcResult(status="ok", result={"module": module_name})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "SyntaxVerifierService", lambda: FakeSyntaxVerifier())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", fake_deploy)
    monkeypatch.setattr(workflow, "OdooRpcService", lambda: FakeOdooRpc())

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            paths=["x_module/models/a.py"],
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=True,
            odoosh_deploy_timeout_seconds=5,
            odoosh_deploy_poll_interval_seconds=0.25,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.passed
    assert calls == {"check": 1, "verify_user": 1, "manifest": 1, "deploy": 1}
    assert set(captured) == {"x_module/models/a.py"}
    assert any(step.name == "odoosh_user_file_verify" and step.status == "passed" for step in finished.steps)
    assert finished.request["expected_user_file_hashes"] == captured


def test_safe_deploy_blocks_upgrade_when_deployed_user_file_differs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    changed = project / "x_module" / "models" / "a.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("from odoo import fields, models\n", encoding="utf-8")
    calls = {"manifest": 0, "deploy": 0}
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout=" M x_module/models/a.py\n")

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

    class FakeSyntaxVerifier:
        def verify(self, req):
            class Result:
                push_blocked = False

                def model_dump(self, mode="json"):
                    return {"push_blocked": False}

            return Result()

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

        def verify_user_file_hashes(self, expected_hashes):
            return CommandResult(
                command=["odoosh", "verify_user_file_hashes"],
                cwd="odoosh:ssh",
                exit_code=1,
                stdout="MISMATCH x_module/models/a.py expected=local actual=remote",
            )

        def verify_module_manifest(self, module_name):
            calls["manifest"] += 1
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def fake_deploy(req):
        calls["deploy"] += 1
        return OdooRpcResult(status="ok", result={"finished": True, "module_name": req.module_name})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "SyntaxVerifierService", lambda: FakeSyntaxVerifier())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", fake_deploy)

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            paths=["x_module/models/a.py"],
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=True,
            odoosh_deploy_timeout_seconds=5,
            odoosh_deploy_poll_interval_seconds=0.25,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.failed
    assert finished.result["status"] == "deploy_code_mismatch"
    assert calls == {"manifest": 0, "deploy": 0}
    assert any(step.name == "odoosh_user_file_verify" and step.status == "failed" for step in finished.steps)


def test_verify_deployed_module_compares_against_odoosh_user_source(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    changed = project / "x_module" / "models" / "a.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("from odoo import fields, models\n", encoding="utf-8")
    calls = {"verify": 0, "list": 0}

    class FakeOdooSh:
        def list_user_files(self, module_name, subdir="", max_items=200):
            calls["list"] += 1
            return {
                "found": True,
                "module_name": module_name,
                "base": "/home/odoo/src/user",
                "files": [{"relative_path": "x_module/models/a.py", "path": "/home/odoo/src/user/x_module/models/a.py", "size": 10}],
            }

        def verify_user_file_hashes(self, expected_hashes):
            calls["verify"] += 1
            assert set(expected_hashes) == {"x_module/models/a.py"}
            return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=1, stdout="MISMATCH x_module/models/a.py")

    class FakeOdooRpc:
        def module_status(self, module_name):
            return OdooRpcResult(status="ok", result=[{"name": module_name, "state": "installed"}])

    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "OdooRpcService", lambda: FakeOdooRpc())

    result = workflow.verify_deployed_module(DeployedModuleVerifyRequest(
        project_path=str(project),
        module_name="x_module",
        paths=["x_module/models/a.py"],
    ))

    assert result["status"] == "failed"
    assert result["source_status"] == "mismatch"
    assert "Do not claim live/deployed/already done" in result["message"]
    assert result["odoosh_user_files"]["base"] == "/home/odoo/src/user"
    assert calls == {"verify": 1, "list": 1}


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


def test_resume_job_without_id_uses_saved_job_and_upgrade_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "x_module").mkdir(parents=True)
    calls = {"deploy": 0}

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
            "upgrade_module": True,
            "verify_ui_paths": [],
        },
        steps=[
            JobStep(name="git_commit", status="passed"),
            JobStep(name="git_head_sha", status="passed", result={"stdout": "abc1234\n"}),
            JobStep(name="git_push", status="passed"),
            JobStep(name="odoosh_check_commit", status="passed"),
            JobStep(name="odoosh_verify_module_manifest", status="passed"),
        ],
    ))
    store.save_session_state({"last_safe_deploy_job_id": job.id, "project_path": str(project), "module_name": "x_module"})

    def fake_deploy(req):
        calls["deploy"] += 1
        return OdooRpcResult(status="ok", result={"finished": True, "module_name": req.module_name})

    class FakeOdooRpc:
        def module_status(self, module_name):
            return OdooRpcResult(status="ok", result=[{"name": module_name, "state": "installed"}])

        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooRpcResult(status="ok", result={"module": module_name})

    monkeypatch.setattr(tools, "start_odoo_module_deploy_job", fake_deploy)
    monkeypatch.setattr(tools, "OdooRpcService", lambda: FakeOdooRpc())

    result = asyncio.run(tools._resume_job(ResumeJobRequest(override_step="upgrade")))

    assert result["status"] == "ok"
    assert result["original_job_id"] == job.id
    assert result["resumed_from"] == "odoo_module_deploy"
    assert calls == {"deploy": 1}
    assert "git_commit" not in result["steps_run"]
    assert "git_push" not in result["steps_run"]


def test_manual_git_commit_blocked_while_safe_deploy_owns_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.running,
        request={"project_path": str(project), "module_name": "x_module"},
    ))
    store.save_session_state({"last_safe_deploy_job_id": job.id, "project_path": str(project), "module_name": "x_module"})

    from odoo_agent_server.routers.git import git_commit
    from odoo_agent_server.schemas import GitCommitRequest

    result = git_commit(GitCommitRequest(project_path=str(project), message="manual commit", paths=[]))

    assert result.exit_code == 2
    assert "workflow.safe_deploy_module already owns" in result.stderr
    assert "workflow.resume_job" in result.stdout


def test_manual_odoosh_update_blocked_while_safe_deploy_owns_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.running,
        request={"project_path": str(tmp_path / "repo"), "module_name": "x_module"},
    ))
    store.save_session_state({"last_safe_deploy_job_id": job.id, "module_name": "x_module"})

    from odoo_agent_server.routers.odoosh import odoosh_module_update
    from odoo_agent_server.schemas import OdooShModuleUpdateRequest

    result = odoosh_module_update(OdooShModuleUpdateRequest(module_name="x_module"))

    assert result.exit_code == 2
    assert "Manual odoosh.module_update is blocked" in result.stderr
    assert "workflow.resume_job" in result.stdout


def test_safe_deploy_blocks_success_when_nested_module_update_still_running(tmp_path, monkeypatch):
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
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

        def verify_module_manifest(self, module_name):
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def fake_deploy(req):
        nested = JsonStore().create_job(JobRecord(job_type="odoo.module_deploy", status=JobStatus.running, request={"module_name": req.module_name}))
        return OdooRpcResult(status="ok", result={"job_id": nested.id, "job_status": "running", "finished": False})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", fake_deploy)

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=True,
            odoo_module_deploy_timeout_seconds=0,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.failed
    assert finished.result["status"] == "module_upgrade_running"
    assert any(step.name == "odoo_module_deploy" and step.status == "failed" for step in finished.steps)


def test_safe_deploy_verifies_inferred_python_field_after_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    field_file = project / "x_module" / "models" / "purchase_order.py"
    field_file.parent.mkdir(parents=True)
    field_file.write_text("from odoo import fields, models\n\nclass PurchaseOrder(models.Model):\n    _inherit = 'purchase.order'\n\n    grok_y = fields.Char(string='Grok Y', tracking=True)\n", encoding="utf-8")
    calls = {"registry_fields": 0}
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout=" M x_module/models/purchase_order.py\n")

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

    class FakeSyntaxVerifier:
        def verify(self, req):
            class Result:
                push_blocked = False
                def model_dump(self, mode="json"):
                    return {"push_blocked": False}
            return Result()

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

        def verify_user_file_hashes(self, expected_hashes):
            return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

        def verify_module_manifest(self, module_name):
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def fake_deploy(req):
        return OdooRpcResult(status="ok", result={"finished": True, "module_name": req.module_name})

    class FakeOdooRpc:
        def module_status(self, module_name):
            return OdooRpcResult(status="ok", result=[{"name": module_name, "state": "installed"}])

        def search_read(self, model, domain, fields, limit=20, offset=0, order=None, context=None):
            calls["registry_fields"] += 1
            assert model == "ir.model.fields"
            assert ("model", "=", "purchase.order") in domain
            assert ("name", "in", ["grok_y"]) in domain
            return OdooRpcResult(status="ok", result=[{"model": "purchase.order", "name": "grok_y", "ttype": "char"}])

        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooRpcResult(status="ok", result={"module": module_name})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "SyntaxVerifierService", lambda: FakeSyntaxVerifier())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", fake_deploy)
    monkeypatch.setattr(workflow, "OdooRpcService", lambda: FakeOdooRpc())

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            paths=["x_module/models/purchase_order.py"],
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=True,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.passed
    assert calls == {"registry_fields": 1}
    assert finished.result["expected_registry_fields"] == {"purchase.order": ["grok_y"]}
    assert any(step.name == "registry_field_verify" and step.status == "passed" for step in finished.steps)


def test_safe_deploy_fails_when_inferred_field_missing_after_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    field_file = project / "x_module" / "models" / "purchase_order.py"
    field_file.parent.mkdir(parents=True)
    field_file.write_text("from odoo import fields, models\n\nclass PurchaseOrder(models.Model):\n    _inherit = 'purchase.order'\n\n    grok_y = fields.Char(string='Grok Y')\n", encoding="utf-8")
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout=" M x_module/models/purchase_order.py\n")
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

    class FakeSyntaxVerifier:
        def verify(self, req):
            class Result:
                push_blocked = False
                def model_dump(self, mode="json"):
                    return {"push_blocked": False}
            return Result()

    class FakeOdooSh:
        def check_commit_deployed(self, sha):
            return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')
        def verify_user_file_hashes(self, expected_hashes):
            return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=0, stdout="ok")
        def verify_module_manifest(self, module_name):
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    def fake_deploy(req):
        return OdooRpcResult(status="ok", result={"finished": True, "module_name": req.module_name})

    class FakeOdooRpc:
        def module_status(self, module_name):
            return OdooRpcResult(status="ok", result=[{"name": module_name, "state": "installed"}])
        def search_read(self, model, domain, fields, limit=20, offset=0, order=None, context=None):
            return OdooRpcResult(status="ok", result=[])
        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooRpcResult(status="ok", result={"module": module_name})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "SyntaxVerifierService", lambda: FakeSyntaxVerifier())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "start_odoo_module_deploy_job", fake_deploy)
    monkeypatch.setattr(workflow, "OdooRpcService", lambda: FakeOdooRpc())

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            paths=["x_module/models/purchase_order.py"],
            run_qa=False,
            wait_for_odoosh=True,
            upgrade_module=True,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.failed
    assert finished.result["status"] == "registry_field_missing"
    assert any(step.name == "registry_field_verify" and step.status == "failed" for step in finished.steps)


def test_safe_deploy_duplicate_call_returns_existing_running_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "sale_order_notebook_tab").mkdir(parents=True)
    submit_calls = {"count": 0}

    def fake_submit(callback):
        submit_calls["count"] += 1
        return None

    monkeypatch.setattr(workflow, "submit_background", fake_submit)

    payload = SafeDeployModuleRequest(
        project_path=str(project),
        module_name="sale_order_notebook_tab",
        commit_message="Add field grok_a",
        branch="test_stagging_18",
        run_qa=False,
        wait_for_odoosh=True,
        upgrade_module=True,
        wait_for_result=False,
    )

    first = asyncio.run(workflow.safe_deploy_module(payload))
    second = asyncio.run(workflow.safe_deploy_module(payload))

    assert first.status == "running"
    assert second.status == "already_in_progress"
    assert second.job_id == first.job_id
    assert second.summary["blocking_reason"] == "active_job"
    assert second.summary["next_call"]["tool"] == "jobs.get"
    assert submit_calls == {"count": 1}
    assert len(JsonStore().list_jobs()) == 1


def test_safe_deploy_resumable_failure_returns_existing_job_to_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "sale_order_notebook_tab").mkdir(parents=True)
    store = JsonStore()
    existing = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.failed,
        request={
            "project_path": str(project),
            "module_name": "sale_order_notebook_tab",
            "commit_message": "Add field grok_a",
        },
        result={"status": "deploy_failed_or_timeout", "message": "Odoo.sh did not deploy the pushed SHA yet."},
    ))
    submit_calls = {"count": 0}

    def fake_submit(callback):
        submit_calls["count"] += 1
        return None

    monkeypatch.setattr(workflow, "submit_background", fake_submit)

    result = asyncio.run(workflow.safe_deploy_module(SafeDeployModuleRequest(
        project_path=str(project),
        module_name="sale_order_notebook_tab",
        commit_message="Add field grok_a retry",
        branch="test_stagging_18",
        run_qa=False,
        wait_for_odoosh=True,
        upgrade_module=True,
        wait_for_result=False,
    )))

    assert result.status == "resumable_existing_job"
    assert result.job_id == existing.id
    assert result.summary["blocking_reason"] == "resumable_job"
    assert result.summary["result_status"] == "deploy_failed_or_timeout"
    assert result.summary["next_call"] == {"tool": "workflow.resume_job", "arguments": {"job_id": existing.id}}
    assert submit_calls == {"count": 0}
    assert len(JsonStore().list_jobs()) == 1


def test_safe_deploy_force_new_after_resumable_failure_starts_new_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "sale_order_notebook_tab").mkdir(parents=True)
    store = JsonStore()
    existing = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.failed,
        request={
            "project_path": str(project),
            "module_name": "sale_order_notebook_tab",
            "commit_message": "Add field grok_a",
        },
        result={"status": "deploy_code_mismatch", "message": "Deployed source did not match the pushed files."},
    ))
    submit_calls = {"count": 0}

    def fake_submit(callback):
        submit_calls["count"] += 1
        return None

    monkeypatch.setattr(workflow, "submit_background", fake_submit)

    result = asyncio.run(workflow.safe_deploy_module(SafeDeployModuleRequest(
        project_path=str(project),
        module_name="sale_order_notebook_tab",
        commit_message="Force restart deploy",
        branch="test_stagging_18",
        run_qa=False,
        wait_for_odoosh=True,
        upgrade_module=True,
        wait_for_result=False,
        force_new_deploy=True,
    )))

    assert result.status == "running"
    assert result.job_id != existing.id
    assert submit_calls == {"count": 1}
    jobs = JsonStore().list_jobs()
    assert len(jobs) == 2
    assert {job.id for job in jobs} == {existing.id, result.job_id}



def test_safe_deploy_request_defaults_to_fast_response_and_qa_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    (project / "sale_order_notebook_tab").mkdir(parents=True)
    submit_calls = {"count": 0}

    def fake_submit(callback):
        submit_calls["count"] += 1
        # Do not execute the background callback; this test asserts the HTTP
        # handler returns immediately with a resumable/pollable job id.
        return None

    monkeypatch.setattr(workflow, "submit_background", fake_submit)

    payload = SafeDeployModuleRequest(
        project_path=str(project),
        module_name="sale_order_notebook_tab",
        commit_message="Add field grok_fast",
        branch="test_stagging_18",
        wait_for_odoosh=True,
        upgrade_module=True,
    )

    assert payload.run_qa is True
    assert payload.wait_for_result is False

    started = time.monotonic()
    result = asyncio.run(workflow.safe_deploy_module(payload))
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert result.status == "running"
    assert result.job_id
    assert result.summary["next_call"]["tool"] == "jobs.get"
    assert submit_calls == {"count": 1}


def test_run_safe_deploy_runs_qa_by_default_when_omitted(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    changed = project / "x_module" / "models" / "a.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("from odoo import fields, models\n", encoding="utf-8")
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))
    calls = {"qa": 0}

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout=" M x_module/models/a.py\n")
        def diff(self, project_path, staged=False, timeout_seconds=30):
            return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="")
        def changed_files(self, project_path):
            return CommandResult(command=["git", "changed-files"], cwd=project_path, exit_code=0, stdout="x_module/models/a.py\n")
        def configure_identity(self, project_path):
            return CommandResult(command=["git", "config"], cwd=project_path, exit_code=0)
        def validate_target(self, project_path, branch=None, remote="origin"):
            return CommandResult(command=["git", "validate-target"], cwd=project_path, exit_code=0)
        def commit(self, project_path, message, paths, timeout_seconds=60):
            return CommandResult(command=["git", "commit"], cwd=project_path, exit_code=0)
        def head_changed_files(self, project_path, timeout_seconds=20):
            return CommandResult(command=["git", "diff-tree"], cwd=project_path, exit_code=0, stdout="x_module/models/a.py\n")
        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")
        def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
            return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="PUSH_OK\nsha=abc1234\n")
        def remote_head_sha(self, project_path, remote="origin", branch=None, timeout_seconds=30):
            return CommandResult(command=["git", "remote-head-sha"], cwd=project_path, exit_code=0, stdout="abc1234\n")

    class FakeSyntaxVerifier:
        def verify(self, req):
            class Result:
                push_blocked = False
                def model_dump(self, mode="json"):
                    return {"push_blocked": False}
            return Result()

    class FakeOdooSh:
        def verify_module_manifest(self, module_name):
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    class FakeOdooRpc:
        def module_status(self, module_name):
            return OdooRpcResult(status="ok", result=[{"name": module_name, "state": "installed"}])
        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooRpcResult(status="ok", result={"module": module_name})

    class QaReport:
        status = "passed"
        def model_dump(self, mode="json"):
            return {"status": "passed"}

    def fake_self_run(req):
        calls["qa"] += 1
        return QaReport()

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "SyntaxVerifierService", lambda: FakeSyntaxVerifier())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "OdooRpcService", lambda: FakeOdooRpc())
    monkeypatch.setattr(workflow, "self_run", fake_self_run)

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            paths=["x_module/models/a.py"],
            wait_for_odoosh=False,
            upgrade_module=False,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert calls == {"qa": 1}
    assert any(step.name == "qa_self_run" and step.status == "passed" for step in finished.steps)


def test_run_safe_deploy_idempotent_retry_skips_commit_and_push_when_head_already_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    changed = project / "x_module" / "models" / "a.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("from odoo import fields, models\n", encoding="utf-8")
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running))
    calls = {"commit": 0, "push": 0}

    class FakeGit:
        def status(self, project_path):
            return CommandResult(command=["git", "status"], cwd=project_path, exit_code=0, stdout="")
        def diff(self, project_path, staged=False, timeout_seconds=30):
            return CommandResult(command=["git", "diff"], cwd=project_path, exit_code=0, stdout="")
        def changed_files(self, project_path):
            return CommandResult(command=["git", "changed-files"], cwd=project_path, exit_code=0, stdout="")
        def head_changed_files(self, project_path, timeout_seconds=20):
            return CommandResult(command=["git", "diff-tree"], cwd=project_path, exit_code=0, stdout="x_module/models/a.py\n")
        def configure_identity(self, project_path):
            return CommandResult(command=["git", "config"], cwd=project_path, exit_code=0)
        def validate_target(self, project_path, branch=None, remote="origin"):
            return CommandResult(command=["git", "validate-target"], cwd=project_path, exit_code=0)
        def commit(self, project_path, message, paths, timeout_seconds=60):
            calls["commit"] += 1
            return CommandResult(command=["git", "commit"], cwd=project_path, exit_code=0)
        def head_sha(self, project_path):
            return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")
        def push(self, project_path, remote="origin", branch=None, set_upstream=True, timeout_seconds=120):
            calls["push"] += 1
            return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="PUSH_OK\nsha=abc1234\n")
        def remote_head_sha(self, project_path, remote="origin", branch=None, timeout_seconds=30):
            return CommandResult(command=["git", "remote-head-sha"], cwd=project_path, exit_code=0, stdout="abc1234\n")

    class FakeSyntaxVerifier:
        def verify(self, req):
            class Result:
                push_blocked = False
                def model_dump(self, mode="json"):
                    return {"push_blocked": False}
            return Result()

    class FakeOdooSh:
        def verify_module_manifest(self, module_name):
            return CommandResult(command=["odoosh", "manifest"], cwd="odoosh:ssh", exit_code=0, stdout="ok")

    class FakeOdooRpc:
        def module_status(self, module_name):
            return OdooRpcResult(status="ok", result=[{"name": module_name, "state": "installed"}])
        def registry_smoke_test(self, module_name, model_names=None, menu_names=None, strict=True):
            return OdooRpcResult(status="ok", result={"module": module_name})

    monkeypatch.setattr(workflow, "GitService", lambda: FakeGit())
    monkeypatch.setattr(workflow, "SyntaxVerifierService", lambda: FakeSyntaxVerifier())
    monkeypatch.setattr(workflow, "OdooShService", lambda: FakeOdooSh())
    monkeypatch.setattr(workflow, "OdooRpcService", lambda: FakeOdooRpc())

    workflow._run_safe_deploy(
        SafeDeployModuleRequest(
            project_path=str(project),
            module_name="x_module",
            commit_message="Deploy x_module",
            paths=["x_module/models/a.py"],
            run_qa=False,
            wait_for_odoosh=False,
            upgrade_module=False,
        ),
        job.id,
    )

    finished = store.get_job(job.id)
    assert finished.status == JobStatus.passed
    assert calls == {"commit": 0, "push": 0}
    assert any(step.name == "idempotent_commit_push_skip" and step.status == "passed" for step in finished.steps)
    assert any(step.name == "git_commit" and step.status == "skipped" for step in finished.steps)
    assert any(step.name == "git_push" and step.status == "skipped" for step in finished.steps)
