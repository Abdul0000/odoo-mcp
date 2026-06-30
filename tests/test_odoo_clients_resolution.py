from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from odoo_agent_server.config import get_odoo_clients, settings
from odoo_agent_server.routers.workflow import coding_task_plan, start_session
from odoo_agent_server.schemas import CodingTaskPlanRequest, StartSessionRequest
from odoo_agent_server.services.client_resolver_service import ClientResolverService
from odoo_agent_server.services.git_service import GitService
from odoo_agent_server.services.repo_service import RepoService


def _client(name: str, repo: str, branch: str, path: str, **extra) -> dict:
    return {
        "name": name,
        "repo": repo,
        "branch": branch,
        "path": path,
        "odoo_url": extra.pop("odoo_url", f"https://{name.lower()}.example.com"),
        "odoo_db": extra.pop("odoo_db", f"{name.lower()}_db"),
        "odoo_login": extra.pop("odoo_login", "user@example.com"),
        "odoo_password": extra.pop("odoo_password", "super-secret"),
        "token": extra.pop("token", "token-secret"),
        **extra,
    }


def _set_clients(monkeypatch, clients: list[dict]) -> None:
    monkeypatch.setenv("ODOO_CLIENTS", json.dumps(clients))


def _init_origin(tmp_path: Path, branch: str = "dev") -> Path:
    source = tmp_path / "source"
    bare = tmp_path / "origin.git"
    source.mkdir()
    subprocess.run(["git", "init", "-b", branch], cwd=source, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.local"], cwd=source, check=True)
    (source / "README.md").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=source, check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", "--bare", str(source), str(bare)], check=True, capture_output=True, text=True)
    return bare


def test_resolver_selects_explicit_name(monkeypatch, tmp_path):
    clients = [
        _client("Alpha", "git@example.com:a.git", "alpha", str(tmp_path / "a")),
        _client("Beta", "git@example.com:b.git", "beta", str(tmp_path / "b")),
    ]
    _set_clients(monkeypatch, clients)

    resolved = ClientResolverService().resolve_client(client_name="Beta")

    assert resolved is not None
    assert resolved.name == "Beta"
    assert resolved.branch == "beta"


def test_resolver_matches_current_odoo_url_and_db(monkeypatch, tmp_path):
    clients = [
        _client("Alpha", "git@example.com:a.git", "alpha", str(tmp_path / "a"), odoo_url="https://a.example.com", odoo_db="a"),
        _client("Beta", "git@example.com:b.git", "beta", str(tmp_path / "b"), odoo_url="https://b.example.com", odoo_db="b"),
    ]
    _set_clients(monkeypatch, clients)
    monkeypatch.setenv("ODOO_BASE_URL", "https://b.example.com")
    monkeypatch.setenv("ODOO_DB_NAME", "b")

    resolved = ClientResolverService().resolve_client()

    assert resolved is not None
    assert resolved.name == "Beta"


def test_resolver_matches_project_path(monkeypatch, tmp_path):
    target = tmp_path / "client-b"
    clients = [
        _client("Alpha", "git@example.com:a.git", "alpha", str(tmp_path / "a")),
        _client("Beta", "git@example.com:b.git", "beta", str(target)),
    ]
    _set_clients(monkeypatch, clients)

    resolved = ClientResolverService().resolve_client(project_path=str(target))

    assert resolved is not None
    assert resolved.name == "Beta"


def test_resolver_auto_selects_single_client(monkeypatch, tmp_path):
    _set_clients(monkeypatch, [_client("Only", "git@example.com:o.git", "dev", str(tmp_path / "only"))])

    resolved = ClientResolverService().resolve_client()

    assert resolved is not None
    assert resolved.name == "Only"


def test_get_odoo_clients_accepts_shell_quoted_escaped_json(monkeypatch):
    monkeypatch.setenv(
        "ODOO_CLIENTS",
        "'[{\\\"name\\\":\\\"Test 18\\\",\\\"repo\\\":\\\"git@example.com:test.git\\\",\\\"branch\\\":\\\"test_stagging_18\\\",\\\"path\\\":\\\"/workspace/repos/test_18\\\"}]'",
    )

    clients = get_odoo_clients()

    assert clients[0]["name"] == "Test 18"
    assert clients[0]["branch"] == "test_stagging_18"
    assert clients[0]["path"] == "/workspace/repos/test_18"


def test_resolver_ambiguous_clients_lists_safe_names(monkeypatch, tmp_path):
    _set_clients(monkeypatch, [
        _client("Alpha", "git@example.com:a.git", "dev", str(tmp_path / "a")),
        _client("Beta", "git@example.com:b.git", "dev", str(tmp_path / "b")),
    ])

    with pytest.raises(HTTPException) as exc:
        ClientResolverService().resolve_client()

    detail = str(exc.value.detail)
    assert "Alpha" in detail
    assert "Beta" in detail
    assert "token-secret" not in detail
    assert "super-secret" not in detail


def test_incomplete_single_client_requires_repo_branch_path(monkeypatch):
    _set_clients(monkeypatch, [{"name": "Broken", "repo": "git@example.com:b.git"}])

    with pytest.raises(HTTPException) as exc:
        ClientResolverService().resolve_client()

    detail = str(exc.value.detail)
    assert "repo/branch/path" in detail
    assert "odoo_password" not in detail


def test_repo_prepare_uses_selected_client_path(monkeypatch, tmp_path):
    origin = _init_origin(tmp_path, "dev")
    checkout = tmp_path / "client-checkout"
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "target_git_repo", None)
    monkeypatch.setattr(settings, "target_git_branch", None)
    monkeypatch.setattr(settings, "target_project_path", None)
    _set_clients(monkeypatch, [_client("Client", str(origin), "dev", str(checkout), token="")])

    prepared = RepoService().prepare_repo(client_name="Client")

    assert prepared.project_path == str(checkout.resolve())
    assert prepared.branch == "dev"
    assert (checkout / ".git").exists()


