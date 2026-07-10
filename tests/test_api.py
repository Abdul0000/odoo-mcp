from pathlib import Path
import shutil

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app


def test_health():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_self_run_endpoint(monkeypatch):
    fixture_root = Path(__file__).parent / "fixtures"
    # Other tests/compileall can create pycache under the fixture module; QA is
    # correct to warn on that in real addons, so keep this fixture clean first.
    shutil.rmtree(fixture_root / "test_module" / "models" / "__pycache__", ignore_errors=True)
    monkeypatch.setattr(settings, "allowed_roots_raw", str(fixture_root))
    client = TestClient(app)
    response = client.post(
        "/qa/self-run",
        headers={"Authorization": "Bearer change-me"},
        json={
            "project_path": str(fixture_root),
            "module_name": "test_module",
            "run_pytest": False,
            "include_git_status": False,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "passed"
    assert data["summary"]["errors"] == 0
