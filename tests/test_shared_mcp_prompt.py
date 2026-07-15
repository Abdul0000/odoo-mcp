from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"}


def _mcp(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def test_prompt_files_keep_original_mcp_and_separate_client_gate():
    root = Path(__file__).resolve().parents[1]
    assert (root / "MCP_SYSTEM_PROMPT.md").is_file()
    assert (root / "CLIENT_CHECK_SYSTEM_PROMPT.md").is_file()
    assert (root / "OPENCODE_SYSTEM_PROMPT.md").is_file()
    assert not (root / "CHATGPT_SYSTEM_PROMPT.md").exists()


def test_all_mcp_profiles_expose_same_system_prompt():
    root = Path(__file__).resolve().parents[1]
    expected = (root / "MCP_SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    client = TestClient(app)

    for path in ("/mcp/system-prompt", "/chatgpt/system-prompt", "/grok/system-prompt", "/claude/system-prompt"):
        response = client.get(path, headers=_auth())
        assert response.status_code == 200
        payload = response.json()
        assert payload["source"] == "MCP_SYSTEM_PROMPT.md"
        assert payload["text"] == expected


def test_initialize_uses_client_gate_prompt_then_mcp_prompt_is_fetchable_for_every_profile():
    root = Path(__file__).resolve().parents[1]
    expected_mcp = (root / "MCP_SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    expected_gate = (root / "CLIENT_CHECK_SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    client = TestClient(app)

    for path in ("/mcp", "/chatgpt/mcp", "/grok/mcp", "/claude/mcp"):
        init = client.post(path, headers=_auth(), json=_mcp("initialize"))
        assert init.status_code == 200
        result = init.json()["result"]
        assert result["prompt_source"] == "CLIENT_CHECK_SYSTEM_PROMPT.md"
        assert result["next_prompt_source_after_client_validation"] == "MCP_SYSTEM_PROMPT.md"
        assert result["instructions"] == expected_gate

        listed = client.post(path, headers=_auth(), json=_mcp("prompts/list", request_id=2))
        assert listed.status_code == 200
        names = [item["name"] for item in listed.json()["result"]["prompts"]]
        assert names == ["client_check_system_prompt", "mcp_system_prompt"]

        fetched_gate = client.post(
            path,
            headers=_auth(),
            json=_mcp("prompts/get", {"name": "client_check_system_prompt"}, request_id=3),
        )
        assert fetched_gate.status_code == 200
        assert fetched_gate.json()["result"]["messages"][0]["content"]["text"] == expected_gate

        fetched_mcp = client.post(
            path,
            headers=_auth(),
            json=_mcp("prompts/get", {"name": "mcp_system_prompt"}, request_id=4),
        )
        assert fetched_mcp.status_code == 200
        assert fetched_mcp.json()["result"]["messages"][0]["content"]["text"] == expected_mcp


def test_opencode_profile_uses_local_checkout_prompt_after_client_gate():
    root = Path(__file__).resolve().parents[1]
    expected = (root / "OPENCODE_SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    client = TestClient(app)

    init = client.post("/opencode/mcp", headers=_auth(), json=_mcp("initialize"))
    assert init.status_code == 200
    assert init.json()["result"]["next_prompt_source_after_client_validation"] == "OPENCODE_SYSTEM_PROMPT.md"
    assert expected in init.json()["result"]["instructions"]

    listed = client.post("/opencode/mcp", headers=_auth(), json=_mcp("prompts/list", request_id=2))
    assert [row["name"] for row in listed.json()["result"]["prompts"]] == [
        "client_check_system_prompt",
        "opencode_system_prompt",
    ]

    fetched = client.post(
        "/opencode/mcp",
        headers=_auth(),
        json=_mcp("prompts/get", {"name": "opencode_system_prompt"}, request_id=3),
    )
    assert fetched.json()["result"]["source"] == "OPENCODE_SYSTEM_PROMPT.md"
    assert fetched.json()["result"]["messages"][0]["content"]["text"] == expected

    direct = client.get("/opencode/system-prompt", headers=_auth())
    assert direct.json()["text"] == expected
    for required in (
        "selected local repository folder",
        "compare local `HEAD` with its upstream",
        "only when the branch is behind",
        "only inside the selected local",
        "successfully pushed local `HEAD` SHA",
        "Hand the local push back to the MCP flow",
    ):
        assert required in expected


def test_mcp_system_prompt_keeps_required_generic_contract():
    root = Path(__file__).resolve().parents[1]
    text = (root / "MCP_SYSTEM_PROMPT.md").read_text(encoding="utf-8")

    required_markers = [
        "Connector-owned internal phases",
        "adaptive Odoo core/enterprise scope discovery",
        "mandatory QA, and compatibility verification",
        "required module upgrade/install checks",
        "registry smoke verification",
        "jobs.get",
    ]
    for marker in required_markers:
        assert marker in text
