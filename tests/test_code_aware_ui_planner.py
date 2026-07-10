from pathlib import Path

from odoo_agent_server.services.code_aware_ui_planner import CodeAwareUiPlanner


def test_code_aware_planner_derives_trolley_preconditions(tmp_path: Path):
    module = tmp_path / "pos_custom_trolley" / "static" / "src" / "xml"
    module.mkdir(parents=True)
    (module / "trolley.xml").write_text(
        '<templates><t t-name="X.Trolley"><div class="pos-theme-trolley-container">'
        '<button t-on-click="packOrder">Pack</button></div></t></templates>',
        encoding="utf-8",
    )
    plan = CodeAwareUiPlanner().plan(
        project_path=str(tmp_path),
        module_name="pos_custom_trolley",
        feature_goal="verify trolley after products are added",
        required_selectors=[".pos-theme-trolley-container"],
        profile="pos_product_screen",
    )
    assert plan["status"] == "planned"
    assert plan["scenario"] == "trolley_with_products"
    assert plan["product_count"] == 2
    assert plan["actions"][0]["action"] == "click_visible_product"
    assert "packOrder" in plan["evidence"]["handlers"]


def test_code_aware_planner_rejects_module_outside_project(tmp_path: Path):
    plan = CodeAwareUiPlanner().plan(
        project_path=str(tmp_path), module_name="../other", feature_goal="test", required_selectors=[], profile="pos_product_screen"
    )
    assert plan["status"] == "failed"
