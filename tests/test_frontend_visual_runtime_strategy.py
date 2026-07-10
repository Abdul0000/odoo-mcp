from pathlib import Path

import pytest
from fastapi import HTTPException

from odoo_agent_server.schemas import FrontendVisualImplementRequest
from odoo_agent_server.services.frontend_visual_service import FrontendVisualService


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
