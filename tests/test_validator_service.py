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


def test_structure_gate_blocks_missing_import_acl_and_manifest_registration(tmp_path):
    module = tmp_path / "x_module"
    (module / "models").mkdir(parents=True)
    (module / "views").mkdir()
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'X', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}",
        encoding="utf-8",
    )
    (module / "models" / "__init__.py").write_text("", encoding="utf-8")
    (module / "models" / "asset.py").write_text(
        "from odoo import fields, models\n"
        "class Asset(models.Model):\n"
        "    _name = 'x.asset'\n"
        "    name = fields.Char()\n",
        encoding="utf-8",
    )
    (module / "views" / "asset_views.xml").write_text("<odoo/>", encoding="utf-8")

    findings = OdooModuleValidator().validate(module)
    codes = {finding.code for finding in findings if finding.severity == Severity.error}

    assert "PYTHON_IMPORT_CLOSURE_MISSING" in codes
    assert "ACCESS_ROW_MISSING_STATIC" in codes
    assert "MANIFEST_FILE_UNREGISTERED" in codes


def test_complete_module_contract_blocks_omitted_requested_layers(tmp_path):
    module = tmp_path / "solar_energy_management"
    module.mkdir()
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'Solar', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}",
        encoding="utf-8",
    )

    findings = OdooModuleValidator().validate(
        module,
        task_description="Build a complete Odoo module with models, security, views, reports, dashboards, automation, and demo data",
    )
    missing = [finding.message for finding in findings if finding.code == "REQUESTED_MODULE_LAYER_MISSING"]

    assert len(missing) == 7
    assert any(finding.code == "COMPLETE_MODULE_EMPTY" for finding in findings)


def test_access_reference_preserves_underscores_in_model_name(tmp_path):
    module = tmp_path / "x_module"
    (module / "models").mkdir(parents=True)
    (module / "security").mkdir()
    (module / "__init__.py").write_text("from . import models\n", encoding="utf-8")
    (module / "models" / "__init__.py").write_text("from . import asset_line\n", encoding="utf-8")
    (module / "models" / "asset_line.py").write_text(
        "from odoo import models\nclass AssetLine(models.Model):\n    _name = 'x.asset_line'\n",
        encoding="utf-8",
    )
    access = module / "security" / "ir.model.access.csv"
    access.write_text(
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x_asset_line,x.asset_line,model_x_asset_line,base.group_user,1,1,1,0\n",
        encoding="utf-8",
    )
    (module / "__manifest__.py").write_text(
        "{'name': 'X', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': ['security/ir.model.access.csv']}",
        encoding="utf-8",
    )

    findings = OdooModuleValidator().validate(module)
    assert not any(finding.code == "ACCESS_ROW_MISSING_STATIC" for finding in findings)
