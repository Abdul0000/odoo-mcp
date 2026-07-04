from __future__ import annotations

import subprocess

from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.schemas import ApprovalRecord, ApprovalStatus, CommandResult
from odoo_agent_server.services.approval_service import ApprovalService
from odoo_agent_server.services.git_service import GitService
from odoo_agent_server.services.odoosh_service import OdooShService
from odoo_agent_server.services.repo_service import RepoService
from odoo_agent_server.services.store_service import JsonStore
from odoo_agent_server.routers.workflow import _pre_push_verify_files


def _init_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "test_stagging_18"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:Abdul0000/test_18.git"], cwd=path, check=True)
    (path / "README.md").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.local", "commit", "-m", "init"], cwd=path, check=True, capture_output=True, text=True)


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
