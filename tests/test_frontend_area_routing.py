from odoo_agent_server.schemas import FrontendPatternPreflightRequest, OdooCodeChangeRequest, SmartToolRouteRequest
from odoo_agent_server.routers.workflow import smart_tool_router
from odoo_agent_server.services.frontend_pattern_service import FrontendPatternService
from odoo_agent_server.routers.tools import (
    _adaptive_workflow_profile,
    _looks_like_frontend_code_change,
    _material_clarification,
)


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


def test_adaptive_workflow_profile_scales_without_agent_file_limit_guessing():
    small = OdooCodeChangeRequest(
        module_name="sample_module",
        task_description="Add one configuration field",
        target_files=["sample_module/models/config.py"],
    )
    large = OdooCodeChangeRequest(
        module_name="sample_module",
        task_description="Build the complete module across all requested layers",
        create_module=True,
    )

    assert _adaptive_workflow_profile(small, {"required_layers": ["models"]})["tier"] == "small"
    large_profile = _adaptive_workflow_profile(
        large,
        {"full_module_requested": True, "required_layers": ["models", "security", "views", "data", "tests", "controllers"]},
    )
    assert large_profile["tier"] == "large"
    assert large_profile["file_budget"] == 50
    assert large_profile["deployment_policy"] == "one_commit_one_deploy_after_contract_closure"


def test_material_clarification_only_blocks_explicit_product_choice():
    ambiguous = OdooCodeChangeRequest(
        module_name="sample_module",
        task_description="Allow either all users or maybe only managers to see the approval button",
    )
    clear = OdooCodeChangeRequest(
        module_name="sample_module",
        task_description="Only sales managers can see the approval button",
    )

    assert _material_clarification(ambiguous)["required"] is True
    assert _material_clarification(clear) is None
    assert _material_clarification(
        ambiguous.model_copy(update={"clarification_answer": "Only sales managers."})
    ) is None


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
        "xml_view": (False, "backend_view_open"),
        "qweb_template": (True, "report_or_page_render"),
        "report": (False, "report_render"),
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


def test_backend_xml_view_does_not_trigger_frontend_preflight():
    payload = OdooCodeChangeRequest(
        module_name="sample_module",
        task_description="Update backend form view",
        task_type="xml_view",
        target_files=["sample_module/views/sample_views.xml"],
    )
    assert not _looks_like_frontend_code_change(payload, payload.target_files)


def test_static_qweb_xml_still_triggers_frontend_preflight():
    payload = OdooCodeChangeRequest(
        module_name="sample_module",
        task_description="Update frontend template",
        task_type="qweb_template",
        target_files=["sample_module/static/src/xml/sample_template.xml"],
    )
    assert _looks_like_frontend_code_change(payload, payload.target_files)


def test_product_path_does_not_trigger_pos_profile():
    payload = OdooCodeChangeRequest(
        module_name="sample_module",
        task_description="Update product backend logic",
        task_type="python_model",
        target_files=["sample_module/models/product_template.py"],
    )
    profile = _task_profile(payload, payload.target_files)
    assert profile["scope"] == "model"
    assert profile["live_verifier"] == "registry_smoke_test"


def test_smart_router_distinguishes_live_data_from_source_reads():
    data = smart_tool_router(SmartToolRouteRequest(task_description="Show unpaid invoices"))
    source = smart_tool_router(SmartToolRouteRequest(task_description="Show the existing Python method"))

    assert data.task_type == "odoo_data_query"
    assert data.recommended_write_tool == "none"
    assert source.task_type == "source_read"
    assert source.recommended_write_tool == "none"
    assert source.steps[-1].tool == "repo.read"


def test_smart_router_does_not_treat_module_as_a_write_signal():
    result = smart_tool_router(SmartToolRouteRequest(task_description="Get module installation status"))

    assert result.task_type == "odoo_data_query"
    assert result.recommended_write_tool == "none"


def test_smart_router_uses_safe_discovery_for_ambiguous_requests():
    result = smart_tool_router(SmartToolRouteRequest(task_description="Customer portal improvements"))

    assert result.task_type == "ambiguous"
    assert result.confidence == "low"
    assert result.clarification_required is True
    assert result.recommended_write_tool == "none"


def test_smart_router_explicit_write_and_file_evidence_win():
    result = smart_tool_router(SmartToolRouteRequest(
        task_description="Adjust the behavior",
        operation="write",
        planned_file_paths=["sample/static/src/js/widget.js"],
    ))

    assert result.task_type == "frontend_code"
    assert result.frontend_required is True
    assert result.confidence == "high"
    assert result.clarification_required is False
