from odoo_agent_server.schemas import CommandResult
from odoo_agent_server.services.odoosh_service import OdooShService


def test_changed_files_and_manifest_share_one_ssh_call(monkeypatch):
    service = OdooShService()
    calls = []
    monkeypatch.setattr(service, "_user_addons_path", lambda: "/home/odoo/src/user")

    def fake_run(script, timeout_seconds=45):
        calls.append(script)
        return CommandResult(command=["ssh"], cwd="odoosh:ssh", exit_code=0, stdout="ODOOSH_DEPLOY_VERIFY_OK")

    monkeypatch.setattr(service, "run_shell", fake_run)
    result = service.verify_deployed_changes(
        {"x_module/__manifest__.py": "a" * 64},
        module_name="x_module",
        verify_manifest=True,
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    assert "sha256sum" in calls[0]
    assert "__manifest__.py" in calls[0]
    assert "MODULE_MANIFEST_OK" in calls[0]
