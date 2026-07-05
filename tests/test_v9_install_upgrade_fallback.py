from __future__ import annotations

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.odoo import start_odoo_module_deploy_job
from odoo_agent_server.schemas import CommandResult, OdooRpcResult
from odoo_agent_server.schemas import OdooRpcModuleRequest
from odoo_agent_server.services.approval_service import ApprovalService
from odoo_agent_server.services.odoo_rpc_service import OdooRpcService
from odoo_agent_server.services.odoosh_service import OdooShService


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
        return OdooRpcResult(status="failed", error="<Fault 3: 'Access Denied'>")

    def fake_ssh_deploy(self, module_name, timeout_seconds=600):
        return CommandResult(command=["odoosh.module_deploy_ssh", module_name], cwd="odoosh:ssh", exit_code=0, stdout="SSH_OK")

    monkeypatch.setattr(OdooRpcService, "execute_kw", fake_execute_kw)
    monkeypatch.setattr(OdooShService, "module_deploy_ssh", fake_ssh_deploy)

    result = OdooRpcService().module_deploy("karachi_school_management")

    assert result.status == "ok"
    assert result.result["fallback"] == "odoosh"
    assert result.result["operation"] == "deploy"
    assert result.result["ssh"]["stdout"] == "SSH_OK"


def test_module_deploy_falls_back_to_ssh_when_module_missing_after_update_list(monkeypatch):
    def fake_execute_kw(self, model, method, args=None, kwargs=None, superuser=False):
        if model == "ir.module.module" and method == "update_list":
            return OdooRpcResult(status="ok", result=True)
        if model == "ir.module.module" and method == "search_read":
            return OdooRpcResult(status="ok", result=[])
        raise AssertionError(f"unexpected XML-RPC call: {model}.{method}")

    def fake_ssh_deploy(self, module_name, timeout_seconds=600):
        return CommandResult(command=["odoosh.module_deploy_ssh", module_name], cwd="odoosh:ssh", exit_code=0, stdout="SSH_DEPLOY_OK")

    monkeypatch.setattr(OdooRpcService, "execute_kw", fake_execute_kw)
    monkeypatch.setattr(OdooShService, "module_deploy_ssh", fake_ssh_deploy)

    result = OdooRpcService().module_deploy("new_custom_module")

    assert result.status == "ok"
    assert result.result["fallback"] == "odoosh"
    assert result.result["operation"] == "deploy"
    assert "not found in ir.module.module" in result.result["cause"]
    assert result.result["ssh"]["stdout"] == "SSH_DEPLOY_OK"


def test_internal_module_deploy_job_does_not_require_second_approval(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "require_approval", True)
    monkeypatch.setattr(settings, "approval_required_actions_raw", "odoo.module_deploy")

    def fake_module_deploy(self, module_name):
        return OdooRpcResult(status="ok", result={"module_name": module_name})

    monkeypatch.setattr(OdooRpcService, "module_deploy", fake_module_deploy)

    result = start_odoo_module_deploy_job(OdooRpcModuleRequest(
        module_name="karachi_school_management",
        wait_timeout_seconds=2,
        poll_interval_seconds=0.25,
    ))

    assert result.status == "ok"
    assert result.result["finished"] is True
    assert result.result["job_status"] == "passed"


def test_odoosh_module_deploy_ssh_uses_upgrade_for_installed_module(monkeypatch):
    def fake_module_status(self, module_name):
        return OdooRpcResult(status="ok", result=[{"name": module_name, "state": "installed"}])

    called = {}

    def fake_run_module_cli(self, module_name, flag, timeout_seconds):
        called["module_name"] = module_name
        called["flag"] = flag
        return CommandResult(command=["odoo-bin", flag, module_name], cwd="odoosh:ssh", exit_code=0, stdout="OK")

    monkeypatch.setattr(OdooRpcService, "module_status", fake_module_status)
    monkeypatch.setattr(OdooShService, "_dependency_guard", lambda self, module_name: None)
    monkeypatch.setattr(OdooShService, "_run_module_cli", fake_run_module_cli)

    result = OdooShService().module_deploy_ssh("karachi_school_management")

    assert result.exit_code == 0
    assert called == {"module_name": "karachi_school_management", "flag": "-u"}
    assert "mode=upgrade" in result.stdout


def test_odoosh_module_deploy_ssh_uses_install_for_uninstalled_module(monkeypatch):
    def fake_module_status(self, module_name):
        return OdooRpcResult(status="ok", result=[{"name": module_name, "state": "uninstalled"}])

    called = {}

    def fake_run_module_cli(self, module_name, flag, timeout_seconds):
        called["flag"] = flag
        return CommandResult(command=["odoo-bin", flag, module_name], cwd="odoosh:ssh", exit_code=0, stdout="OK")

    monkeypatch.setattr(OdooRpcService, "module_status", fake_module_status)
    monkeypatch.setattr(OdooShService, "_dependency_guard", lambda self, module_name: None)
    monkeypatch.setattr(OdooShService, "_run_module_cli", fake_run_module_cli)

    result = OdooShService().module_deploy_ssh("new_module")

    assert result.exit_code == 0
    assert called["flag"] == "-i"
    assert "mode=install" in result.stdout
