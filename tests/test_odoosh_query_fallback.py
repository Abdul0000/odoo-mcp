import json

from odoo_agent_server.routers import tools
from odoo_agent_server.schemas import OdooFunctionalQueryRequest, OdooResult


def _client_env():
    return json.dumps([
        {
            "name": "ClientA",
            "repo": "https://github.com/ClientA/ClientA.git",
            "branch": "22-06-2026",
            "path": "/workspace/repos/ClientA",
            "token": "ghp_test",
            "odoo_url": "https://client-a.example.odoo.com",
            "odoo_db": "client-a-db",
            "odoo_login": "bad@example.com",
            "odoo_password": "bad",
            "odoosh_db_name": "client-a-db",
            "odoosh_ssh_host": "client-a.example.odoo.com",
            "odoosh_ssh_port": 22,
            "odoosh_ssh_user": "34342188",
            "odoosh_ssh_private_key_b64": "ZmFrZQ==",
            "odoosh_odoo_path": "/home/odoo/src/odoo",
            "odoosh_user_addons_path": "/home/odoo/src/user",
        }
    ])


def test_odoo_query_does_not_replay_ssh_after_primary_paths_fail(monkeypatch):
    monkeypatch.setenv("ODOO_CLIENTS", _client_env())

    def fake_shell_search_read(self, model, domain, fields, limit, offset, order, context, timeout_seconds=45):
        return OdooResult(status="failed", error="Odoo Odoo shell authentication failed")

    def fake_ssh_search_read(*args, **kwargs):
        raise AssertionError("exhausted primary query must not replay SSH")

    monkeypatch.setattr(tools.OdooService, "search_read", fake_shell_search_read)
    monkeypatch.setattr(tools.OdooShService, "search_read", fake_ssh_search_read)

    result = tools._search_read_via_shell(
        model="account.move",
        domain=[("move_type", "=", "out_invoice")],
        fields=["name", "amount_total", "state"],
        limit=5,
        client_name="ClientA",
        query_text="latest customer invoices",
    )

    assert result["status"] == "failed"
    assert result["source"] == "odoo_ssh_or_https_jsonrpc_failed"
    assert result["ssh_fallback_used"] is False
    assert "duplicate SSH replay was skipped" in result["message"]


