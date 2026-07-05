from __future__ import annotations

import json

import pytest

from odoo_agent_server.config import settings
from odoo_agent_server.schemas import CommandResult, GitPushRequest, JobRecord, JobStatus
from odoo_agent_server.services.module_detection_service import detect_affected_modules
from odoo_agent_server.services.store_service import JsonStore
from odoo_agent_server.routers import git as git_router


class _FakeApproval:
    def ensure_allowed(self, *args, **kwargs):
        return None

    def finalize_command(self, *args, **kwargs):
        return None


class _FakeGit:
    def __init__(self, changed_stdout: str, calls: dict[str, int]) -> None:
        self.changed_stdout = changed_stdout
        self.calls = calls

    def current_branch(self, project_path: str) -> CommandResult:
        self.calls["current_branch"] = self.calls.get("current_branch", 0) + 1
        return CommandResult(command=["git", "branch"], cwd=project_path, exit_code=0, stdout="test_branch\n")

    def head_changed_files(self, project_path: str) -> CommandResult:
        self.calls["head_changed_files"] = self.calls.get("head_changed_files", 0) + 1
        return CommandResult(command=["git", "diff-tree"], cwd=project_path, exit_code=0, stdout=self.changed_stdout)

    def push(self, project_path: str, remote: str = "origin", branch: str | None = None, set_upstream: bool = True, timeout_seconds: int = 120) -> CommandResult:
        self.calls["push"] = self.calls.get("push", 0) + 1
        return CommandResult(command=["git", "push"], cwd=project_path, exit_code=0, stdout="push ok\n")

    def head_sha(self, project_path: str) -> CommandResult:
        self.calls["head_sha"] = self.calls.get("head_sha", 0) + 1
        return CommandResult(command=["git", "rev-parse", "HEAD"], cwd=project_path, exit_code=0, stdout="abc1234\n")


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "require_approval", False)
    monkeypatch.setattr(settings, "block_raw_push_for_modules", True)
    monkeypatch.setattr(git_router, "ApprovalService", lambda: _FakeApproval())
    yield


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "repo"
    (root / "x_module" / "models").mkdir(parents=True)
    (root / "x_module" / "__manifest__.py").write_text("{'name': 'X'}\n", encoding="utf-8")
    (root / "y_module" / "views").mkdir(parents=True)
    (root / "y_module" / "__manifest__.py").write_text("{'name': 'Y'}\n", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    return root


def test_git_push_refuses_raw_push_for_odoo_module_changes(project, monkeypatch):
    calls: dict[str, int] = {}
    monkeypatch.setattr(git_router, "GitService", lambda: _FakeGit("x_module/models/sale_order.py\n", calls))

    result = git_router.git_push(GitPushRequest(project_path=str(project)))

    assert result.exit_code == 2
    assert calls.get("push", 0) == 0
    assert "Refusing raw git.push for Odoo module changes" in result.stderr
    assert "x_module" in result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "raw_git_push_blocked_for_odoo_modules"
    assert payload["module_names"] == ["x_module"]
    assert payload["next_call"]["tool"] == "workflow.safe_deploy_module"
    assert payload["next_call"]["arguments"]["module_name"] == "x_module"


def test_git_push_acknowledged_module_push_succeeds_with_not_upgraded_warning(project, monkeypatch):
    calls: dict[str, int] = {}
    monkeypatch.setattr(git_router, "GitService", lambda: _FakeGit("x_module/__manifest__.py\n", calls))

    result = git_router.git_push(GitPushRequest(project_path=str(project), acknowledge_manual_deploy=True))

    assert result.exit_code == 0
    assert calls.get("push", 0) == 1
    assert "MCP_MANUAL_PUSH_PENDING_ODOO_UPGRADE" in result.stdout
    assert "No Odoo.sh module upgrade was triggered automatically" in result.stdout
    assert '"module_names": [\n    "x_module"\n  ]' in result.stdout
    assert '"tool": "workflow.safe_deploy_module"' in result.stdout
    state = JsonStore().load_session_state()
    assert state["last_git_push"]["module_names"] == ["x_module"]
    assert state["last_git_push"]["manual_push_requires_upgrade"] is True


def test_git_push_non_module_change_succeeds_without_warning(project, monkeypatch):
    calls: dict[str, int] = {}
    monkeypatch.setattr(git_router, "GitService", lambda: _FakeGit("README.md\n", calls))

    result = git_router.git_push(GitPushRequest(project_path=str(project)))

    assert result.exit_code == 0
    assert calls.get("push", 0) == 1
    assert "MCP_MANUAL_PUSH_PENDING_ODOO_UPGRADE" not in result.stdout
    state = JsonStore().load_session_state()
    assert state["last_git_push"]["module_names"] == []
    assert state["last_git_push"]["manual_push_requires_upgrade"] is False


def test_detect_affected_modules_from_mixed_changed_paths(project):
    modules = detect_affected_modules(
        str(project),
        [
            "README.md",
            "x_module/models/sale_order.py",
            "y_module/views/form.xml",
            str(project / "x_module" / "__manifest__.py"),
        ],
    )

    assert modules == ["x_module", "y_module"]


def test_safe_deploy_command_block_takes_priority_over_raw_push_module_guard(project, monkeypatch):
    calls: dict[str, int] = {}
    monkeypatch.setattr(git_router, "GitService", lambda: _FakeGit("x_module/models/sale_order.py\n", calls))
    job = JsonStore().create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.running,
        request={"project_path": str(project), "module_name": "x_module"},
        result={"status": "running"},
    ))

    result = git_router.git_push(GitPushRequest(project_path=str(project)))

    assert result.exit_code == 2
    assert calls == {}
    payload = json.loads(result.stdout)
    assert payload["status"] == "safe_deploy_active"
    assert payload["job_id"] == job.id
    assert "workflow.safe_deploy_module already owns" in result.stderr
