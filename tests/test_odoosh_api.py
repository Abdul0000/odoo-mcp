import base64
import json

from fastapi.testclient import TestClient

from odoo_agent_server.main import app
from odoo_agent_server.schemas import CommandResult
from odoo_agent_server.services.odoosh_service import OdooShService


def test_odoosh_health_requires_config():
    client = TestClient(app)
    response = client.post(
        "/odoosh/health",
        headers={"Authorization": "Bearer change-me"},
    )
    assert response.status_code in (200, 400)
    if response.status_code == 400:
        assert "ODOOSH_SSH_HOST" in response.json()["detail"]


def test_odoosh_log_schema_without_auth_fails():
    client = TestClient(app)
    response = client.post("/odoosh/log", json={"log_name": "odoo", "tail_lines": 100})
    assert response.status_code == 401


def test_odoosh_read_user_file_reads_real_src_user_path(monkeypatch):
    captured = {}

    def fake_run_shell(self, script, timeout_seconds=60):
        captured["script"] = script
        payload = {
            "ok": True,
            "path": "/home/odoo/src/user/x_module/models/a.py",
            "relative_path": "x_module/models/a.py",
            "size": 23,
            "truncated": False,
            "content_b64": base64.b64encode(b"from odoo import models\n").decode("ascii"),
        }
        return CommandResult(command=["ssh"], cwd="odoosh:ssh", exit_code=0, stdout=json.dumps(payload))

    monkeypatch.setattr(OdooShService, "run_shell", fake_run_shell)

    result = OdooShService().read_user_file("x_module/models/a.py")

    assert result.path == "/home/odoo/src/user/x_module/models/a.py"
    assert result.content == "from odoo import models\n"
    assert result.extra["source"] == "odoosh_user"
    assert "/home/odoo/src/user" in captured["script"]
