from __future__ import annotations

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.odoo import _module_deploy_with_timeout_recovery, start_odoo_module_deploy_job
from odoo_agent_server.schemas import CommandResult, OdooResult
from odoo_agent_server.schemas import OdooModuleRequest
from odoo_agent_server.services.approval_service import ApprovalService
from odoo_agent_server.services.odoo_service import OdooService
from odoo_agent_server.services.odoosh_service import OdooShService
from odoo_agent_server.services.store_service import JsonStore


def test_approval_required_actions_do_not_block_when_global_approval_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "require_approval", False)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "repo.write,odoo.module_deploy,odoosh.module_update")

    # Must not raise even though the action list contains the action.
    ApprovalService().ensure_allowed("odoo.module_deploy", None, {"module_name": "x"})


def test_tools_list_reports_no_approval_when_global_approval_disabled(monkeypatch):
    monkeypatch.setattr(settings, "require_approval", False)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "odoo.module_deploy,odoosh.module_update")
    client = TestClient(app)

    response = client.get("/tools/list", headers={"Authorization": f"Bearer {settings.api_token}"})

    assert response.status_code == 200
    items = {row["name"]: row for row in response.json()}
    assert items["odoo_module_deploy"]["requires_approval"] is False
    assert items["odoosh_module_update"]["requires_approval"] is False


def test_module_deploy_falls_back_to_ssh_when_update_list_access_denied(monkeypatch):
    def fake_execute_kw(self, model, method, args=None, kwargs=None, superuser=False):
        assert model == "ir.module.module"
        assert method == "update_list"
        return OdooResult(status="failed", error="<Fault 3: 'Access Denied'>")

    def fake_ssh_deploy(self, module_name, timeout_seconds=600):
        return CommandResult(command=["odoosh.module_deploy_ssh", module_name], cwd="odoosh:ssh", exit_code=0, stdout="SSH_OK")

    monkeypatch.setattr(OdooService, "execute_kw", fake_execute_kw)
    monkeypatch.setattr(OdooShService, "module_deploy_ssh", fake_ssh_deploy)

    result = OdooService().module_deploy("sample_custom_module")

    assert result.status == "ok"
    assert result.result["fallback"] == "odoosh"
    assert result.result["operation"] == "deploy"
    assert result.result["ssh"]["stdout"] == "SSH_OK"


def test_module_deploy_falls_back_to_ssh_when_module_missing_after_update_list(monkeypatch):
    def fake_execute_kw(self, model, method, args=None, kwargs=None, superuser=False):
        if model == "ir.module.module" and method == "update_list":
            return OdooResult(status="ok", result=True)
        if model == "ir.module.module" and method == "search_read":
            return OdooResult(status="ok", result=[])
        raise AssertionError(f"unexpected Odoo shell call: {model}.{method}")

    def fake_ssh_deploy(self, module_name, timeout_seconds=600):
        return CommandResult(command=["odoosh.module_deploy_ssh", module_name], cwd="odoosh:ssh", exit_code=0, stdout="SSH_DEPLOY_OK")

    monkeypatch.setattr(OdooService, "execute_kw", fake_execute_kw)
    monkeypatch.setattr(OdooShService, "module_deploy_ssh", fake_ssh_deploy)

    result = OdooService().module_deploy("new_custom_module")

    assert result.status == "ok"
    assert result.result["fallback"] == "odoosh"
    assert result.result["operation"] == "deploy"
    assert "not found in ir.module.module" in result.result["cause"]
    assert result.result["ssh"]["stdout"] == "SSH_DEPLOY_OK"


def test_module_deploy_timeout_is_confirmed_from_live_installed_state():
    class Service:
        def module_deploy(self, module_name, timeout_seconds=600):
            return OdooResult(status="failed", error="SSH command timed out")

        def module_status(self, module_name):
            return OdooResult(status="ok", result=[{"name": module_name, "state": "installed"}])

    result = _module_deploy_with_timeout_recovery(
        Service(),
        OdooModuleRequest(module_name="x_module"),
    )

    assert result.status == "ok"
    assert result.result["timeout_recovered"] is True
    assert result.result["attempts"] == 1


