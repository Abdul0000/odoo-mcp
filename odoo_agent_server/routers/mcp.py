from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from ..auth import AuthDependency
from ..config import settings
from ..services.oauth_service import OAuthStore
from ..services.qwen_token_service import QwenTokenService
from ..services.store_service import JsonStore
from .tools import _dispatch, internal_tool_name, list_tools, strict_safe_tool_name, tool_input_schema

router = APIRouter(prefix="/mcp", tags=["mcp"], dependencies=[AuthDependency])
qwen_router = APIRouter(prefix="/qwen", tags=["mcp-qwen"] )
claude_router = APIRouter(prefix="/claude", tags=["mcp-claude"], dependencies=[AuthDependency])
chatgpt_router = APIRouter(prefix="/chatgpt", tags=["mcp-chatgpt"], dependencies=[AuthDependency])


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
    """Truncate an oversized tool-result text payload to keep agent context
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


def _preview_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head_chars = max_chars // 2
    tail_chars = max_chars - head_chars
    return f"{text[:head_chars]}\n\n...[{len(text) - max_chars} chars omitted; use artifacts.read for exact content]...\n\n{text[-tail_chars:]}"


def _artifact_next_call(artifact: dict[str, Any], max_chars: int = 12000) -> dict[str, Any]:
    return {
        "tool": "artifacts.read",
        "arguments": {"artifact_id": artifact["artifact_id"], "offset": 0, "max_chars": max_chars},
    }


def _normalize_chatgpt_structured(value: Any, tool_name: str) -> dict[str, Any]:
    """Keep strict compatibility-profile MCP payloads bounded, with full data available by artifact.

    The visible content text was already capped, but several clients also load
    structuredContent into their tool-call state. That can hang on large logs,
    diffs, command output, or job records. This keeps high-signal previews in
    place and stores exact full data for focused follow-up reads.
    """
    store = JsonStore()
    full_json = json.dumps(value, ensure_ascii=False, default=str)
    if len(full_json) <= 18000:
        return value if isinstance(value, dict) else {"result": value}

    def normalize(item: Any) -> Any:
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="json")
        if isinstance(item, list):
            normalized_items = [normalize(child) for child in item[:50]]
            return {
                "items": normalized_items,
                "count": len(item),
                "truncated": len(item) > 50,
                "next_action": "Re-call the source tool with a narrower limit or filter if more items are needed.",
            }
        if not isinstance(item, dict):
            text = str(item)
            if len(text) <= 8000:
                return item
            artifact = store.create_artifact("text", tool_name, text)
            return {
                "preview": _preview_text(text, 8000),
                "truncated": True,
                "artifact": artifact,
                "next_call": _artifact_next_call(artifact),
            }

        out: dict[str, Any] = {}
        for key, child in item.items():
            if key in {"stdout", "stderr", "content", "status"} and isinstance(child, str) and len(child) > 12000:
                artifact = store.create_artifact(key, tool_name, child)
                out[key] = _preview_text(child, 12000)
                out[f"{key}_truncated"] = True
                out[f"{key}_artifact"] = artifact
                out[f"{key}_next_call"] = _artifact_next_call(artifact)
            elif key == "result":
                out[key] = normalize(child)
            elif key == "steps" and isinstance(child, list):
                out[key] = [normalize(step) for step in child[:25]]
                if len(child) > 25:
                    out["steps_truncated"] = True
            else:
                out[key] = normalize(child)
        return out

    normalized = normalize(value)
    normalized_json = json.dumps(normalized, ensure_ascii=False, default=str)
    if isinstance(normalized, dict) and len(normalized_json) <= 24000:
        return normalized

    artifact = store.create_artifact("structured", tool_name, value)
    return {
        "status": value.get("status", "ok") if isinstance(value, dict) else "ok",
        "truncated": True,
        "preview": _preview_text(full_json, 12000),
        "artifact": artifact,
        "next_call": _artifact_next_call(artifact),
        "next_action": "Use artifacts.read for exact output, or repo.search/repo.read_range for focused code inspection.",
    }


def _arguments_for_profile(tool_name: str, arguments: dict[str, Any], profile: str | None) -> dict[str, Any]:
    if profile != "chatgpt" or tool_name not in _CHATGPT_QUICK_WAIT_TOOLS:
        return arguments
    adjusted = dict(arguments)
    adjusted["wait_for_result"] = False
    adjusted["wait_timeout_seconds"] = min(int(adjusted.get("wait_timeout_seconds", 5) or 5), 5)
    return adjusted


def _humanize_tool_name(name: str) -> str:
    """Fallback: convert 'git.push' -> 'Git Push' if no explicit title is set."""
    tail = name.split(".", 1)[-1]
    return tail.replace("_", " ").title()


def _mcp_safe_tool_name(name: str) -> str:
    return strict_safe_tool_name(name)


def _mcp_name_to_internal(name: str) -> str:
    return internal_tool_name(name)


_CLAUDE_TOOL_NAMES = {
    "connector.health",
    "odoo.query",
    "odoo.search_read",
    "odoo.health",
    "odoo.module_status",
}

_CHATGPT_TOOL_NAMES = {
    "connector.health",
    "artifacts.read",
    "workflow.agent_instructions",
    "workflow.smart_tool_router",
    "workflow.coding_task_plan",
    "workflow.start_session",
    "workflow.frontend_pattern_preflight",
    "workflow.repo_index",
    "workflow.find_core_file",
    "workflow.find_similar_view",
    "workflow.list_module_files",
    "workflow.scope_advice",
    "workflow.verify_against_core",
    "workflow.verify_pos_change",
    "workflow.safe_deploy_module",
    "repo.prepare",
    "repo.list_custom_modules",
    "repo.read",
    "repo.read_range",
    "repo.search",
    "repo.write_many_smart",
    "repo.apply_patch_atomic",
    "git.diff",
    "qa.self_run",
    "jobs.get",
    "odoosh.error_pack",
    "odoosh.check_commit",
    "odoo.health",
    "odoo.query",
    "odoo.search_read",
    "odoo.module_status",
    "odoo.module_deploy",
    "browser.screenshot",
    "browser.pos_flow",
}

_CHATGPT_QUICK_WAIT_TOOLS = {
    "jobs.get",
    "odoo.module_deploy",
    "odoo.module_install",
    "odoo.module_update",
    "odoosh.module_deploy_ssh",
    "odoosh.module_update",
}

_REMOTE_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _force_remote_tool_name(name: Any, fallback_index: int = 0) -> str:
    """Final guard for strict remote-MCP name validation.

    Some clients reject the whole connection when even one returned tool name
    contains a dot or other punctuation. This function is intentionally used at
    the final serialization boundary, not only in descriptors, so no future code
    path can accidentally leak dotted internal names.
    """
    value = strict_safe_tool_name(str(name or f"tool_{fallback_index}"))
    value = re.sub(r"[^a-zA-Z0-9_-]", "_", value).strip("_") or f"tool_{fallback_index}"
    value = value[:64]
    if _REMOTE_TOOL_NAME_RE.match(value):
        return value
    return f"tool_{fallback_index}"[:64]


def _force_remote_tool_list(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hard-sanitize every tools/list item right before returning to clients."""
    sanitized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, tool in enumerate(tools):
        item = dict(tool)
        name = _force_remote_tool_name(item.get("name"), idx)
        if name in seen:
            suffix = f"_{idx}"
            name = f"{name[:64-len(suffix)]}{suffix}"
        seen.add(name)
        item["name"] = name
        schema = item.get("inputSchema")
        if not isinstance(schema, dict):
            item["inputSchema"] = {"type": "object", "properties": {}}
        item.setdefault("description", "Odoo MCP tool")
        sanitized.append(item)
    return sanitized


