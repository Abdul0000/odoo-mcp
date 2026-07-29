from __future__ import annotations

from pathlib import Path

import pytest

from odoo_agent_server.schemas import OdooResult, OdooTargetResolutionHint
from odoo_agent_server.services.existing_target_resolution_service import ExistingTargetResolutionService
from odoo_agent_server.services.odoo_jsonrpc_service import OdooJsonRpcService


@pytest.fixture(autouse=True)
def clear_resolution_cache():
    ExistingTargetResolutionService._cache.clear()
    yield
    ExistingTargetResolutionService._cache.clear()


def _module(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path, list[Path]]:
    project = tmp_path / "repo"
    module = project / "sample"
    module.mkdir(parents=True)
    paths = []
    for relative, content in files.items():
        path = module / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        paths.append(path)
    return project, module, paths


def _resolve(
    project: Path,
    module: Path,
    paths: list[Path],
    *,
    mode: str,
    kind: str,
    identifier: str,
    model: str | None = None,
    client=None,
):
    return ExistingTargetResolutionService().resolve(
        project=project,
        module_path=module,
        module_name="sample",
        task_description=f"Update existing {kind} {identifier}",
        target_hint=identifier,
        intent="modify_existing_feature" if mode == "existing" else "add_field",
        task_type="auto",
        change_request={},
        hint=OdooTargetResolutionHint(
            mode=mode,
            kind=kind,
            identifier=identifier,
            model=model,
        ),
        candidate_paths=paths,
        planned_paths=[],
        client=client,
        create_module=False,
    )


def test_existing_field_is_source_verified_without_forcing_live_odoo(tmp_path):
    project, module, paths = _module(tmp_path, {
        "models/res_partner.py": (
            "from odoo import fields, models\n"
            "class ResPartner(models.Model):\n"
            "    _inherit = 'res.partner'\n"
            "    x_note = fields.Char()\n"
        ),
    })

    result = _resolve(
        project, module, paths,
        mode="existing", kind="field", identifier="x_note", model="res.partner",
    )

    assert result.status == "source_verified_live_unverified"
    assert result.source_verified is True
    assert result.live_checked is False
    assert result.live_status == "unavailable"
    assert result.writes_blocked is False
    assert result.matches[0].role == "definition"


def test_ambiguous_existing_field_blocks_writes(tmp_path):
    source = (
        "from odoo import fields, models\n"
        "class Demo(models.Model):\n"
        "    _inherit = 'res.partner'\n"
        "    x_note = fields.Char()\n"
    )
    project, module, paths = _module(tmp_path, {
        "models/first.py": source,
        "models/second.py": source.replace("class Demo", "class DemoTwo"),
    })

    result = _resolve(
        project, module, paths,
        mode="existing", kind="field", identifier="x_note", model="res.partner",
    )

    assert result.status == "ambiguous"
    assert result.writes_blocked is True
    assert "Multiple targets" in (result.question or "")


def test_missing_existing_target_blocks_writes(tmp_path):
    project, module, paths = _module(tmp_path, {
        "models/res_partner.py": "from odoo import models\nclass ResPartner(models.Model):\n    _inherit = 'res.partner'\n",
    })

    result = _resolve(
        project, module, paths,
        mode="existing", kind="field", identifier="x_missing", model="res.partner",
    )

    assert result.status == "not_found"
    assert result.writes_blocked is True
    assert "exact technical identifier" in (result.question or "")


def test_broad_free_text_does_not_invent_a_technical_identifier(tmp_path):
    project, module, paths = _module(tmp_path, {
        "controllers/main.py": "from odoo import http\nclass Main(http.Controller):\n    pass\n",
    })

    result = ExistingTargetResolutionService().resolve(
        project=project,
        module_path=module,
        module_name="sample",
        task_description="Fix the existing controller bug",
        target_hint=None,
        intent="fix_bug",
        task_type="controller",
        change_request={},
        hint=OdooTargetResolutionHint(),
        candidate_paths=paths,
        planned_paths=[],
        client=None,
        create_module=False,
    )

    assert result.status == "scope_verified"
    assert result.identifier is None
    assert result.writes_blocked is False


def test_new_target_collision_blocks_but_absent_target_proceeds(tmp_path):
    project, module, paths = _module(tmp_path, {
        "models/res_partner.py": (
            "from odoo import fields, models\n"
            "class ResPartner(models.Model):\n"
            "    _inherit = 'res.partner'\n"
            "    x_existing = fields.Char()\n"
        ),
    })

    collision = _resolve(
        project, module, paths,
        mode="new", kind="field", identifier="x_existing", model="res.partner",
    )
    absent = _resolve(
        project, module, paths,
        mode="new", kind="field", identifier="x_new", model="res.partner",
    )

    assert collision.status == "collision"
    assert collision.writes_blocked is True
    assert absent.status == "source_verified_live_unverified"
    assert absent.writes_blocked is False


def test_live_field_lookup_is_bounded_and_cached(monkeypatch, tmp_path):
    project, module, paths = _module(tmp_path, {
        "models/res_partner.py": (
            "from odoo import fields, models\n"
            "class ResPartner(models.Model):\n"
            "    _inherit = 'res.partner'\n"
            "    x_note = fields.Char()\n"
        ),
    })
    calls = []
    monkeypatch.setattr(OdooJsonRpcService, "configured", lambda self: True)

    def fields_get(self, model, timeout_seconds=60):
        calls.append((model, timeout_seconds))
        return OdooResult(status="ok", result={"x_note": {"type": "char"}})

    monkeypatch.setattr(OdooJsonRpcService, "fields_get", fields_get)
    client = object()

    first = _resolve(
        project, module, paths,
        mode="existing", kind="field", identifier="x_note", model="res.partner", client=client,
    )
    second = _resolve(
        project, module, paths,
        mode="existing", kind="field", identifier="x_note", model="res.partner", client=client,
    )

    assert first.status == "verified"
    assert first.live_checked is True
    assert calls == [("res.partner", 5)]
    assert second.cache_hit is True
    assert calls == [("res.partner", 5)]