def test_repo_prepare_clones_into_existing_empty_client_path(monkeypatch, tmp_path):
    origin = _init_origin(tmp_path, "dev")
    checkout = tmp_path / "empty-checkout"
    checkout.mkdir()
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _set_clients(monkeypatch, [_client("Client", str(origin), "dev", str(checkout), token="")])

    prepared = RepoService().prepare_repo()

    assert prepared.project_path == str(checkout.resolve())
    assert (checkout / "README.md").exists()


def test_repo_prepare_rejects_non_git_non_empty_client_path(monkeypatch, tmp_path):
    origin = _init_origin(tmp_path, "dev")
    checkout = tmp_path / "occupied"
    checkout.mkdir()
    (checkout / "local.txt").write_text("do not overwrite\n", encoding="utf-8")
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _set_clients(monkeypatch, [_client("Client", str(origin), "dev", str(checkout), token="")])

    with pytest.raises(HTTPException) as exc:
        RepoService().prepare_repo()

    assert "not a git repository and is not empty" in str(exc.value.detail)
    assert "repo config" not in str(exc.value.detail).lower()


def test_git_validate_target_uses_client_branch_and_repo(monkeypatch, tmp_path):
    origin = _init_origin(tmp_path, "dev")
    checkout = tmp_path / "client-checkout"
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "target_git_repo", None)
    monkeypatch.setattr(settings, "target_git_branch", None)
    _set_clients(monkeypatch, [_client("Client", str(origin), "dev", str(checkout), token="")])
    RepoService().prepare_repo()

    ok = GitService().validate_target(str(checkout), branch="dev")
    bad = GitService().validate_target(str(checkout), branch="main")

    assert ok.exit_code == 0
    assert bad.exit_code == 2
    assert "expected dev" in bad.stderr


def test_coding_task_plan_defaults_from_single_client(monkeypatch, tmp_path):
    _set_clients(monkeypatch, [_client("Client", "git@example.com:c.git", "dev", str(tmp_path / "checkout"), token="")])

    result = coding_task_plan(CodingTaskPlanRequest(task="Add a field"))

    assert result.defaults["client_name"] == "Client"
    assert result.defaults["repo_url"] == "git@example.com:c.git"
    assert result.defaults["branch"] == "dev"
    assert result.defaults["project_path"] == str(tmp_path / "checkout")


def test_start_session_saves_safe_client_metadata(monkeypatch, tmp_path):
    origin = _init_origin(tmp_path, "dev")
    checkout = tmp_path / "session-checkout"
    state = tmp_path / "state"
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(state))
    _set_clients(monkeypatch, [_client("Client", str(origin), "dev", str(checkout))])

    report = start_session(StartSessionRequest(include_odoo=False, include_odoosh=False))

    assert report.status in {"passed", "warning"}
    session_file = state / "session_state__Client.json"
    data = json.loads(session_file.read_text(encoding="utf-8"))
    text = json.dumps(data)
    assert data["client_name"] == "Client"
    assert data["project_path"] == str(checkout.resolve())
    assert "token-secret" not in text
    assert "super-secret" not in text
    assert "oauth2:" not in text
