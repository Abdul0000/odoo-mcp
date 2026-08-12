from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from pydantic import ValidationError

from ..auth import AuthDependency
from ..config import settings
from ..services.oauth_service import OAuthStore
from ..services.prompt_service import load_mcp_system_prompt, load_client_check_system_prompt, shared_prompt_payload, client_check_prompt_payload
from ..services.qwen_token_service import QwenTokenService
from ..services.store_service import JsonStore
from ..services.mcp_operation_guard_service import McpOperationGuardService
from ..services.workflow_watchdog_service import WorkflowWatchdogService
from .tools import _dispatch, format_tool_validation_error, internal_tool_name, list_tools, strict_safe_tool_name, tool_input_schema, tool_output_schema

router = APIRouter(prefix="/mcp", tags=["mcp"], dependencies=[AuthDependency])
qwen_router = APIRouter(prefix="/qwen", tags=["mcp-qwen"] )
claude_router = APIRouter(prefix="/claude", tags=["mcp-claude"], dependencies=[AuthDependency])
chatgpt_router = APIRouter(prefix="/chatgpt", tags=["mcp-chatgpt"], dependencies=[AuthDependency])
grok_router = APIRouter(prefix="/grok", tags=["mcp-grok"], dependencies=[AuthDependency])


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
    """Return the server's backward-compatible object response envelope.

    Several tools (repo.list, repo.list_custom_modules, jobs.list,
    approvals.list, audit.list, workflow tools returning matches, etc.)
    naturally return a bare list of Pydantic models. Older provider clients
    used by this connector reject a raw list at the top level with:
        "Input should be a valid dictionary [type=dict_type]"
    Wrap any non-dict result in {"items": [...], "count": N} so every tool
    call stays compatible while newer MCP clients can still consume it as JSON.
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


_CONTROL_SCHEMA_VERSION = "1.0"
_CONTROL_REQUIRED_FIELDS = [
    "status",
    "outcome",
    "terminal",
    "completion_required",
    "success_allowed",
    "next_call",
    "input_required",
    "error",
]


def _ensure_provider_control_envelope(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    """Add one deterministic provider-facing control contract.

    Existing workflow status values and payload fields stay untouched. The added
    fields let a remote agent decide whether to continue, ask the user, retry, or
    stop without interpreting prose. Incomplete job responses receive a safe poll
    call when the workflow returned a job id but omitted its continuation.
    """
    out = dict(value) if isinstance(value, dict) else {"result": value}
    status_value = out.get("status")
    status_text = str(status_value or ("failed" if is_error else "ok"))
    status_key = status_text.strip().lower().replace("-", "_").replace(" ", "_")

    error_like = is_error or any(token in status_key for token in ("failed", "error", "invalid"))
    blocked_like = any(token in status_key for token in ("blocked", "stale"))
    input_like = any(token in status_key for token in ("clarification", "authorization", "approval", "input_required"))
    pending_like = (
        status_key.startswith("needs_")
        or any(token in status_key for token in ("running", "queued", "started", "waiting", "pending", "required", "ready_to_deploy"))
    )

    next_call = out.get("next_call") if isinstance(out.get("next_call"), dict) else None
    explicit_completion = out.get("completion_required")
    explicit_success = out.get("success_allowed")
    if isinstance(explicit_completion, bool):
        completion_required = explicit_completion
    else:
        completion_required = bool(next_call or pending_like or (explicit_success is False and not error_like))

    if completion_required and next_call is None and out.get("job_id"):
        next_call = {
            "tool": "jobs.get",
            "arguments": {
                "job_id": str(out["job_id"]),
                "wait_for_result": True,
                "wait_timeout_seconds": 600,
            },
        }

    input_required = bool(out.get("input_required")) or input_like
    if completion_required and next_call is None:
        # Never leave a non-terminal response without an executable continuation
        # or an explicit signal that human input is required.
        input_required = True

    if error_like:
        outcome = "failed"
    elif input_required:
        outcome = "input_required"
    elif blocked_like:
        outcome = "blocked"
    elif completion_required:
        outcome = "pending"
    else:
        outcome = "success"

    success_allowed = False if completion_required or error_like else (
        explicit_success if isinstance(explicit_success, bool) else outcome == "success"
    )
    error_value = out.get("error")
    error_code = out.get("error_code") or out.get("error_type")
    retryable_value = out.get("retryable")

    out.update(
        {
            "schema_version": str(out.get("schema_version") or _CONTROL_SCHEMA_VERSION),
            "status": status_text,
            "outcome": outcome,
            "terminal": not completion_required,
            "completion_required": completion_required,
            "success_allowed": bool(success_allowed),
            "next_call": next_call,
            "input_required": input_required,
            "error": error_value if error_value is not None else None,
            "error_code": str(error_code) if error_code is not None else None,
            "retryable": retryable_value if isinstance(retryable_value, bool) else None,
        }
    )
    return out


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
    if internal_tool_name(str(out.get("tool") or "")) == "jobs.get" and isinstance(out.get("arguments"), dict):
        arguments = dict(out["arguments"])
        requested = int(arguments.get("wait_timeout_seconds", 600) or 600)
        arguments["wait_timeout_seconds"] = WorkflowWatchdogService().provider_wait_timeout(requested)
        out["arguments"] = arguments
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

    truncated_any = False

    def normalize(item: Any, *, list_limit: int = 50) -> Any:
        nonlocal truncated_any
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="json")
        if isinstance(item, list):
            if len(item) > list_limit:
                truncated_any = True
            # Preserve the advertised array type. Metadata is added by the
            # containing object and the exact payload is retained as an artifact.
            return [normalize(child) for child in item[:list_limit]]
        if not isinstance(item, dict):
            text = str(item)
            if len(text) <= 8000:
                return item
            truncated_any = True
            # Keep scalar fields scalar so outputSchema types remain truthful.
            return _preview_text(text, 8000)

        out: dict[str, Any] = {}
        for key, child in item.items():
            if key in {"stdout", "stderr", "content"} and isinstance(child, str) and len(child) > 12000:
                artifact = store.create_artifact(key, tool_name, child)
                out[key] = _preview_text(child, 12000)
                out[f"{key}_truncated"] = True
                out[f"{key}_artifact"] = artifact
                out[f"{key}_next_call"] = _artifact_next_call(artifact)
            elif isinstance(child, list):
                limit = 25 if key == "steps" else 50
                out[key] = normalize(child, list_limit=limit)
                if len(child) > limit:
                    out[f"{key}_count"] = len(child)
                    out[f"{key}_truncated"] = True
            else:
                out[key] = normalize(child)
        return out

    normalized = normalize(value)
    if truncated_any and isinstance(normalized, dict):
        artifact = store.create_artifact("structured", tool_name, value)
        normalized["truncated"] = True
        normalized["details_ref"] = artifact
        normalized["details_next_call"] = _artifact_next_call(artifact)
        normalized["next_action"] = "Use details_next_call for exact output, or re-call the source tool with a narrower filter."
    normalized_json = json.dumps(normalized, ensure_ascii=False, default=str)
    if isinstance(normalized, dict) and len(normalized_json) <= 24000:
        return normalized

    artifact = store.create_artifact("structured", tool_name, value)
    source = normalized if isinstance(normalized, dict) else {}
    compact_keys = {
        "schema_version", "status", "outcome", "phase", "job_id", "terminal",
        "completion_required", "success_allowed", "next_call", "input_required",
        "error", "error_code", "retryable", "deploy_confirmed",
        "reproduction_passed", "expected_result_verified", "claim_status", "truth_gate",
    }
    compact = {key: source[key] for key in compact_keys if key in source}
    compact.update({
        "truncated": True,
        "preview": _preview_text(full_json, 12000),
        "artifact": artifact,
        "details_ref": artifact,
        "details_next_call": _artifact_next_call(artifact),
        "next_action": "Use details_next_call for exact output; preserve next_call for workflow continuation.",
    })
    return compact


def _arguments_for_profile(tool_name: str, arguments: dict[str, Any], profile: str | None) -> dict[str, Any]:
    adjusted = dict(arguments)

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
        # Poll briefly so the UI never hangs. A running response carries
        # completion_required=true and success_allowed=false, so providers cannot
        # honestly exit early or report success until a terminal job result is read.
        if "wait_for_result" not in adjusted:
            adjusted["wait_for_result"] = True
        if adjusted.get("wait_for_result") is True:
            requested_wait = min(max(int(adjusted.get("wait_timeout_seconds", 600) or 600), 1), 600)
            adjusted["wait_timeout_seconds"] = WorkflowWatchdogService().provider_wait_timeout(requested_wait)
            adjusted["poll_interval_seconds"] = min(max(float(adjusted.get("poll_interval_seconds", 1.0) or 1.0), 0.25), 10.0)
            return adjusted
        if profile not in {"provider", "claude", "chatgpt", "grok", "qwen"} or "wait_for_result" not in adjusted:
            return adjusted

    # Keep the legacy ChatGPT-only quick-wait clamp for non-safe-deploy tools.
    # Other MCP clients keep their previous behaviour unless they explicitly use
    # safe_deploy/jobs.get above.
    if profile not in {"provider", "claude", "chatgpt", "grok", "qwen"} or tool_name not in _CHATGPT_QUICK_WAIT_TOOLS:
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
    "skills.list",
    "skills.read",
    "client.check",
    "workflow.agent_instructions",
    "workflow.odoo_code_change",
    "workflow.odoo_debug_and_fix",
    "workflow.module_analyze",
    "workflow.module_test",
    "workflow.module_install_and_check",
    "workflow.frontend_visual_implement",
    # Live read-only model access. OdooService uses Odoo.sh SSH first and falls
    # back to HTTPS JSON-RPC; no raw SQL or write method is exposed.
    "odoo.query",
    "odoo.search_read",
    "odoo.read_group",
    "odoo.record_create",
    "odoo.record_write",
    "odoo.record_action",
    "odoo.flow_test",
    "odoo.mutation_audit",
    # Source discovery and verification are server-owned steps inside the
    # composite workflows. Keeping them off the provider surface avoids extra
    # planning loops and repeated input-schema payloads.
    # Recovery/continuation tools that provider responses may legitimately return
    # in next_call.  Exposing them keeps the MCP contract honest and prevents
    # agents from retrying the heavy code-change workflow just to read logs or
    # resume a failed safe-deploy step.
    "artifacts.read",
    "workflow.resume_job",
    "jobs.list",
    "jobs.get",
    "odoosh.error_pack",
}

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


_PROVIDER_COMPACT_TOOL_DESCRIPTIONS = {
    "client.check": (
        "Select and validate the configured Odoo client before protected work. Without client_name it lists choices; "
        "with client_name and validate=true it prepares/verifies checkout, Git, Odoo.sh SSH, Odoo shell, and addons. "
        "Slow validation returns job_id/next_call; poll jobs.get. Writes, Git, and deploy stay blocked until validation passes."
    ),
    "workflow.odoo_code_change": (
        "Server-owned Odoo code workflow for existing changes or complete new addons. It prepares checkout, resolves the "
        "module/target, returns focused contexts, applies one complete writes set, runs QA/core verification, and starts "
        "safe deploy. On needs_writes, preserve coding_session_id/workspace_session_id and call once with generated writes. "
        "Poll the returned job; running or pushed alone is not success. On workflow_busy, wait retry_after_seconds and retry unchanged."
    ),
    "workflow.odoo_debug_and_fix": (
        "Bounded runtime-debug workflow. It collects redacted evidence, diagnoses the exact failure, routes focused writes "
        "through workflow.odoo_code_change, and replays an immutable assertion after deployment. Preserve debug/session IDs "
        "and continue returned calls. Claim fixed only when truth_gate.verified_fixed=true."
    ),
    "workflow.module_install_and_check": (
        "Fast path for an explicit module install, upgrade, or install-and-fix request. It starts the guarded Odoo job and "
        "returns job_id/next_call for jobs.get. Repair source only from its concrete traceback or dependency error; an "
        "uninstallable registry state alone is not permission to edit code."
    ),
    "workflow.frontend_visual_implement": (
        "Odoo visual workflow for POS/OWL/dashboard/website/backend UI assets and styling. It generates deterministic "
        "SCSS/SVG/XML/JS writes, reads native context, verifies, and uses safe deploy. Use create_module=true only for a new "
        "standalone root addon; otherwise modify the validated custom addon without editing Odoo core."
    ),
}


def _provider_tool_description(tool) -> str:
    """Return a complete semantic description without cutting a sentence mid-word."""
    description = _PROVIDER_COMPACT_TOOL_DESCRIPTIONS.get(tool.name) or " ".join(str(tool.description).split())
    if len(description) <= 500:
        return description
    compact = description[:497].rsplit(" ", 1)[0].rstrip(" ,;:")
    return compact + "."

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
    return [tool for tool in tools if tool.name in _PROVIDER_TOOL_NAMES]


def _remote_compatible_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a compact JSON schema accepted by strict remote MCP clients.

    Claude's remote-MCP frontend can fail the message stream while loading tools
    when schemas contain lots of generated Pydantic metadata or nested $defs. This
    removes non-essential metadata and simplifies nullable anyOf values.
    """
    definitions = schema.get("$defs") if isinstance(schema.get("$defs"), dict) else {}

    def clean(value: Any) -> Any:
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, dict):
            return value

        if "$ref" in value:
            ref = str(value.get("$ref") or "")
            definition_name = ref.rsplit("/", 1)[-1]
            if definition_name in {
                "OdooDebugRecordAssertion",
                "OdooDebugRuntimeReplay",
                "OdooDynamicFlowStep",
            }:
                target = definitions.get(definition_name)
                if isinstance(target, dict):
                    return clean(target)
            return {"type": "object"}
        if "anyOf" in value:
            non_null = [item for item in value.get("anyOf", []) if not (isinstance(item, dict) and item.get("type") == "null")]
            if len(non_null) == 1:
                base = clean(non_null[0])
                if "description" in value and isinstance(base, dict):
                    base.setdefault("description", " ".join(str(value["description"]).split())[:80])
                return base
            return {
                "type": "string",
                "description": " ".join(str(value.get("description", "JSON value")).split())[:80],
            }

        out: dict[str, Any] = {}
        for key, child in value.items():
            if key in {"$defs", "title", "default", "examples", "computed_fields"}:
                continue
            if key == "description" and isinstance(child, str):
                # Tool descriptions carry the workflow contract. Field docs only
                # need a compact hint; long examples materially inflate every
                # tools/list response and the provider's planning context.
                out[key] = " ".join(str(child).split())[:80]
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
    output_schema = tool_output_schema(tool.name)
    if profile in {"provider", "claude", "chatgpt", "grok", "qwen"}:
        # Some remote-MCP builds are strict and can fail while
        # validating the initial tool list. For ChatGPT, keep initialize/tools/list
        # anonymous but mark every tool call as OAuth-protected so the client can
        # link/authenticate at call time instead of failing the connector handshake.
        item = {
            "name": _mcp_safe_tool_name(tool.name),
            "description": _provider_tool_description(tool),
            "inputSchema": _remote_compatible_schema(tool_input_schema(tool.name)),
            "annotations": {
                "readOnlyHint": bool(tool.read_only),
                "destructiveHint": bool(tool.destructive or tool.requires_approval),
                "idempotentHint": bool(tool.idempotent),
                "openWorldHint": bool(tool.open_world),
            },
        }
        if output_schema:
            # Provider planning needs the stable control envelope, not every
            # tool-specific diagnostic branch. Full structured data is still
            # returned at call time and large results remain artifact-readable.
            compact_output = {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "outcome": {"type": "string", "enum": ["success", "pending", "blocked", "failed", "input_required"]},
                    "message": {"type": "string"},
                    "job_id": {"type": ["string", "null"]},
                    "terminal": {"type": "boolean"},
                    "success_allowed": {"type": "boolean"},
                    "completion_required": {"type": "boolean"},
                    "next_call": {"type": ["object", "null"], "additionalProperties": True},
                    "input_required": {"type": "boolean"},
                    "error": {"type": ["string", "object", "null"], "additionalProperties": True},
                },
                "required": list(_CONTROL_REQUIRED_FIELDS),
                "additionalProperties": True,
            }
            preserve = {
                "workflow.odoo_code_change": {"handoff", "coding_session", "coding_workspace", "inspection_artifact", "write_generation"},
                "workflow.odoo_debug_and_fix": {
                    "debug_session", "deploy_confirmed", "reproduction_passed", "expected_result_verified",
                    "claim_status", "truth_gate", "diagnosis", "handoff",
                },
                "skills.read": {"name", "description", "content"},
                "artifacts.read": {
                    "artifact_id", "kind", "source_tool", "created_at", "bytes",
                    "offset", "returned_chars", "next_offset", "text",
                },
            }.get(tool.name, set())
            source_properties = output_schema.get("properties") or {}
            for key in preserve:
                if key in source_properties:
                    property_schema = _remote_compatible_schema(source_properties[key])
                    if source_properties[key].get("type") != "object":
                        property_schema.pop("properties", None)
                    compact_output["properties"][key] = property_schema
            item["outputSchema"] = compact_output
        return item
    item = {
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
            "destructiveHint": bool(tool.destructive or tool.requires_approval),
            "idempotentHint": bool(tool.idempotent),
            "openWorldHint": bool(tool.open_world),
        },
    }
    if output_schema:
        item["outputSchema"] = output_schema
    return item





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
        return _shell_response(
            request_id,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {
                    # Clients must refresh the tool manifest after a connector
                    # update/redeploy. Advertising listChanged avoids stale
                    # manifests that produce "Resource not found" for newly
                    # registered composite workflows.
                    "tools": {"listChanged": True},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": "odoo-mcp-agent-connector", "version": "1.8.0"},
                "instructions": load_client_check_system_prompt(),
                "prompt_source": "CLIENT_CHECK_SYSTEM_PROMPT.md",
                "next_prompt_source_after_client_validation": "MCP_SYSTEM_PROMPT.md",
            },
        )
    if method == "prompts/list":
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
                        "name": "mcp_system_prompt",
                        "title": "Original MCP System Prompt",
                        "description": "Exact content from MCP_SYSTEM_PROMPT.md. Load after client.check validation passes.",
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
        if name not in {"mcp_system_prompt", "system_prompt", "MCP_SYSTEM_PROMPT"}:
            return _shell_response(request_id, error={"code": -32602, "message": f"Unknown prompt: {name}"})
        prompt = load_mcp_system_prompt()
        return _shell_response(
            request_id,
            {
                "description": "Exact content from MCP_SYSTEM_PROMPT.md. Load after client.check validation passes.",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": prompt},
                    }
                ],
                "source": "MCP_SYSTEM_PROMPT.md",
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
            if profile in {"provider", "claude", "chatgpt", "grok", "qwen"} and internal_name not in _PROVIDER_TOOL_NAMES:
                guard_structured = McpOperationGuardService().tool_profile_block(internal_name, arguments if isinstance(arguments, dict) else {})
                guard_structured = _provider_safe_next_calls(_ensure_provider_control_envelope(guard_structured))
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
            if settings.mcp_operation_guard_enabled and profile in {"provider", "claude", "chatgpt", "grok", "qwen"} and isinstance(arguments, dict):
                op_guard = McpOperationGuardService()
                arguments = op_guard.canonicalize_arguments(internal_name, arguments)
                guarded = op_guard.before_tool_call(internal_name, arguments)
                if guarded:
                    guarded = _provider_safe_next_calls(_ensure_provider_control_envelope(guarded))
                    text = json.dumps(guarded, ensure_ascii=False, default=str)
                    return _shell_response(
                        request_id,
                        {
                            "content": [{"type": "text", "text": text}],
                            "structuredContent": guarded,
                            "isError": False,
                        },
                    )
            if settings.workflow_watchdog_enabled and profile in {"provider", "claude", "chatgpt", "grok", "qwen"}:
                result = await WorkflowWatchdogService().run_tool_call(
                    internal_name,
                    lambda: _dispatch(internal_name, arguments),
                )
            else:
                result = await _dispatch(internal_name, arguments)
            structured = _to_structured_content(result, tool_name=internal_name)
            if profile in {"provider", "claude", "chatgpt", "grok", "qwen"}:
                structured = _provider_safe_next_calls(structured)
            if settings.mcp_operation_guard_enabled and profile in {"provider", "claude", "chatgpt", "grok", "qwen"} and isinstance(arguments, dict):
                op_guard = McpOperationGuardService()
                op_guard.after_tool_call(internal_name, arguments, structured if isinstance(structured, dict) else {"result": structured})
                structured = op_guard.annotate_result(internal_name, arguments, structured if isinstance(structured, dict) else {"result": structured})
            if profile in {"provider", "claude", "chatgpt", "grok", "qwen"}:
                structured = _ensure_provider_control_envelope(structured)
                if internal_name != "artifacts.read":
                    structured = _normalize_remote_structured(structured, internal_name)
                structured = _provider_safe_next_calls(_ensure_provider_control_envelope(structured))
            text = json.dumps(structured, ensure_ascii=False, default=str)
            # Context-bloat guard: in long sessions, oversized tool outputs (large diffs,
            # logs, file reads) compound across many calls and slow down/derail the model.
            # Compatibility profiles also receive bounded structuredContent with artifact refs.
            text = _cap_text(text, max_chars=12000 if profile in {"provider", "claude", "chatgpt", "grok", "qwen"} else 24000)
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
            structured_error = _provider_safe_next_calls(
                _ensure_provider_control_envelope(
                    {
                        "status": "failed",
                        "error": message,
                        "error_type": "tool_input_invalid",
                        "error_code": "tool_input_invalid",
                        "retryable": False,
                    },
                    is_error=True,
                )
            )
            return _shell_response(
                request_id,
                {
                    "content": [{"type": "text", "text": message}],
                    "structuredContent": structured_error,
                    "isError": True,
                },
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            structured_error = _provider_safe_next_calls(
                _ensure_provider_control_envelope(
                    {
                        "status": "failed",
                        "error": message,
                        "error_code": type(exc).__name__,
                        "retryable": None,
                    },
                    is_error=True,
                )
            )
            return _shell_response(
                request_id,
                {
                    "content": [{"type": "text", "text": message}],
                    "structuredContent": structured_error,
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