def test_invoice_query_generates_relative_date_domain(monkeypatch):
    calls = []

    def fake_query(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == "ir.model.fields":
            rows = [
                {"model": "account.move", "name": "name", "field_description": "Number", "ttype": "char", "modules": "account"},
                {"model": "account.move", "name": "partner_id", "field_description": "Customer", "ttype": "many2one", "relation": "res.partner", "modules": "account"},
                {"model": "account.move", "name": "invoice_date", "field_description": "Invoice Date", "ttype": "date", "modules": "account"},
                {"model": "account.move", "name": "amount_total", "field_description": "Total", "ttype": "monetary", "modules": "account"},
                {"model": "account.move", "name": "move_type", "field_description": "Type", "ttype": "selection", "selection": "[('entry', 'Journal Entry'), ('out_invoice', 'Customer Invoice'), ('in_invoice', 'Vendor Bill')]", "modules": "account"},
                {"model": "account.move", "name": "state", "field_description": "Status", "ttype": "selection", "selection": "[('draft', 'Draft'), ('posted', 'Posted')]", "modules": "account"},
            ]
            return {"status": "ok", "source": "test_metadata", "client_name": "ClientA", "odoo": {"status": "ok", "result": rows}}
        return {"status": "ok", "source": "test", "client_name": "ClientA", "odoo": {"status": "ok", "result": []}}

    monkeypatch.setattr(tools, "_search_read_via_shell", fake_query)
    result = tools._odoo_functional_query(OdooFunctionalQueryRequest(
        client_name="ClientA",
        query="last two days customer invoices",
        limit=20,
    ))

    assert result["status"] == "ok"
    assert result["planner"] == "live_odoo_metadata"
    assert result["parsed"]["model"] == "account.move"
    record_call = calls[-1]
    assert record_call["model"] == "account.move"
    assert ("move_type", "=", "out_invoice") in record_call["domain"]
    assert any(item[0] == "invoice_date" and item[1] == ">=" for item in record_call["domain"])


def test_odoo_query_can_infer_client_name_from_text(monkeypatch):
    monkeypatch.setenv("ODOO_CLIENTS", _client_env())

    seen = {}

    def fake_shell_search_read(self, model, domain, fields, limit, offset, order, context, timeout_seconds=45):
        seen["client"] = self.client.name
        return OdooResult(status="ok", database="client-a-db", result=[])

    def fake_ssh_search_read(*args, **kwargs):
        raise AssertionError("successful primary query must not call direct SSH fallback")

    monkeypatch.setattr(tools.OdooService, "search_read", fake_shell_search_read)
    monkeypatch.setattr(tools.OdooShService, "search_read", fake_ssh_search_read)

    result = tools._search_read_via_shell(
        model="res.users",
        domain=[],
        fields=["name", "login"],
        limit=3,
        query_text="Use ClientA and read users",
    )

    assert result["status"] == "ok"
    assert result["client_name"] == "ClientA"
    assert seen["client"] == "ClientA"


def test_dynamic_query_discovers_salary_model_and_orders_by_wage(monkeypatch):
    calls = []

    def fake_query(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == "ir.model.fields" and kwargs["domain"] != [("model", "=", "hr.contract")]:
            return {
                "status": "ok",
                "source": "test_metadata",
                "client_name": "ClientA",
                "odoo": {"status": "ok", "result": [
                    {"model": "hr.contract", "name": "wage", "field_description": "Wage", "ttype": "monetary", "relation": False, "readonly": False, "modules": "hr_contract"},
                    {"model": "hr.contract", "name": "employee_id", "field_description": "Employee", "ttype": "many2one", "relation": "hr.employee", "readonly": False, "modules": "hr_contract"},
                    {"model": "hr.employee", "name": "name", "field_description": "Employee Name", "ttype": "char", "relation": False, "readonly": False},
                ]},
            }
        if kwargs["model"] == "ir.model.fields":
            return {
                "status": "ok",
                "source": "test_metadata",
                "client_name": "ClientA",
                "odoo": {"status": "ok", "result": [
                    {"model": "hr.contract", "name": "employee_id", "field_description": "Employee", "ttype": "many2one", "relation": "hr.employee", "readonly": False, "modules": "hr_contract"},
                    {"model": "hr.contract", "name": "wage", "field_description": "Wage", "ttype": "monetary", "relation": False, "readonly": False, "modules": "hr_contract"},
                    {"model": "hr.contract", "name": "state", "field_description": "Status", "ttype": "selection", "relation": False, "readonly": False},
                    {"model": "hr.contract", "name": "date_start", "field_description": "Start Date", "ttype": "date", "relation": False, "readonly": False},
                    {"model": "hr.contract", "name": "company_id", "field_description": "Company", "ttype": "many2one", "relation": "res.company", "readonly": False},
                ]},
            }
        assert kwargs["model"] == "hr.contract"
        assert kwargs["order"] == "wage desc, id desc"
        assert "employee_id" in kwargs["fields"]
        assert "wage" in kwargs["fields"]
        return {
            "status": "ok",
            "source": "test_records",
            "client_name": "ClientA",
            "odoo": {"status": "ok", "result": [
                {"id": 4, "employee_id": [9, "Ayesha"], "wage": 250000.0, "state": "open"},
            ]},
            "ssh_fallback_used": False,
            "message": "ok",
        }

    monkeypatch.setattr(tools, "_search_read_via_shell", fake_query)
    result = tools._odoo_functional_query(OdooFunctionalQueryRequest(
        client_name="ClientA",
        query="highest salary employees",
        limit=5,
    ))

    assert result["status"] == "ok"
    assert result["planner"] == "live_odoo_metadata"
    assert result["parsed"]["model"] == "hr.contract"
    assert result["parsed"]["target_module"] == "hr_contract"
    assert result["parsed"]["measure_field"] == "wage"
    assert result["odoo"]["result"][0]["employee_id"][1] == "Ayesha"
    assert [call["model"] for call in calls] == ["ir.model.fields", "ir.model.fields", "hr.contract"]


def test_users_query_uses_the_same_live_metadata_planner(monkeypatch):
    calls = []

    def fake_query(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == "ir.model.fields":
            return {
                "status": "ok",
                "source": "test_metadata",
                "client_name": "ClientA",
                "odoo": {"status": "ok", "result": [
                    {"model": "res.users", "name": "name", "field_description": "Name", "ttype": "char", "modules": "base"},
                    {"model": "res.users", "name": "login", "field_description": "Login", "ttype": "char", "modules": "base"},
                    {"model": "res.users", "name": "active", "field_description": "Active", "ttype": "boolean", "modules": "base"},
                ]},
            }
        return {"status": "ok", "source": "test_records", "client_name": "ClientA", "odoo": {"status": "ok", "result": []}}

    monkeypatch.setattr(tools, "_search_read_via_shell", fake_query)
    result = tools._odoo_functional_query(OdooFunctionalQueryRequest(
        client_name="ClientA",
        query="list active users",
    ))

    assert result["status"] == "ok"
    assert result["planner"] == "live_odoo_metadata"
    assert result["parsed"]["model"] == "res.users"
    assert ("active", "=", True) in result["parsed"]["domain"]
    assert [call["model"] for call in calls] == ["ir.model.fields", "ir.model.fields", "res.users"]


def test_custom_odoo_model_is_discovered_without_a_static_mapping(monkeypatch):
    calls = []

    def fake_query(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == "ir.model.fields":
            return {
                "status": "ok",
                "source": "test_metadata",
                "client_name": "ClientA",
                "odoo": {"status": "ok", "result": [
                    {"model": "lab.calibration", "name": "name", "field_description": "Calibration", "ttype": "char", "modules": "lab_quality"},
                    {"model": "lab.calibration", "name": "coefficient", "field_description": "Calibration Coefficient", "ttype": "float", "modules": "lab_quality"},
                    {"model": "lab.calibration", "name": "status", "field_description": "Status", "ttype": "selection", "selection": "[('valid', 'Valid'), ('expired', 'Expired')]", "modules": "lab_quality"},
                ]},
            }
        return {"status": "ok", "source": "test_records", "client_name": "ClientA", "odoo": {"status": "ok", "result": []}}

    monkeypatch.setattr(tools, "_search_read_via_shell", fake_query)
    result = tools._odoo_functional_query(OdooFunctionalQueryRequest(
        client_name="ClientA",
        query="show valid calibration coefficients above 1.5",
    ))

    assert result["status"] == "ok"
    assert result["parsed"]["model"] == "lab.calibration"
    assert result["parsed"]["target_module"] == "lab_quality"
    assert ("status", "=", "valid") in result["parsed"]["domain"]
    assert ("coefficient", ">", 1.5) in result["parsed"]["domain"]
    assert calls[-1]["model"] == "lab.calibration"


def test_absent_employee_question_uses_one_composite_shell_report(monkeypatch):
    monkeypatch.setenv("ODOO_CLIENTS", _client_env())
    calls = []

    def fake_report(self, report_date, limit=100, timeout_seconds=45):
        calls.append((self.client.name, report_date, limit, timeout_seconds))
        return OdooResult(status="ok", database="client-a-db", result={
            "status": "ok",
            "report_date": report_date,
            "absent": [{"id": 7, "name": "Ayesha", "department": "Sales", "job": "Manager"}],
            "counts": {"absent": 1},
            "evidence_models": ["hr.employee", "hr.attendance", "hr.leave"],
        })

    monkeypatch.setattr(tools.OdooShService, "workforce_presence_report", fake_report)
    monkeypatch.setattr(
        tools,
        "_search_read_via_shell",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("composite report must not start metadata shells")),
    )
    result = tools._odoo_functional_query(OdooFunctionalQueryRequest(
        client_name="ClientA",
        query="on 16 July 2026 which employees are absent",
        limit=100,
    ))

    assert result["status"] == "ok"
    assert result["planner"] == "single_shell_composite_recipe"
    assert result["parsed"]["single_odoo_shell"] is True
    assert result["parsed"]["report_date"] == "2026-07-16"
    assert result["odoo"]["result"]["absent"][0]["name"] == "Ayesha"
    assert calls == [("ClientA", "2026-07-16", 100, 45)]


def test_absence_report_requires_an_exact_date_without_querying_odoo(monkeypatch):
    monkeypatch.setattr(
        tools.OdooShService,
        "workforce_presence_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("missing date must stop before Odoo")),
    )
    result = tools._odoo_functional_query(OdooFunctionalQueryRequest(
        client_name="ClientA",
        query="which employees are absent",
    ))

    assert result["status"] == "needs_clarification"
    assert result["intent"] == "workforce_presence_gap"
    assert "exact calendar date" in result["message"]


def test_dynamic_query_never_defaults_unknown_request_to_partners(monkeypatch):
    monkeypatch.setattr(tools, "_search_read_via_shell", lambda **kwargs: {
        "status": "ok",
        "source": "test_metadata",
        "client_name": "ClientA",
        "odoo": {"status": "ok", "result": []},
    })

    result = tools._odoo_functional_query(OdooFunctionalQueryRequest(
        client_name="ClientA",
        query="show unusual calibration coefficients",
    ))

    assert result["status"] == "needs_clarification"
    assert result["intent"] == "dynamic_query"
    assert "do not guess" in result["message"].lower()


def test_query_engine_hard_blocks_record_mutations_without_any_odoo_call(monkeypatch):
    monkeypatch.setattr(
        tools,
        "_search_read_via_shell",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("blocked mutation must not query Odoo")),
    )

    result = tools._odoo_functional_query(OdooFunctionalQueryRequest(
        client_name="ClientA",
        query="delete all old employee contracts",
    ))

    assert result["status"] == "blocked"
    assert result["error_type"] == "read_only_query_guard"
    assert result["read_only_enforced"] is True
    assert result["success_allowed"] is False


def test_query_guard_does_not_confuse_create_date_with_a_create_request(monkeypatch):
    calls = []

    def fake_query(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == "ir.model.fields":
            return {"status": "ok", "source": "test", "client_name": "ClientA", "odoo": {"status": "ok", "result": [
                {"model": "account.move", "name": "invoice_date", "field_description": "Invoice Date", "ttype": "date", "modules": "account"},
                {"model": "account.move", "name": "create_date", "field_description": "Created on", "ttype": "datetime", "modules": "base"},
                {"model": "account.move", "name": "move_type", "field_description": "Type", "ttype": "selection", "selection": "[('out_invoice', 'Customer Invoice')]", "modules": "account"},
            ]}}
        return {"status": "ok", "source": "test", "client_name": "ClientA", "odoo": {"status": "ok", "result": []}}

    monkeypatch.setattr(tools, "_search_read_via_shell", fake_query)
    result = tools._odoo_functional_query(OdooFunctionalQueryRequest(
        client_name="ClientA",
        query="show invoices by create date",
    ))

    assert result["status"] == "ok"
    assert result["planner"] == "live_odoo_metadata"
    assert calls[-1]["model"] == "account.move"

from odoo_agent_server.services.client_resolver_service import ResolvedClient
from odoo_agent_server.services.odoosh_service import OdooShService
from odoo_agent_server.schemas import CommandResult


def test_odoosh_search_read_accepts_valid_marker_despite_warning_exit(monkeypatch):
    client = ResolvedClient(
        name="ClientA",
        repo="https://github.com/ClientA/ClientA.git",
        branch="22-06-2026",
        path="/workspace/repos/ClientA",
        odoosh_db_name="client-a-db",
        odoosh_ssh_host="host",
        odoosh_ssh_user="34342188",
        odoosh_ssh_private_key_b64="ZmFrZQ==",
        odoosh_odoo_path="/home/odoo/src/odoo",
        odoosh_user_addons_path="/home/odoo/src/user",
    )

    def fake_run_shell(self, remote_script, timeout_seconds=60):
        assert "./odoo-bin shell" in remote_script
        assert "MCP_ODOOSH_QUERY_JSON:" in remote_script
        return CommandResult(
            command=["ssh"],
            cwd="odoosh:ssh",
            exit_code=1,
            stdout='noise\nWARNING MCP_ODOOSH_QUERY_JSON:{"status":"ok","result":[{"name":"INV/1"}],"count":1} trailing shutdown warning\n',
            stderr="Odoo startup warning",
        )

    monkeypatch.setattr(OdooShService, "run_shell", fake_run_shell)
    result = OdooShService(client).search_read(
        "account.move",
        [["move_type", "=", "out_invoice"]],
        ["name"],
        limit=1,
    )
    assert result.status == "ok"
    assert result.database == "client-a-db"
    assert result.result == [{"name": "INV/1"}]


def test_workforce_presence_report_builds_one_valid_remote_python_script(monkeypatch):
    client = ResolvedClient(
        name="ClientA",
        repo="https://github.com/ClientA/ClientA.git",
        branch="22-06-2026",
        path="/workspace/repos/ClientA",
        odoosh_db_name="client-a-db",
        odoosh_ssh_host="host",
        odoosh_ssh_user="34342188",
        odoosh_ssh_private_key_b64="ZmFrZQ==",
        odoosh_odoo_path="/home/odoo/src/odoo",
        odoosh_user_addons_path="/home/odoo/src/user",
    )
    captured = {}

    def fake_run_shell(self, remote_script, timeout_seconds=60):
        captured["script"] = remote_script
        return CommandResult(
            command=["ssh"],
            cwd="odoosh:ssh",
            exit_code=0,
            stdout='MCP_ODOOSH_ANALYTICS_JSON:{"status":"ok","absent":[],"counts":{"absent":0}}\n',
        )

    monkeypatch.setattr(OdooShService, "run_shell", fake_run_shell)
    result = OdooShService(client).workforce_presence_report("2026-07-16", limit=50)

    assert result.status == "ok"
    assert captured["script"].count("./odoo-bin shell") == 1
    remote_python = captured["script"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    compile(remote_python, "<workforce-presence-report>", "exec")
