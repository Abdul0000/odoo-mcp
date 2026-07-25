from pathlib import Path

from odoo_agent_server.routers.tools import (
    _build_safe_generic_autopatch,
    _analysis_index_target_paths,
    _contract_candidate_paths,
    _exact_write_context,
    _focused_search,
    _infer_code_change_search_patterns,
    _rank_core_directory_files,
    _rank_core_required_paths,
    _task_description_search_clues,
)
from odoo_agent_server.schemas import OdooCodeChangeRequest


def _payload(description: str, **overrides) -> OdooCodeChangeRequest:
    return OdooCodeChangeRequest(
        module_name="custom_sale",
        task_description=description,
        auto_deploy=False,
        **overrides,
    )


def test_task_description_extracts_concrete_code_clues_only():
    description = (
        "Fix method `_compute_total()` for XML ID sale.report_saleorder "
        "and selector .o_order_total on route /shop/cart."
    )

    clues = _task_description_search_clues(description)
    patterns = _infer_code_change_search_patterns(_payload(description))

    assert "_compute_total()" in clues
    assert "sale.report_saleorder" in clues
    assert ".o_order_total" in clues
    assert any(value in patterns for value in ("_compute_total()", "_compute_total"))
    assert "sale.report_saleorder" in patterns


def test_focused_search_returns_exact_match_lines_for_range_reads():
    class FakeRepo:
        def search(self, project_path, pattern, module_path, max_matches, context_lines, regex):
            return {
                "count": 1,
                "matches": [{
                    "path": "custom_sale/models/sale_order.py",
                    "line": 121,
                    "text": "    def _compute_total(self):",
                    "context": [],
                }],
            }

    results, paths, lines = _focused_search(FakeRepo(), "C:/repo", "custom_sale", ["_compute_total"], 20)

    assert results[0]["focused_count"] == 1
    assert paths == ["custom_sale/models/sale_order.py"]
    assert lines == {"custom_sale/models/sale_order.py": [121]}


def test_exact_write_context_returns_complete_enclosing_python_method(tmp_path):
    target = tmp_path / "custom_sale" / "models" / "sync.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "class Sync:\n"
        "    def unrelated(self):\n"
        "        return False\n\n"
        "    def run_browser_note_sync(self):\n"
        "        request = {\n"
        "            'title': 'Narrative',\n"
        "            'body': 'full note',\n"
        "        }\n"
        "        return request\n\n"
        "    def after(self):\n"
        "        return True\n",
        encoding="utf-8",
    )

    contexts = _exact_write_context(
        tmp_path,
        [{
            "relative_path": "custom_sale/models/sync.py",
            "status": "read",
            "read_mode": "match_range",
            "match_lines": [5],
        }],
    )

    assert len(contexts) == 1
    assert contexts[0]["context_type"] == "complete_python_function"
    assert contexts[0]["complete_unit"] is True
    assert "def run_browser_note_sync" in contexts[0]["content"]
    assert "'body': 'full note'" in contexts[0]["content"]
    assert "def unrelated" not in contexts[0]["content"]
    assert "def after" not in contexts[0]["content"]


def test_exact_write_context_recovers_method_line_from_task_when_preview_has_no_matches(tmp_path):
    target = tmp_path / "custom_sale" / "models" / "sync.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "class Sync:\n"
        "    def run_browser_note_sync(self):\n"
        "        return {'body': 'full note'}\n"
        + "\n" * 13_000,
        encoding="utf-8",
    )

    contexts = _exact_write_context(
        tmp_path,
        [{
            "relative_path": "custom_sale/models/sync.py",
            "status": "read",
            "read_mode": "module_analysis_cache",
        }],
        payload=_payload(
            "Fix `run_browser_note_sync` so it sends the complete narrative",
            target_hint="run_browser_note_sync",
        ),
    )

    assert contexts[0]["context_type"] == "complete_python_function"
    assert "return {'body': 'full note'}" in contexts[0]["content"]


def test_contract_candidates_include_implementation_and_existing_tests(tmp_path):
    module = tmp_path / "rethink"
    (module / "models").mkdir(parents=True)
    (module / "tests").mkdir()
    (module / "models" / "session_note.py").write_text(
        "API_METHOD = 'GetAppointmentsByIdAsync'\n",
        encoding="utf-8",
    )
    (module / "tests" / "test_session_note.py").write_text(
        "def test_flow():\n    assert 'GetAppointmentsByIdAsync'\n",
        encoding="utf-8",
    )
    payload = OdooCodeChangeRequest(
        module_name="rethink",
        task_description="Implement GetAppointmentsByIdAsync with mocked tests that pass",
        target_hint="GetAppointmentsByIdAsync",
    )
    contract = {
        "required_methods": [],
        "required_symbols": ["GetAppointmentsByIdAsync"],
        "required_layers": ["tests"],
    }

    paths = _contract_candidate_paths(tmp_path, "rethink", payload, contract)

    assert "rethink/models/session_note.py" in paths
    assert "rethink/tests/test_session_note.py" in paths


