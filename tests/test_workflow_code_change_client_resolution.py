from types import SimpleNamespace

from odoo_agent_server.routers import tools
from odoo_agent_server.schemas import OdooCodeChangeRequest


def test_resolve_code_change_project_returns_resolved_client_name(monkeypatch, tmp_path):
    resolved = SimpleNamespace(name="Client A", path=str(tmp_path))
    monkeypatch.setattr(
        tools.ClientResolverService,
        "resolve_client",
        lambda self, **kwargs: resolved,
    )

    payload = OdooCodeChangeRequest(
        client_name="Client A",
        module_name="sample_frontend_module",
        task_description="Inspect themes",
    )

    client_name, project_path = tools._resolve_code_change_project(payload)

    assert client_name == "Client A"
    assert project_path == str(tmp_path)


def test_resolve_code_change_project_uses_explicit_path_without_client(monkeypatch, tmp_path):
    monkeypatch.setattr(
        tools.ClientResolverService,
        "resolve_client",
        lambda self, **kwargs: None,
    )

    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="sample_frontend_module",
        task_description="Inspect themes",
    )

    client_name, project_path = tools._resolve_code_change_project(payload)

    assert client_name is None
    assert project_path == str(tmp_path)


def test_inspection_signature_survives_dirty_correction_but_not_new_head(tmp_path):
    module = tmp_path / "sample_frontend_module"
    module.mkdir()
    target = module / "views.xml"
    target.write_text("<odoo/>\n", encoding="utf-8")
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="sample_frontend_module",
        task_description="Fix the same view",
        target_files=["sample_frontend_module/views.xml"],
    )

    first = tools._evidence_signature(tmp_path, payload.module_name, payload, "head-a")
    target.write_text("<odoo><data/></odoo>\n", encoding="utf-8")
    correction = tools._evidence_signature(tmp_path, payload.module_name, payload, "head-a")
    committed = tools._evidence_signature(tmp_path, payload.module_name, payload, "head-b")

    assert correction == first
    assert committed != first


def test_short_apply_reuses_pending_code_change_identity(tmp_path):
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="Nexus Operations Hub",
        task_description="apply",
        writes=[{
            "relative_path": "static/src/css/dashboard.scss",
            "search": ".panel {",
            "replace": ".panel {\n  border-color: white;",
        }],
    )
    pending = {
        "project_path": str(tmp_path),
        "module_name": "nexus_operations_hub",
        "task_description": "Add one shared white dark-mode border rule",
        "task_type": "scss_css",
        "intent": "change_style",
        "target_hint": "dashboard panels",
        "target_files": ["nexus_operations_hub/static/src/css/dashboard.scss"],
    }

    resumed = tools._reuse_pending_code_change(payload, pending, tmp_path)

    assert resumed.task_description == pending["task_description"]
    assert resumed.task_type == "scss_css"
    assert resumed.intent == "change_style"
    assert resumed.writes == payload.writes
    assert resumed.target_files == pending["target_files"]


def test_fast_ui_unique_selector_builds_minimal_style_patch(tmp_path):
    module = tmp_path / "nexus_operations_hub"
    style_file = module / "static" / "src" / "css" / "dashboard.scss"
    style_file.parent.mkdir(parents=True)
    style_file.write_text(
        ".o_nexus_heatmap {\n    background-color: #111;\n    color: #aaa;\n}\n",
        encoding="utf-8",
    )

    writes = tools._build_selector_style_autopatch(
        tmp_path,
        "nexus_operations_hub",
        ["nexus_operations_hub/static/src/css/dashboard.scss"],
        ".o_nexus_heatmap",
        {"background_color": "#17233a", "color": "#ffffff"},
    )

    assert len(writes) == 1
    assert writes[0].relative_path.endswith("dashboard.scss")
    assert "background-color: #17233a !important;" in writes[0].replace
    assert "color: #ffffff !important;" in writes[0].replace
    assert writes[0].count == 1


def test_fast_ui_autopatch_refuses_ambiguous_selector(tmp_path):
    module = tmp_path / "x_module" / "static" / "src" / "scss"
    module.mkdir(parents=True)
    (module / "a.scss").write_text(".heatmap { color: red; }\n", encoding="utf-8")
    (module / "b.scss").write_text(".heatmap { color: blue; }\n", encoding="utf-8")

    writes = tools._build_selector_style_autopatch(
        tmp_path,
        "x_module",
        [],
        ".heatmap",
        {"color": "white"},
    )

    assert writes == []


def test_fast_ui_locator_cache_reuses_only_existing_module_files(tmp_path):
    module = tmp_path / "x_module"
    target = module / "static" / "src" / "scss" / "heatmap.scss"
    target.parent.mkdir(parents=True)
    target.write_text(".heatmap {}\n", encoding="utf-8")
    cache = {
        "key": {
            "paths": [
                "x_module/static/src/scss/heatmap.scss",
                "x_module/static/src/scss/missing.scss",
                "../outside.scss",
            ]
        }
    }

    paths = tools._cached_ui_paths(cache, "key", tmp_path, "x_module")

    assert paths == ["x_module/static/src/scss/heatmap.scss"]


