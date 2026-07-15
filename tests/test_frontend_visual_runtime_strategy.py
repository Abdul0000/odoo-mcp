from pathlib import Path
import subprocess

import pytest
from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.schemas import FrontendVisualImplementRequest, OdooCodeChangeRequest
from odoo_agent_server.routers.tools import _ensure_root_module_scaffold, _is_legacy_placeholder_module
from odoo_agent_server.services.frontend_visual_service import FrontendVisualService
from odoo_agent_server.services.repo_service import RepoService


def _request(tmp_path: Path, goal: str, methods=None):
    css = tmp_path / "x_module" / "static" / "src" / "css" / "visual.scss"
    css.parent.mkdir(parents=True, exist_ok=True)
    css.write_text("", encoding="utf-8")
    return FrontendVisualImplementRequest(
        project_path=str(tmp_path),
        module_name="x_module",
        design_goal=goal,
        allowed_methods=methods or [],
        css_file_path="x_module/static/src/css/visual.scss",
    )


def test_dynamic_pipe_uses_scss_at_runtime(tmp_path):
    writes, plan = FrontendVisualService().build_writes(
        _request(tmp_path, "Create an expandable trolley pipe that follows content height"),
        "x_module",
    )
    assert plan["implementation_type"] == "scss"
    assert all(not write.relative_path.endswith(".svg") for write in writes)


def test_allowed_methods_is_strict_allow_list(tmp_path):
    writes, plan = FrontendVisualService().build_writes(
        _request(tmp_path, "Create an expandable trolley pipe", ["scss"]),
        "x_module",
    )
    assert plan["implementation_type"] == "scss"
    assert len(writes) == 1


def test_dynamic_visual_rejects_svg_only(tmp_path):
    with pytest.raises(HTTPException, match="requires SCSS"):
        FrontendVisualService().build_writes(
            _request(tmp_path, "Create a dynamic expandable trolley rail", ["svg_asset"]),
            "x_module",
        )


def test_fixed_logo_can_use_svg(tmp_path):
    writes, plan = FrontendVisualService().build_writes(
        _request(tmp_path, "Create a fixed artwork logo", ["svg_asset"]),
        "x_module",
    )
    assert plan["implementation_type"] == "svg_asset"
    assert any(write.relative_path.endswith(".svg") for write in writes)


def test_new_module_scaffold_is_created_only_at_checkout_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="new_visual_module",
        create_module=True,
        task_description="Create a standalone visual addon",
        writes=[
            {"relative_path": "__init__.py", "content": "from . import models\n"},
            {
                "relative_path": "__manifest__.py",
                "content": repr({
                    "name": "New Visual Module",
                    "depends": ["base"],
                    "data": ["views/visual_views.xml"],
                    "assets": {"web.assets_backend": ["new_visual_module/static/src/scss/visual.scss"]},
                    "installable": True,
                }) + "\n",
            },
            {"relative_path": "models/__init__.py", "content": "from . import visual\n"},
            {"relative_path": "models/visual.py", "content": "from odoo import models\n\nclass Visual(models.Model):\n    _name = 'new.visual'\n"},
            {"relative_path": "views/visual_views.xml", "content": "<odoo/>\n"},
            {"relative_path": "static/src/scss/visual.scss", "content": ".o_visual { color: red; }\n"},
        ],
    )

    _ensure_root_module_scaffold(RepoService(), tmp_path, payload)

    assert (tmp_path / "new_visual_module" / "__manifest__.py").exists()
    assert (tmp_path / "new_visual_module" / "__init__.py").exists()
    assert (tmp_path / "new_visual_module" / "models" / "visual.py").exists()
    assert (tmp_path / "new_visual_module" / "views" / "visual_views.xml").exists()


def test_new_module_incomplete_batch_leaves_checkout_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    (tmp_path / ".git").mkdir()
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="incomplete_module",
        create_module=True,
        task_description="Create a complete addon",
        writes=[
            {"relative_path": "__init__.py", "content": "from . import models\n"},
            {
                "relative_path": "__manifest__.py",
                "content": repr({"name": "Incomplete", "data": ["views/missing.xml"], "installable": True}) + "\n",
            },
        ],
    )

    with pytest.raises(HTTPException, match="missing complete files"):
        _ensure_root_module_scaffold(RepoService(), tmp_path, payload)

    assert not (tmp_path / "incomplete_module").exists()


def test_old_two_file_scaffold_is_detected_as_placeholder(tmp_path):
    module = tmp_path / "legacy_module"
    module.mkdir()
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        repr({"name": "Legacy Module", "depends": ["base"], "installable": True}) + "\n",
        encoding="utf-8",
    )

    assert _is_legacy_placeholder_module(module) is True

    (module / "__init__.py").write_text("from . import models\n", encoding="utf-8")
    assert _is_legacy_placeholder_module(module) is False


def test_new_module_scaffold_rejects_an_addon_as_project_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    outer = tmp_path / "existing_addon"
    (outer / ".git").mkdir(parents=True)
    (outer / "__manifest__.py").write_text("{'name': 'Existing'}\n", encoding="utf-8")
    payload = OdooCodeChangeRequest(
        project_path=str(outer),
        module_name="nested_addon",
        create_module=True,
        task_description="Create another addon",
    )

    with pytest.raises(HTTPException, match="points to an addon"):
        _ensure_root_module_scaffold(RepoService(), outer, payload)