def test_module_deploy_timeout_retries_once_after_uninstalled_status():
    class Service:
        def __init__(self):
            self.deploy_calls = 0

        def module_deploy(self, module_name, timeout_seconds=600):
            self.deploy_calls += 1
            if self.deploy_calls == 1:
                return OdooResult(status="failed", error="operation timeout")
            return OdooResult(status="ok", result={"module_name": module_name})

        def module_status(self, module_name):
            return OdooResult(status="ok", result=[{"name": module_name, "state": "uninstalled"}])

    service = Service()
    result = _module_deploy_with_timeout_recovery(service, OdooModuleRequest(module_name="x_module"))

    assert result.status == "ok"
    assert service.deploy_calls == 2
    assert result.result["timeout_recovered"] is True
    assert result.result["attempts"] == 2


def test_internal_module_deploy_job_does_not_require_second_approval(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "require_approval", True)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "odoo.module_deploy")

    def fake_module_deploy(self, module_name):
        return OdooResult(status="ok", result={"module_name": module_name})

    monkeypatch.setattr(OdooService, "module_deploy", fake_module_deploy)

    result = start_odoo_module_deploy_job(OdooModuleRequest(
        module_name="sample_custom_module",
        wait_timeout_seconds=2,
        poll_interval_seconds=0.25,
    ))

    assert result.status == "ok"
    assert result.result["finished"] is True
    assert result.result["job_status"] == "passed"


def test_public_module_deploy_blocks_after_manual_push_until_sha_deployed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    JsonStore().save_session_state({
        "last_git_push": {
            "sha": "abc1234",
            "started_epoch": 123,
            "module_names": ["x_module"],
            "expected_user_file_hashes": {"x_module/models/a.py": "0" * 64},
            "verified_for_module_deploy": False,
        }
    })

    def fake_check(self, sha):
        return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=1, stdout='{"deployed": false}')

    def fake_module_deploy(self, module_name):
        raise AssertionError("module_deploy must not run before Odoo.sh deploy guard passes")

    monkeypatch.setattr(OdooShService, "check_commit_deployed", fake_check)
    monkeypatch.setattr(OdooService, "module_deploy", fake_module_deploy)

    result = start_odoo_module_deploy_job(
        OdooModuleRequest(
            module_name="x_module",
            wait_timeout_seconds=2,
            poll_interval_seconds=0.25,
            deploy_guard_timeout_seconds=0,
            deploy_guard_poll_interval_seconds=0.25,
        ),
        enforce_deploy_guard=True,
    )

    assert result.status == "failed"
    assert result.result["job_status"] == "failed"
    assert "stale code" in result.error
    assert result.result["job"]["result"]["result"]["status"] == "deployment_waiting"


def test_public_module_deploy_blocks_when_manual_push_hash_mismatches(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    JsonStore().save_session_state({
        "last_git_push": {
            "sha": "abc1234",
            "started_epoch": 123,
            "module_names": ["x_module"],
            "expected_user_file_hashes": {"x_module/models/a.py": "0" * 64},
            "verified_for_module_deploy": False,
        }
    })

    def fake_check(self, sha):
        return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

    def fake_recent(self, since_epoch):
        return CommandResult(command=["odoosh", "recent_build_failure"], cwd="odoosh:ssh", exit_code=0, stdout='{"failed": false}')

    def fake_hashes(self, expected):
        return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=1, stdout="MISMATCH x_module/models/a.py")

    def fake_module_deploy(self, module_name):
        raise AssertionError("module_deploy must not run when deployed source mismatches")

    monkeypatch.setattr(OdooShService, "check_commit_deployed", fake_check)
    monkeypatch.setattr(OdooShService, "recent_build_failure", fake_recent)
    monkeypatch.setattr(OdooShService, "verify_user_file_hashes", fake_hashes)
    monkeypatch.setattr(OdooService, "module_deploy", fake_module_deploy)

    result = start_odoo_module_deploy_job(
        OdooModuleRequest(
            module_name="x_module",
            wait_timeout_seconds=2,
            poll_interval_seconds=0.25,
            deploy_guard_timeout_seconds=0,
            deploy_guard_poll_interval_seconds=0.25,
        ),
        enforce_deploy_guard=True,
    )

    assert result.status == "failed"
    assert result.result["job_status"] == "failed"
    assert "/home/odoo/src/user" in result.error
    assert result.result["job"]["result"]["result"]["status"] == "deploy_code_mismatch"


