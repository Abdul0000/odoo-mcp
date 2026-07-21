from pathlib import Path

from odoo_agent_server.services.module_analysis_service import ModuleAnalysisService


def _build_module(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "repo"
    module = project / "iot_device_management"
    for folder in ("models", "controllers", "security", "views"):
        (module / folder).mkdir(parents=True, exist_ok=True)
    (module / "__init__.py").write_text("from . import models, controllers\n", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'IoT', 'version': '18.0.1.0.0', 'depends': ['base'], "
        "'data': ['security/ir.model.access.csv', 'views/device_views.xml']}", encoding="utf-8"
    )
    (module / "models" / "__init__.py").write_text("from . import device\n", encoding="utf-8")
    (module / "models" / "device.py").write_text(
        "from odoo import fields, models\n"
        "class Device(models.Model):\n"
        "    _name = 'iot.device'\n"
        "    name = fields.Char()\n"
        "    def action_ping(self):\n"
        "        return True\n",
        encoding="utf-8",
    )
    (module / "controllers" / "__init__.py").write_text("from . import api\n", encoding="utf-8")
    (module / "controllers" / "api.py").write_text(
        "from odoo import http\n"
        "class Api(http.Controller):\n"
        "    @http.route('/iot/ping', type='json', auth='user')\n"
        "    def ping(self):\n"
        "        return {'ok': True}\n",
        encoding="utf-8",
    )
    (module / "security" / "ir.model.access.csv").write_text(
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_iot_device,iot.device,model_iot_device,base.group_user,1,1,1,0\n",
        encoding="utf-8",
    )
    (module / "views" / "device_views.xml").write_text(
        "<odoo><record id='view_iot_device_form' model='ir.ui.view'>"
        "<field name='name'>iot.device.form</field><field name='model'>iot.device</field>"
        "<field name='arch' type='xml'><form><field name='name'/></form></field>"
        "</record><menuitem id='menu_iot_root' name='IoT'/></odoo>",
        encoding="utf-8",
    )
    return project, module


def test_module_analysis_is_one_pass_and_returns_architecture(tmp_path):
    project, module = _build_module(tmp_path)
    result = ModuleAnalysisService().analyze(
        project=project,
        module_path=module,
        module_name="iot_device_management",
        analysis_goal="Explain architecture and practical gaps",
        max_files=50,
        max_total_chars=12000,
    )

    assert result["status"] == "analyzed"
    assert result["complete_for_analysis"] is True
    assert result["inventory"]["total_relevant_files"] == 8
    models = [model for item in result["architecture"]["python"] for model in item["models"]]
    routes = [route for item in result["architecture"]["python"] for route in item["routes"]]
    assert any(model["model"] == "iot.device" and "name" in model["fields"] for model in models)
    assert any("/iot/ping" in route["decorator"] for route in routes)
    assert result["next_call"] is None


def test_module_analysis_cache_invalidates_on_file_change(tmp_path):
    project, module = _build_module(tmp_path)
    service = ModuleAnalysisService()
    args = dict(
        project=project, module_path=module, module_name="iot_device_management",
        analysis_goal="Explain", max_files=50, max_total_chars=12000,
    )

    first = service.analyze(**args)
    second = service.analyze(**args)
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True

    device = module / "models" / "device.py"
    device.write_text(device.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    third = service.analyze(**args)
    assert third["cache_hit"] is False
