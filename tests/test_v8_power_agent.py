from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers import workflow
from odoo_agent_server.services.lock_service import RepoLockService
from odoo_agent_server.services.odoosh_service import OdooShService
from odoo_agent_server.schemas import BrowserCheckResult, PosFlowCheckResult, PosFlowStepResult, SafeDeployModuleRequest
from odoo_agent_server.services.validator_service import OdooModuleValidator
from odoo_agent_server.schemas import Severity


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"}


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, stdout=subprocess.PIPE)


def test_new_v8_tools_are_exposed():
    client = TestClient(app)
    response = client.get("/tools/list", headers=_auth())
    names = {item["name"] for item in response.json()}
    assert "workflow_start_session" in names
    assert "workflow_registry_smoke_test" in names
    assert "workflow_restore_last_good_commit" in names
    assert "workflow_browser_menu_crawl" in names
    assert "odoosh_error_pack" in names


def test_safe_deploy_request_accepts_ui_paths(tmp_path):
    payload = SafeDeployModuleRequest(
        project_path=str(tmp_path),
        module_name="x_module",
        commit_message="Deploy x module",
        verify_ui_paths=["/web#action=1&model=sale.order&view_type=form", "/web#menu_id=2"],
    )
    assert payload.verify_ui_paths[0].startswith("/web#action=")
    assert payload.verify_ui_timeout_seconds == 45


def test_pos_flow_result_preserves_console_all_fields():
    result = PosFlowCheckResult(
        status="ok",
        console_all=["log: boot ok"],
        steps=[PosFlowStepResult(step="open_pos", status="ok", console_all=["debug: ready"])],
    )
    payload = result.model_dump(mode="json")

    assert payload["console_all"] == ["log: boot ok"]
    assert payload["steps"][0]["console_all"] == ["debug: ready"]


def test_browser_menu_crawl_fails_on_odoo_ui_breakage(monkeypatch):
    class FakeCdpBrowserService:
        async def screenshot_odoo_page(self, *args, **kwargs):
            return BrowserCheckResult(
                status="failed",
                url="https://example.test/web#action=1",
                title="Odoo",
                screenshot_path="/tmp/odoo.png",
                console_errors=["odoo_ui_error_text:shell_ERROR"],
                network_errors=[],
                error="odoo_ui_error_text:shell_ERROR",
            )

    monkeypatch.setattr(workflow, "CdpBrowserService", FakeCdpBrowserService)
    client = TestClient(app)
    response = client.post(
        "/workflow/browser-menu-crawl",
        headers=_auth(),
        json={"paths": ["/web#action=1"], "timeout_seconds": 10},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["steps"][0]["status"] == "failed"


def test_repo_lock_blocks_second_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    lock = RepoLockService(stale_after_seconds=999)
    first = lock.acquire(str(repo), owner="a", timeout_seconds=1)
    assert first.exit_code == 0
    second = lock.acquire(str(repo), owner="b", timeout_seconds=1)
    assert second.exit_code == 124
    released = lock.release(str(repo), owner="a")
    assert released.exit_code == 0


def test_error_pack_extracts_hints():
    service = OdooShService()
    sample = """
__ODOO_MCP_LOG_FILE__:~/logs/odoo.log
2026-01-01 ERROR db odoo.modules.loading: Failed to load registry
Traceback (most recent call last):
  File "/home/odoo/src/user/x/models/a.py", line 1, in <module>
    import missing_lib
ModuleNotFoundError: No module named 'missing_lib'
"""
    blocks = service._extract_error_blocks(sample)
    assert blocks and "ModuleNotFoundError" in blocks[0]


def test_validator_nested_one2many_no_false_positive_and_action_order(tmp_path):
    module = tmp_path / "school_x"
    (module / "models").mkdir(parents=True)
    (module / "views").mkdir()
    (module / "security").mkdir()
    (module / "__init__.py").write_text("from . import models\n", encoding="utf-8")
    (module / "models" / "__init__.py").write_text("from . import m\n", encoding="utf-8")
    (module / "models" / "m.py").write_text(
        "from odoo import fields, models\n"
        "class Parent(models.Model):\n"
        "    _name = 'school.parent'\n"
        "    name = fields.Char()\n"
        "    line_ids = fields.One2many('school.line', 'parent_id')\n"
        "class Line(models.Model):\n"
        "    _name = 'school.line'\n"
        "    parent_id = fields.Many2one('school.parent')\n"
        "    amount = fields.Float()\n",
        encoding="utf-8",
    )
    (module / "security" / "ir.model.access.csv").write_text(
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_parent,school.parent,model_school_parent,base.group_user,1,1,1,0\n"
        "access_line,school.line,model_school_line,base.group_user,1,1,1,0\n",
        encoding="utf-8",
    )
    (module / "views" / "menus.xml").write_text(
        "<odoo><menuitem id='menu_parent' name='Parent' action='action_parent'/></odoo>", encoding="utf-8"
    )
    (module / "views" / "views.xml").write_text(
        "<odoo>"
        "<record id='view_parent' model='ir.ui.view'><field name='name'>x</field><field name='model'>school.parent</field>"
        "<field name='arch' type='xml'><form><field name='line_ids'><list><field name='amount'/></list></field></form></field></record>"
        "<record id='action_parent' model='ir.actions.act_window'><field name='name'>Parent</field><field name='res_model'>school.parent</field></record>"
        "</odoo>",
        encoding="utf-8",
    )
    (module / "__manifest__.py").write_text(
        "{'name':'School X','version':'1.0','depends':['base'],'data':['security/ir.model.access.csv','views/menus.xml','views/views.xml']}",
        encoding="utf-8",
    )
    findings = OdooModuleValidator().validate(module)
    codes = [f.code for f in findings]
    assert "VIEW_FIELD_NOT_FOUND_STATIC" not in codes
    assert "MENU_ACTION_DEFINED_LATE" in codes
    assert any(f.severity == Severity.warning for f in findings if f.code == "MENU_ACTION_DEFINED_LATE")