def test_public_module_deploy_runs_after_manual_push_guard_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    JsonStore().save_session_state({
        "last_git_push": {
            "sha": "abc1234",
            "started_epoch": 123,
            "module_names": ["x_module"],
            "expected_user_file_hashes": {"x_module/models/a.py": "0" * 64},
            "verified_for_module_deploy": False,
        }
    })
    calls = {"deploy": 0, "hash": 0}

    def fake_check(self, sha):
        return CommandResult(command=["odoosh", "check"], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

    def fake_recent(self, since_epoch):
        return CommandResult(command=["odoosh", "recent_build_failure"], cwd="odoosh:ssh", exit_code=0, stdout='{"failed": false}')

    def fake_hashes(self, expected):
        calls["hash"] += 1
        return CommandResult(command=["odoosh", "verify_user_file_hashes"], cwd="odoosh:ssh", exit_code=0, stdout="ODOOSH_USER_FILE_VERIFY_OK")

    def fake_module_deploy(self, module_name):
        calls["deploy"] += 1
        return OdooResult(status="ok", result={"module_name": module_name})

    monkeypatch.setattr(OdooShService, "check_commit_deployed", fake_check)
    monkeypatch.setattr(OdooShService, "recent_build_failure", fake_recent)
    monkeypatch.setattr(OdooShService, "verify_user_file_hashes", fake_hashes)
    monkeypatch.setattr(OdooService, "module_deploy", fake_module_deploy)

    result = start_odoo_module_deploy_job(
        OdooModuleRequest(
            module_name="x_module",
            wait_timeout_seconds=2,
            poll_interval_seconds=0.25,
            deploy_guard_timeout_seconds=0,
            deploy_guard_poll_interval_seconds=0.25,
        ),
        enforce_deploy_guard=True,
    )

    assert result.status == "ok"
    assert result.result["job_status"] == "passed"
    assert calls == {"deploy": 1, "hash": 1}
    assert JsonStore().load_session_state()["last_git_push"]["verified_for_module_deploy"] is True


def test_opencode_expected_sha_blocks_stale_odoosh_source(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr("odoo_agent_server.routers.odoo._prepare_validated_client", lambda payload, action: None)
    monkeypatch.setattr(
        OdooShService,
        "check_commit_deployed",
        lambda self, sha: CommandResult(
            command=["odoosh", "check", sha], cwd="odoosh:ssh", exit_code=1, stdout='{"deployed": false}'
        ),
    )
    monkeypatch.setattr(
        OdooService,
        "module_deploy",
        lambda self, module_name, **kwargs: (_ for _ in ()).throw(
            AssertionError("module deploy must wait for the exact OpenCode push")
        ),
    )

    result = start_odoo_module_deploy_job(
        OdooModuleRequest(
            module_name="x_module",
            expected_sha="abc1234",
            wait_timeout_seconds=2,
            poll_interval_seconds=0.25,
            deploy_guard_timeout_seconds=0,
        ),
        enforce_deploy_guard=True,
    )

    assert result.status == "failed"
    terminal = result.result["job"]["result"]["result"]
    assert terminal["status"] == "deployment_waiting"
    assert terminal["sha"] == "abc1234"
    assert terminal["next_call"]["arguments"]["expected_sha"] == "abc1234"


def test_opencode_expected_sha_allows_deploy_only_after_exact_commit(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr("odoo_agent_server.routers.odoo._prepare_validated_client", lambda payload, action: None)
    checks = []
    deploys = []

    def fake_check(self, sha):
        checks.append(sha)
        return CommandResult(command=["odoosh", "check", sha], cwd="odoosh:ssh", exit_code=0, stdout='{"deployed": true}')

    def fake_deploy(self, module_name, timeout_seconds=600):
        deploys.append(module_name)
        return OdooResult(status="ok", result={"module_name": module_name})

    monkeypatch.setattr(OdooShService, "check_commit_deployed", fake_check)
    monkeypatch.setattr(OdooService, "module_deploy", fake_deploy)

    result = start_odoo_module_deploy_job(
        OdooModuleRequest(
            module_name="x_module",
            expected_sha="def5678",
            wait_timeout_seconds=2,
            poll_interval_seconds=0.25,
            deploy_guard_timeout_seconds=0,
        ),
        enforce_deploy_guard=True,
    )

    assert result.status == "ok"
    assert checks == ["def5678"]
    assert deploys == ["x_module"]
    assert JsonStore().load_session_state()["opencode_expected_sha"]["deployed"] is True


def test_odoosh_module_deploy_ssh_uses_upgrade_for_installed_module(monkeypatch):
    def fake_module_status(self, module_name):
        return OdooResult(status="ok", result=[{"name": module_name, "state": "installed"}])

    called = {}

    def fake_run_module_cli(self, module_name, flag, timeout_seconds):
        called["module_name"] = module_name
        called["flag"] = flag
        return CommandResult(command=["odoo-bin", flag, module_name], cwd="odoosh:ssh", exit_code=0, stdout="OK")

    monkeypatch.setattr(OdooService, "module_status", fake_module_status)
    monkeypatch.setattr(OdooShService, "_dependency_guard", lambda self, module_name: None)
    monkeypatch.setattr(OdooShService, "_run_module_cli", fake_run_module_cli)

    result = OdooShService().module_deploy_ssh("sample_custom_module")

    assert result.exit_code == 0
    assert called == {"module_name": "sample_custom_module", "flag": "-u"}
    assert "mode=upgrade" in result.stdout


def test_odoosh_module_deploy_ssh_uses_install_for_uninstalled_module(monkeypatch):
    def fake_module_status(self, module_name):
        return OdooResult(status="ok", result=[{"name": module_name, "state": "uninstalled"}])

    called = {}

    def fake_run_module_cli(self, module_name, flag, timeout_seconds):
        called["flag"] = flag
        return CommandResult(command=["odoo-bin", flag, module_name], cwd="odoosh:ssh", exit_code=0, stdout="OK")

    monkeypatch.setattr(OdooService, "module_status", fake_module_status)
    monkeypatch.setattr(OdooShService, "_dependency_guard", lambda self, module_name: None)
    monkeypatch.setattr(OdooShService, "_run_module_cli", fake_run_module_cli)

    result = OdooShService().module_deploy_ssh("new_module")

    assert result.exit_code == 0
    assert called["flag"] == "-i"
    assert "mode=install" in result.stdout


def test_odoosh_module_deploy_reuses_dependency_probe_module_state(monkeypatch):
    calls = {"dependencies": 0, "module_status": 0}

    def fake_dependencies(self, module_name):
        calls["dependencies"] += 1
        return OdooResult(
            status="ok",
            result={"module": module_name, "state": "installed", "missing": []},
        )

    def unexpected_module_status(self, module_name):
        calls["module_status"] += 1
        raise AssertionError("module state should be reused from dependency preflight")

    def fake_run_module_cli(self, module_name, flag, timeout_seconds):
        return CommandResult(command=["odoo-bin", flag, module_name], cwd="odoosh:ssh", exit_code=0, stdout="OK")

    monkeypatch.setattr(OdooService, "module_dependencies_status", fake_dependencies)
    monkeypatch.setattr(OdooService, "module_status", unexpected_module_status)
    monkeypatch.setattr(OdooShService, "_run_module_cli", fake_run_module_cli)

    result = OdooShService().module_deploy_ssh("sample_custom_module")

    assert result.exit_code == 0
    assert result.command == ["odoosh.module_deploy_ssh", "sample_custom_module", "-u"]
    assert calls == {"dependencies": 1, "module_status": 0}


def test_non_strict_registry_smoke_reuses_status_without_model_discovery(monkeypatch):
    status = OdooResult(status="ok", result=[{"name": "x_module", "state": "installed"}])

    def unexpected_query(*args, **kwargs):
        raise AssertionError("non-strict empty smoke test must not start discovery queries")

    monkeypatch.setattr(OdooService, "module_status", unexpected_query)
    monkeypatch.setattr(OdooService, "search_read", unexpected_query)

    result = OdooService().registry_smoke_test(
        "x_module",
        [],
        [],
        strict=False,
        module_status_result=status,
    )

    assert result.status == "ok"
    assert result.result["module_status"]["result"][0]["state"] == "installed"
    assert result.result["models"] == []
