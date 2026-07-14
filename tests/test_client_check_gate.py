from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.main import agent_instructions
from odoo_agent_server.routers.tools import _workflow_resume_context
from odoo_agent_server.routers.repo import repo_read
from odoo_agent_server.schemas import ClientCheckRequest, JobRecord, JobStatus, RepoReadRequest, ResumeContextRequest
from odoo_agent_server.services.client_gate_service import ClientGateService
from odoo_agent_server.services.store_service import JsonStore


def _client(name: str, repo: str, branch: str, path: str, **extra) -> dict:
    return {
        "name": name,
        "repo": repo,
        "branch": branch,
        "path": path,
        "token": extra.pop("token", "token-secret"),
        "odoo_url": extra.pop("odoo_url", f"https://{name.lower()}.dev.odoo.com"),
        "odoo_db": extra.pop("odoo_db", f"{name.lower()}_db"),
        "odoo_login": extra.pop("odoo_login", "user@acme.test"),
        "odoo_password": extra.pop("odoo_password", "secret-password"),
        "odoosh_db_name": extra.pop("odoosh_db_name", f"{name.lower()}_db"),
        "odoosh_ssh_host": extra.pop("odoosh_ssh_host", f"{name.lower()}.dev.odoo.com"),
        "odoosh_ssh_port": extra.pop("odoosh_ssh_port", 22),
        "odoosh_ssh_user": extra.pop("odoosh_ssh_user", "123456"),
        "odoosh_ssh_private_key_b64": extra.pop("odoosh_ssh_private_key_b64", "a2V5"),
        "odoosh_odoo_path": extra.pop("odoosh_odoo_path", "/home/odoo/src/odoo"),
        "odoosh_user_addons_path": extra.pop("odoosh_user_addons_path", "/home/odoo/src/user"),
        **extra,
    }


def _set_clients(monkeypatch, clients: list[dict]) -> None:
    monkeypatch.setenv("ODOO_CLIENTS", json.dumps(clients))


def _make_checkout(path: Path) -> None:
    path.mkdir(parents=True)
    (path / ".git").mkdir()
    (path / "README.md").write_text("ok\n", encoding="utf-8")


def _patch_successful_validation(monkeypatch) -> None:
    monkeypatch.setattr(ClientGateService, "_check_repo_branch", lambda self, client: {"status": "ok", "branch_ok": True, "log": ["repo ok"]})
    monkeypatch.setattr(ClientGateService, "_check_checkout", lambda self, client: {"status": "ok", "log": ["checkout ok"]})
    monkeypatch.setattr(ClientGateService, "_check_odoo_shell", lambda self, client: {"status": "ok", "log": ["odoo ok"]})
    monkeypatch.setattr(ClientGateService, "_check_odoosh", lambda self, client: {"status": "ok", "addons_path_status": "ok", "log": ["ssh ok"]})


def test_client_check_lists_all_configured_clients_even_placeholders(monkeypatch, tmp_path):
    _set_clients(monkeypatch, [
        _client("Alpha", "https://github.com/acme/a.git", "dev", str(tmp_path / "a")),
        _client("Client 2", "https://github.com/Abdul0000/client_2.git", "18.0", str(tmp_path / "b"), token="CHANGE_ME_CLIENT_2_GITHUB_TOKEN"),
    ])

    result = ClientGateService().check()

    assert result["requires_client_selection"] is True
    assert result["available_client_names"] == ["Alpha", "Client 2"]
    assert "CHANGE_ME" not in json.dumps(result["clients"])


