from __future__ import annotations

from pathlib import Path

from odoo_agent_server.schemas import SmartToolRouteRequest, SyntaxVerifyRequest
from odoo_agent_server.routers.workflow import smart_tool_router
from odoo_agent_server.services.frontend_pattern_service import FrontendPatternService
from odoo_agent_server.services.repo_index_service import RepoIndexService
from odoo_agent_server.services.syntax_verifier_service import SyntaxVerifierService


def test_smart_router_uses_direct_query_for_data_lookup():
    result = smart_tool_router(SmartToolRouteRequest(task_description="list 5 draft invoices"))
    assert result.task_type == "odoo_data_query"
    assert result.recommended_write_tool == "none"
    assert result.steps[0].tool == "odoo.search_read"


def test_smart_router_frontend_uses_preflight_and_smart_write():
    result = smart_tool_router(SmartToolRouteRequest(
        task_description="fix POS OWL popup JS",
        planned_file_paths=["my_mod/static/src/js/popup.js"],
    ))
    assert result.frontend_required is True
    assert result.recommended_write_tool == "repo.write_many_smart"
    assert any(step.tool == "workflow.frontend_pattern_preflight" for step in result.steps)


def test_repo_index_and_impact_related_files(tmp_path: Path):
    module = tmp_path / "my_mod"
    (module / "views").mkdir(parents=True)
    (module / "static" / "src").mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name':'X','depends':['web'],'assets':{'web.assets_backend':['my_mod/static/src/a.js']}}", encoding="utf-8")
    (module / "views" / "templates.xml").write_text('<templates><t t-name="my_mod.Template"/></templates>', encoding="utf-8")
    js = module / "static" / "src" / "a.js"
    js.write_text('/** @odoo-module **/\nclass X { static template = "my_mod.Template"; }', encoding="utf-8")

    index = RepoIndexService().index(str(tmp_path), force_refresh=True)
    assert index.modules[0].name == "my_mod"
    assert index.template_names["my_mod.Template"] == "my_mod/views/templates.xml"

    related = RepoIndexService().related_files([str(js)], str(tmp_path), max_related=5)
    assert str(module / "__manifest__.py") in related
    assert str(module / "views" / "templates.xml") in related


def test_frontend_preflight_cache_returns_same_area():
    req = {
        "task_description": "fix website OWL snippet",
        "target_area": "website",
        "planned_file_paths": ["x/static/src/js/snippet.js"],
    }
    first = FrontendPatternService().preflight(type("Req", (), req)()) if False else None
    # Use real pydantic request to avoid depending on installed Odoo source paths.
    from odoo_agent_server.schemas import FrontendPatternPreflightRequest
    service = FrontendPatternService()
    r1 = service.preflight(FrontendPatternPreflightRequest(**req))
    r2 = service.preflight(FrontendPatternPreflightRequest(**req))
    assert r1.target_area == "website"
    assert r2.target_area == "website"
    assert "cache" in r2.message.lower()


def test_syntax_verifier_impact_aware_does_not_crash(tmp_path: Path):
    module = tmp_path / "my_mod"
    (module / "views").mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name':'X','depends':['web']}", encoding="utf-8")
    xml = module / "views" / "x.xml"
    xml.write_text("<odoo></odoo>", encoding="utf-8")
    result = SyntaxVerifierService().verify(SyntaxVerifyRequest(
        written_files=[str(xml)],
        project_path=str(tmp_path),
        impact_aware=True,
    ))
    assert str(xml) in result.files_checked
