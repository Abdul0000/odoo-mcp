from __future__ import annotations

import os
import uuid

from fastapi import FastAPI, Request

from .config import settings

from .routers.approvals import router as approvals_router
from .routers.browser import router as browser_router
from .routers.git import router as git_router
from .routers.jobs import router as jobs_router
from .routers.mcp import (
    router as mcp_router,
    qwen_router as qwen_mcp_router,
    claude_router as claude_mcp_router,
    chatgpt_router as chatgpt_mcp_router,
    grok_router as grok_mcp_router,
    provider_tool_names_safe,
)
from .routers.odoo import router as odoo_router
from .routers.odoosh import router as odoosh_router
from .routers.oauth import router as oauth_router
from .routers.qa import router as qa_router
from .routers.repo import router as repo_router
from .routers.tools import router as tools_router
from .routers.workflow import router as workflow_router
from .services.prompt_service import load_mcp_system_prompt, load_client_check_system_prompt
from .services.skill_service import SkillService

if os.getenv("RENDER", "").lower() in {"1", "true", "yes"} and settings.api_token == "change-me":
    raise RuntimeError("ODOO_AGENT_API_TOKEN must be set to a non-default secret on Render")

app = FastAPI(
    title="Odoo MCP Agent Connector",
    version="1.7.0",
    description=(
        "Provider-safe Odoo 16-19 development connector with server-owned source discovery, "
        "verification, Git, Odoo.sh deployment, recovery jobs, OAuth, and on-demand skills."
    ),
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "odoo-mcp-agent-connector"}