def test_client_check_validation_saves_active_client_and_allows_guarded_repo_tool(monkeypatch, tmp_path):
    state = tmp_path / "state"
    checkout = tmp_path / "alpha"
    _make_checkout(checkout)
    monkeypatch.setattr(settings, "state_dir", str(state))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _set_clients(monkeypatch, [
        _client("Alpha", "https://github.com/acme/a.git", "dev", str(checkout)),
        _client("Beta", "https://github.com/acme/b.git", "dev", str(tmp_path / "b")),
    ])

    with pytest.raises(HTTPException) as missing_client:
        repo_read(RepoReadRequest(project_path=str(checkout), relative_path="README.md"))
    assert "requires_client_selection" in str(missing_client.value.detail)

    with pytest.raises(HTTPException) as not_validated:
        repo_read(RepoReadRequest(client_name="Alpha", project_path=str(checkout), relative_path="README.md"))
    assert "requires_validation" in str(not_validated.value.detail)

    _patch_successful_validation(monkeypatch)
    checked = ClientGateService().check(client_name="Alpha", validate=True)
    assert checked["ok"] is True
    assert checked["validated"] is True
    assert checked["next_call"]["tool"] == "workflow.agent_instructions"

    content = repo_read(RepoReadRequest(client_name="Alpha", project_path=str(checkout), relative_path="README.md"))
    assert content.content == "ok\n"


