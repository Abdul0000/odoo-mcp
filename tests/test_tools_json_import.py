from odoo_agent_server.routers import tools


def test_tools_module_has_json_available_for_evidence_signature():
    assert tools.json.dumps({"ok": True}, sort_keys=True) == '{"ok": true}'
