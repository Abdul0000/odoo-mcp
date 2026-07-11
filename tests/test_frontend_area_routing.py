from odoo_agent_server.schemas import FrontendPatternPreflightRequest, OdooCodeChangeRequest
from odoo_agent_server.services.frontend_pattern_service import FrontendPatternService
from odoo_agent_server.routers.tools import _looks_like_frontend_code_change


def test_product_word_does_not_trigger_pos():
    req = FrontendPatternPreflightRequest(
        task_description="Update backend controller behavior",
        target_module_name="sample_website_module",
        custom_file_paths=["/repo/sample_website_module/controllers/main.py"],
        planned_file_paths=["sample_website_module/controllers/main.py"],
    )
    assert FrontendPatternService._detect_area(req) == "website"


def test_python_website_controller_skips_frontend_preflight():
    payload = OdooCodeChangeRequest(
        module_name="sample_website_module",
        task_description="Update website controller logic",
        task_type="controller",
        target_files=["sample_website_module/controllers/main.py"],
    )
    assert not _looks_like_frontend_code_change(payload, payload.target_files)


def test_real_frontend_file_still_triggers_preflight():
    payload = OdooCodeChangeRequest(
        module_name="sample_website_module",
        task_description="Update public page behavior",
        task_type="controller",
        target_files=["sample_website_module/static/src/js/page_widget.js"],
    )
    assert _looks_like_frontend_code_change(payload, payload.target_files)

from odoo_agent_server.routers.tools import _task_profile


def test_task_profiles_are_proportional():
    cases = {
        "python_model": (False, "registry_smoke_test"),
        "controller": (False, "endpoint_smoke"),
        "xml_view": (True, "backend_view_open"),
        "qweb_template": (True, "report_or_page_render"),
        "report": (True, "report_render"),
        "js_report": (True, "report_ui_smoke"),
        "js_owl": (True, "owl_ui_smoke"),
        "pos_owl": (True, "pos_flow"),
        "scss_css": (True, "page_render"),
        "manifest": (False, "module_upgrade"),
        "data_xml": (False, "module_upgrade"),
        "init": (False, "module_import"),
        "security_csv": (False, "access_smoke"),
        "cron": (False, "method_smoke"),
        "payment": (False, "payment_route_smoke"),
        "payment_frontend": (True, "payment_ui_smoke"),
    }
    for task_type, (frontend, verifier) in cases.items():
        payload = OdooCodeChangeRequest(
            module_name="sample_module",
            task_description="targeted change",
            task_type=task_type,
        )
        profile = _task_profile(payload, [])
        assert profile["frontend"] is frontend
        assert profile["live_verifier"] == verifier


def test_mixed_module_routes_from_changed_file_not_module_name():
    payload = OdooCodeChangeRequest(
        module_name="website_pos_payment_mix",
        task_description="fix backend controller",
        task_type="controller",
    )
    assert not _looks_like_frontend_code_change(payload, ["website_pos_payment_mix/controllers/main.py"])
    assert _looks_like_frontend_code_change(payload, ["website_pos_payment_mix/static/src/js/payment_form.js"])
