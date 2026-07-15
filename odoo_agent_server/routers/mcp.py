from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from pydantic import ValidationError

from ..auth import AuthDependency
from ..config import settings
from ..services.oauth_service import OAuthStore
from ..services.prompt_service import (
    client_check_prompt_payload,
    load_client_check_system_prompt,
    load_mcp_system_prompt,
    load_opencode_system_prompt,
    opencode_prompt_payload,
    shared_prompt_payload,
)
from ..services.qwen_token_service import QwenTokenService
from ..services.store_service import JsonStore
from ..services.client_gate_service import ClientGateService
from ..services.mcp_operation_guard_service import McpOperationGuardService
from .tools import _dispatch, format_tool_validation_error, internal_tool_name, list_tools, strict_safe_tool_name, tool_input_schema

router = APIRouter(prefix="/mcp", tags=["mcp"], dependencies=[AuthDependency])
qwen_router = APIRouter(prefix="/qwen", tags=["mcp-qwen"] )
claude_router = APIRouter(prefix="/claude", tags=["mcp-claude"], dependencies=[AuthDependency])
chatgpt_router = APIRouter(prefix="/chatgpt", tags=["mcp-chatgpt"], dependencies=[AuthDependency])
grok_router = APIRouter(prefix="/grok", tags=["mcp-grok"], dependencies=[AuthDependency])
opencode_router = APIRouter(prefix="/opencode", tags=["mcp-opencode"], dependencies=[AuthDependency])

_REMOTE_PROFILES = {"provider", "claude", "chatgpt", "grok", "qwen", "opencode"}


