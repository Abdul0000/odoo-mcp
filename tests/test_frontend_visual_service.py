from pathlib import Path

import pytest
from fastapi import HTTPException

from odoo_agent_server.schemas import FrontendVisualImplementRequest
from odoo_agent_server.services.frontend_visual_service import FrontendVisualService


def request(tmp_path: Path, **overrides):
    values = {
        "project_path": str(tmp_path),
        "module_name": "pos_dynamic_theme",
        "target_area": "pos",
        "design_goal": "Match the premium trolley pipe reference",
        "component_type": "border",
        "allowed_methods": ["svg_asset"],
        "asset_name": "trolley_pipe",
        "target_files": [
            "pos_dynamic_theme/static/src/css/pos_theme_trolley.scss",
            "pos_dynamic_theme/static/src/css/pos_theme_trolley_final_match.scss",
        ],
    }
    values.update(overrides)
    return FrontendVisualImplementRequest(**values)


def test_build_writes_uses_existing_trolley_stylesheet(tmp_path):
    target = tmp_path / "pos_dynamic_theme/static/src/css/pos_theme_trolley_final_match.scss"
    target.parent.mkdir(parents=True)
    target.write_text(".existing { color: red; }\n", encoding="utf-8")

    writes, plan = FrontendVisualService().build_writes(request(tmp_path), "pos_dynamic_theme")

    assert plan["css_file"] == "pos_dynamic_theme/static/src/css/pos_theme_trolley_final_match.scss"
    css_write = writes[-1]
    assert ".existing { color: red; }" in css_write.content
    assert "BEGIN GENERATED VISUAL: trolley_pipe" in css_write.content
    assert len(writes) == 4


def test_explicit_css_target_can_be_module_relative(tmp_path):
    payload = request(tmp_path, css_file_path="static/src/css/custom.scss", target_files=[])
    writes, plan = FrontendVisualService().build_writes(payload, "pos_dynamic_theme")
    assert plan["css_file"] == "pos_dynamic_theme/static/src/css/custom.scss"
    assert writes[-1].relative_path == plan["css_file"]


def test_generated_block_is_replaced_not_duplicated(tmp_path):
    service = FrontendVisualService()
    first = service._merge_generated_css(".base {}", "trolley_pipe", ".one {}")
    second = service._merge_generated_css(first, "trolley_pipe", ".two {}")
    assert second.count("BEGIN GENERATED VISUAL: trolley_pipe") == 1
    assert ".one {}" not in second
    assert ".two {}" in second


def test_variant_filename_keeps_single_svg_extension(tmp_path):
    payload = request(tmp_path, variants=[{"name": "active", "file_name": "custom-active.svg"}])
    writes, plan = FrontendVisualService().build_writes(payload, "pos_dynamic_theme")
    assert plan["assets"] == ["pos_dynamic_theme/static/src/img/custom-active.svg"]
    assert writes[0].relative_path.endswith("custom-active.svg")
    assert not writes[0].relative_path.endswith("_svg.svg")


def test_css_target_rejects_parent_traversal(tmp_path):
    payload = request(tmp_path, css_file_path="../outside.scss")
    with pytest.raises(HTTPException):
        FrontendVisualService().build_writes(payload, "pos_dynamic_theme")
