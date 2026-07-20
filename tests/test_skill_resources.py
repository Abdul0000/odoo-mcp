from pathlib import Path

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.services.skill_service import SkillService


EXPECTED = {
    "odoo-accounting-development": "01-SKILL-accounting.md",
    "odoo-manufacturing-development": "02-SKILL-manufacturing.md",
    "odoo-inventory-development": "03-SKILL-inventory.md",
    "odoo-sales-development": "04-SKILL-sales.md",
    "odoo-purchase-development": "05-SKILL-purchase.md",
    "odoo-hr-payroll-development": "06-SKILL-hr-payroll.md",
    "odoo-crm-pipeline-development": "07-SKILL-crm-pipeline.md",
    "odoo-helpdesk-development": "08-SKILL-helpdesk.md",
    "odoo-sql-query-development": "09-SKILL-sql-queries.md",
    "odoo-all-modules-reference": "10-ALL-MODULES-REFERENCE.md",
    "odoo-qweb-reports-development": "11-SKILL-qweb-reports-development.md",
    "odoo-accounting-reports": "12-SKILL-accounting-reports.md",
    "odoo-pos-development": "odoo-pos-development-skill.md",
    "odoo-website-development": "odoo-website-development.md",
    "odoo-custom-module-development": "13-SKILL-custom-module.md",
    "odoo-task-planning": "14-SKILL-planning.md",
}


def test_exact_skill_files_are_loaded_only_on_demand():
    root = Path(__file__).resolve().parents[1] / "skills"
    service = SkillService()
    listed = service.list()

    assert listed["loading"] == "on_demand"
    assert listed["count"] == len(EXPECTED)
    assert {item["name"] for item in listed["skills"]} == set(EXPECTED)

    for name, filename in EXPECTED.items():
        result = service.read(name)
        assert result["exact"] is True
        assert result["source"] == f"skills/{filename}"
        assert result["content"] == (root / filename).read_text(encoding="utf-8")


def test_provider_can_list_and_read_exact_skill_resource():
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    listed = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert {"skills_list", "skills_read"} <= names

    read = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "skills_read", "arguments": {"name": "accounting"}},
        },
    )
    assert read.status_code == 200
    result = read.json()["result"]["structuredContent"]
    expected = (Path(__file__).resolve().parents[1] / "skills" / EXPECTED["odoo-accounting-development"]).read_text(encoding="utf-8")
    assert result["name"] == "odoo-accounting-development"
    assert result["content"] == expected
    assert result["exact"] is True


def test_query_reporting_and_module_reference_aliases_load_on_demand():
    service = SkillService()

    reporting = service.read("reporting")
    modules = service.read("modules")

    assert reporting["name"] == "odoo-sql-query-development"
    assert reporting["source"] == "skills/09-SKILL-sql-queries.md"
    assert modules["name"] == "odoo-all-modules-reference"
    assert modules["source"] == "skills/10-ALL-MODULES-REFERENCE.md"


def test_generic_report_alias_uses_qweb_while_reporting_stays_query_focused():
    service = SkillService()

    assert service.read("report")["name"] == "odoo-qweb-reports-development"
    assert service.read("reports")["name"] == "odoo-qweb-reports-development"
    assert service.read("reporting")["name"] == "odoo-sql-query-development"


def test_skill_list_recommends_without_loading_skill_content():
    service = SkillService()

    accounting = service.list("Customize the balance sheet accounting report")
    qweb = service.list("Create a QWeb PDF report template")
    query = service.list("Use read_group to return record counts by state")

    assert accounting["recommendation"]["name"] == "odoo-accounting-reports"
    assert qweb["recommendation"]["name"] == "odoo-qweb-reports-development"
    assert query["recommendation"]["name"] == "odoo-sql-query-development"
    assert all("content" not in result for result in (accounting, qweb, query))


def test_qweb_report_aliases_load_dedicated_report_skill():
    service = SkillService()

    for alias in ("qweb", "qweb-report", "pdf-report", "report-template"):
        result = service.read(alias)
        assert result["name"] == "odoo-qweb-reports-development"
        assert result["source"] == "skills/11-SKILL-qweb-reports-development.md"


def test_accounting_report_aliases_load_dedicated_accounting_report_skill():
    service = SkillService()

    for alias in ("accounting-report", "financial-report", "invoice-report", "ar-aging", "tax-report"):
        result = service.read(alias)
        assert result["name"] == "odoo-accounting-reports"
        assert result["source"] == "skills/12-SKILL-accounting-reports.md"
