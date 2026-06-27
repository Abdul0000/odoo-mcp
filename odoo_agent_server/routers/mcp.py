from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter

from ..auth import AuthDependency
from .tools import _dispatch, list_tools, tool_input_schema

router = APIRouter(prefix="/mcp", tags=["mcp"], dependencies=[AuthDependency])


def _rpc_response(request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return payload


def _tool_to_mcp(tool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool_input_schema(tool.name),
        "annotations": {
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
            structured = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            return _rpc_response(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False, default=str)}],
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
