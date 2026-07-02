from pathlib import Path

from odoo_agent_server.schemas import FrontendPatternPreflightRequest, SyntaxVerifyRequest
from odoo_agent_server.services.frontend_pattern_service import FrontendPatternService
from odoo_agent_server.services.syntax_verifier_service import SyntaxVerifierService


def test_frontend_preflight_detects_pos_task():
    result = FrontendPatternService().preflight(
        FrontendPatternPreflightRequest(task_description="Fix POS OWL PaymentScreen JS patch")
    )
    assert result.target_area == "pos"
    assert result.is_frontend_task is True
    assert result.push_blocked_until_done is True
    assert "point_of_sale._assets_pos" in result.required_asset_bundles


def test_syntax_verifier_blocks_modern_js_without_odoo_header(tmp_path: Path):
    module = tmp_path / "my_module"
    (module / "static/src/js").mkdir(parents=True)
    (module / "static/src/xml").mkdir(parents=True)
    js = module / "static/src/js/component.js"
    xml = module / "static/src/xml/component.xml"
    manifest = module / "__manifest__.py"
    js.write_text('import { Component } from "@odoo/owl";\nexport class X extends Component {}\nX.template = "my_module.X";\n', encoding="utf-8")
    xml.write_text('<templates><t t-name="my_module.X" owl="1"><div/></t></templates>', encoding="utf-8")
    manifest.write_text("{'name': 'X', 'depends': ['base'], 'assets': {'web.assets_backend': ['my_module/static/src/js/component.js', 'my_module/static/src/xml/component.xml']}}", encoding="utf-8")

    result = SyntaxVerifierService().verify(
        SyntaxVerifyRequest(written_files=[str(manifest), str(js), str(xml)])
    )
    assert result.push_blocked is True
    assert any(item.check == "js_odoo_module_header" for item in result.findings)


def test_syntax_verifier_blocks_missing_manifest_asset(tmp_path: Path):
    module = tmp_path / "my_module"
    module.mkdir()
    manifest = module / "__manifest__.py"
    manifest.write_text("{'name': 'X', 'depends': ['base'], 'assets': {'web.assets_backend': ['my_module/static/src/js/missing.js']}}", encoding="utf-8")

    result = SyntaxVerifierService().verify(SyntaxVerifyRequest(written_files=[str(manifest)]))
    assert result.push_blocked is True
    assert any(item.check == "manifest_asset_exists" for item in result.findings)
