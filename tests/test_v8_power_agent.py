from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers import workflow
from odoo_agent_server.services.lock_service import RepoLockService
from odoo_agent_server.services.odoosh_service import OdooShService
from odoo_agent_server.schemas import BrowserCheckResult, CommandResult, OdooResult, PosFlowCheckResult, PosFlowStepResult, RestoreLastGoodCommitRequest, SafeDeployModuleRequest
from odoo_agent_server.services.validator_service import OdooModuleValidator
from odoo_agent_server.schemas import Severity


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"}


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
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


def test_safe_deploy_request_has_no_browser_ui_contract(tmp_path):
    payload = SafeDeployModuleRequest(
        project_path=str(tmp_path),
        module_name="x_module",
        commit_message="Deploy x module",
    )
    assert "verify_ui_paths" not in payload.model_fields
    assert "verify_ui_screenshot_prefix" not in payload.model_fields
    assert "verify_ui_timeout_seconds" not in payload.model_fields


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


def test_error_pack_structures_invalid_field_and_rejects_xml_eval_hint(monkeypatch):
    service = OdooShService()
    sample = (
        "odoo.tools.convert.ParseError: while parsing /home/odoo/src/user/x/data/data.xml:313\n"
        "ValueError: Invalid field 'total_copies' on model 'school.library.book'\n"
    )
    monkeypatch.setattr(
        service,
        "grep_errors",
        lambda tail_lines: CommandResult(
            command=["grep"], cwd="odoosh:ssh", exit_code=0, stdout=sample
        ),
    )

    result = service.error_pack(50)

    assert '"field": "total_copies"' in result.stdout
    assert '"model": "school.library.book"' in result.stdout
    assert "do not change the XML value syntax or add eval" in result.stdout


def test_restore_latest_commit_uses_git_inverse_bumps_manifest_and_hands_off_deploy(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    module = repo / "custom_website_home"
    module.mkdir()
    manifest = module / "__manifest__.py"
    homepage = module / "homepage.xml"
    manifest.write_text("{'name': 'Home', 'version': '18.0.1.0.0', 'installable': True}\n", encoding="utf-8")
    homepage.write_text("<odoo><template id='home'>Old home</template></odoo>\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "old home"], cwd=repo, check=True, stdout=subprocess.PIPE)
    homepage.write_text("<odoo><template id='home'>Unwanted redesign</template></odoo>\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "homepage redesign"], cwd=repo, check=True, stdout=subprocess.PIPE)
    bad_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    monkeypatch.setattr(workflow.GitService, "validate_target", lambda self, *args, **kwargs: CommandResult(command=["git", "validate"], cwd=str(repo), exit_code=0))
    monkeypatch.setattr(workflow.GitService, "push", lambda self, *args, **kwargs: CommandResult(command=["git", "push"], cwd=str(repo), exit_code=0, stdout="PUSH_OK"))

    class QaPassed:
        status = "passed"

        def model_dump(self, mode=None):
            return {"status": "passed"}

    monkeypatch.setattr(workflow, "self_run", lambda payload: QaPassed())
    monkeypatch.setattr(
        workflow,
        "start_odoo_module_deploy_job",
        lambda *args, **kwargs: OdooResult(status="failed", error="running", result={"job_id": "deploy-job", "status": "running"}),
    )

    result = workflow.restore_last_good_commit(RestoreLastGoodCommitRequest(
        project_path=str(repo),
        module_name="custom_website_home",
        revert_latest=True,
        bump_manifest_version=True,
        deploy_module=True,
    ))

    assert result.status == "running"
    assert result.job_id == "deploy-job"
    assert result.summary["bad_sha"] == bad_sha
    assert result.summary["parent_file_reconstruction"] is False
    assert "Old home" in homepage.read_text(encoding="utf-8")
    assert "Unwanted redesign" not in homepage.read_text(encoding="utf-8")
    assert "18.0.1.0.1" in manifest.read_text(encoding="utf-8")
    changed = subprocess.run(["git", "show", "--name-only", "--format="], cwd=repo, check=True, capture_output=True, text=True).stdout
    assert "custom_website_home/homepage.xml" in changed
    assert "custom_website_home/__manifest__.py" in changed


def test_validator_nested_one2many_no_false_positive_and_action_order(tmp_path):
    module = tmp_path / "sample_relational_module"
    (module / "models").mkdir(parents=True)
    (module / "views").mkdir()
    (module / "security").mkdir()
    (module / "__init__.py").write_text("from . import models\n", encoding="utf-8")
    (module / "models" / "__init__.py").write_text("from . import m\n", encoding="utf-8")
    (module / "models" / "m.py").write_text(
        "from odoo import fields, models\n"
        "class Parent(models.Model):\n"
        "    _name = 'sample.parent'\n"
        "    name = fields.Char()\n"
        "    line_ids = fields.One2many('sample.line', 'parent_id')\n"
        "class Line(models.Model):\n"
        "    _name = 'sample.line'\n"
        "    parent_id = fields.Many2one('sample.parent')\n"
        "    amount = fields.Float()\n",
        encoding="utf-8",
    )
    (module / "security" / "ir.model.access.csv").write_text(
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_parent,sample.parent,model_sample_parent,base.group_user,1,1,1,0\n"
        "access_line,sample.line,model_sample_line,base.group_user,1,1,1,0\n",
        encoding="utf-8",
    )
    (module / "views" / "menus.xml").write_text(
        "<odoo><menuitem id='menu_parent' name='Parent' action='action_parent'/></odoo>", encoding="utf-8"
    )
    (module / "views" / "views.xml").write_text(
        "<odoo>"
        "<record id='view_parent' model='ir.ui.view'><field name='name'>x</field><field name='model'>sample.parent</field>"
        "<field name='arch' type='xml'><form><field name='line_ids'><list><field name='amount'/></list></field></form></field></record>"
        "<record id='action_parent' model='ir.actions.act_window'><field name='name'>Parent</field><field name='res_model'>sample.parent</field></record>"
        "</odoo>",
        encoding="utf-8",
    )
    (module / "__manifest__.py").write_text(
        "{'name':'Sample Relational Module','version':'1.0','depends':['base'],'data':['security/ir.model.access.csv','views/menus.xml','views/views.xml']}",
        encoding="utf-8",
    )
    findings = OdooModuleValidator().validate(module)
    codes = [f.code for f in findings]
    assert "VIEW_FIELD_NOT_FOUND_STATIC" not in codes
    assert "MENU_ACTION_DEFINED_LATE" in codes
    assert any(f.severity == Severity.warning for f in findings if f.code == "MENU_ACTION_DEFINED_LATE")
