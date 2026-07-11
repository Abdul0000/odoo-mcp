from fastapi import HTTPException

from odoo_agent_server.services.core_finder_service import CoreFinderService
from odoo_agent_server.schemas import ScopeAdviceRequest
from odoo_agent_server.routers.workflow import scope_advice


CLIENTS_JSON = '''[
  {"name":"Test 18","repo":"https://example.com/test18.git","branch":"main","path":"/tmp/test18"},
  {"name":"ClientA","repo":"https://example.com/client-a.git","branch":"main","path":"/tmp/client-a"}
]'''


def test_core_scope_advice_respects_explicit_client_name_in_multiclient(monkeypatch):
    monkeypatch.setenv("ODOO_CLIENTS", CLIENTS_JSON)

    result = CoreFinderService().scope_advice(
        custom_module_name="sample_website_module",
        custom_file_paths=[],
        task_description="style-only website change",
        client_name="ClientA",
    )

    assert result.custom_module == "sample_website_module"


def test_core_scope_advice_without_client_name_still_blocks_ambiguous_multiclient(monkeypatch):
    monkeypatch.setenv("ODOO_CLIENTS", CLIENTS_JSON)

    try:
        CoreFinderService().scope_advice(
            custom_module_name="sample_website_module",
            custom_file_paths=[],
            task_description="style-only website change",
        )
    except HTTPException as exc:
        assert exc.status_code == 409
        assert "Multiple ODOO_CLIENTS entries match" in str(exc.detail)
    else:
        raise AssertionError("Expected ambiguous multi-client scope advice to require client_name")


def test_workflow_scope_advice_forwards_client_name_to_core_finder(monkeypatch):
    monkeypatch.setenv("ODOO_CLIENTS", CLIENTS_JSON)

    result = scope_advice(
        ScopeAdviceRequest(
            custom_module_name="sample_website_module",
            custom_file_paths=[],
            task_description="style-only website change",
            client_name="ClientA",
        )
    )

    assert result.custom_module == "sample_website_module"
