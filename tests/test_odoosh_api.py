from fastapi.testclient import TestClient

from odoo_agent_server.main import app


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
