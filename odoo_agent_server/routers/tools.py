from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from ..auth import AuthDependency
from ..config import settings
from ..schemas import (
    ApprovalCreateRequest,
    ApprovalDecisionRequest,
    ArtifactReadRequest,
    AuditListRequest,
    BaseProjectRequest,
    BrowserCheckRequest,
    PosFlowCheckRequest,
    StartSessionRequest,
    RegistrySmokeTestRequest,
    RestoreLastGoodCommitRequest,
    MenuCrawlerRequest,
    CodingTaskPlanRequest,
    EmptyRequest,
    FullVerifyRequest,
    SafeDeployModuleRequest,
    GitCommitRequest,
    GitDiffRequest,
    GitPushRequest,
    JobGetRequest,
    LimitRequest,
    OdooRpcModuleRequest,
    OdooSearchReadRequest,
    OdooFunctionalQueryRequest,
    OdooShCheckCommitRequest,
    OdooShCommandRequest,
    OdooShLogRequest,
    OdooShModuleUpdateRequest,
    OdooShVerifyManifestRequest,
    OdooShWaitForCommitRequest,
    RepoAtomicPatchRequest,
    RepoCleanPycRequest,
    RepoCustomModulesRequest,
    RepoDeleteRequest,
    RepoListRequest,
    RepoPatchRequest,
    RepoPrepareRequest,
    RepoReadRangeRequest,
    RepoReadRequest,
    RepoSearchRequest,
    RepoWriteManyAtomicRequest,
    RepoWriteManyFastSafeRequest,
    RepoWriteManySmartRequest,
    RepoWriteRequest,
    SelfRunRequest,
    ToolCallRequest,
    ToolCallResult,
    ToolDescriptor,
    # Fix 3/7/10: new request schemas
    ResumeJobRequest,
    SessionStateSaveRequest,
    SessionStateLoadRequest,
    EnsureDependenciesRequest,
    # V14: Self-Learning Agent — Core Discovery
    FindCoreFileRequest,
    FindSimilarViewRequest,
    ListModuleFilesRequest,
    # V14+: Pillar 2 — Adaptive Scope Advisor
    ScopeAdviceRequest,
    # V14+: Pillar 3 — Pre-Push Syntax Verifier
    SyntaxVerifyRequest,
    PosChangeVerifyRequest,
    # V15: Strict Odoo frontend/OWL/JS/XML preflight
    FrontendPatternPreflightRequest,
    # V16: fast/correct router and repo index
    SmartToolRouteRequest,
    RepoIndexRequest,
)
from ..services.git_service import GitService
from ..services.odoo_rpc_service import OdooRpcService
from ..services.odoosh_service import OdooShService
from ..services.browser_cdp_service import CdpBrowserService
from ..services.repo_service import RepoService
from .qa import self_run
from .repo import repo_delete, repo_write, repo_write_many_atomic, repo_write_many_fast_safe, repo_write_many_smart, repo_apply_patch, repo_apply_patch_atomic, repo_clean_pyc, repo_prepare, repo_custom_modules
from .git import git_commit, git_push
from .odoo import odoo_module_install, odoo_module_update, odoo_module_deploy
from .odoosh import odoosh_module_install_ssh, odoosh_module_deploy_ssh, odoosh_module_update, odoosh_wait_for_commit, odoosh_verify_module_manifest
from .workflow import capability_check, full_verify, safe_deploy_module, coding_task_plan, start_session, registry_smoke_test, restore_last_good_commit, browser_menu_crawl, find_core_file, find_similar_view, list_module_files, scope_advice, verify_against_core, verify_pos_change, frontend_pattern_preflight, smart_tool_router, repo_index
from ..schemas import ApprovalRecord, ApprovalStatus
from ..services.store_service import JsonStore

router = APIRouter(prefix="/tools", tags=["tools"], dependencies=[AuthDependency])

_STRICT_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def strict_safe_tool_name(name: str) -> str:
    """Return a strict MCP/Claude/ChatGPT/Qwen-safe tool name.

    Some frontends validate tool names with ^[a-zA-Z0-9_-]{1,64}$.
    Internal Odoo-agent tool names use dotted namespaces such as
    ``odoo.search_read`` and ``workflow.smart_tool_router``. Externally expose
    underscored names while accepting both formats on calls.
    """
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_")
    safe = (safe or "tool")[:64]
    if _STRICT_TOOL_NAME_RE.match(safe):
        return safe
    return "tool"


def internal_tool_name(name: str) -> str:
    """Map an external strict tool name back to the internal dotted name."""
    if name in _TOOL_INPUT_MODELS:
        return name
    for actual in _TOOL_INPUT_MODELS:
        if strict_safe_tool_name(actual) == name:
            return actual
    return name


def _agent_priority(name: str) -> tuple[int, str]:
    """Put functional Odoo query tools early so Claude/Qwen choose them first.

    Functional questions like "list draft invoices" should call odoo_search_read
    directly and not enter the coding workflow.
    """
    order = {
        "connector.health": 0,
        "odoo.query": 1,
        "odoo.search_read": 2,
        "odoo.health": 3,
        "odoo.module_status": 3,
        "workflow.smart_tool_router": 4,
        "workflow.agent_instructions": 5,
        "workflow.capability_check": 6,
    }
    if name in order:
        return (order[name], name)
    if name.startswith("workflow."):
        return (20, name)
    if name.startswith("repo."):
        return (30, name)
    if name.startswith("git."):
        return (40, name)
    if name.startswith("odoosh."):
        return (50, name)
    if name.startswith("odoo."):
        return (60, name)
    return (70, name)


def _external_tool_descriptor(item: ToolDescriptor) -> ToolDescriptor:
    """Descriptor form safe for strict remote tool frontends and REST clients."""
    safe_name = strict_safe_tool_name(item.name)
    description = item.description
    if safe_name != item.name:
        description = f"External tool name: {safe_name}. Internal route: {item.name}. {description}"
    return item.model_copy(update={"name": safe_name, "description": description})