def _is_claude_request(request: Request | None) -> bool:
    if request is None:
        return False
    headers = request.headers
    joined = " ".join(
        str(headers.get(name, ""))
        for name in ("user-agent", "x-client-name", "anthropic-version", "mcp-client")
    ).lower()
    return "claude" in joined or "anthropic" in joined


def _tools_for_profile(profile: str | None = None) -> list[Any]:
    tools = list_tools(strict_names=False)
    if profile == "claude":
        # Some compatibility clients fail more easily on very large remote MCP tool lists
        # and complex coding schemas. Keep the Claude profile tiny and functional
        # by default; the standard /mcp endpoint still uses the full tool list.
        return [tool for tool in tools if tool.name in _CLAUDE_TOOL_NAMES]
    if profile == "chatgpt":
        return [tool for tool in tools if tool.name in _CHATGPT_TOOL_NAMES]
    return tools


def _claude_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a small Claude-safe JSON schema.

    Claude's remote-MCP frontend can fail the message stream while loading tools
    when schemas contain lots of generated Pydantic metadata or nested $defs. This
    removes non-essential metadata and simplifies nullable anyOf values.
    """
    def clean(value: Any) -> Any:
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, dict):
            return value

        if "$ref" in value:
            return {"type": "object"}
        if "anyOf" in value:
            non_null = [item for item in value.get("anyOf", []) if not (isinstance(item, dict) and item.get("type") == "null")]
            if len(non_null) == 1:
                base = clean(non_null[0])
                if "description" in value and isinstance(base, dict):
                    base.setdefault("description", value["description"])
                return base
            return {"type": "string", "description": value.get("description", "JSON value")}

        out: dict[str, Any] = {}
        for key, child in value.items():
            if key in {"$defs", "title", "default", "examples", "computed_fields"}:
                continue
            if key == "additionalProperties":
                # Keep strict false, but drop complex schemas under additionalProperties.
                out[key] = child if isinstance(child, bool) else True
                continue
            out[key] = clean(child)
        return out

    cleaned = clean(schema)
    if not isinstance(cleaned, dict):
        cleaned = {"type": "object"}
    cleaned.setdefault("type", "object")
    cleaned.setdefault("properties", {})
    return cleaned


def _tool_to_mcp(tool, profile: str | None = None) -> dict[str, Any]:
    title = getattr(tool, "title", None) or _humanize_tool_name(tool.name)
    if profile in {"claude", "chatgpt"}:
        # Some remote-MCP builds are strict and can fail while
        # validating the initial tool list. Use the minimal MCP tool shape for
        # these compatibility profiles: strict-safe name, description, schema only.
        return {
            "name": _mcp_safe_tool_name(tool.name),
            "description": str(tool.description)[:900],
            "inputSchema": _claude_schema(tool_input_schema(tool.name)),
        }
    return {
        "name": _mcp_safe_tool_name(tool.name),
        "title": title,
        "description": (
            f"Internal tool: {tool.name}. "
            f"{tool.description}"
        ),
        "inputSchema": tool_input_schema(tool.name),
        "annotations": {
            "title": title,
            "readOnlyHint": bool(tool.read_only),
            "destructiveHint": bool(tool.requires_approval),
        },
    }


@router.post("")
async def mcp_rpc(payload: dict[str, Any], request: Request) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Lightweight JSON-RPC MCP-compatible endpoint.

    It exposes initialize, tools/list, and tools/call over one HTTPS POST route.
    The same service functions are used by REST/OpenAPI and this MCP-style bridge.
    """
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_rpc(item, profile="claude" if _is_claude_request(request) else None)
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_rpc(payload, profile="claude" if _is_claude_request(request) else None)


