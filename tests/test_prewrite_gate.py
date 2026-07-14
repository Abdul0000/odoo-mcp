from pathlib import Path
import subprocess
from uuid import uuid4

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.routers.tools import _safe_module_relative
from odoo_agent_server.services.hybrid_change_service import HybridChangeService


def _auth():
    return {"Authorization": f"Bearer {settings.api_token}"}


def _make_module(project: Path) -> Path:
    module = project / "x_module"
    (module / "models").mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'X Module', 'installable': True}\n", encoding="utf-8")
    (module / "models" / "a.py").write_text("from odoo import models\n\nclass X(models.Model):\n    _name='x.model'\n", encoding="utf-8")
    return module


def test_prewrite_gate_blocks_existing_addon_until_custom_read_and_scope_advice(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    module = _make_module(project)
    client = TestClient(app)

    blocked = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/__manifest__.py", "content": "{'name':'Changed'}\n"},
    )
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert detail["status"] == "prewrite_gate_blocked"
    assert detail["blocked"][0]["reason"] == "custom_module_not_read"

    read = client.post(
        "/repo/read",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/__manifest__.py"},
    )
    assert read.status_code == 200

    blocked_scope = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/__manifest__.py", "content": "{'name':'Changed'}\n"},
    )
    assert blocked_scope.status_code == 409
    assert blocked_scope.json()["detail"]["blocked"][0]["reason"] == "scope_advice_missing"

    scope = client.post(
        "/workflow/scope-advice",
        headers=_auth(),
        json={
            "custom_module_name": "x_module",
            "custom_file_paths": [str(module / "__manifest__.py")],
            "task_description": "edit manifest author",
        },
    )
    assert scope.status_code == 200

    allowed = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/__manifest__.py", "content": "{'name':'Changed'}\n"},
    )
    assert allowed.status_code == 200
    assert "Changed" in (module / "__manifest__.py").read_text(encoding="utf-8")


def test_hybrid_change_patch_is_scoped_idempotent_and_detects_diff_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "target_project_path", None)
    monkeypatch.delenv("ODOO_CLIENTS", raising=False)
    project = tmp_path / "repo"
    project.mkdir()
    module = _make_module(project)
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.local"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)
    client = TestClient(app)

    assert client.post(
        "/repo/read",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/__manifest__.py"},
    ).status_code == 200
    assert client.post(
        "/workflow/scope-advice",
        headers=_auth(),
        json={
            "custom_module_name": "x_module",
            "custom_file_paths": [str(module / "__manifest__.py")],
            "task_description": "change module label",
        },
    ).status_code == 200

    change_id = f"chg_{uuid4().hex}"
    prepared_identity = HybridChangeService.checkout_identity(str(project))
    HybridChangeService().save(change_id, {
        "change_id": change_id,
        "project_path": str(project),
        "module_name": "x_module",
        "task_description": "change module label",
        "evidence_ready": True,
        "permitted_paths": ["x_module/__manifest__.py"],
        "prepared_branch": prepared_identity["branch"],
        "prepared_sha": prepared_identity["sha"],
    })
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "repo_patch_change",
            "arguments": {
                "change_id": change_id,
                "edits": [{"relative_path": "x_module/__manifest__.py", "search": "X Module", "replace": "Hybrid Module"}],
            },
        },
    }
    probe_call = {**call, "params": {**call["params"], "arguments": {"change_id": change_id, "edits": []}}}
    probe = client.post("/mcp", headers=_auth(), json=probe_call)
    assert probe.status_code == 200
    assert probe.json()["result"]["structuredContent"]["status"] == "awaiting_edits"
    assert probe.json()["result"]["structuredContent"]["permitted_paths"] == ["x_module/__manifest__.py"]

    outside_call = {
        **call,
        "params": {
            **call["params"],
            "arguments": {
                "change_id": change_id,
                "edits": [{"relative_path": "x_module/models/a.py", "content": "from odoo import models\n"}],
            },
        },
    }
    outside = client.post("/mcp", headers=_auth(), json=outside_call)
    assert outside.status_code == 200
    assert "patch_scope_mismatch" in outside.json()["result"]["content"][0]["text"]

    first = client.post("/mcp", headers=_auth(), json=call)
    assert first.status_code == 200
    assert first.json()["result"]["structuredContent"]["status"] == "patched"
    assert "Hybrid Module" in (module / "__manifest__.py").read_text(encoding="utf-8")

    second = client.post("/mcp", headers=_auth(), json=call)
    assert second.status_code == 200
    assert second.json()["result"]["structuredContent"]["status"] == "already_applied"

    try:
        HybridChangeService().validate_for_deploy(
            change_id=change_id,
            client_name=None,
            project_path=str(project),
            module_name="x_module",
            expected_paths=["x_module/models/a.py"],
        )
        assert False, "deploy path set must exactly match the prepared patch"
    except Exception as exc:
        assert "Deploy paths do not match" in str(exc)

    (module / "models" / "untracked.py").write_text("# unrelated untracked mutation\n", encoding="utf-8")
    try:
        HybridChangeService().validate_for_deploy(
            change_id=change_id,
            client_name=None,
            project_path=str(project),
            module_name="x_module",
        )
        assert False, "diff mutation must invalidate prepared deploy evidence"
    except Exception as exc:
        assert "diff changed" in str(exc)


