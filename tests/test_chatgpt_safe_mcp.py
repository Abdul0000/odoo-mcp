from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app


def _auth():
    return {"Authorization": f"Bearer {settings.api_token}"}


def _mcp(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def test_chatgpt_mcp_uses_curated_tool_profile():
    client = TestClient(app)

    chatgpt = client.post("/chatgpt/mcp", headers=_auth(), json=_mcp("tools/list"))
    assert chatgpt.status_code == 200
    chatgpt_names = {tool["name"] for tool in chatgpt.json()["result"]["tools"]}

    full = client.post("/mcp", headers=_auth(), json=_mcp("tools/list"))
    assert full.status_code == 200
    full_names = {tool["name"] for tool in full.json()["result"]["tools"]}

    assert "repo_read" in chatgpt_names
    assert "repo_read_range" in chatgpt_names
    assert "repo_search" in chatgpt_names
    assert "artifacts_read" in chatgpt_names
    assert "odoosh_wait_for_commit" not in chatgpt_names
    assert "git_push" not in chatgpt_names
    assert len(full_names) > len(chatgpt_names)


def test_chatgpt_large_repo_read_returns_artifact_ref(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    big_text = "line 1\n" + ("x" * 45000) + "\nline 3\n"
    (tmp_path / "big.py").write_text(big_text, encoding="utf-8")
    client = TestClient(app)

    called = client.post(
        "/chatgpt/mcp",
        headers=_auth(),
        json=_mcp(
            "tools/call",
            {
                "name": "repo_read",
                "arguments": {"project_path": str(tmp_path), "relative_path": "big.py", "max_bytes": 100000},
            },
        ),
    )

    assert called.status_code == 200
    structured = called.json()["result"]["structuredContent"]
    assert structured["content_truncated"] is True
    artifact_id = structured["content_artifact"]["artifact_id"]
    assert len(structured["content"]) < len(big_text)

    fetched = client.post(
        "/chatgpt/mcp",
        headers=_auth(),
        json=_mcp(
            "tools/call",
            {
                "name": "artifacts_read",
                "arguments": {"artifact_id": artifact_id, "offset": 0, "max_chars": 50000},
            },
            request_id=2,
        ),
    )
    assert fetched.status_code == 200
    artifact = fetched.json()["result"]["structuredContent"]
    assert artifact["text"] == big_text


def test_repo_search_and_read_range_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "thing.py").write_text("alpha\nneedle = True\nomega\n", encoding="utf-8")
    client = TestClient(app)

    found = client.post(
        "/chatgpt/mcp",
        headers=_auth(),
        json=_mcp(
            "tools/call",
            {
                "name": "repo_search",
                "arguments": {"project_path": str(tmp_path), "pattern": "needle", "relative_path": "models"},
            },
        ),
    )
    assert found.status_code == 200
    assert found.json()["result"]["structuredContent"]["matches"][0]["line"] == 2

    ranged = client.post(
        "/chatgpt/mcp",
        headers=_auth(),
        json=_mcp(
            "tools/call",
            {
                "name": "repo_read_range",
                "arguments": {"project_path": str(tmp_path), "relative_path": "models/thing.py", "start_line": 2, "max_lines": 1},
            },
            request_id=2,
        ),
    )
    assert ranged.status_code == 200
    structured = ranged.json()["result"]["structuredContent"]
    assert structured["content"] == "2: needle = True"


def test_chatgpt_clamps_waiting_tools(monkeypatch):
    captured = {}

    async def fake_dispatch(name, arguments):
        captured["name"] = name
        captured["arguments"] = arguments
        return {"status": "ok"}

    monkeypatch.setattr("odoo_agent_server.routers.mcp._dispatch", fake_dispatch)
    client = TestClient(app)

    response = client.post(
        "/chatgpt/mcp",
        headers=_auth(),
        json=_mcp(
            "tools/call",
            {
                "name": "jobs_get",
                "arguments": {"job_id": "job-1", "wait_for_result": True, "wait_timeout_seconds": 20},
            },
        ),
    )

    assert response.status_code == 200
    assert captured["name"] == "jobs.get"
    assert captured["arguments"]["wait_for_result"] is False
    assert captured["arguments"]["wait_timeout_seconds"] == 5
