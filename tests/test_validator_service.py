from pathlib import Path

from odoo_agent_server.schemas import Severity
from odoo_agent_server.services.validator_service import OdooModuleValidator


def test_valid_fixture_has_no_errors():
    module_path = Path(__file__).parent / "fixtures" / "test_module"
    findings = OdooModuleValidator().validate(module_path)
    errors = [item for item in findings if item.severity == Severity.error]
    assert errors == []


def test_missing_manifest_is_error(tmp_path):
    module_path = tmp_path / "broken_module"
    module_path.mkdir()
    findings = OdooModuleValidator().validate(module_path)
    assert any(item.code == "MISSING_MANIFEST" for item in findings)


def test_xml_parse_error_is_detected(tmp_path):
    module_path = tmp_path / "broken_xml"
    module_path.mkdir()
    (module_path / "__init__.py").write_text("", encoding="utf-8")
    (module_path / "__manifest__.py").write_text(
        "{'name': 'Broken XML', 'depends': ['base'], 'data': ['views/broken.xml']}",
        encoding="utf-8",
    )
    (module_path / "views").mkdir()
    (module_path / "views" / "broken.xml").write_text("<odoo><record></odoo>", encoding="utf-8")
    findings = OdooModuleValidator().validate(module_path)
    assert any(item.code == "XML_PARSE_ERROR" for item in findings)