_TOOL_DESCRIPTORS = [
    ToolDescriptor(name="connector.health",
        title="Check connector health", description="Check connector service health", read_only=True),
    ToolDescriptor(
        name="artifacts.read",
        title="Read saved artifact",
        description="Read an exact slice of a large log, diff, command output, or structured result saved from an earlier MCP response.",
        read_only=True,
    ),
    ToolDescriptor(
        name="workflow.agent_instructions",
        title="Load agent instructions",
        description=(
            "CALL THIS FIRST in every session before any other tool. "
            "Returns full agent rules, flow, error guide, and repo config from the connector. "
            "Follow these instructions exactly for every task."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="workflow.capability_check",
        title="Check connector capabilities", description="Check local connector capabilities and missing runtime configuration", read_only=True),
    ToolDescriptor(name="workflow.coding_task_plan",
        title="Get coding workflow plan", description="Return the repo-first coding workflow guidance for ChatGPT MCP clients", read_only=True),
    ToolDescriptor(
        name="workflow.smart_tool_router",
        title="Choose fastest safe workflow",
        description=(
            "V16: lightweight planner that chooses the fastest correct tool sequence: direct Odoo query, "
            "targeted read/preflight, repo.write_many_smart, changed-files validation, and browser smoke test only when needed."
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="workflow.repo_index",
        title="Build/read repo index",
        description="V16: cached lightweight index of modules, manifests, assets, JS/XML templates, and source files to avoid full repo scans.",
        read_only=True,
    ),
    ToolDescriptor(name="workflow.full_verify",
        title="Run full verification", description="Run the full self-verification workflow for an Odoo module", read_only=True),
    ToolDescriptor(name="workflow.safe_deploy_module",
        title="Deploy module (full pipeline)", description="Safely commit, push, wait for Odoo.sh deployment, verify manifest, upgrade an Odoo module, and optionally open exact UI paths with verify_ui_paths", requires_approval=True),
    ToolDescriptor(name="workflow.start_session",
        title="Start coding session", description="Bootstrap a coding session: capability check, repo.prepare, git status, module discovery, Odoo/Odoo.sh health", requires_approval=True),
    ToolDescriptor(name="workflow.registry_smoke_test",
        title="Test Odoo registry", description="Verify live Odoo registry models/menus after install or upgrade", read_only=True),
    ToolDescriptor(name="workflow.restore_last_good_commit",
        title="Restore last working commit", description="Emergency restore by reverting a bad commit and optionally pushing the revert", requires_approval=True),
    ToolDescriptor(name="workflow.browser_menu_crawl",
        title="Crawl Odoo menus in browser", description="Open multiple Odoo paths through CDP and capture console/network/Odoo UI errors", read_only=True),
    ToolDescriptor(
        name="workflow.frontend_pattern_preflight",
        title="Strict frontend/OWL preflight",
        description=(
            "MANDATORY before writing any Odoo JS, OWL, POS, website, payment, QWeb/XML template, "
            "SCSS, or asset code. Returns source files/areas to read first and strict syntax/bundle rules."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="qa.self_run",
        title="Run code quality checks", description="Run Odoo custom module static QA and optional pytest/git status", read_only=True),
    ToolDescriptor(name="repo.prepare",
        title="Clone or update repository", description="Clone or refresh target repo and checkout/pull the target branch before coding", requires_approval=True),
    ToolDescriptor(name="repo.list_custom_modules",
        title="List custom Odoo modules", description="Discover custom Odoo modules from repo __manifest__.py files", read_only=True),
    ToolDescriptor(name="repo.list",
        title="List repository files", description="List files inside an allowed project path", read_only=True),
    ToolDescriptor(name="repo.read",
        title="Read file contents", description="Read a UTF-8 file. Default max 300KB. For large files pass max_bytes=500000. Returns file content and line count.", read_only=True),
    ToolDescriptor(
        name="repo.read_range",
        title="Read file line range",
        description="Read exact line-numbered slices from a UTF-8 file. Use after repo.search or when repo.read is too large.",
        read_only=True,
    ),
    ToolDescriptor(
        name="repo.search",
        title="Search repository text",
        description="Bounded text search inside a repo path with line numbers and small context. Use before reading large or unknown files.",
        read_only=True,
    ),
    ToolDescriptor(
        name="repo.write",
        title="Write file contents",
        description=(
            "Write a complete file. Single-pass writes are supported up to 1000 lines. "
            "For files >1000 lines, use CHUNKED MODE to avoid token limits: "
            "Split file into chunks; follow-up chunks have no single-pass warning limit. "
            "First call: chunk_index=0, total_chunks=N. "
            "Next calls: chunk_index=1,2...N-1 with total_chunks=N. "
            "Each chunk is appended. Response shows progress and confirms when complete. "
            "NEVER write partial files without chunked mode."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(
        name="repo.write_many_smart",
        title="Write multiple files smartly",
        description=(
            "PREFERRED for multi-file writes. Auto-selects fast-safe lock+backup+rollback for small related edits "
            "and falls back to git temp-worktree atomic for large/risky writes. Keeps quality high while avoiding "
            "slow repo.write_many_atomic for normal OWL/POS/website/XML changes."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(
        name="repo.write_many_fast_safe",
        title="Write small multi-file change fast",
        description=(
            "Fast-safe small multi-file writer: validates paths, locks repo, backs up original files, writes through "
            "temporary files, and rolls back on failure. Use for <= configured limits; still run git.diff and changed-files verification after."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(name="repo.write_many_atomic",
        title="Write multiple files safely", description="Slowest/premium safety path: write many files as one isolated git temp-worktree transaction. Use for large/risky refactors only.", requires_approval=True),
    ToolDescriptor(name="repo.delete",
        title="Delete file from repo", description="Delete a file inside an allowed project path", requires_approval=True),
    ToolDescriptor(name="repo.apply_patch",
        title="Apply code patch", description="[LEGACY — use repo.apply_patch_atomic instead] Check/apply a unified diff patch", requires_approval=True, deprecated=True),
    ToolDescriptor(name="repo.apply_patch_atomic",
        title="Apply patch safely (atomic)", description="Apply a patch through an isolated temp worktree/branch before touching the real checkout", requires_approval=True),
    ToolDescriptor(name="repo.clean_pyc",
        title="Clean compiled Python files", description="Remove .pyc and __pycache__ from a project", requires_approval=True),
    ToolDescriptor(name="git.status",
        title="Check Git status", description="Read git status", read_only=True),
    ToolDescriptor(name="git.current_branch",
        title="Check current Git branch", description="Read current git branch", read_only=True),
    ToolDescriptor(name="git.diff",
        title="Review code changes (diff)", description="Read git diff", read_only=True),
    ToolDescriptor(name="git.commit",
        title="Commit code changes", description="Create a git commit", requires_approval=True),
    ToolDescriptor(name="git.push",
        title="Push code to GitHub", description="Push current branch to remote", requires_approval=True),
    ToolDescriptor(
        name="odoosh.module_install_ssh",
        title="Install module via SSH",
        description=(
            "[LEGACY — odoo.module_deploy auto-falls-back to SSH on Access Denied, no need to call this directly] "
            "Install a NEW module via Odoo.sh SSH using odoo-bin -i flag."
        ),
        deprecated=True,
    ),
    ToolDescriptor(name="odoosh.health",
        title="Check Odoo.sh connection", description="Check Odoo.sh SSH health", read_only=True),
    ToolDescriptor(name="odoosh.git_status",
        title="Check Odoo.sh Git status", description="Read Odoo.sh deployed git status", read_only=True),
    ToolDescriptor(
        name="odoosh.check_commit",
        title="Check Odoo.sh deployment status",
        description=(
            "NON-BLOCKING single check: returns immediately with deployed=true/false. "
            "Use this in a polling loop after git.push: call once, read the 'deployed' field. "
            "If false, wait 15 seconds then call again. Repeat max 20 times. "
            "When deployed=true proceed to odoosh.module_update. Never use odoosh.wait_for_commit."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="odoosh.wait_for_commit",
        title="Wait for Odoo.sh deployment", description="[DEPRECATED - use odoosh.check_commit in a polling loop] Blocking wait for Odoo.sh deploy", read_only=True),
    ToolDescriptor(name="odoosh.verify_module_manifest",
        title="Verify module manifest", description="Verify a module manifest exists and is installable on Odoo.sh", read_only=True),
    ToolDescriptor(name="odoosh.log",
        title="Read Odoo.sh server log", description="Tail an Odoo.sh log", read_only=True),
    ToolDescriptor(name="odoosh.grep_errors",
        title="Search Odoo.sh error logs", description="Search Odoo.sh logs for errors", read_only=True),
    ToolDescriptor(name="odoosh.error_pack",
        title="Collect Odoo.sh error details", description="Return Odoo.sh tracebacks plus root-cause hints/files/XML/model names", read_only=True),
    ToolDescriptor(name="odoosh.module_deploy_ssh",
        title="Deploy module via SSH", description="SSH smart install/upgrade via Odoo.sh. Background job. Use only when you explicitly need SSH; odoo.module_deploy falls back to this automatically.", requires_approval=True),
    ToolDescriptor(name="odoosh.module_update",
        title="Update module on Odoo.sh", description="Run Odoo module update through Odoo.sh SSH", requires_approval=True),
    ToolDescriptor(name="odoo.health",
        title="Check Odoo server health", description="Check Odoo XML-RPC health", read_only=True),
    ToolDescriptor(
        name="odoo.query",
        title="Smart Odoo functional query",
        description=(
            "Best tool for simple functional Odoo questions in Claude/Qwen/ChatGPT: list users, employees, "
            "draft invoices, unpaid invoices, customers, vendors, products, sale orders, purchase orders, or modules. "
            "Takes a natural-language query and returns parsed model/domain/fields plus live Odoo records."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="odoo.module_status",
        title="Check module install status", description="Read Odoo module state (installed/uninstalled/not found)", read_only=True),
    ToolDescriptor(
        name="odoo.module_deploy",
        title="Install or upgrade module",
        description=(
            "SMART INSTALL/UPGRADE — always use this for ALL first-time installs and upgrades. "
            "Runs XML-RPC when allowed; if update_list/buttons fail with Access Denied, "
            "it automatically falls back to Odoo.sh SSH in the same background job. "
            "Waits server-side by default; returns final result if quick, otherwise returns job_id for jobs.get which also waits."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(name="odoo.module_install",
        title="Install Odoo module", description="[LEGACY] Direct button_immediate_install — use odoo.module_deploy instead", requires_approval=True, deprecated=True),
    ToolDescriptor(name="odoo.module_update",
        title="Upgrade Odoo module", description="[LEGACY] Direct button_immediate_upgrade — use odoo.module_deploy instead", requires_approval=True, deprecated=True),
    ToolDescriptor(name="odoo.search_read",
        title="Query Odoo records", description="Run Odoo search_read via XML-RPC", read_only=True),
    ToolDescriptor(name="browser.screenshot",
        title="Check browser page", description="Navigate an Odoo page through external CDP browser and capture console/network errors; screenshot_path is optional", read_only=True),
    ToolDescriptor(name="browser.pos_flow",
        title="Run POS end-to-end flow", description="Login, open POS, click product, pay, validate order in one CDP browser session with console/network capture at every step. Use for any POS OWL change.", read_only=True),
    ToolDescriptor(name="approvals.list",
        title="List pending approvals", description="List pending/recent approval records", read_only=True),
    ToolDescriptor(name="approvals.request",
        title="Request approval", description="Create an approval request", requires_approval=False),
    # Phase 4: approvals.approve/reject are intentionally NOT exposed as MCP
    # tools. Protected-branch git.push creates an approval record that must
    # be approved by a human via the REST API (POST /approvals/approve) with
    # the connector's own API token — not by the agent itself. Exposing
    # these here would let the agent self-approve the exact gate meant to
    # require human review.
    ToolDescriptor(name="jobs.list",
        title="List background jobs", description="List recent connector jobs", read_only=True),
    ToolDescriptor(name="jobs.get",
        title="Check background job status", description="Read one connector job by id; waits server-side by default until done or timeout to avoid fragile client polling", read_only=True),
    ToolDescriptor(name="audit.list",
        title="View audit log", description="List recent audit events", read_only=True),
    # Fix 3: Resume failed pipeline from last successful step
    ToolDescriptor(
        name="workflow.resume_job",
        title="Resume interrupted job",
        description=(
            "Fix 3 — Resume a failed safe_deploy_module or start_session pipeline from its last "
            "successful step. Pass the job_id from the failed run. Instead of starting over from "
            "step 1, the agent continues from where it left off. Optionally override the start "
            "step with override_step (qa | commit | push | wait_odoosh | upgrade | smoke_test)."
        ),
        requires_approval=True,
    ),
    # Fix 7: Session state persistence
    ToolDescriptor(
        name="session.save_state",
        title="Save session state",
        description=(
            "Fix 7 — Persist session context (last_sha, module_name, step_history, etc.) so a new "
            "ChatGPT session after a Render restart can reload it with session.load_state instead "
            "of rediscovering everything from scratch."
        ),
    ),
    ToolDescriptor(
        name="session.load_state",
        title="Load saved session state",
        description="Fix 7 — Load the last persisted session state (sha, module, steps, etc.).",
        read_only=True,
    ),
    # Fix 10: Explicit ensure-dependencies
    ToolDescriptor(
        name="odoo.ensure_dependencies",
        title="Install missing dependencies",
        description=(
            "Fix 10 — Auto-install any missing/uninstalled Odoo module dependencies before "
            "installing or upgrading the target module. Call this when odoo.module_deploy fails "
            "with a dependency error. Previously the agent had to install deps manually one by one."
        ),
        requires_approval=True,
    ),
    # ---------------------------------------------------------------------------
    # V14 — Self-Learning Agent: Core File Discovery
    # ---------------------------------------------------------------------------
    ToolDescriptor(
        name="workflow.find_core_file",
        title="Find Odoo core source file",
        description=(
            "V14 SELF-LEARNING: Locate an Odoo core source file by module + type + keyword. "
            "CALL THIS before any _inherit or depends addition to read the real field/method definitions. "
            "Search priority: /home/odoo/src/user → /home/odoo/src/enterprise → /home/odoo/src/odoo. "
            "Returns best_match path — then call repo.read(best_match) to read the actual source. "
            "Example: module_name='sale', file_type='model', keyword='order' "
            "→ /home/odoo/src/odoo/addons/sale/models/sale_order.py"
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="workflow.find_similar_view",
        title="Find similar Odoo view",
        description=(
            "V14 SELF-LEARNING: Locate an Odoo core view XML file by model + view_type. "
            "CALL THIS before writing any <xpath expr='...'> to read the exact node structure. "
            "Search priority: user → enterprise → odoo. "
            "Returns best_match path — then call repo.read(best_match) to inspect real xpath/attrs. "
            "Example: model='sale.order', view_type='form' "
            "→ /home/odoo/src/enterprise/sale/views/sale_order_views.xml"
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="workflow.list_module_files",
        title="Browse module files",
        description=(
            "Browse all files in a core or custom module directory when you don't know the "
            "exact filename/keyword yet. Use subdir='models'|'views'|'static/src' to narrow. "
            "Search priority: user → enterprise → odoo. "
            "Use this BEFORE find_core_file when unsure what files exist, or find_core_file "
            "directly when you already know module+type+keyword."
        ),
        read_only=True,
    ),
    # ---------------------------------------------------------------------------
    # V14+ — Pillar 2: Adaptive Scope Advisor
    # ---------------------------------------------------------------------------
    ToolDescriptor(
        name="workflow.scope_advice",
        title="Get code-reading scope advice",
        description=(
            "V14+ MANDATORY DUAL READ: Analyse custom module files and return adaptive read plan. "
            "Call AFTER repo.read on custom files and BEFORE writing any code. "
            "Scans for _inherit (L1), mixins (L2), computed/relational fields (L3), cross-module refs (L4). "
            "Returns ordered list of ScopeReadInstruction — execute ALL mandatory instructions before coding. "
            "Example: custom_module_name='my_sale_ext', custom_file_paths=['/path/to/models/sale_order.py']"
        ),
        read_only=True,
    ),
    # ---------------------------------------------------------------------------
    # V14+ — Pillar 3: Pre-Push Syntax Verifier
    # ---------------------------------------------------------------------------
    ToolDescriptor(
        name="workflow.verify_against_core",
        title="Verify code against core",
        description=(
            "V14+ PRE-PUSH MANDATORY: Cross-check written files against Odoo core source before git.push. "
            "CALL THIS before every git.push. If push_blocked=True, fix CRITICAL findings and call again (max 3 cycles). "
            "Verifies: Python — _inherit model existence, depends[] field names, comodel_name modules, related= chains, compute= method presence. "
            "XML — xpath @name nodes in core view, <field name=...> vs core model fields. "
            "Manifest — depends[] module names in /home/odoo/src/* roots. "
            "push_blocked=False means push is safe to proceed."
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="workflow.verify_pos_change",
        title="Verify POS change (static + live browser)",
        description=(
            "V17 MANDATORY FOR POS CHANGES: Combines workflow.verify_against_core (static, incl. real node --check JS parse) "
            "with a live browser.pos_flow run (login -> open POS -> click product -> payment -> validate) under ONE bounded "
            "fix_cycle counter (max 3, same contract as verify_against_core). Only runs the live browser step if written_files "
            "look POS-related AND static checks have zero CRITICAL findings. Use this instead of verify_against_core alone "
            "whenever the task touches point_of_sale JS/XML/OWL — static checks cannot prove the POS UI actually still works."
        ),
        read_only=True,
    ),
]

_TOOL_INPUT_MODELS = {
    "connector.health": EmptyRequest,
    "artifacts.read": ArtifactReadRequest,
    "workflow.agent_instructions": EmptyRequest,
    "workflow.capability_check": EmptyRequest,
    "workflow.coding_task_plan": CodingTaskPlanRequest,
    "workflow.smart_tool_router": SmartToolRouteRequest,
    "workflow.repo_index": RepoIndexRequest,
    "workflow.full_verify": FullVerifyRequest,
    "workflow.safe_deploy_module": SafeDeployModuleRequest,
    "workflow.start_session": StartSessionRequest,
    "workflow.registry_smoke_test": RegistrySmokeTestRequest,
    "workflow.restore_last_good_commit": RestoreLastGoodCommitRequest,
    "workflow.browser_menu_crawl": MenuCrawlerRequest,
    "workflow.frontend_pattern_preflight": FrontendPatternPreflightRequest,
    "qa.self_run": SelfRunRequest,
    "repo.prepare": RepoPrepareRequest,
    "repo.list_custom_modules": RepoCustomModulesRequest,
    "repo.list": RepoListRequest,
    "repo.read": RepoReadRequest,
    "repo.read_range": RepoReadRangeRequest,
    "repo.search": RepoSearchRequest,
    "repo.write": RepoWriteRequest,
    "repo.write_many_smart": RepoWriteManySmartRequest,
    "repo.write_many_fast_safe": RepoWriteManyFastSafeRequest,
    "repo.write_many_atomic": RepoWriteManyAtomicRequest,
    "repo.delete": RepoDeleteRequest,
    "repo.apply_patch": RepoPatchRequest,
    "repo.apply_patch_atomic": RepoAtomicPatchRequest,
    "repo.clean_pyc": RepoCleanPycRequest,
    "git.status": BaseProjectRequest,
    "git.current_branch": BaseProjectRequest,
    "git.diff": GitDiffRequest,
    "git.commit": GitCommitRequest,
    "git.push": GitPushRequest,
    "odoosh.module_install_ssh": OdooRpcModuleRequest,
    "odoosh.health": EmptyRequest,
    "odoosh.git_status": EmptyRequest,
    "odoosh.check_commit": OdooShCheckCommitRequest,
    "odoosh.wait_for_commit": OdooShWaitForCommitRequest,
    "odoosh.verify_module_manifest": OdooShVerifyManifestRequest,
    "odoosh.log": OdooShLogRequest,
    "odoosh.grep_errors": OdooShCommandRequest,
    "odoosh.error_pack": OdooShCommandRequest,
    "odoosh.module_deploy_ssh": OdooShModuleUpdateRequest,
    "odoosh.module_update": OdooShModuleUpdateRequest,
    "odoo.health": EmptyRequest,
    "odoo.query": OdooFunctionalQueryRequest,
    "odoo.module_status": OdooRpcModuleRequest,
    "odoo.module_deploy": OdooRpcModuleRequest,
    "odoo.module_install": OdooRpcModuleRequest,
    "odoo.module_update": OdooRpcModuleRequest,
    "odoo.search_read": OdooSearchReadRequest,
    "browser.screenshot": BrowserCheckRequest,
    "browser.pos_flow": PosFlowCheckRequest,
    "approvals.list": LimitRequest,
    "approvals.request": ApprovalCreateRequest,
    "jobs.list": LimitRequest,
    "jobs.get": JobGetRequest,
    "audit.list": AuditListRequest,
    # Fix 3/7/10
    "workflow.resume_job": ResumeJobRequest,
    "session.save_state": SessionStateSaveRequest,
    "session.load_state": SessionStateLoadRequest,
    "odoo.ensure_dependencies": EnsureDependenciesRequest,
    # V14: Self-Learning Agent
    "workflow.find_core_file": FindCoreFileRequest,
    "workflow.find_similar_view": FindSimilarViewRequest,
    "workflow.list_module_files": ListModuleFilesRequest,
    "workflow.scope_advice": ScopeAdviceRequest,
    "workflow.verify_against_core": SyntaxVerifyRequest,
    "workflow.verify_pos_change": PosChangeVerifyRequest,
}


def tool_input_schema(name: str) -> dict:
    internal_name = internal_tool_name(name)
    model = _TOOL_INPUT_MODELS.get(internal_name, EmptyRequest)
    schema = model.model_json_schema()
    schema.setdefault("type", "object")
    schema.setdefault("additionalProperties", False)
    return schema


@router.get("/list", response_model=list[ToolDescriptor])
def list_tools(include_deprecated: bool = False, strict_names: bool = True) -> list[ToolDescriptor]:
    # If global approval is disabled, do not advertise approval as required even
    # when an old ODOO_AGENT_APPROVAL_REQUIRED_ACTIONS value is still present.
    required = settings.approval_required_actions if settings.require_approval else set()
    visible = _TOOL_DESCRIPTORS if include_deprecated else [t for t in _TOOL_DESCRIPTORS if not t.deprecated]
    ordered = sorted(visible, key=lambda item: _agent_priority(item.name))
    result = [item.model_copy(update={"requires_approval": item.name in required}) for item in ordered]
    if strict_names:
        return [_external_tool_descriptor(item) for item in result]
    return result


@router.post("/call", response_model=ToolCallResult)
async def call_tool(payload: ToolCallRequest) -> ToolCallResult:
    external_name = payload.name
    name = internal_tool_name(payload.name)
    try:
        result = await _dispatch(name, payload.arguments)
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        return ToolCallResult(name=external_name, status="ok", result=result)
    except HTTPException as exc:
        return ToolCallResult(name=external_name, status="failed", error=str(exc.detail))
    except Exception as exc:  # noqa: BLE001
        return ToolCallResult(name=external_name, status="failed", error=str(exc))




def _odoo_functional_query(req: OdooFunctionalQueryRequest) -> object:
    """Map common functional Odoo questions to safe search_read calls.

    This is intentionally read-only and conservative. It gives Claude/Qwen a
    tiny, reliable functional tool so prompts like "List users" do not need to
    inspect the larger coding toolset or manually construct a model/domain.
    """
    text = (req.query or "").strip().lower()
    limit = req.limit

    model = "res.partner"
    domain: list = []
    fields = ["name", "email", "phone", "active"]
    order = "id desc"
    intent = "partners"

    if any(word in text for word in ["user", "users", "login"]):
        model = "res.users"
        domain = []
        if "inactive" in text or "archived" in text:
            domain = [("active", "=", False)]
        elif "all" not in text:
            domain = [("active", "=", True)]
        fields = ["name", "login", "active", "partner_id", "company_ids"]
        order = "id asc"
        intent = "users"
    elif "employee" in text or "employees" in text or "staff" in text:
        model = "hr.employee"
        domain = []
        if "inactive" in text or "archived" in text:
            domain = [("active", "=", False)]
        elif "all" not in text:
            domain = [("active", "=", True)]
        fields = ["name", "work_email", "work_phone", "department_id", "job_id", "active"]
        order = "id asc"
        intent = "employees"
    elif "invoice" in text or "invoices" in text:
        model = "account.move"
        move_type = "in_invoice" if any(w in text for w in ["vendor", "bill", "bills", "supplier"]) else "out_invoice"
        domain = [("move_type", "=", move_type)]
        if "draft" in text:
            domain.append(("state", "=", "draft"))
        elif "posted" in text or "confirmed" in text:
            domain.append(("state", "=", "posted"))
        if "unpaid" in text or "not paid" in text or "open" in text:
            domain.append(("payment_state", "in", ["not_paid", "partial"]))
        elif "paid" in text and "unpaid" not in text and "not paid" not in text:
            domain.append(("payment_state", "=", "paid"))
        if "cancel" in text:
            domain.append(("state", "=", "cancel"))
        fields = ["name", "partner_id", "invoice_date", "invoice_date_due", "amount_total", "amount_residual", "currency_id", "state", "payment_state"]
        order = "invoice_date desc, id desc"
        intent = "vendor_bills" if move_type == "in_invoice" else "customer_invoices"
    elif any(w in text for w in ["customer", "customers", "client", "clients"]):
        model = "res.partner"
        domain = [("customer_rank", ">", 0)]
        fields = ["name", "email", "phone", "customer_rank", "active"]
        order = "customer_rank desc, name asc"
        intent = "customers"
    elif any(w in text for w in ["vendor", "vendors", "supplier", "suppliers"]):
        model = "res.partner"
        domain = [("supplier_rank", ">", 0)]
        fields = ["name", "email", "phone", "supplier_rank", "active"]
        order = "supplier_rank desc, name asc"
        intent = "vendors"
    elif "product" in text or "products" in text:
        model = "product.product"
        domain = []
        if "can be sold" in text or "sale" in text or "sell" in text:
            domain.append(("sale_ok", "=", True))
        if "can be purchased" in text or "purchase" in text or "buy" in text:
            domain.append(("purchase_ok", "=", True))
        fields = ["display_name", "default_code", "list_price", "standard_price", "qty_available", "uom_id", "active"]
        order = "id desc"
        intent = "products"
    elif "sale order" in text or "sales order" in text or "quotation" in text or "quotations" in text:
        model = "sale.order"
        domain = []
        if "draft" in text or "quotation" in text:
            domain.append(("state", "in", ["draft", "sent"]))
        elif "done" in text:
            domain.append(("state", "=", "done"))
        elif "cancel" in text:
            domain.append(("state", "=", "cancel"))
        elif "confirmed" in text or "sale" in text:
            domain.append(("state", "=", "sale"))
        fields = ["name", "partner_id", "date_order", "amount_total", "currency_id", "state", "invoice_status"]
        order = "date_order desc, id desc"
        intent = "sale_orders"
    elif "purchase order" in text or "rfq" in text or "purchase" in text:
        model = "purchase.order"
        domain = []
        if "draft" in text or "rfq" in text:
            domain.append(("state", "in", ["draft", "sent"]))
        elif "done" in text:
            domain.append(("state", "=", "done"))
        elif "cancel" in text:
            domain.append(("state", "=", "cancel"))
        elif "confirmed" in text or "purchase" in text:
            domain.append(("state", "=", "purchase"))
        fields = ["name", "partner_id", "date_order", "amount_total", "currency_id", "state", "invoice_status"]
        order = "date_order desc, id desc"
        intent = "purchase_orders"
    elif "module" in text or "modules" in text or "apps" in text:
        model = "ir.module.module"
        domain = []
        if "installed" in text:
            domain.append(("state", "=", "installed"))
        elif "uninstalled" in text:
            domain.append(("state", "=", "uninstalled"))
        fields = ["name", "shortdesc", "state", "latest_version", "installed_version"]
        order = "name asc"
        intent = "modules"

    result = OdooRpcService().search_read(model, domain, fields, limit, 0, order, None)
    dumped = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    return {
        "status": dumped.get("status") if isinstance(dumped, dict) else "ok",
        "intent": intent,
        "parsed": {"model": model, "domain": domain, "fields": fields, "limit": limit, "order": order},
        "odoo": dumped,
    }

async def _dispatch(name: str, args: dict) -> object:
    if name == "connector.health":
        EmptyRequest.model_validate(args)
        return {"status": "ok", "service": "odoo-chatgpt-direct-connector"}
    if name == "artifacts.read":
        req = ArtifactReadRequest.model_validate(args)
        return JsonStore().read_artifact(req.artifact_id, req.offset, req.max_chars)
    if name == "workflow.agent_instructions":
        import httpx, asyncio
        from ..config import settings
        base = settings.public_base_url or "http://localhost:8080"
        try:
            resp = httpx.get(f"{base}/agent-instructions", timeout=5)
            return resp.json()
        except Exception as exc:
            return {"error": str(exc), "fallback": "Call workflow.coding_task_plan for workflow guidance"}
    if name == "workflow.capability_check":
        EmptyRequest.model_validate(args)
        return capability_check()
    if name == "workflow.coding_task_plan":
        return coding_task_plan(CodingTaskPlanRequest.model_validate(args))
    if name == "workflow.smart_tool_router":
        return smart_tool_router(SmartToolRouteRequest.model_validate(args))
    if name == "workflow.repo_index":
        return repo_index(RepoIndexRequest.model_validate(args))
    if name == "workflow.full_verify":
        return await full_verify(FullVerifyRequest.model_validate(args))
    if name == "workflow.safe_deploy_module":
        return safe_deploy_module(SafeDeployModuleRequest.model_validate(args))
    if name == "workflow.start_session":
        return start_session(StartSessionRequest.model_validate(args))
    if name == "workflow.registry_smoke_test":
        return registry_smoke_test(RegistrySmokeTestRequest.model_validate(args))
    if name == "workflow.restore_last_good_commit":
        return restore_last_good_commit(RestoreLastGoodCommitRequest.model_validate(args))
    if name == "workflow.browser_menu_crawl":
        return await browser_menu_crawl(MenuCrawlerRequest.model_validate(args))
    if name == "workflow.frontend_pattern_preflight":
        return frontend_pattern_preflight(FrontendPatternPreflightRequest.model_validate(args))
    if name == "qa.self_run":
        return self_run(SelfRunRequest.model_validate(args))
    if name == "repo.prepare":
        return repo_prepare(RepoPrepareRequest.model_validate(args))
    if name == "repo.list_custom_modules":
        return repo_custom_modules(RepoCustomModulesRequest.model_validate(args))
    if name == "repo.list":
        req = RepoListRequest.model_validate(args)
        return RepoService().list_files(req.project_path, req.relative_path, req.max_depth, req.max_items)
    if name == "repo.read":
        req = RepoReadRequest.model_validate(args)
        return RepoService().read_file(req.project_path, req.relative_path, req.max_bytes)
    if name == "repo.read_range":
        req = RepoReadRangeRequest.model_validate(args)
        return RepoService().read_range(req.project_path, req.relative_path, req.start_line, req.max_lines)
    if name == "repo.search":
        req = RepoSearchRequest.model_validate(args)
        return RepoService().search(req.project_path, req.pattern, req.relative_path, req.max_matches, req.context_lines, req.regex)
    if name == "repo.write":
        return repo_write(RepoWriteRequest.model_validate(args))
    if name == "repo.write_many_smart":
        return repo_write_many_smart(RepoWriteManySmartRequest.model_validate(args))
    if name == "repo.write_many_fast_safe":
        return repo_write_many_fast_safe(RepoWriteManyFastSafeRequest.model_validate(args))
    if name == "repo.write_many_atomic":
        return repo_write_many_atomic(RepoWriteManyAtomicRequest.model_validate(args))
    if name == "repo.delete":
        return repo_delete(RepoDeleteRequest.model_validate(args))
    if name == "repo.apply_patch":
        return repo_apply_patch(RepoPatchRequest.model_validate(args))
    if name == "repo.apply_patch_atomic":
        return repo_apply_patch_atomic(RepoAtomicPatchRequest.model_validate(args))
    if name == "repo.clean_pyc":
        return repo_clean_pyc(RepoCleanPycRequest.model_validate(args))
    if name == "git.status":
        req = BaseProjectRequest.model_validate(args)
        return GitService().status(req.project_path)
    if name == "git.current_branch":
        req = BaseProjectRequest.model_validate(args)
        return GitService().current_branch(req.project_path)
    if name == "git.diff":
        req = GitDiffRequest.model_validate(args)
        return GitService().diff(req.project_path, req.staged, req.timeout_seconds)
    if name == "git.commit":
        return git_commit(GitCommitRequest.model_validate(args))
    if name == "git.push":
        return git_push(GitPushRequest.model_validate(args))
    if name == "odoosh.module_install_ssh":
        return odoosh_module_install_ssh(OdooRpcModuleRequest.model_validate(args))
    if name == "odoosh.health":
        return OdooShService().health()
    if name == "odoosh.git_status":
        return OdooShService().git_status()
    if name == "odoosh.check_commit":
        req = OdooShCheckCommitRequest.model_validate(args)
        return OdooShService().check_commit_deployed(req.expected_sha)
    if name == "odoosh.wait_for_commit":
        return odoosh_wait_for_commit(OdooShWaitForCommitRequest.model_validate(args))
    if name == "odoosh.verify_module_manifest":
        return odoosh_verify_module_manifest(OdooShVerifyManifestRequest.model_validate(args))
    if name == "odoosh.log":
        req = OdooShLogRequest.model_validate(args)
        return OdooShService().tail_log(req.log_name, req.tail_lines)
    if name == "odoosh.grep_errors":
        req = OdooShCommandRequest.model_validate(args)
        return OdooShService().grep_errors(req.tail_lines)
    if name == "odoosh.error_pack":
        req = OdooShCommandRequest.model_validate(args)
        return OdooShService().error_pack(req.tail_lines)
    if name == "odoosh.module_deploy_ssh":
        return odoosh_module_deploy_ssh(OdooShModuleUpdateRequest.model_validate(args))
    if name == "odoosh.module_update":
        return odoosh_module_update(OdooShModuleUpdateRequest.model_validate(args))
    if name == "odoo.health":
        return OdooRpcService().health()
    if name == "odoo.query":
        return _odoo_functional_query(OdooFunctionalQueryRequest.model_validate(args))
    if name == "odoo.module_deploy":
        return odoo_module_deploy(OdooRpcModuleRequest.model_validate(args))
    if name == "odoo.module_status":
        req = OdooRpcModuleRequest.model_validate(args)
        return OdooRpcService().module_status(req.module_name)
    if name == "odoo.module_install":
        return odoo_module_install(OdooRpcModuleRequest.model_validate(args))
    if name == "odoo.module_update":
        return odoo_module_update(OdooRpcModuleRequest.model_validate(args))
    if name == "odoo.search_read":
        req = OdooSearchReadRequest.model_validate(args)
        return OdooRpcService().search_read(req.model, req.domain, req.fields, req.limit, req.offset, req.order, req.context)
    if name == "browser.pos_flow":
        req = PosFlowCheckRequest.model_validate(args)
        return await CdpBrowserService().run_pos_flow(req)
    if name == "browser.screenshot":
        req = BrowserCheckRequest.model_validate(args)
        return await CdpBrowserService().screenshot_odoo_page(
            req.base_url,
            req.login,
            req.password,
            req.start_path,
            req.screenshot_name,
            req.timeout_seconds,
            req.full_page,
        )
    if name == "approvals.list":
        req = LimitRequest.model_validate(args)
        return JsonStore().list_approvals(limit=req.limit)
    if name == "approvals.request":
        req = ApprovalCreateRequest.model_validate(args)
        return JsonStore().create_approval(ApprovalRecord(action=req.action, summary=req.summary, payload_preview=req.payload_preview))
    if name == "jobs.list":
        req = LimitRequest.model_validate(args)
        return JsonStore().list_jobs(limit=req.limit)
    if name == "jobs.get":
        req = JobGetRequest.model_validate(args)
        if req.wait_for_result:
            return JsonStore().wait_job(req.job_id, req.wait_timeout_seconds, req.poll_interval_seconds)
        return JsonStore().get_job(req.job_id)
    if name == "audit.list":
        req = AuditListRequest.model_validate(args)
        return JsonStore().list_audit(limit=req.limit)
    # Fix 3: Resume pipeline from last successful step
    if name == "workflow.resume_job":
        req = ResumeJobRequest.model_validate(args)
        return await _resume_job(req)
    # Fix 7: Session state persistence
    if name == "session.save_state":
        req = SessionStateSaveRequest.model_validate(args)
        return JsonStore().save_session_state(req.state, client_name=req.client_name)
    if name == "session.load_state":
        req = SessionStateLoadRequest.model_validate(args)
        return JsonStore().load_session_state(client_name=req.client_name)
    # Fix 10: Auto-install dependencies
    if name == "odoo.ensure_dependencies":
        req = EnsureDependenciesRequest.model_validate(args)
        return OdooRpcService().ensure_dependencies(req.module_name)
    # ---------------------------------------------------------------------------
    # V14 — Self-Learning Agent: Core File Discovery
    # ---------------------------------------------------------------------------
    if name == "workflow.find_core_file":
        return find_core_file(FindCoreFileRequest.model_validate(args))
    if name == "workflow.find_similar_view":
        return find_similar_view(FindSimilarViewRequest.model_validate(args))
    if name == "workflow.list_module_files":
        return list_module_files(ListModuleFilesRequest.model_validate(args))
    if name == "workflow.scope_advice":
        return scope_advice(ScopeAdviceRequest.model_validate(args))
    if name == "workflow.verify_against_core":
        return verify_against_core(SyntaxVerifyRequest.model_validate(args))
    if name == "workflow.verify_pos_change":
        return await verify_pos_change(PosChangeVerifyRequest.model_validate(args))
    raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Fix 3: Resume job implementation
# ---------------------------------------------------------------------------

_SAFE_DEPLOY_STEPS = ["qa", "commit", "push", "wait_odoosh", "upgrade", "smoke_test"]


async def _resume_job(req: "ResumeJobRequest") -> dict:
    """Fix 3: Resume a safe_deploy_module pipeline from last successful step.

    Reads the original job record, finds the last step that succeeded, and
    re-runs the pipeline from the next step.  The SafeDeployModuleRequest
    stored in job.request is used to rebuild the context.
    """
    store = JsonStore()
    try:
        job = store.get_job(req.job_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Job {req.job_id} not found")

    # Find last successful step name
    completed_steps = [
        s.name for s in (job.steps or []) if s.status in ("ok", "passed", "skipped")
    ]
    # Determine resume point
    if req.override_step:
        if req.override_step not in _SAFE_DEPLOY_STEPS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown override_step '{req.override_step}'. Valid: {_SAFE_DEPLOY_STEPS}",
            )
        start_from = req.override_step
    else:
        # Find the first step NOT in completed_steps
        start_from = None
        for step_name in _SAFE_DEPLOY_STEPS:
            if step_name not in completed_steps:
                start_from = step_name
                break
        if start_from is None:
            return {"status": "already_complete", "message": "All pipeline steps already completed.", "job_id": req.job_id}

    # Recover original request payload
    original_request = job.request or {}
    module_name = original_request.get("module_name")
    project_path = original_request.get("project_path")
    if not module_name or not project_path:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resume: job {req.job_id} is missing module_name or project_path in its request payload.",
        )

    # Re-run from start_from step
    store.log_audit(
        "workflow.resume_job",
        "running",
        f"Resuming job {req.job_id} from step '{start_from}' (completed: {completed_steps})",
    )
    resume_results: dict = {
        "resumed_from": start_from,
        "original_job_id": req.job_id,
        "completed_steps": completed_steps,
        "steps_run": {},
    }

    step_order = _SAFE_DEPLOY_STEPS[_SAFE_DEPLOY_STEPS.index(start_from):]

    for step_name in step_order:
        try:
            if step_name == "qa":
                qa_req = SelfRunRequest(project_path=project_path)
                result = self_run(qa_req)
                resume_results["steps_run"]["qa"] = {"status": "ok", "passed": result.passed if hasattr(result, "passed") else True}

            elif step_name == "commit":
                commit_msg = original_request.get("commit_message", f"Resume: update {module_name}")
                paths = original_request.get("paths", [])
                result = GitService().commit(project_path, commit_msg, paths, timeout_seconds=60)
                resume_results["steps_run"]["commit"] = {"status": "ok" if result.exit_code == 0 else "failed", "sha": result.stdout.strip()}
                if result.exit_code != 0:
                    resume_results["error"] = f"git commit failed: {result.stderr}"
                    return resume_results

            elif step_name == "push":
                remote = original_request.get("remote", "origin")
                branch = original_request.get("branch")
                result = GitService().push(project_path, remote, branch, timeout_seconds=60)
                resume_results["steps_run"]["push"] = {"status": "ok" if result.exit_code == 0 else "failed"}
                if result.exit_code != 0:
                    resume_results["error"] = f"git push failed: {result.stderr}"
                    return resume_results

            elif step_name == "wait_odoosh":
                # NON-BLOCKING single check — mirrors odoosh.check_commit behaviour.
                # Resuming a job must return fast; the event loop must never block here.
                # If not yet deployed, the agent should call workflow.resume_job again
                # after a short wait, exactly like polling odoosh.check_commit directly.
                odoosh = OdooShService()
                check = odoosh.check_commit_deployed(
                    original_request.get("last_sha", "HEAD")
                )
                deployed = check.exit_code == 0 and "true" in check.stdout.lower()
                resume_results["steps_run"]["wait_odoosh"] = {"deployed": deployed}
                if not deployed:
                    resume_results["error"] = (
                        "Not deployed yet. This is not a failure — wait ~15s and call "
                        "workflow.resume_job again with the same job_id to re-check."
                    )
                    resume_results["status"] = "retry_later"
                    return resume_results

            elif step_name == "upgrade":
                result = OdooRpcService().module_deploy(module_name)
                resume_results["steps_run"]["upgrade"] = {"status": result.status, "error": result.error}
                if result.status != "ok":
                    resume_results["error"] = f"upgrade failed: {result.error}"
                    return resume_results

            elif step_name == "smoke_test":
                result = OdooRpcService().registry_smoke_test(module_name)
                resume_results["steps_run"]["smoke_test"] = {"status": result.status}

        except Exception as exc:
            resume_results["steps_run"][step_name] = {"status": "error", "error": str(exc)}
            resume_results["error"] = f"Step '{step_name}' raised exception: {exc}"
            return resume_results

    resume_results["status"] = "ok"
    resume_results["message"] = f"Pipeline resumed and completed from step '{start_from}'"
    store.log_audit("workflow.resume_job", "ok", f"Completed resume of job {req.job_id}")
    return resume_results