def _shell_response(request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
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


def _provider_safe_next_calls(value: Any) -> Any:
    """Convert next_call.tool values to callable remote-MCP tool names.

    Structured results are internal JSON, not actual Tool descriptors.  Provider
    clients only see safe names like ``jobs_get``; returning dotted internal names
    makes the recovery path non-callable and causes duplicate heavy retries.
    """
    if isinstance(value, list):
        return [_provider_safe_next_calls(item) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, child in value.items():
        if key == "tool" and isinstance(child, str):
            internal = internal_tool_name(child)
            if internal in _PROVIDER_TOOL_NAMES:
                out[key] = strict_safe_tool_name(internal)
            else:
                out[key] = child
            continue
        out[key] = _provider_safe_next_calls(child)
    if out.get("tool") == strict_safe_tool_name("jobs.get") and isinstance(out.get("arguments"), dict):
        # Remote MCP clients commonly enforce a shorter HTTP timeout than our
        # server-side long poll. Keep every advertised follow-up immediately
        # responsive; the background job remains the source of truth.
        out["arguments"] = {
            **out["arguments"],
            "wait_for_result": False,
            "wait_timeout_seconds": 0,
        }
    return out


def _normalize_remote_structured(value: Any, tool_name: str) -> dict[str, Any]:
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
    adjusted = dict(arguments)

    if tool_name in {"odoo.module_deploy", "odoo.module_install", "odoo.module_update"}:
        adjusted["wait_for_result"] = False
        adjusted["wait_timeout_seconds"] = 0
        return adjusted

    if tool_name == "workflow.safe_deploy_module":
        # Hybrid safety contract for MCP browser clients (Grok/ChatGPT/Claude/Qwen):
        # preserve the full safe-deploy pipeline in a connector-owned background
        # job, but do not keep one MCP HTTP request open while SSH/Odoo.sh work
        # runs. A started/running response is never success; agents must poll the
        # returned next_call/jobs.get until a terminal verified result.
        adjusted["wait_for_result"] = False
        adjusted["wait_timeout_seconds"] = 0
        adjusted.setdefault("poll_interval_seconds", 2.0)
        return adjusted

    if tool_name == "jobs.get":
        if profile in _REMOTE_PROFILES:
            # Never hold a remote MCP request open while a background job runs.
            # Some clients terminate the call before our old 20/90 second wait,
            # producing JSON-RPC -32001 even though the job itself is healthy.
            adjusted["wait_for_result"] = False
            adjusted["wait_timeout_seconds"] = 0
            return adjusted

    # Keep the legacy ChatGPT-only quick-wait clamp for non-safe-deploy tools.
    # Other MCP clients keep their previous behaviour unless they explicitly use
    # safe_deploy/jobs.get above.
    if profile not in _REMOTE_PROFILES or tool_name not in _CHATGPT_QUICK_WAIT_TOOLS:
        return adjusted

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


# Unified provider-safe MCP profile for all remote providers.
# The detailed Odoo coding pipeline is server-owned by workflow.odoo_code_change;
# providers only request the change and poll jobs.get.  This removes duplicate
# repo/scope/deploy loops and prevents direct push/module-deploy bypasses.
_PROVIDER_TOOL_NAMES = {
    "connector.health",
    "client.check",
    # Read-only functional data path. Keep the natural-language helper first
    # for common business questions and expose search_read for precise/custom
    # model queries without forcing providers into shell commands.
    "odoo.query",
    "odoo.search_read",
    # Existing module lifecycle requests are not source-code changes. Expose the
    # dedicated idempotent smart install/upgrade workflow directly.
    "odoo.module_deploy",
    "workflow.odoo_code_change",
    "workflow.restore_last_good_commit",
    # Recovery/continuation tools that provider responses may legitimately return
    # in next_call.  Exposing them keeps the MCP contract honest and prevents
    # agents from retrying the heavy code-change workflow just to read logs or
    # continue a paused or recoverable safe-deploy checkpoint.
    "artifacts.read",
    "workflow.resume_job",
    "jobs.list",
    "jobs.get",
    "odoosh.error_pack",
}

# OpenCode owns the user's local filesystem and Git checkout. Do not expose the
# server-checkout code workflow on this profile, otherwise custom source could be
# inspected or edited in a different machine/worktree than the one OpenCode sees.
_OPENCODE_TOOL_NAMES = {
    "connector.health",
    "client.check",
    "odoo.query",
    "odoo.search_read",
    "odoo.module_deploy",
    "artifacts.read",
    "jobs.list",
    "jobs.get",
    "odoosh.error_pack",
}

_CLIENT_GATED_PROVIDER_TOOLS = {
    "odoo.query",
    "odoo.search_read",
    "odoo.module_deploy",
    "workflow.odoo_code_change",
    "workflow.restore_last_good_commit",
}

_MODULE_LIFECYCLE_RE = re.compile(
    r"^\s*(?:please\s+)?(?:install|upgrade|update)\b",
    re.I,
)
_SOURCE_CHANGE_TERMS_RE = re.compile(
    r"\b(?:fix|change|modify|add|remove|refactor|code|source|field|button|view|menu|method|class|xml|javascript|python|scss|css|label)\b",
    re.I,
)
_COMMIT_REVERT_INTENT_RE = re.compile(r"\b(?:revert|rollback|roll\s+back|undo|restore)\b", re.I)
_COMMIT_REVERT_TARGET_RE = re.compile(r"\b(?:commit|revision|release|version|previous|latest|last)\b", re.I)
_SHA_RE = re.compile(r"\b[0-9a-fA-F]{7,64}\b")
_ODOO_VERSION_RE = re.compile(r"\b\d+(?:\.\d+){2,5}\b")


def _provider_commit_revert_route(tool_name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Bypass expensive source reconstruction for explicit commit/version reverts."""
    if tool_name != "workflow.odoo_code_change" or not isinstance(arguments, dict) or arguments.get("writes"):
        return tool_name, arguments
    text = " ".join(
        str(arguments.get(key) or "")
        for key in ("task_description", "intent", "target_hint")
    )
    if not (_COMMIT_REVERT_INTENT_RE.search(text) and _COMMIT_REVERT_TARGET_RE.search(text)):
        return tool_name, arguments
    sha_match = _SHA_RE.search(text)
    version_match = _ODOO_VERSION_RE.search(text)
    routed = {
        key: arguments[key]
        for key in ("client_name", "project_path", "module_name", "commit_message")
        if arguments.get(key) not in {None, ""}
    }
    routed.update({
        "bad_sha": sha_match.group(0) if sha_match else None,
        "revert_latest": sha_match is None,
        "manifest_version": version_match.group(0) if version_match else None,
        "bump_manifest_version": bool(re.search(r"\b(?:manifest|version|release)\b", text, re.I)) and version_match is None,
        "push": True,
        "deploy_module": bool(arguments.get("module_name")),
    })
    return "workflow.restore_last_good_commit", routed


def _provider_lifecycle_route(tool_name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Route install-only requests away from the source code-change pipeline.

    This also protects clients that cached an older MCP tool list and still send
    an install request through workflow.odoo_code_change.
    """
    if tool_name != "workflow.odoo_code_change" or not isinstance(arguments, dict):
        return tool_name, arguments
    text = str(arguments.get("task_description") or "").strip()
    module_name = str(arguments.get("module_name") or "").strip().lower()
    lifecycle_target_named = bool(
        (module_name and module_name in text.lower())
        or re.search(r"\b(?:module|addon|app)\b", text, re.I)
    )
    if (
        arguments.get("create_module")
        or arguments.get("writes")
        or not _MODULE_LIFECYCLE_RE.search(text)
        or not lifecycle_target_named
        or _SOURCE_CHANGE_TERMS_RE.search(text)
    ):
        return tool_name, arguments
    routed = {
        key: arguments[key]
        for key in ("client_name", "project_path", "module_name")
        if arguments.get(key) not in {None, ""}
    }
    routed.update({
        "wait_for_result": False,
        "wait_timeout_seconds": 0,
    })
    return "odoo.module_deploy", routed

_FORBIDDEN_RECORD_MUTATION_RE = re.compile(r"\b(delete|remove|unlink|erase|purge|destroy|archive|deactivate)\b", re.I)
_ODOO_RECORD_TERMS_RE = re.compile(
    r"\b(record|records|data|database|customer|customers|employee|employees|contract|contracts|invoice|invoices|order|orders|product|products|partner|partners)\b",
    re.I,
)
_CODE_SURFACE_TERMS_RE = re.compile(r"\b(field|button|view|menu|method|file|class|xml|javascript|python|scss|css|label)\b", re.I)


def _forbidden_odoo_record_mutation(tool_name: str, arguments: dict[str, Any]) -> bool:
    if tool_name != "workflow.odoo_code_change":
        return False
    text = " ".join(str(arguments.get(key) or "") for key in ("task_description", "intent", "target_hint"))
    if not _FORBIDDEN_RECORD_MUTATION_RE.search(text) or not _ODOO_RECORD_TERMS_RE.search(text):
        return False
    explicit_data_target = bool(re.search(r"\b(record|records|data|database)\b", text, re.I))
    return explicit_data_target or not bool(_CODE_SURFACE_TERMS_RE.search(text))

# Backward-compatible names retained for tests/imports; all provider routes use
# the same minimal flow.
_CLAUDE_TOOL_NAMES = set(_PROVIDER_TOOL_NAMES)
_CHATGPT_TOOL_NAMES = set(_PROVIDER_TOOL_NAMES)
_GROK_TOOL_NAMES = set(_PROVIDER_TOOL_NAMES)


def provider_tool_names_safe() -> list[str]:
    """Return the exact strict tool names advertised to remote provider clients.

    Keep root/health metadata, tests, and MCP tools/list in sync from one
    source of truth so deployments cannot appear to expose only the old five
    tools after recovery tools are added.
    """
    ordered = [tool.name for tool in _tools_for_profile("provider")]
    return [_force_remote_tool_name(strict_safe_tool_name(name), idx) for idx, name in enumerate(ordered)]


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
    # All remote/provider MCP endpoints intentionally expose the same tiny tool
    # surface.  Full low-level tools remain available through REST /tools for
    # debugging/admin use, but not to provider agents.
    allowed = _OPENCODE_TOOL_NAMES if profile == "opencode" else _PROVIDER_TOOL_NAMES
    return [tool for tool in tools if tool.name in allowed]


def _remote_compatible_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a compact JSON schema accepted by strict remote MCP clients.

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


# Backward-compatible internal aliases for integrations/tests that imported the
# former vendor-specific helper names.
_normalize_chatgpt_structured = _normalize_remote_structured
_claude_schema = _remote_compatible_schema


def _tool_to_mcp(tool, profile: str | None = None) -> dict[str, Any]:
    title = getattr(tool, "title", None) or _humanize_tool_name(tool.name)
    if profile in _REMOTE_PROFILES:
        # Some remote-MCP builds are strict and can fail while
        # validating the initial tool list. For ChatGPT, keep initialize/tools/list
        # anonymous but mark every tool call as OAuth-protected so the client can
        # link/authenticate at call time instead of failing the connector handshake.
        item = {
            "name": _mcp_safe_tool_name(tool.name),
            "description": str(tool.description)[:900],
            "inputSchema": _remote_compatible_schema(tool_input_schema(tool.name)),
            "annotations": {
                "readOnlyHint": bool(tool.read_only),
                "destructiveHint": bool(tool.requires_approval),
                "openWorldHint": not bool(tool.read_only),
            },
        }
        return item
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
async def mcp_shell(request: Request, payload: Any = Body(...)) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Lightweight JSON-RPC MCP-compatible endpoint.

    It exposes initialize, tools/list, and tools/call over one HTTPS POST route.
    The same service functions are used by REST/OpenAPI and this MCP-style bridge.
    """
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_shell(item, profile="provider", request=request)
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_shell(payload, profile="provider", request=request)


async def _handle_shell(payload: dict[str, Any], profile: str | None = None, request: Request | None = None) -> dict[str, Any] | None:
    method = payload.get("method")
    request_id = payload.get("id")
    params = payload.get("params") or {}

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        next_prompt_source = "OPENCODE_SYSTEM_PROMPT.md" if profile == "opencode" else "MCP_SYSTEM_PROMPT.md"
        initialize_instructions = load_client_check_system_prompt()
        if profile == "opencode":
            initialize_instructions = f"{initialize_instructions}\n\n{load_opencode_system_prompt()}"
        return _shell_response(
            request_id,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {
                    "tools": {"listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": "odoo-mcp-agent-connector", "version": "1.1.0"},
                "instructions": initialize_instructions,
                "prompt_source": "CLIENT_CHECK_SYSTEM_PROMPT.md",
                "next_prompt_source_after_client_validation": next_prompt_source,
            },
        )
    if method == "prompts/list":
        workflow_prompt_name = "opencode_system_prompt" if profile == "opencode" else "mcp_system_prompt"
        workflow_prompt_title = "OpenCode Local Checkout Prompt" if profile == "opencode" else "Original MCP System Prompt"
        workflow_prompt_source = "OPENCODE_SYSTEM_PROMPT.md" if profile == "opencode" else "MCP_SYSTEM_PROMPT.md"
        return _shell_response(
            request_id,
            {
                "prompts": [
                    {
                        "name": "client_check_system_prompt",
                        "title": "Client Check Gate Prompt",
                        "description": "Pre-MCP client selection and validation gate from CLIENT_CHECK_SYSTEM_PROMPT.md.",
                        "arguments": [],
                    },
                    {
                        "name": workflow_prompt_name,
                        "title": workflow_prompt_title,
                        "description": f"Exact content from {workflow_prompt_source}. Load after client.check validation passes.",
                        "arguments": [],
                    },
                ]
            },
        )
    if method == "prompts/get":
        name = params.get("name")
        if name in {"client_check_system_prompt", "client_check", "CLIENT_CHECK_SYSTEM_PROMPT"}:
            prompt = load_client_check_system_prompt()
            return _shell_response(
                request_id,
                {
                    "description": "Pre-MCP client selection and validation gate from CLIENT_CHECK_SYSTEM_PROMPT.md.",
                    "messages": [
                        {
                            "role": "user",
                            "content": {"type": "text", "text": prompt},
                        }
                    ],
                    "source": "CLIENT_CHECK_SYSTEM_PROMPT.md",
                },
            )
        opencode_prompt_names = {"opencode_system_prompt", "system_prompt", "OPENCODE_SYSTEM_PROMPT"}
        normal_prompt_names = {"mcp_system_prompt", "system_prompt", "MCP_SYSTEM_PROMPT"}
        accepted_prompt_names = opencode_prompt_names if profile == "opencode" else normal_prompt_names
        if name not in accepted_prompt_names:
            return _shell_response(request_id, error={"code": -32602, "message": f"Unknown prompt: {name}"})
        prompt = load_opencode_system_prompt() if profile == "opencode" else load_mcp_system_prompt()
        prompt_source = "OPENCODE_SYSTEM_PROMPT.md" if profile == "opencode" else "MCP_SYSTEM_PROMPT.md"
        return _shell_response(
            request_id,
            {
                "description": f"Exact content from {prompt_source}. Load after client.check validation passes.",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": prompt},
                    }
                ],
                "source": prompt_source,
            },
        )
    if method == "tools/list":
        tools_payload = [_tool_to_mcp(tool, profile=profile) for tool in _tools_for_profile(profile)]
        return _shell_response(request_id, {"tools": _force_remote_tool_list(tools_payload)})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not name:
            return _shell_response(request_id, error={"code": -32602, "message": "tools/call requires params.name"})
        internal_name = _mcp_name_to_internal(name)
        try:
            if isinstance(arguments, dict) and profile in _REMOTE_PROFILES:
                internal_name, arguments = _provider_commit_revert_route(internal_name, arguments)
                internal_name, arguments = _provider_lifecycle_route(internal_name, arguments)
            allowed_tools = _OPENCODE_TOOL_NAMES if profile == "opencode" else _PROVIDER_TOOL_NAMES
            if profile in _REMOTE_PROFILES and internal_name not in allowed_tools:
                guard_structured = McpOperationGuardService().tool_profile_block(internal_name, arguments if isinstance(arguments, dict) else {})
                text = json.dumps(guard_structured, ensure_ascii=False, default=str)
                return _shell_response(
                    request_id,
                    {
                        "content": [{"type": "text", "text": text}],
                        "structuredContent": guard_structured,
                        "isError": False,
                    },
                )
            arguments = _arguments_for_profile(internal_name, arguments, profile)
            if isinstance(arguments, dict) and _forbidden_odoo_record_mutation(internal_name, arguments):
                structured = {
                    "status": "blocked",
                    "error_type": "odoo_record_delete_guard",
                    "read_only_enforced": True,
                    "success_allowed": False,
                    "message": "Destructive Odoo record deletion/archive requests cannot be routed through the query or code-change workflow.",
                    "next_call": None,
                }
                text = json.dumps(structured, ensure_ascii=False)
                return _shell_response(request_id, {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": structured,
                    "isError": False,
                })
            if (
                profile in _REMOTE_PROFILES
                and internal_name in _CLIENT_GATED_PROVIDER_TOOLS
                and isinstance(arguments, dict)
                and (
                    internal_name in {
                        "workflow.odoo_code_change",
                        "workflow.restore_last_good_commit",
                        "odoo.module_deploy",
                    }
                    or not str(arguments.get("client_name") or "").strip()
                )
            ):
                ClientGateService().require_validated_client(arguments.get("client_name"), internal_name)
            if settings.mcp_operation_guard_enabled and profile in _REMOTE_PROFILES and isinstance(arguments, dict):
                op_guard = McpOperationGuardService()
                arguments = op_guard.canonicalize_arguments(internal_name, arguments)
                guarded = op_guard.before_tool_call(internal_name, arguments)
                if guarded:
                    text = json.dumps(guarded, ensure_ascii=False, default=str)
                    return _shell_response(
                        request_id,
                        {
                            "content": [{"type": "text", "text": text}],
                            "structuredContent": guarded,
                            "isError": False,
                        },
                    )
            result = await _dispatch(internal_name, arguments)
            structured = _to_structured_content(result, tool_name=internal_name)
            if profile in _REMOTE_PROFILES:
                structured = _provider_safe_next_calls(structured)
            if settings.mcp_operation_guard_enabled and profile in _REMOTE_PROFILES and isinstance(arguments, dict):
                op_guard = McpOperationGuardService()
                op_guard.after_tool_call(internal_name, arguments, structured if isinstance(structured, dict) else {"result": structured})
                structured = op_guard.annotate_result(internal_name, arguments, structured if isinstance(structured, dict) else {"result": structured})
            if profile in _REMOTE_PROFILES and internal_name != "artifacts.read":
                structured = _normalize_remote_structured(structured, internal_name)
                structured = _provider_safe_next_calls(structured)
            text = json.dumps(structured, ensure_ascii=False, default=str)
            # Context-bloat guard: in long sessions, oversized tool outputs (large diffs,
            # logs, file reads) compound across many calls and slow down/derail the model.
            # Compatibility profiles also receive bounded structuredContent with artifact refs.
            text = _cap_text(text, max_chars=12000 if profile in _REMOTE_PROFILES else 24000)
            return _shell_response(
                request_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": structured,
                    "isError": False,
                },
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            is_client_gate = bool(detail.get("requires_client_selection") or detail.get("requires_validation"))
            if not is_client_gate:
                structured = {
                    "status": "failed",
                    "error_type": "http_error",
                    "http_status": exc.status_code,
                    **detail,
                    "success_allowed": False,
                }
                text = json.dumps(structured, ensure_ascii=False, default=str)
                return _shell_response(
                    request_id,
                    {
                        "content": [{"type": "text", "text": text}],
                        "structuredContent": structured,
                        "isError": True,
                    },
                )
            structured = {
                "status": "client_selection_required" if detail.get("requires_client_selection") else "client_validation_required",
                **detail,
                "success_allowed": False,
                "must_ask_user": bool(detail.get("requires_client_selection")),
                "next_call": None if detail.get("requires_client_selection") else detail.get("next_call"),
            }
            text = json.dumps(structured, ensure_ascii=False, default=str)
            return _shell_response(
                request_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": structured,
                    "isError": False,
                },
            )
        except ValidationError as exc:
            message = format_tool_validation_error(exc)
            return _shell_response(
                request_id,
                {
                    "content": [{"type": "text", "text": message}],
                    "structuredContent": {
                        "status": "failed",
                        "error": message,
                        "error_type": "tool_input_invalid",
                    },
                    "isError": True,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _shell_response(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
    return _shell_response(request_id, error={"code": -32601, "message": f"Unknown method: {method}"})


@claude_router.get("/mcp")
def claude_mcp_health() -> dict[str, Any]:
    return {"status": "ok", "endpoint": "unified-provider-mcp", "transport": "http-post-json-rpc", "tools_profile": "unified-provider-safe"}


@claude_router.post("/mcp")
async def claude_mcp_shell(payload: Any = Body(...)) -> dict[str, Any] | list[dict[str, Any]] | None:
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_shell(item, profile="claude")
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_shell(payload, profile="claude")


@chatgpt_router.get("/mcp")
def chatgpt_mcp_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "endpoint": "unified-provider-mcp",
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
async def chatgpt_mcp_shell(request: Request, payload: Any = Body(...)) -> dict[str, Any] | list[dict[str, Any]] | None:
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_shell(item, profile="chatgpt")
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_shell(payload, profile="chatgpt")


@grok_router.get("/mcp")
def grok_mcp_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "endpoint": "grok-mcp",
        "transport": "http-post-json-rpc",
        "tool_names": "strict_regex_safe",
        "profile": "unified-provider-safe",
    }


@grok_router.get("/tools-debug")
def grok_tools_debug() -> dict[str, Any]:
    # Grok gets a narrow, safe profile per xAI Remote MCP guidance: fewer tools, no low-level deploy bypasses.
    tools_payload = _force_remote_tool_list([_tool_to_mcp(tool, profile="grok") for tool in _tools_for_profile("grok")])
    bad = [tool.get("name") for tool in tools_payload if not _REMOTE_TOOL_NAME_RE.match(str(tool.get("name", "")))]
    return {
        "status": "ok" if not bad else "bad_names",
        "count": len(tools_payload),
        "bad_names": bad,
        "first_names": [tool.get("name") for tool in tools_payload[:20]],
    }


@grok_router.post("/mcp")
async def grok_mcp_shell(request: Request, payload: Any = Body(...)) -> dict[str, Any] | list[dict[str, Any]] | None:
    # Keep Grok on the same hybrid-safe contract as ChatGPT: short bounded waits,
    # OAuth/tool-call auth, strict tool names, and success_allowed=false until terminal proof.
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_shell(item, profile="grok")
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_shell(payload, profile="grok")


@opencode_router.get("/mcp")
def opencode_mcp_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "endpoint": "opencode-local-checkout-mcp",
        "transport": "http-post-json-rpc",
        "profile": "local-checkout-first",
        "custom_module_source": "opencode-native-local-filesystem",
        "odoosh_custom_source_reads": False,
        "git_sync": "opencode-native-fetch-and-pull-if-behind",
        "git_push": "opencode-native-then-mcp-expected-sha-handoff",
    }


@opencode_router.post("/mcp")
async def opencode_mcp_shell(request: Request, payload: Any = Body(...)) -> dict[str, Any] | list[dict[str, Any]] | None:
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_shell(item, profile="opencode", request=request)
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_shell(payload, profile="opencode", request=request)


@router.get("/system-prompt")
def mcp_system_prompt() -> dict[str, str]:
    return shared_prompt_payload()


@router.get("/client-check-prompt")
def mcp_client_check_prompt() -> dict[str, str]:
    return client_check_prompt_payload()


@claude_router.get("/system-prompt")
def claude_system_prompt() -> dict[str, str]:
    return shared_prompt_payload()


@chatgpt_router.get("/system-prompt")
def chatgpt_system_prompt() -> dict[str, str]:
    return shared_prompt_payload()


@chatgpt_router.get("/client-check-prompt")
def chatgpt_client_check_prompt() -> dict[str, str]:
    return client_check_prompt_payload()


@grok_router.get("/system-prompt")
def grok_system_prompt() -> dict[str, str]:
    return shared_prompt_payload()


@grok_router.get("/client-check-prompt")
def grok_client_check_prompt() -> dict[str, str]:
    return client_check_prompt_payload()


@opencode_router.get("/system-prompt")
def opencode_system_prompt() -> dict[str, str]:
    return opencode_prompt_payload()


@opencode_router.get("/client-check-prompt")
def opencode_client_check_prompt() -> dict[str, str]:
    return client_check_prompt_payload()


@claude_router.get("/client-check-prompt")
def claude_client_check_prompt() -> dict[str, str]:
    return client_check_prompt_payload()


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


@qwen_router.get("/system-prompt/{access_token}")
def qwen_system_prompt_path(access_token: str) -> dict[str, str]:
    _validate_qwen_url_token(access_token)
    return shared_prompt_payload()


@qwen_router.get("/system-prompt")
def qwen_system_prompt_query(access_token: str = Query(default="")) -> dict[str, str]:
    _validate_qwen_url_token(access_token)
    return shared_prompt_payload()


@qwen_router.get("/mcp/{access_token}")
def qwen_mcp_path_health(access_token: str) -> dict[str, Any]:
    _validate_qwen_url_token(access_token)
    return {"status": "ok", "endpoint": "qwen-url-token-mcp", "transport": "http-post-json-rpc"}


@qwen_router.get("/mcp")
def qwen_mcp_query_health(access_token: str = Query(default="")) -> dict[str, Any]:
    _validate_qwen_url_token(access_token)
    return {"status": "ok", "endpoint": "qwen-url-token-mcp", "transport": "http-post-json-rpc"}


@qwen_router.post("/mcp/{access_token}")
async def qwen_mcp_path_shell(access_token: str, payload: Any = Body(...)) -> dict[str, Any] | list[dict[str, Any]] | None:
    _validate_qwen_url_token(access_token)
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_shell(item, profile="qwen")
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_shell(payload, profile="qwen")


@qwen_router.post("/mcp")
async def qwen_mcp_query_shell(payload: Any = Body(...), access_token: str = Query(default="")) -> dict[str, Any] | list[dict[str, Any]] | None:
    _validate_qwen_url_token(access_token)
    if isinstance(payload, list):
        responses = []
        for item in payload:
            response = await _handle_shell(item, profile="qwen")
            if response is not None:
                responses.append(response)
        return responses
    return await _handle_shell(payload, profile="qwen")
