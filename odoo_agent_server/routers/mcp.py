from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from ..auth import AuthDependency
from ..config import settings
from ..services.oauth_service import OAuthStore
from ..services.qwen_token_service import QwenTokenService
from .tools import _dispatch, list_tools, tool_input_schema

router = APIRouter(prefix="/mcp", tags=["mcp"], dependencies=[AuthDependency])
qwen_router = APIRouter(prefix="/qwen", tags=["mcp-qwen"] )


def _rpc_response(request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return payload


def _dump_any(value: Any) -> Any:
    """Recursively convert Pydantic models (and lists/dicts of them) into
    plain JSON-safe values using model_dump(mode="json")."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_dump_any(item) for item in value]
    if isinstance(value, dict):
        return {k: _dump_any(v) for k, v in value.items()}
    return value


def _to_structured_content(result: Any, tool_name: str) -> dict[str, Any]:
    """MCP's CallToolResult.structuredContent MUST be a JSON object (dict).

    Several tools (repo.list, repo.list_custom_modules, jobs.list,
    approvals.list, audit.list, workflow tools returning matches, etc.)
    naturally return a bare list of Pydantic models. A raw list at the top
    level fails MCP schema validation with:
        "Input should be a valid dictionary [type=dict_type]"
    Wrap any non-dict result in {"items": [...], "count": N} so every tool
    call is schema-valid regardless of what the underlying function returns.
    """
    dumped = _dump_any(result)
    if isinstance(dumped, dict):
        return dumped
    if isinstance(dumped, list):
        return {"items": dumped, "count": len(dumped)}
    # Scalars (str/int/bool/None) — still must be a dict per spec.
    return {"result": dumped}


def _cap_text(text: str, max_chars: int = 24000) -> str:
    """Truncate an oversized tool-result text payload to keep ChatGPT's context
    lean across long sessions. Keeps the head and tail (most relevant parts for
    errors/diffs/logs) and drops the middle with a clear marker.
    """
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    omitted = len(text) - max_chars
    return (
        f"{head}\n\n...[{omitted} chars omitted — output was too large for chat context. "
        f"Full data is still available in structuredContent, or re-call with a narrower "
        f"scope, e.g. fewer tail_lines / max_bytes / a more specific file path]...\n\n{tail}"
    )


def _humanize_tool_name(name: str) -> str:
    """Fallback: convert 'git.push' -> 'Git Push' if no explicit title is set."""
    tail = name.split(".", 1)[-1]
    return tail.replace("_", " ").title()


def _tool_to_mcp(tool) -> dict[str, Any]:
    title = getattr(tool, "title", None) or _humanize_tool_name(tool.name)
    return {
        "name": tool.name,
        "title": title,
        "description": tool.description,
        "inputSchema": tool_input_schema(tool.name),
        "annotations": {
            "title": title,
            "readOnlyHint": bool(tool.read_only),
            "destructiveHint": bool(tool.requires_approval),
        },
    }


@router.post("")
async def mcp_rpc(payload: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Lightweight JSON-RPC MCP-compatible endpoint.

    It exposes initialize, tools/list, and tools/call over one HTTPS POST route.
    The same service functions are used by REST/OpenAPI and this MCP-style bridge.
    """
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_rpc(item)
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_rpc(payload)


async def _handle_rpc(payload: dict[str, Any]) -> dict[str, Any] | None:
    method = payload.get("method")
    request_id = payload.get("id")
    params = payload.get("params") or {}

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return _rpc_response(
            request_id,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "odoo-chatgpt-direct-connector", "version": "1.1.0"},
            },
        )
    if method == "tools/list":
        return _rpc_response(request_id, {"tools": [_tool_to_mcp(tool) for tool in list_tools()]})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not name:
            return _rpc_response(request_id, error={"code": -32602, "message": "tools/call requires params.name"})
        try:
            result = await _dispatch(name, arguments)
            structured = _to_structured_content(result, tool_name=name)
            text = json.dumps(structured, ensure_ascii=False, default=str)
            # Context-bloat guard: in long sessions, oversized tool outputs (large diffs,
            # logs, file reads) compound across many calls and slow down/derail the model.
            # Cap the text view shown in chat; structuredContent below stays complete so
            # the agent can still act on the full data programmatically if needed.
            text = _cap_text(text, max_chars=24000)
            return _rpc_response(
                request_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": structured,
                    "isError": False,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _rpc_response(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
    return _rpc_response(request_id, error={"code": -32601, "message": f"Unknown method: {method}"})


def _validate_qwen_url_token(token: str | None) -> None:
    """Validate a token embedded in the Qwen-specific URL fallback.

    Some Qwen app builds POST to the MCP endpoint without forwarding custom
    Authorization headers. The normal /mcp endpoint remains the preferred,
    standards-aligned endpoint. This fallback lets a private HTTPS deployment
    place the existing ODOO_AGENT_API_TOKEN in the URL path or query string:

        /qwen/mcp/<ODOO_AGENT_API_TOKEN>
        /qwen/mcp?access_token=<ODOO_AGENT_API_TOKEN>

    The token is still checked with constant-time comparison and OAuth access
    tokens are also accepted for compatibility.
    """
    import secrets

    value = (token or "").strip()
    if value and QwenTokenService().validate(value):
        return
    if value and secrets.compare_digest(value, settings.api_token):
        return
    if value and OAuthStore().validate_access_token(value):
        return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid Qwen MCP access token")


@qwen_router.post("/token", dependencies=[AuthDependency])
def qwen_short_token() -> dict[str, Any]:
    """Mint a short-lived Qwen URL token after normal bearer/OAuth auth.

    Use this instead of putting the permanent ODOO_AGENT_API_TOKEN in Qwen app
    URLs. Configure Qwen with the returned qwen_mcp_url and Auth=None.
    """
    return QwenTokenService().mint()


@qwen_router.get("/mcp/{access_token}")
def qwen_mcp_path_health(access_token: str) -> dict[str, Any]:
    _validate_qwen_url_token(access_token)
    return {"status": "ok", "endpoint": "qwen-url-token-mcp", "transport": "http-post-json-rpc"}


@qwen_router.get("/mcp")
def qwen_mcp_query_health(access_token: str = Query(default="")) -> dict[str, Any]:
    _validate_qwen_url_token(access_token)
    return {"status": "ok", "endpoint": "qwen-url-token-mcp", "transport": "http-post-json-rpc"}


@qwen_router.post("/mcp/{access_token}")
async def qwen_mcp_path_rpc(access_token: str, payload: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]] | None:
    _validate_qwen_url_token(access_token)
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_rpc(item)
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_rpc(payload)


@qwen_router.post("/mcp")
async def qwen_mcp_query_rpc(payload: dict[str, Any], access_token: str = Query(default="")) -> dict[str, Any] | list[dict[str, Any]] | None:
    _validate_qwen_url_token(access_token)
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_rpc(item)
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_rpc(payload)