async def _handle_rpc(payload: dict[str, Any], profile: str | None = None) -> dict[str, Any] | None:
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
                "serverInfo": {"name": "odoo-mcp-agent-connector", "version": "1.1.0"},
            },
        )
    if method == "tools/list":
        tools_payload = [_tool_to_mcp(tool, profile=profile) for tool in _tools_for_profile(profile)]
        return _rpc_response(request_id, {"tools": _force_remote_tool_list(tools_payload)})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not name:
            return _rpc_response(request_id, error={"code": -32602, "message": "tools/call requires params.name"})
        internal_name = _mcp_name_to_internal(name)
        try:
            arguments = _arguments_for_profile(internal_name, arguments, profile)
            result = await _dispatch(internal_name, arguments)
            structured = _to_structured_content(result, tool_name=internal_name)
            if profile == "chatgpt" and internal_name != "artifacts.read":
                structured = _normalize_chatgpt_structured(structured, internal_name)
            text = json.dumps(structured, ensure_ascii=False, default=str)
            # Context-bloat guard: in long sessions, oversized tool outputs (large diffs,
            # logs, file reads) compound across many calls and slow down/derail the model.
            # Compatibility profiles also receive bounded structuredContent with artifact refs.
            text = _cap_text(text, max_chars=12000 if profile == "chatgpt" else 24000)
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


@claude_router.get("/mcp")
def claude_mcp_health() -> dict[str, Any]:
    return {"status": "ok", "endpoint": "claude-slim-mcp", "transport": "http-post-json-rpc", "tools_profile": "functional"}


@claude_router.post("/mcp")
async def claude_mcp_rpc(payload: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]] | None:
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_rpc(item, profile="claude")
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_rpc(payload, profile="claude")


@chatgpt_router.get("/mcp")
def chatgpt_mcp_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "endpoint": "chatgpt-strict-mcp",
        "transport": "http-post-json-rpc",
        "tool_names": "strict_regex_safe",
    }


@chatgpt_router.get("/tools-debug")
def chatgpt_tools_debug() -> dict[str, Any]:
    tools_payload = _force_remote_tool_list([_tool_to_mcp(tool, profile="chatgpt") for tool in _tools_for_profile("chatgpt")])
    bad = [tool.get("name") for tool in tools_payload if not _REMOTE_TOOL_NAME_RE.match(str(tool.get("name", "")))]
    return {
        "status": "ok" if not bad else "bad_names",
        "count": len(tools_payload),
        "bad_names": bad,
        "first_names": [tool.get("name") for tool in tools_payload[:20]],
    }


@chatgpt_router.post("/mcp")
async def chatgpt_mcp_rpc(payload: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]] | None:
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_rpc(item, profile="chatgpt")
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_rpc(payload, profile="chatgpt")


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