def test_agent_instructions_returns_client_gate_until_validation_passes(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setattr(settings, "state_dir", str(state))
    _set_clients(monkeypatch, [
        _client("Alpha", "https://github.com/acme/a.git", "dev", str(tmp_path / "a")),
        _client("Beta", "https://github.com/acme/b.git", "dev", str(tmp_path / "b")),
    ])

    before = agent_instructions()
    assert before["prompt_source"] == "CLIENT_CHECK_SYSTEM_PROMPT.md"
    assert before["requires_client_selection"] is True

    JsonStore().save_session_state(
        {
            "client_name": "Alpha",
            "active_client_name": "Alpha",
            "client_validated": True,
            "client_validation": {"validated": True, "client_name": "Alpha", "summary": "ok"},
        },
        client_name="Alpha",
    )

    after = agent_instructions()
    assert after["prompt_source"] == "MCP_SYSTEM_PROMPT.md"
    assert "MCP_SYSTEM_PROMPT" in after["system_prompt"] or after["system_prompt"]


def test_client_check_shell_failure_still_allows_coding_when_ssh_ok(monkeypatch, tmp_path):
    state = tmp_path / "state"
    checkout = tmp_path / "alpha"
    _make_checkout(checkout)
    monkeypatch.setattr(settings, "state_dir", str(state))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _set_clients(monkeypatch, [
        _client("Alpha", "https://github.com/acme/a.git", "dev", str(checkout)),
        _client("Beta", "https://github.com/acme/b.git", "dev", str(tmp_path / "b")),
    ])
    monkeypatch.setattr(ClientGateService, "_check_repo_branch", lambda self, client: {"status": "ok", "branch_ok": True, "log": ["repo ok"]})
    monkeypatch.setattr(ClientGateService, "_check_checkout", lambda self, client: {"status": "ok", "log": ["checkout ok"]})
    monkeypatch.setattr(ClientGateService, "_check_odoo_shell", lambda self, client: {"status": "failed", "error_type": "odoo_login_error", "log": ["shell failed"]})
    monkeypatch.setattr(ClientGateService, "_check_odoosh", lambda self, client: {"status": "ok", "addons_path_status": "ok", "log": ["ssh ok"]})

    checked = ClientGateService().check(client_name="Alpha", validate=True)

    assert checked["ok"] is True
    assert checked["coding_ready"] is True
    assert checked["shell_ready"] is False
    assert checked["ssh_ready"] is True
    assert checked["deploy_ready"] is True
    assert "SSH fallback" in checked["summary"]

    content = repo_read(RepoReadRequest(client_name="Alpha", project_path=str(checkout), relative_path="README.md"))
    assert content.content == "ok\n"


def test_client_check_shell_ok_without_ssh_does_not_enable_safe_deploy(monkeypatch, tmp_path):
    state = tmp_path / "state"
    checkout = tmp_path / "alpha"
    _make_checkout(checkout)
    monkeypatch.setattr(settings, "state_dir", str(state))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _set_clients(monkeypatch, [
        _client("Alpha", "https://github.com/acme/a.git", "dev", str(checkout)),
        _client("Beta", "https://github.com/acme/b.git", "dev", str(tmp_path / "b")),
    ])
    monkeypatch.setattr(ClientGateService, "_check_repo_branch", lambda self, client: {"status": "ok", "branch_ok": True, "log": ["repo ok"]})
    monkeypatch.setattr(ClientGateService, "_check_checkout", lambda self, client: {"status": "ok", "log": ["checkout ok"]})
    monkeypatch.setattr(ClientGateService, "_check_odoo_shell", lambda self, client: {"status": "ok", "log": ["https jsonrpc ok"]})
    monkeypatch.setattr(ClientGateService, "_check_odoosh", lambda self, client: {"status": "failed", "addons_path_status": "skipped", "log": ["ssh timeout"]})

    checked = ClientGateService().check(client_name="Alpha", validate=True)

    assert checked["ok"] is True
    assert checked["coding_ready"] is True
    assert checked["data_query_ready"] is True
    assert checked["shell_ready"] is True
    assert checked["ssh_ready"] is False
    assert checked["deploy_ready"] is False
    assert "Odoo.sh SSH is required" in checked["deploy_blocker"]

    with pytest.raises(HTTPException) as blocked:
        ClientGateService().require_validated_client("Alpha", "workflow.safe_deploy_module")
    assert blocked.value.detail["required_capability"] == "deploy_ready"


def test_eight_hour_validation_lease_uses_local_cached_state_without_network(monkeypatch, tmp_path):
    state = tmp_path / "state"
    checkout = tmp_path / "alpha"
    _make_checkout(checkout)
    monkeypatch.setattr(settings, "state_dir", str(state))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "client_validation_ttl_seconds", 28800)
    _set_clients(monkeypatch, [_client("Alpha", "https://github.com/acme/a.git", "dev", str(checkout))])
    JsonStore().save_session_state(
        {
            "client_validation": {
                "validated": True,
                "coding_ready": True,
                "deploy_ready": True,
                "ssh_ready": True,
                "validated_at": datetime.now(timezone.utc).isoformat(),
                "project_path": str(checkout),
                "checks": {"cached": "ok"},
            }
        },
        client_name="Alpha",
    )
    monkeypatch.setattr(ClientGateService, "_check_repo_branch", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network validation repeated")))
    monkeypatch.setattr(ClientGateService, "_check_odoo_shell", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Odoo validation repeated")))
    monkeypatch.setattr(ClientGateService, "_check_odoosh", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("SSH validation repeated")))

    result = ClientGateService().check(client_name="Alpha", validate=True)

    assert result["validated"] is True
    assert result["summary"] == "Client already validated."


def test_resume_context_is_local_only_and_returns_pending_change(monkeypatch, tmp_path):
    state = tmp_path / "state"
    checkout = tmp_path / "alpha"
    _make_checkout(checkout)
    monkeypatch.setattr(settings, "state_dir", str(state))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "client_validation_ttl_seconds", 28800)
    _set_clients(monkeypatch, [_client("Alpha", "https://github.com/acme/a.git", "dev", str(checkout))])
    monkeypatch.setattr("odoo_agent_server.routers.tools.GitService.current_branch", lambda self, path: type("R", (), {"exit_code": 0, "stdout": "dev\n"})())
    JsonStore().save_session_state(
        {
            "project_path": str(checkout),
            "branch": "dev",
            "pending_code_change": {"module_name": "x_module", "task_description": "Fix label", "target_files": []},
            "client_validation": {
                "validated": True,
                "coding_ready": True,
                "deploy_ready": True,
                "ssh_ready": True,
                "validated_at": datetime.now(timezone.utc).isoformat(),
                "project_path": str(checkout),
            },
        },
        client_name="Alpha",
    )

    result = _workflow_resume_context(ResumeContextRequest(client_name="Alpha"))

    assert result["resume_mode"] == "fast_local"
    assert result["network_checks_performed"] is False
    assert result["pending_change"]["module_name"] == "x_module"
    assert result["next_call"]["tool"] == "workflow.prepare_change_context"


def test_coding_capability_refresh_skips_odoo_and_ssh(monkeypatch, tmp_path):
    checkout = tmp_path / "alpha"
    _make_checkout(checkout)
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _set_clients(monkeypatch, [_client("Alpha", "https://github.com/acme/a.git", "dev", str(checkout))])
    monkeypatch.setattr(ClientGateService, "_check_repo_branch", lambda self, client: {"status": "ok", "branch_ok": True, "log": []})
    monkeypatch.setattr(ClientGateService, "_check_checkout", lambda self, client: {"status": "ok", "log": []})
    monkeypatch.setattr(ClientGateService, "_check_odoo_shell", lambda *args: (_ for _ in ()).throw(AssertionError("Odoo must be skipped")))
    monkeypatch.setattr(ClientGateService, "_check_odoosh", lambda *args: (_ for _ in ()).throw(AssertionError("SSH must be skipped")))

    result = ClientGateService().check(client_name="Alpha", validate=True, required_capability="coding_ready")

    assert result["coding_ready"] is True
    assert result["validation_scope"] == "coding_ready"


