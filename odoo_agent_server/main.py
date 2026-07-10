from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded
except ModuleNotFoundError:  # pragma: no cover - local/test fallback when optional rate-limit dep is absent
    class RateLimitExceeded(Exception):
        pass

    def get_remote_address(request: Request) -> str:
        return request.client.host if request.client else "local"

    class Limiter:
        def __init__(self, *args, **kwargs) -> None:
            self.enabled = False

        def limit(self, *args, **kwargs):
            def decorator(func):
                return func
            return decorator

    async def _rate_limit_exceeded_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)

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

limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])

app = FastAPI(
    title="Odoo MCP Agent Connector",
    version="1.4.0",
    description=(
        "V14 Self-Learning Agent connector for Odoo development automation over MCP/HTTP: "
        "core file discovery (find_core_file + find_similar_view), "
        "repo patching, self QA, Git, Odoo.sh, Odoo shell, CDP browser diagnostics, "
        "approvals, jobs/audit, full MCP conversion, OAuth 2.1/PKCE MCP auth, and tool-call bridge."
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
        "version": "v14+",
        "prompt_source": "MCP_SYSTEM_PROMPT.md",
        "system_prompt": load_mcp_system_prompt(),
        "instructions": {
            "role": "You are an Odoo developer agent (supports Odoo 16/17/18/19). Use MCP tools to code and deploy autonomously. You are a Self-Learning Agent — you ALWAYS read real Odoo core source AND custom module source before writing any code. No static skill. No hallucination. No push without verify.",
            "rules": [
                "Always start with repo.prepare",
                "Write COMPLETE files only — never partial or placeholder",
                "Never change module version",
                "Never edit src/odoo or src/enterprise core files — READ them only",
                "Dummy data requests → add data.xml",
                "Coding patterns: github.com/fhidalgodev/odoo-development-skill",
                "Module reference priority for installed-source reads: /home/odoo/src/odoo/addons first, then /home/odoo/src/enterprise fallback, then /home/odoo/src/user only as fallback for other related custom dependencies. For editing, read the current custom module from the active checkout; for already-live/deployed/done proof, verify real /home/odoo/src/user with workflow.verify_deployed_module or odoosh.read_user_file.",
                "Before install/upgrade: repo.read each file and verify imports against core modules",
                "Long operations return job_id and next_call — poll jobs.get with short bounded waits until passed/failed/warning. Never treat status=running, deploy_running, module_upgrade_running, or job_status=running as complete.",
                "Job/workflow responses include status_label and steps_summary fields (e.g. '✅ git.push') — show these directly to the user instead of writing your own status text",
                "Tools that return a list (repo.list, repo.list_custom_modules, jobs.list, approvals.list, audit.list) come wrapped as {items: [...], count: N} — read result.items, not result directly",
                "On error: diagnose first, fix, retry max 3x — never blind retry",
                "Binary files (.png/.jpg/.ico/.woff/.ttf) cannot be read via repo.read — skip them (415 error expected)",
                "For files >300KB pass max_bytes=500000 to repo.read",
                "For simple field/view/notebook edits, use the small_patch_fast_path only after the active client is explicit. If more than one ODOO_CLIENTS client is available and the user did not name the client/repo in this request, ask which client before any repo/session/write/deploy tool call. Do not reuse saved session state to choose silently.",
                "Never say a custom module change is already live/deployed/done because repo.read/GitHub/local checkout shows it. First call workflow.verify_deployed_module or odoosh.read_user_file for the exact /home/odoo/src/user file.",
                "For any code-changing request, do not stop after writing files and ask the user to run upgrade/tests. Continue through git.diff, qa_self_run, verify_against_core, and workflow.safe_deploy_module. Only ask the user when required information is missing or a real blocker remains after diagnostics.",
                "If the deploy cycle breaks mid-flow, use workflow.resume_job. If job_id is missing, call it with client_name/project_path/module_name or no job_id to resume the latest saved safe-deploy job. Use override_step=push, install, or upgrade only when the user explicitly asks to continue from that point. Never call git.commit/git.push/odoosh.module_update after workflow.safe_deploy_module starts; these are blocked because safe_deploy owns the whole pipeline.",
                "If workflow.safe_deploy_module or odoo.module_deploy returns a transport/transient/server/timeout/serialization error after code was changed, do not summarize local code as done and do not ask the user to run upgrade manually. Recover immediately with session.load_state, jobs.list/jobs.get, or workflow.resume_job, then continue until a terminal upgrade success or terminal failure is confirmed.",
                "Never answer a code-changing request by printing JSON MCP tool calls or saying the client/user should execute them. You are connected to the MCP tools; call them yourself.",
                "Never create module schema changes by direct ORM/Studio writes to ir.model.fields or ir.ui.view unless the user explicitly asks for a no-code Studio/database customization. For normal Odoo development, write Python/XML files in the custom module and deploy the module.",
                "Never generate Pydantic models/imports inside Odoo addon code. Pydantic is only for this MCP server's API schemas; Odoo modules must use the normal Odoo ORM: models.Model, fields, api, and XML views.",
                "SMART ROUTER: for coding tasks, call workflow.smart_tool_router early to choose the fastest correct path. For pure data queries, use direct odoo.search_read and skip repo tools.",
                "STRICT FRONTEND: before writing any Odoo JS/OWL/POS/website/payment/QWeb/XML/SCSS asset code, call workflow.frontend_pattern_preflight and read the mandatory pattern files it returns. Results are cached; do not guess framework syntax.",
                "POS CONFIG RULE: never use legacy load_fields('pos.config', ...) / models.load_fields for pos.config. Read installed point_of_sale source and use the version's real loader/backend pattern.",
                "FAST WRITE RULE: for multi-file edits use repo.write_many_smart first. It keeps lock+backup+rollback for small changes and automatically uses repo.write_many_atomic only when size/risk requires it. Never skip git.diff or impact-aware changed-files verification.",
                "CACHE RULE: use workflow.repo_index for module/assets/template discovery instead of full repo scans. Reuse cache unless files changed or force_refresh is required.",
            ],
            "small_patch_fast_path": {
                "when": "Use for small tasks like adding one field, one xpath, one notebook page, or a tiny label/view change in a known module.",
                "goal": "Keep simple edits near the minimum tool calls; most time should be Odoo.sh deploy, not discovery.",
                "steps": [
                    "1. If available_clients has more than one real client and the current user request does not explicitly name a client/repo, ask the user which client first. Do not call session.load_state, repo.*, or workflow.start_session silently.",
                    "2. After explicit client selection, call workflow.start_session(client_name=..., include_odoo=false, include_odoosh=false), then session.load_state(client_name=...).",
                    "3. repo.prepare once, then read only the target manifest/model/view files.",
                    "4. Read core/enterprise only for the exact inherited model/view pattern that is not already clear from existing custom code. If local code appears already changed and you plan to answer 'already done/live', call workflow.verify_deployed_module or odoosh.read_user_file first.",
                    "4a. For field/view changes, use module files only. Do not create ir.model.fields or patch ir.ui.view records directly as a Studio shortcut.",
                    "4b. For Odoo Python files, use Odoo ORM only. Do not import pydantic or create Pydantic BaseModel classes inside addons.",
                    "5. Patch with repo.apply_patch_atomic or repo.write_many_smart. Use repo.write_many_atomic only for large/risky refactors.",
                    "6. Run git.diff and qa.self_run(run_pytest=false) unless tests/shared logic are touched.",
                    "7. workflow.verify_against_core only for changed files.",
                    "8. Call workflow.safe_deploy_module with the changed paths. It must pull/fetch the configured branch before work begins, poll Odoo.sh every 10 seconds for up to 10 minutes, verify deployed file hashes, upgrade the module, and run registry smoke tests.",
                    "9. If safe_deploy returns status=running, module_upgrade_running, or any next_call, call the returned next_call/jobs.get or workflow.resume_job. If Odoo.sh deploy polling times out, recent Odoo.sh logs show build/test failure, changed /home/odoo/src/user file hashes do not match, the Odoo module deploy is still running, or registry_field_verify fails, do not claim success. Do not repeat commit/push.",
                    "10. If the client loses job_id mid-flow, call workflow.resume_job without job_id to use the latest saved safe-deploy job, or pass override_step=push/install/upgrade for an explicit user-requested continuation point.",
                    "11. Finish only after safe deploy/resume reports passed, or report the exact blocker and next_call returned by the workflow.",
                ],
                "do_not": [
                    "Do not repeatedly look up the tool list after the first successful tool call.",
                    "Do not crawl all custom modules when the target module is already known.",
                    "Do not end with 'next steps', 'want to', or ask the user to run module upgrade after you have changed code. Use workflow.safe_deploy_module yourself.",
                    "Do not print sample MCP JSON blocks for the user to execute. Call the MCP tools directly.",
                    "Do not use Studio/ORM database schema mutations for normal module-code requests.",
                    "Do not put Pydantic code in Odoo addons.",
                ],
            },
            "mandatory_dual_read": {
                "rule": "BOTH custom module AND core module must ALWAYS be read — neither is optional",
                "step_1_custom": "repo.read ALL relevant files in the custom module first",
                "step_2_scope": "For backend/model/controller/security/structural changes: workflow.scope_advice(custom_module_name, custom_file_paths) → get adaptive read plan. For lightweight UI/text/style XML/CSS/SCSS edits: still perform mandatory dual read; read exact custom target plus workflow.scope_advice mandatory core source, then write and safe_deploy.",
                "step_3_core": "Execute EVERY mandatory ScopeReadInstruction: repo.read each path returned",
                "step_4_code": "ONLY THEN write code — using ONLY verified field/method names from core reads",
                "step_5_verify": "Before git.push → ALWAYS workflow.verify_against_core(written_files)",
                "step_6_push": "If push_blocked=False → git.push. If True → fix CRITICAL findings, re-verify (max 3 cycles)",
                "enforcement": "Agent MUST NOT skip any mandatory instruction. If core file not found → WARN user, do not guess",
            },
            "core_reading_rules": {
                "purpose": "Read real Odoo source to eliminate hallucination — no static skill, no guessing",
                "before_inherit_model": [
                    "1. workflow.find_core_file(module_name, 'model', keyword) → get best_match path",
                    "2. repo.read(project_path=best_match_dir, relative_path=best_match_filename)",
                    "3. Read exact field names, field types, comodel_name, compute methods, _sql_constraints",
                    "4. Then write _inherit using ONLY verified field/method names",
                ],
                "before_inherit_view": [
                    "1. workflow.find_similar_view(model, view_type) → get best_match path",
                    "2. repo.read(best_match) — inspect real xpath node IDs, field refs, string attrs",
                    "3. Write <xpath expr='...'/> using node paths confirmed from source",
                ],
                "before_adding_depends": [
                    "1. workflow.find_core_file(module_name, 'manifest', module_name) → get __manifest__.py path",
                    "2. repo.read(manifest_path) — verify technical module name matches exactly",
                    "3. Add to depends[] only verified module names",
                ],
                "search_priority": [
                    "1st: /home/odoo/src/odoo/addons",
                    "2nd: /home/odoo/src/enterprise",
                    "3rd: /home/odoo/src/user      (fallback for other related custom dependencies only)",
                ],
            },
            "strict_frontend_owl_js_xml_preflight": {
                "rule": "MANDATORY before any JS, OWL, POS, website, payment, QWeb/XML template, SCSS, or asset-bundle implementation.",
                "tool": "workflow.frontend_pattern_preflight(task_description, target_area='auto', target_module_name, custom_file_paths, planned_file_paths)",
                "sequence": [
                    "1. Read existing custom module files first: manifest, views, static/src JS/XML/SCSS related to the task.",
                    "2. Call workflow.frontend_pattern_preflight with those custom file paths and planned files.",
                    "3. Execute every mandatory instruction returned: repo.read each concrete source path; for NOT_FOUND hints call workflow.list_module_files to browse manually.",
                    "4. Confirm exact import aliases, registry category, service names, hooks, patch syntax, Component.template/t-name, t-inherit/xpath, and asset bundle from real source.",
                    "5. Only then write code; keep syntax/style matching the installed Odoo framework source.",
                    "6. After coding, call workflow.verify_against_core with changed .py, .xml, .js, .scss, and __manifest__.py files.",
                ],
                "areas": {
                    "pos": "Read point_of_sale/static/src/app + views and use point_of_sale._assets_pos unless source proves otherwise.",
                    "website": "Decide frontend vs editor assets from website/website_form/website_sale source before coding.",
                    "payment": "Read payment + website_payment source before provider/checkout/frontend code.",
                    "web": "Read web/static/src/core/views/webclient before backend OWL/action/dashboard code.",
                },
                "blocked_mistakes": [
                    "Missing /** @odoo-module **/ in modern JS",
                    "Mixed odoo.define/require with ES imports",
                    "Wrong @web/@point_of_sale import path",
                    "Wrong registry category/service/hook usage",
                    "Component.template not matching t-name",
                    "t-inherit/xpath target not read from source",
                    "POS assets outside point_of_sale._assets_pos",
                    "Website editor code loaded only in frontend assets or frontend code loaded only in editor assets",
                ],
            },
            "adaptive_scope_levels": {
                "L0": "Always: custom manifest (baseline)",
                "L1": "_inherit detected → read parent core model file(s)",
                "L2": "Mixin/abstract detected → read mail.thread, portal.mixin, etc.",
                "L3": "Computed/relational fields or ir.model → scan full core models/ directory",
                "L4": "Cross-module comodel → read that module's core manifest + model",
                "behavior": "scope_advice auto-detects level from scanned files. Each higher level adds more mandatory reads.",
            },
            "pre_push_verify": {
                "rule": "ALWAYS call workflow.verify_against_core BEFORE every git.push",
                "if_blocked": "Fix all CRITICAL findings, then call verify_against_core again",
                "max_cycles": "3 fix cycles maximum — after 3, report to user and STOP",
                "what_is_checked": {
                    "python": "_inherit model existence, depends[] field names, comodel_name modules, related= chains, compute= method presence",
                    "xml": "xpath @name nodes in core view, <field name=...> vs core model fields",
                    "manifest": "depends[] module names in /home/odoo/src/* roots",
                },
            },
            "large_file_writing": {
                "threshold": "Single-pass repo.write is supported up to 1000 lines; files >1000 lines should use chunked mode",
                "chunk_size": "Choose practical chunks; follow-up chunks have no single-pass warning limit",
                "how": [
                    "chunk_index=0, total_chunks=N → create file with first chunk",
                    "chunk_index=1..N-1, total_chunks=N → append next chunks",
                    "Response confirms 'complete' on last chunk",
                ],
            },
            "flow": [
                "1. repo.prepare → repo.list_custom_modules",
                "2. repo.read ALL relevant custom module files (models, views, manifest)",
                "3. For backend/structural changes only: workflow.scope_advice(custom_module, [custom_file_paths]) → get mandatory read plan. For lightweight UI/text/style XML/CSS/SCSS edits, do not skip dual read: read target custom file, call workflow.scope_advice, read mandatory core source, then repo.write.",
                "4. Execute ALL mandatory ScopeReadInstructions: repo.read each path",
                "   L1: IF _inherit → repo.read core model(s)",
                "   L2: IF mixin → repo.read mail.thread / portal.mixin / etc.",
                "   L3: IF computed/relational → repo.read full models/ dir scan result",
                "   L4: IF cross-module → repo.read dep module manifest",
                "5. IF view inherit: workflow.find_similar_view → repo.read core view XML",
                "5a. CALL workflow.smart_tool_router for coding tasks; if frontend is detected: workflow.frontend_pattern_preflight → repo.read every mandatory pattern source before coding",
                "6. Write code using ONLY verified field/method/xpath/template/import/registry/service names from steps 2-5a",
                "7. repo.write_many_smart (multi-file default) OR repo.write chunks (single large file); use repo.write_many_atomic only for large/risky edits",
                "8. git.diff → qa.self_run (fix errors before continuing)",
                "9. workflow.verify_against_core(written_files, fix_cycle=0)",
                "   → If push_blocked=True: fix CRITICAL findings → re-verify (max 3 cycles)",
                "   → If push_blocked=False: proceed to git.commit",
                "10. Prefer workflow.safe_deploy_module for commit → push → Odoo.sh verification → upgrade",
                "11. Manual git.push is only safe when followed by Odoo.sh SHA/failure/hash verification",
                "12. odoo.ensure_dependencies → odoo.module_deploy → poll jobs.get every 10s; direct module_deploy blocks itself if recent pushed code is not verified",
                "    If Access Denied → SSH fallback auto-triggered inside module_deploy",
                "13. If failed → odoosh.grep_errors → read traceback → fix → back to step 6 (max 3x)",
                "15. odoo.module_status = installed ✅",
            ],
            "error_guide": {
                "workflow.resume_job status=retry_later": "Odoo.sh not deployed yet — wait ~15s, call workflow.resume_job again with the same job_id. Non-blocking, safe to repeat up to 20x.",
                "odoo.module_deploy blocks after git.push": "The DB upgrade was not run because Odoo.sh has not proven the pushed code is active. Use workflow.resume_job or workflow.safe_deploy_module; do not claim success.",
                "path not found": "Call repo.prepare first",
                "module not found": "Call odoo.ensure_dependencies then odoo.module_deploy",
                "Access Denied": "SSH fallback auto-triggered — check jobs.get for result",
                "ImportError or ParseError": "odoosh.grep_errors → fix file → recommit",
                "push failed": "TARGET_GIT_REPO must use HTTPS+token format",
                "job stuck running": "Call jobs.get — may have timed out",
                "415 UnicodeDecodeError": "Binary file — skip repo.read for .png/.jpg/.ico/.woff/.ttf",
                "413 too large": "Pass max_bytes=500000 to repo.read",
                "find_core_file returns 0 matches": "Module may not be installed on this Odoo.sh server — try workflow.list_module_files first to browse, or check /home/odoo/src/odoo/addons directly",
                "not sure what files exist in a module": "Call workflow.list_module_files(module_name, subdir) to browse before find_core_file",
                "verify_against_core push_blocked=True": "Fix ALL CRITICAL findings shown in findings[] then call verify_against_core again with fix_cycle incremented",
                "scope_advice mandatory_count>0": "Execute ALL mandatory ScopeReadInstructions before any Odoo addon write. Lightweight UI/text/style XML/CSS/SCSS edits still need scope_advice and mandatory installed-source reads before writing; keep scope capped at L2.",
                "frontend_pattern_preflight push_blocked_until_done=true": "Read all mandatory frontend pattern instructions before writing JS/OWL/XML/assets — no guessing.",
            },
            "new_tools_v14plus": {
                "workflow.find_core_file": "Find exact core source file before _inherit. Returns best_match absolute path.",
                "workflow.find_similar_view": "Find exact core view XML before xpath inherit. Returns best_match absolute path.",
                "workflow.list_module_files": "Browse all files in a module dir when keyword unknown. Use subdir to narrow (models/views/static/src).",
                "workflow.scope_advice": "NEW V14+: Adaptive mandatory read plan for all Odoo addon code changes, including lightweight UI/text/style edits; keep capped at L2.",
                "workflow.verify_against_core": "NEW V14+: Pre-push cross-check — call before every git.push. Blocks push on CRITICAL findings.",
                "workflow.verify_deployed_module": "Compare active checkout files to real /home/odoo/src/user and live module status before claiming a change is already deployed.",
                "odoosh.read_user_file": "Read exact deployed custom-source file from /home/odoo/src/user.",
                "odoosh.list_user_files": "List exact deployed custom-source files from /home/odoo/src/user.",
                "workflow.frontend_pattern_preflight": "NEW V15: Mandatory preflight for Odoo JS/OWL/POS/website/payment/QWeb/XML/SCSS asset work. Returns source patterns to read first.",
            },
            "multi_client": {
                "rule": (
                    "ODOO_CLIENTS is authoritative. At the start of every session call "
                    "workflow.start_session. If more than one client is listed, client_name is mandatory "
                    "and the agent must ask the user which client/repo before calling any repo/write/deploy/session tool. "
                    "Do not reuse previous session state for a new ambiguous coding request. Repo URL, branch, path, token, and Odoo creds resolve automatically. "
                    "Do not ask for separate repo config when the selected client has repo/branch/path. "
                    "NEVER ask user for token or password. NEVER log or expose token."
                ),
                "available_clients": client_names if client_names else ["(no clients — using TARGET_GIT_* env vars)"],
                "switch_client": "Call workflow.start_session(client_name='...') anytime to switch. If one client exists, client_name is optional; if more than one client exists, client_name is mandatory and no saved session may silently select it.",
            },
            "version_compatibility": {
                "supported_versions": ["Odoo 16", "Odoo 17", "Odoo 18", "Odoo 19"],
                "rule": (
                    "Version is auto-detected from /home/odoo/src/odoo — it already contains "
                    "the exact Odoo version running on this server. "
                    "ALWAYS read core source first. NEVER assume syntax. "
                    "tree vs list, attrs= vs invisible=, old domain vs py expression "
                    "— all differ by version. Confirm from source before writing."
                ),
            },
            "config": {
                "repo_url": repo,
                "branch": branch,
                "project_path": path,
                "odoo_src_roots": [
                    "/home/odoo/src/odoo/addons (priority)",
                    "/home/odoo/src/enterprise (fallback only)",
                    "/home/odoo/src/user (other related custom dependencies only)",
                ],
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
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    import uuid
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
