from fastapi.testclient import TestClient

from odoo_agent_server.main import app
from odoo_agent_server.config import settings


def _auth():
    return {"Authorization": f"Bearer {settings.api_token}"}


def test_health_and_root_are_backend_only():
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["ui"] == "not_required"
    assert root.json()["openapi"] == "/openapi.json"


def test_tools_are_exposed_for_direct_chatgpt():
    client = TestClient(app)
    response = client.get("/tools/list", headers=_auth())
    assert response.status_code == 200
    names = {item["name"] for item in response.json()}
    assert "qa.self_run" in names
    assert "repo.apply_patch" in names
    assert "git.push" in names
    assert "odoosh.grep_errors" in names
    assert "browser.screenshot" in names


def test_capability_check_loads():
    client = TestClient(app)
    response = client.get("/workflow/capability-check", headers=_auth())
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ready", "partial", "failed"}
    assert "git" in payload["checks"]
    assert "allowed_roots" in payload["checks"]
