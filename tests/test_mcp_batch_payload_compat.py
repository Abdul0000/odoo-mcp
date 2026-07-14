from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app


def _headers(monkeypatch):
    # Auth intentionally follows the original settings-backed token behaviour.
    # Do not rely on mutating ODOO_AGENT_API_TOKEN after app/settings import.
    return {"Authorization": f"Bearer {settings.api_token}"}


def test_chatgpt_mcp_accepts_json_shell_batch(monkeypatch):
    client = TestClient(app)
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]

    response = client.post("/chatgpt/mcp", json=payload, headers=_headers(monkeypatch))

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert body[0]["id"] == 1
    assert body[1]["id"] == 2
    assert "tools" in body[1]["result"]


def test_standard_mcp_accepts_json_shell_batch(monkeypatch):
    client = TestClient(app)
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]

    response = client.post("/mcp", json=payload, headers=_headers(monkeypatch))

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert body[0]["id"] == 1
    assert body[1]["id"] == 2


def test_streamable_http_notification_returns_202_and_protocol_header(monkeypatch):
    client = TestClient(app)
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        headers={**_headers(monkeypatch), "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
    )
    assert response.status_code == 202
    assert response.headers["mcp-protocol-version"] == "2025-06-18"
    assert response.content == b""


def test_streamable_http_get_is_405_when_sse_is_not_supported(monkeypatch):
    client = TestClient(app)
    for path in ("/mcp", "/chatgpt/mcp", "/claude/mcp", "/grok/mcp"):
        response = client.get(path, headers=_headers(monkeypatch))
        assert response.status_code == 405
        assert response.headers["allow"] == "POST"


def test_streamable_http_rejects_unknown_protocol_version(monkeypatch):
    client = TestClient(app)
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={**_headers(monkeypatch), "MCP-Protocol-Version": "2099-01-01"},
    )
    assert response.status_code == 400
    assert "Unsupported MCP-Protocol-Version" in response.json()["error"]


def test_streamable_http_accepts_unknown_notifications_and_jsonrpc_responses(monkeypatch):
    client = TestClient(app)
    headers = {**_headers(monkeypatch), "MCP-Protocol-Version": "2025-06-18", "Accept": "application/json, text/event-stream"}
    notification = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/vendor-event", "params": {"ok": True}},
        headers=headers,
    )
    response_message = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 7, "result": {"accepted": True}},
        headers=headers,
    )
    assert notification.status_code == 202 and notification.content == b""
    assert response_message.status_code == 202 and response_message.content == b""


def test_streamable_http_rejects_untrusted_browser_origin(monkeypatch):
    client = TestClient(app)
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={**_headers(monkeypatch), "Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert "Origin" in response.json()["error"]


def test_streamable_http_does_not_trust_spoofed_host_as_browser_origin(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", None)
    monkeypatch.setattr(settings, "oauth_issuer", None)
    monkeypatch.setattr(settings, "mcp_allowed_origins_raw", "")
    client = TestClient(app)
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={**_headers(monkeypatch), "Host": "evil.example", "X-Forwarded-Host": "evil.example", "Origin": "http://evil.example"},
    )
    assert response.status_code == 403


def test_streamable_http_accepts_explicit_browser_origin(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://agent.example.com")
    client = TestClient(app)
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={**_headers(monkeypatch), "Origin": "https://agent.example.com"},
    )
    assert response.status_code == 200


def test_initialize_negotiates_supported_legacy_protocol(monkeypatch):
    client = TestClient(app)
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        },
        headers=_headers(monkeypatch),
    )
    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == "2025-03-26"
    assert response.headers["mcp-protocol-version"] == "2025-03-26"


def test_2025_streamable_http_requires_both_response_media_types(monkeypatch):
    client = TestClient(app)
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={**_headers(monkeypatch), "MCP-Protocol-Version": "2025-06-18", "Accept": "application/json"},
    )
    assert response.status_code == 406
    assert "text/event-stream" in response.json()["error"]