def test_adaptive_task_lane_keeps_targeted_normal_work_fast(tmp_path):
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="x_module",
        task_description="Fix delivery total calculation",
        target_hint="_compute_delivery_total",
        task_type="python_model",
    )

    assert tools._task_lane(payload) == "fast"


def test_adaptive_task_lane_uses_normal_for_vague_non_broad_bug(tmp_path):
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="x_module",
        task_description="Fix incorrect calculation",
    )

    assert tools._task_lane(payload) == "normal"


def test_adaptive_task_lane_promotes_broad_work_and_normalizes_bad_long_hint(tmp_path):
    broad = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="x_module",
        task_description="Refactor the entire module architecture",
    )
    forced = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="x_module",
        task_description="Fix one label",
        complexity="long",
    )

    assert tools._task_lane(broad) == "long"
    assert tools._task_lane(forced) == "fast"


def test_adaptive_task_lane_supports_deliberate_manual_override(tmp_path):
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="x_module",
        task_description="Fix one label",
        complexity="long",
        force_complexity=True,
    )

    assert tools._task_lane(payload) == "long"


def test_adaptive_task_lane_corrects_provider_long_hint_for_small_border_fix(tmp_path):
    payload = OdooCodeChangeRequest(
        client_name="odoo 18",
        project_path=str(tmp_path),
        module_name="nexus_operations_hub",
        task_description=(
            "Fix overly bright white table and header borders in dark mode across dashboard sections. "
            "Preserve layout and light mode; change only the necessary styling."
        ),
        complexity="long",
        task_type="scss_css",
        intent="fix dark mode border color",
    )

    assert tools._task_lane(payload) == "fast"


def test_adaptive_task_lane_keeps_genuine_ui_redesign_long(tmp_path):
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="x_module",
        task_description="Redesign the whole dashboard with a new layout and multiple components",
        complexity="long",
        task_type="scss_css",
        intent="dashboard redesign",
    )

    assert tools._task_lane(payload) == "long"


def test_adaptive_task_lane_refuses_fast_override_for_sensitive_work(tmp_path):
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="x_module",
        task_description="Change payment transaction access rights",
        task_type="security_csv",
        complexity="fast",
        target_hint="ir.model.access.csv",
    )

    assert tools._task_lane(payload) == "long"


def test_adaptive_task_lane_generalizes_fast_detection_across_task_types(tmp_path):
    cases = [
        ("python_model", "Fix one method condition", "_compute_total"),
        ("xml_view", "Add one button to the confirmed order form", "sale.view_order_form"),
        ("controller", "Fix one validation in the portal route", "/portal/orders"),
        ("manifest", "Update one dependency in the manifest", "__manifest__.py"),
        ("js_owl", "Fix one field renderer condition", "OrderLineRenderer"),
    ]

    for task_type, description, target_hint in cases:
        payload = OdooCodeChangeRequest(
            project_path=str(tmp_path),
            module_name="x_module",
            task_description=description,
            task_type=task_type,
            target_hint=target_hint,
            complexity="long",
        )
        decision = tools._task_lane_decision(payload)
        assert decision["lane"] == "fast"
        assert decision["score"] >= 4


def test_adaptive_task_lane_keeps_single_loader_feature_fast_despite_long_hint(tmp_path):
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="nexus_operations_hub",
        task_description="Add dashboard loader spin for module nexus_operations_hub",
        complexity="long",
    )

    decision = tools._task_lane_decision(payload)

    assert decision["lane"] == "fast"
    assert "frontend micro-feature" in decision["reason"]


def test_adaptive_task_lane_does_not_trust_unforced_long_hint_for_vague_work(tmp_path):
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="x_module",
        task_description="Improve current behavior where needed",
        complexity="long",
    )

    decision = tools._task_lane_decision(payload)

    assert decision["lane"] == "normal"
    assert "Unforced provider lane hints" in decision["reason"]


def test_adaptive_task_lane_does_not_trust_unforced_fast_hint_for_vague_work(tmp_path):
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="x_module",
        task_description="Improve current behavior where needed",
        complexity="fast",
    )

    assert tools._task_lane(payload) == "normal"


def test_compatibility_file_count_expands_reads_without_promoting_fast_lane():
    budget, expanded = tools._compatibility_lane_budget("fast", mandatory_count=24)

    assert expanded is True
    assert budget["core_files"] == 24
    assert budget["target_files"] == tools._TASK_LANE_BUDGETS["fast"]["target_files"]


