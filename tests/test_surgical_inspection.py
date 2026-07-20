from pathlib import Path

from odoo_agent_server.routers.tools import (
    _build_safe_generic_autopatch,
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