def test_exact_write_context_returns_relevant_functions_across_multiple_files(tmp_path):
    first = tmp_path / "rethink" / "models" / "session_note.py"
    second = tmp_path / "rethink" / "services" / "browser.py"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(
        "def fetch_appointment():\n"
        "    return 'GetAppointmentsByIdAsync'\n",
        encoding="utf-8",
    )
    second.write_text(
        "def fetch_response():\n"
        "    return 'GetFormResponsesAsync'\n",
        encoding="utf-8",
    )
    payload = OdooCodeChangeRequest(
        module_name="rethink",
        task_description="Implement GetAppointmentsByIdAsync then GetFormResponsesAsync",
    )

    contexts = _exact_write_context(
        tmp_path,
        [
            {"relative_path": "rethink/models/session_note.py", "status": "read"},
            {"relative_path": "rethink/services/browser.py", "status": "read"},
        ],
        payload=payload,
    )

    assert {context["relative_path"] for context in contexts} == {
        "rethink/models/session_note.py",
        "rethink/services/browser.py",
    }
    assert all(context["context_type"] == "complete_python_function" for context in contexts)


def test_core_paths_and_directory_files_are_relevance_ranked(tmp_path):
    sale = tmp_path / "sale" / "models"
    mail = tmp_path / "mail" / "models"
    sale.mkdir(parents=True)
    mail.mkdir(parents=True)
    sale_order = sale / "sale_order.py"
    mail_thread = mail / "mail_thread.py"
    sale_order.write_text("class SaleOrder: pass\n", encoding="utf-8")
    mail_thread.write_text("class MailThread: pass\n", encoding="utf-8")
    payload = _payload("Fix `sale_order` total computation", task_type="python_model")

    ranked_paths = _rank_core_required_paths(
        [str(mail_thread), str(sale_order)],
        payload,
        ["custom_sale/models/sale_order.py"],
        1,
    )
    ranked_files = _rank_core_directory_files(tmp_path, payload, ["custom_sale/models/sale_order.py"])

    assert ranked_paths == [str(sale_order)]
    assert ranked_files[0] == sale_order


def test_unique_quoted_text_replacement_is_single_call_deterministic(tmp_path):
    module = tmp_path / "custom_sale"
    views = module / "views"
    views.mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'Custom Sale'}\n", encoding="utf-8")
    target = views / "sale.xml"
    target.write_text("<span>Old label</span>\n", encoding="utf-8")
    payload = _payload('Replace "Old label" with "New label"')

    writes, strategy, reason = _build_safe_generic_autopatch(tmp_path, "custom_sale", payload, [])

    assert strategy == "task_description_text_replace"
    assert reason is None
    assert len(writes) == 1
    assert writes[0].relative_path == "custom_sale/views/sale.xml"
    assert writes[0].search == "Old label"
    assert writes[0].replace == "New label"


def test_cached_analysis_targets_exact_controller_without_graph_scan():
    analysis = {
        "source_evidence": [
            {"path": "models/device.py", "content": "class Device: pass"},
            {"path": "controllers/api.py", "content": "@http.route('/iot/v1/heartbeat')\ndef heartbeat(): return True"},
            {"path": "views/device_views.xml", "content": "<form><field name='name'/></form>"},
        ]
    }
    payload = _payload(
        "Fix route `/iot/v1/heartbeat` in the device API controller",
        task_type="controller",
        target_hint="/iot/v1/heartbeat",
    )

    paths = _analysis_index_target_paths(analysis, payload)

    assert paths[0] == "controllers/api.py"
    assert "models/device.py" not in paths


def test_ambiguous_or_complex_task_keeps_two_pass_flow(tmp_path):
    module = tmp_path / "custom_sale"
    views = module / "views"
    views.mkdir(parents=True)
    (module / "__manifest__.py").write_text("{'name': 'Custom Sale'}\n", encoding="utf-8")
    (views / "one.xml").write_text("<span>Old label</span>\n", encoding="utf-8")
    (views / "two.xml").write_text("<span>Old label</span>\n", encoding="utf-8")

    ambiguous = _payload('Replace "Old label" with "New label"')
    writes, strategy, reason = _build_safe_generic_autopatch(tmp_path, "custom_sale", ambiguous, [])
    assert writes == []
    assert strategy == "task_description_text_replace"
    assert reason == "replacement_not_unique_or_not_found"

    complex_task = _payload("Add a computed approval workflow with a new button")
    writes, strategy, reason = _build_safe_generic_autopatch(tmp_path, "custom_sale", complex_task, [])
    assert writes == []
    assert strategy is None
    assert reason == "autopatch_not_requested"