def test_invalid_field_diagnostic_finds_duplicate_primary_models_and_import_order(tmp_path):
    module = tmp_path / "karachi_school_management"
    models = module / "models"
    data = module / "data"
    models.mkdir(parents=True)
    data.mkdir()
    (models / "__init__.py").write_text(
        "from . import school_facilities\nfrom . import library\n",
        encoding="utf-8",
    )
    (models / "school_facilities.py").write_text(
        "from odoo import fields, models\n"
        "class Book(models.Model):\n"
        "    _name = 'school.library.book'\n"
        "    total_copies = fields.Integer(default=1)\n",
        encoding="utf-8",
    )
    (models / "library.py").write_text(
        "from odoo import fields, models\n"
        "class LibraryBook(models.Model):\n"
        "    _name = 'school.library.book'\n"
        "    copies_available = fields.Integer()\n",
        encoding="utf-8",
    )
    (data / "data.xml").write_text(
        '<odoo><record id="book" model="school.library.book">'
        '<field name="total_copies">5</field></record></odoo>',
        encoding="utf-8",
    )
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="karachi_school_management",
        task_description="ValueError: Invalid field 'total_copies' on model 'school.library.book'",
    )

    result = tools._invalid_field_registry_diagnostic(tmp_path, "karachi_school_management", payload)

    assert result is not None
    assert result["classification"] == "duplicate_model_definitions"
    assert len([item for item in result["definitions"] if item["declares_name"]]) == 2
    assert result["models_init_import_order"] == ["school_facilities", "library"]
    assert result["xml_value_format_change_is_fix"] is False
    assert "karachi_school_management/data/data.xml" in result["target_files"]


def test_invalid_field_duplicate_model_guard_rejects_xml_only_write(tmp_path):
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="x_module",
        task_description="Invalid field 'total_copies' on model 'school.library.book'",
        writes=[{
            "relative_path": "data/data.xml",
            "search": '<field name="total_copies">5</field>',
            "replace": '<field name="total_copies" eval="5"/>',
        }],
    )
    diagnostic = {"classification": "duplicate_model_definitions"}

    assert tools._invalid_field_xml_only_write_block(diagnostic, payload.writes) is True


def test_fast_micro_style_edit_skips_remote_core_discovery():
    payload = OdooCodeChangeRequest(
        project_path="C:/workspace/repo",
        module_name="x_module",
        task_description="Change one dashboard border color",
        target_hint=".dashboard-card border-color",
        writes=[{
            "relative_path": "static/src/scss/dashboard.scss",
            "search": "border-color: #fff;",
            "replace": "border-color: #333;",
        }],
    )

    assert tools._is_low_risk_micro_change(payload, payload.writes, "fast") is True


def test_code_change_write_accepts_path_alias_from_mcp_provider():
    payload = OdooCodeChangeRequest(
        project_path="C:/workspace/repo",
        module_name="x_module",
        task_description="Update one complete stylesheet",
        writes=[{"path": "static/src/scss/dashboard.scss", "content": ".card { color: red; }\n"}],
    )

    assert payload.writes[0].relative_path == "static/src/scss/dashboard.scss"


def test_fast_micro_lane_refuses_python_and_broad_redesigns():
    python_payload = OdooCodeChangeRequest(
        project_path="C:/workspace/repo",
        module_name="x_module",
        task_description="Change one model method",
        writes=[{"relative_path": "models/order.py", "search": "old", "replace": "new"}],
    )
    redesign_payload = OdooCodeChangeRequest(
        project_path="C:/workspace/repo",
        module_name="x_module",
        task_description="Redesign the homepage architecture",
        writes=[{"relative_path": "static/src/scss/home.scss", "search": "old", "replace": "new"}],
    )

    assert tools._is_low_risk_micro_change(python_payload, python_payload.writes, "fast") is False
    assert tools._is_low_risk_micro_change(redesign_payload, redesign_payload.writes, "fast") is False


def test_code_change_scope_result_is_not_shadowed_by_a_local_import():
    assert "ScopeAdviceResult" not in tools._workflow_odoo_code_change.__code__.co_varnames


def test_missing_manifest_assets_are_diagnosed_locally_without_frontend_core_scan(tmp_path):
    module = tmp_path / "custom_website_home"
    module.mkdir()
    manifest = module / "__manifest__.py"
    manifest.write_text(
        repr({"assets": {"web.assets_frontend": [
            "custom_website_home/static/src/js/homepage.js",
            "custom_website_home/static/src/scss/homepage.scss",
        ]}}),
        encoding="utf-8",
    )
    payload = OdooCodeChangeRequest(
        project_path=str(tmp_path),
        module_name="custom_website_home",
        task_description="Odoo cannot read the manifest JS and SCSS asset files; check which assets are missing",
    )

    missing = tools._missing_manifest_asset_paths(tmp_path, module, manifest)

    assert missing == [
        "custom_website_home/static/src/js/homepage.js",
        "custom_website_home/static/src/scss/homepage.scss",
    ]
    assert tools._is_missing_asset_diagnostic_request(payload, missing) is True