@app.get("/agent-instructions")
def agent_instructions() -> dict:
    """Full agent system prompt stored in connector.
    MCP client system instructions can contain only:
    'Before any task, call workflow.agent_instructions first and follow exactly.'
    """
    from .config import settings, get_client_names
    from .services.client_resolver_service import ClientResolverService
    from .services.client_gate_service import ClientGateService
    client_names = get_client_names()
    if len(client_names) > 1 and not ClientGateService().any_validated_client():
        return {
            "version": "client-check-gate",
            "prompt_source": "CLIENT_CHECK_SYSTEM_PROMPT.md",
            "system_prompt": load_client_check_system_prompt(),
            "requires_client_selection": True,
            "requires_validation": True,
            "available_client_names": client_names,
            "next_action": "Call client.check, ask the user to select client_name, then call client.check(client_name=..., validate=true). After it passes, call workflow.agent_instructions again to load MCP_SYSTEM_PROMPT.md.",
        }
    try:
        active_client = ClientResolverService().resolve_client(require_complete=False)
    except Exception:
        active_client = None
    repo = (active_client.repo if active_client else None) or settings.target_git_repo or "<repo_url>"
    branch = (active_client.branch if active_client else None) or settings.target_git_branch or "<branch>"
    path = (active_client.path if active_client else None) or settings.target_project_path or "/workspace/repos/<repo>"

    return {
        "version": "v18-compact",
        "prompt_source": "MCP_SYSTEM_PROMPT.md",
        "system_prompt": load_mcp_system_prompt(),
        "instructions": {
            "role": "Odoo 16-19 development agent. Use the provider-safe composite workflow; installed source is final authority.",
            "rules": [
                "Select and validate the client before any coding workflow when multiple clients exist.",
                "Load only the matching skill with skills.read; never preload all skill bodies.",
                "For deterministic changes—including controller/cron/configuration edits—call workflow.odoo_code_change. For actual runtime errors, unclear behavior, repeated failures, controller/cron/API execution failures, browser/security/performance issues call workflow.odoo_debug_and_fix, which reuses code-change and safe-deploy ownership and requires replay proof. For read-only custom-addon explanation call workflow.module_analyze; for visual generation call workflow.frontend_visual_implement; for explicit install/upgrade/install-and-fix call workflow.module_install_and_check immediately.",
                "Do not call hidden repo, Git, source-discovery, verification, or deploy tools; the composite workflow owns those steps.",
                "Follow workflow_profile for adaptive small/medium/large scope. After focused inspection, ask the returned single question only for status=needs_clarification; otherwise resolve technical details from source. For status=needs_writes, generate one contract-complete writes set, never deploy partial local batches, and use jobs.get or workflow.resume_job until terminal.",
                "When repair_required=true, must_continue=true, or contract_status=pending, treat it as non-terminal: generate only the blocking repairs from structure_findings and execute next_call immediately without summarizing or asking the user; allow at most three diagnosed repair cycles.",
                "When deploy_confirmed or already_deployed_confirmed is true, stop and summarize for normal workflows; for debug work manual text/booleans remain untrusted, continue through replay, and say fixed only when truth_gate.verified_fixed=true—otherwise preserve claim_status such as implemented_unverified or blocked_with_evidence.",
                "Produce production-ready, task-scoped code: clean, minimal, secure, batch-safe, and compatible with the installed Odoo version. Preserve architecture, prevent N+1 queries, use environment-aware tests, prefer ORM/framework built-ins, use parameterized SQL only when justified, scope sudo/security correctly, and avoid unrelated refactoring. Never expose credentials, create normal module schema through Studio/ORM shortcuts, or put Pydantic code inside Odoo addons.",
            ],
            "provider_flow": {
                "code_change": "workflow.odoo_code_change",
                "debug_and_fix": "workflow.odoo_debug_and_fix",
                "module_analysis": "workflow.module_analyze",
                "visual_change": "workflow.frontend_visual_implement",
                "direct_install_check": "workflow.module_install_and_check",
                "continuation": ["jobs.get", "workflow.resume_job"],
                "diagnostics": ["artifacts.read", "odoosh.error_pack"],
                "terminal_success": ["deploy_confirmed=true", "already_deployed_confirmed=true"],
                "debug_terminal_success": "truth_gate.verified_fixed=true",
                "terminal_action": "stop_and_summarize",
                "inspect_then_write": "If the composite workflow requests writes, use its focused inspection result and call the same composite tool again with complete writes.",
            },
            "skills": {
                "mode": "on_demand",
                "list_tool": "skills.list",
                "read_tool": "skills.read",
                "selection": (
                    "For an unclear domain, call skills.list with task_description and load only its "
                    "single primary recommendation. Do not load overlapping skills."
                ),
                "available": SkillService.names(),
            },
            "multi_client": {
                "available_clients": client_names if client_names else ["(using TARGET_GIT_* configuration)"],
                "selection_rule": "When more than one client exists, client.check must pass for the explicitly selected client before workflow calls.",
            },
            "config": {
                "repo_url": repo,
                "branch": branch,
                "project_path": path,
            },
        },
    }

@app.get("/")
def root() -> dict[str, object]:
    return {
        "service": "Odoo MCP Agent Connector",
        "ui": "not_required",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "tools": "/tools/list",
        "mcp": "/mcp",
        "mcp_profile": "unified-provider-safe",
        # Derived from routers.mcp._PROVIDER_TOOL_NAMES, not hardcoded, so root
        # metadata stays consistent with the actual MCP tools/list response.
        "provider_tools": provider_tool_names_safe(),
        "compatibility_mcp": {
            "chatgpt": "/chatgpt/mcp",
            "claude": "/claude/mcp",
            "qwen": "/qwen/mcp",
            "grok": "/grok/mcp",
        },
        "workflow": ["/workflow/capability-check", "/workflow/full-verify", "/workflow/safe-deploy-module"],
    }


app.include_router(oauth_router)
app.include_router(qa_router)
app.include_router(repo_router)
app.include_router(git_router)
app.include_router(odoosh_router)
app.include_router(odoo_router)
app.include_router(browser_router)
app.include_router(approvals_router)
app.include_router(jobs_router)
app.include_router(tools_router)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex[:12])
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response

app.include_router(mcp_router)
app.include_router(qwen_mcp_router)
app.include_router(claude_mcp_router)
app.include_router(chatgpt_mcp_router)
app.include_router(grok_mcp_router)
app.include_router(workflow_router)
