from odoo_agent_server.routers.tools import (
    _persist_module_contract,
    _update_module_contract_status,
)


class MemoryStore:
    def __init__(self):
        self.state = {}

    def load_session_state(self, client_name):
        return dict(self.state)

    def save_session_state(self, state, client_name=None):
        self.state.update(state)
        return dict(self.state)


def test_module_contract_survives_short_followups_and_reopens_when_expanded(tmp_path):
    store = MemoryStore()
    original = {
        "full_module_requested": True,
        "required_packages": ["controllers", "wizard"],
        "required_models": ["iot.device"],
    }
    merged, first = _persist_module_contract(
        store,
        client_name="odoo 18",
        project_path=str(tmp_path),
        module_name="iot_device_management",
        current_contract=original,
        task_description="Build the complete IoT module with all requested device fields and views.",
    )
    assert first["status"] == "pending"
    assert merged["required_packages"] == ["controllers", "wizard"]

    _update_module_contract_status(
        store,
        client_name="odoo 18",
        module_name="iot_device_management",
        status="fulfilled",
    )
    retained, second = _persist_module_contract(
        store,
        client_name="odoo 18",
        project_path=str(tmp_path),
        module_name="iot_device_management",
        current_contract={},
        task_description="continue",
    )
    assert second["status"] == "fulfilled"
    assert retained["required_models"] == ["iot.device"]
    assert second["requirement_sources"] == ["Build the complete IoT module with all requested device fields and views."]

    expanded, third = _persist_module_contract(
        store,
        client_name="odoo 18",
        project_path=str(tmp_path),
        module_name="iot_device_management",
        current_contract={"required_methods": ["execute_command"]},
        task_description="Add execute_command to the original module scope.",
    )
    assert third["status"] == "pending"
    assert expanded["required_methods"] == ["execute_command"]
    assert expanded["required_models"] == ["iot.device"]