def test_hybrid_path_scope_preserves_nested_addons_directory():
    assert _safe_module_relative("x_module", "models/a.py", "addons/custom/x_module") == "addons/custom/x_module/models/a.py"
    assert _safe_module_relative("x_module", "x_module/views/a.xml", "addons/custom/x_module") == "addons/custom/x_module/views/a.xml"


def test_hybrid_change_rejects_branch_change_and_invalidates_verification_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    project.mkdir()
    _make_module(project)
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.local"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)

    service = HybridChangeService()
    change_id = f"chg_{uuid4().hex}"
    identity = service.checkout_identity(str(project))
    context = {
        "change_id": change_id,
        "project_path": str(project),
        "module_name": "x_module",
        "prepared_branch": identity["branch"],
        "prepared_sha": identity["sha"],
    }
    service.save(change_id, context)
    first_fingerprint = service.verification_fingerprint(str(project), ["x_module/__manifest__.py"], True)
    service.store_verification(change_id, first_fingerprint, {"verified_files": 1})
    assert service.cached_verification(change_id, first_fingerprint) is not None

    (project / "x_module" / "__manifest__.py").write_text("{'name': 'Mutated'}\n", encoding="utf-8")
    second_fingerprint = service.verification_fingerprint(str(project), ["x_module/__manifest__.py"], True)
    assert second_fingerprint != first_fingerprint
    assert service.cached_verification(change_id, second_fingerprint) is None

    subprocess.run(["git", "checkout", "-b", "other"], cwd=project, check=True, capture_output=True)
    try:
        service.validate_checkout_identity(context, str(project))
        assert False, "prepared change must be bound to its branch"
    except Exception as exc:
        assert "Checkout branch changed" in str(exc)


def test_successful_terminal_hybrid_change_is_not_returned_as_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    service = HybridChangeService()
    change_id = f"chg_{uuid4().hex}"
    service.save(change_id, {
        "change_id": change_id,
        "project_path": str(tmp_path / "repo"),
        "prepared_at": "2026-07-14T00:00:00+00:00",
        "deploy_terminal": True,
        "deploy_succeeded": True,
    })
    assert service.latest(project_path=str(tmp_path / "repo")) is None


def test_prewrite_gate_does_not_block_non_addon_helper_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    client = TestClient(app)

    response = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "notes.txt", "content": "ok\n"},
    )
    assert response.status_code == 200


def test_prewrite_gate_rejects_addon_nested_inside_existing_addon(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    project.mkdir()
    _make_module(project)
    client = TestClient(app)

    response = client.post(
        "/repo/write",
        headers=_auth(),
        json={
            "project_path": str(project),
            "relative_path": "x_module/nested_module/__manifest__.py",
            "content": "{'name': 'Nested'}\n",
        },
    )

    assert response.status_code == 400
    assert "Nested Odoo addon rejected" in response.json()["detail"]
    assert not (project / "x_module" / "nested_module" / "__manifest__.py").exists()


def test_prewrite_gate_ui_requires_scope_after_target_read(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    module = _make_module(project)
    (module / "views").mkdir()
    target = module / "views" / "template.xml"
    target.write_text("<odoo><template id='x'>Prices are inclusive</template></odoo>\n", encoding="utf-8")
    client = TestClient(app)

    blocked = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/views/template.xml", "content": "<odoo/>\n"},
    )
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert detail["blocked"][0]["reason"] == "custom_module_not_read"
    assert detail["gate_modes"] == ["mandatory_dual_read"]

    read = client.post(
        "/repo/read",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/views/template.xml"},
    )
    assert read.status_code == 200

    blocked_scope = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/views/template.xml", "content": "<odoo><template id='x'><b style='color:red'>Prices are inclusive</b></template></odoo>\n"},
    )
    assert blocked_scope.status_code == 409
    assert blocked_scope.json()["detail"]["blocked"][0]["reason"] == "scope_advice_missing"

    scope = client.post(
        "/workflow/scope-advice",
        headers=_auth(),
        json={
            "custom_module_name": "x_module",
            "custom_file_paths": [str(target)],
            "task_description": "style template text",
        },
    )
    assert scope.status_code == 200

    allowed = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/views/template.xml", "content": "<odoo><template id='x'><b style='color:red'>Prices are inclusive</b></template></odoo>\n"},
    )
    assert allowed.status_code == 200
    assert "color:red" in target.read_text(encoding="utf-8")


def test_prewrite_gate_models_still_requires_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    project = tmp_path / "repo"
    project.mkdir()
    _make_module(project)
    client = TestClient(app)

    read = client.post(
        "/repo/read",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/models/a.py"},
    )
    assert read.status_code == 200
    blocked = client.post(
        "/repo/write",
        headers=_auth(),
        json={"project_path": str(project), "relative_path": "x_module/models/a.py", "content": "from odoo import models\n"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["blocked"][0]["reason"] == "scope_advice_missing"