def test_deploy_capability_refresh_checks_ssh_but_skips_odoo_login(monkeypatch, tmp_path):
    checkout = tmp_path / "alpha"
    _make_checkout(checkout)
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _set_clients(monkeypatch, [_client("Alpha", "https://github.com/acme/a.git", "dev", str(checkout))])
    calls = {"ssh": 0}
    monkeypatch.setattr(ClientGateService, "_check_repo_branch", lambda self, client: {"status": "ok", "branch_ok": True, "log": []})
    monkeypatch.setattr(ClientGateService, "_check_checkout", lambda self, client: {"status": "ok", "log": []})
    monkeypatch.setattr(ClientGateService, "_check_odoo_shell", lambda *args: (_ for _ in ()).throw(AssertionError("Odoo login must be skipped")))

    def ssh(self, client):
        calls["ssh"] += 1
        return {"status": "ok", "addons_path_status": "ok", "log": []}

    monkeypatch.setattr(ClientGateService, "_check_odoosh", ssh)
    result = ClientGateService().check(client_name="Alpha", validate=True, required_capability="deploy_ready")

    assert result["deploy_ready"] is True
    assert result["validation_scope"] == "deploy_ready"
    assert calls["ssh"] == 1


def test_deploy_capability_refresh_reuses_local_coding_base_without_remote_git(monkeypatch, tmp_path):
    checkout = tmp_path / "alpha"
    _make_checkout(checkout)
    subprocess.run(["git", "init", "-b", "dev"], cwd=checkout, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md"], cwd=checkout, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.local", "commit", "-m", "initial"],
        cwd=checkout,
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    _set_clients(monkeypatch, [_client("Alpha", "https://github.com/acme/a.git", "dev", str(checkout))])
    JsonStore().save_session_state({
        "client_validation": {
            "validated": True,
            "coding_ready": True,
            "deploy_ready": False,
            "project_path": str(checkout),
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "checks": {"github": "ok", "checkout": "ok"},
        }
    }, client_name="Alpha")
    monkeypatch.setattr(ClientGateService, "_check_repo_branch", lambda *args: (_ for _ in ()).throw(AssertionError("remote Git must be skipped")))
    monkeypatch.setattr(ClientGateService, "_check_checkout", lambda *args: (_ for _ in ()).throw(AssertionError("repo prepare must be skipped")))
    monkeypatch.setattr(ClientGateService, "_check_odoosh", lambda self, client: {"status": "ok", "addons_path_status": "ok", "log": []})

    result = ClientGateService().check(
        client_name="Alpha", validate=True, force_refresh=True, required_capability="deploy_ready"
    )

    assert result["deploy_ready"] is True
    assert result["checks"]["coding_base"] == "cached"


def test_resume_context_detects_deploy_worker_lost_after_restart(monkeypatch, tmp_path):
    checkout = tmp_path / "alpha"
    _make_checkout(checkout)
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    _set_clients(monkeypatch, [_client("Alpha", "https://github.com/acme/a.git", "dev", str(checkout))])
    monkeypatch.setattr("odoo_agent_server.routers.tools.GitService.current_branch", lambda self, path: type("R", (), {"exit_code": 0, "stdout": "dev\n"})())
    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.running,
        request={"client_name": "Alpha", "project_path": str(checkout), "module_name": "x_module", "worker_instance_id": "previous-process"},
    ))

    result = _workflow_resume_context(ResumeContextRequest(client_name="Alpha"))

    assert result["active_job"]["job_id"] == job.id
    assert result["active_job"]["worker_lost_after_restart"] is True
    assert result["next_call"] == {"tool": "workflow.resume_job", "arguments": {"job_id": job.id}}
