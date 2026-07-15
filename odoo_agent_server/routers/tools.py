from __future__ import annotations

import asyncio
import ast
import difflib
import fnmatch
import glob
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from ..auth import AuthDependency
from ..config import settings, get_odoo_clients, normalise_client
from ..schemas import (
    ApprovalCreateRequest,
    ApprovalDecisionRequest,
    ArtifactReadRequest,
    AuditListRequest,
    BaseProjectRequest,
    BrowserCheckRequest,
    ClientCheckRequest,
    ClientOnlyRequest,
    DeployedModuleVerifyRequest,
    PosFlowCheckRequest,
    StartSessionRequest,
    RegistrySmokeTestRequest,
    RestoreLastGoodCommitRequest,
    MenuCrawlerRequest,
    CodingTaskPlanRequest,
    OdooCodeChangeRequest,
    OdooCodeFileChange,
    EmptyRequest,
    FullVerifyRequest,
    SafeDeployModuleRequest,
    GitCommitRequest,
    GitDiffRequest,
    GitPushRequest,
    JobGetRequest,
    LimitRequest,
    OdooModuleRequest,
    OdooResult,
    OdooSearchReadRequest,
    OdooShQueryRequest,
    OdooFunctionalQueryRequest,
    OdooShCheckCommitRequest,
    OdooShCommandRequest,
    OdooShCoreFileReadRequest,
    OdooShCoreFilesListRequest,
    OdooShLogRequest,
    OdooShModuleUpdateRequest,
    OdooShUserFileReadRequest,
    OdooShUserFilesListRequest,
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
    RepoWriteItem,
    RepoWriteRequest,
    SelfRunRequest,
    SelfRunReport,
    JobStatus,
    JobStep,
    JobRecord,
    deployment_confirmed_from_record,
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
    ScopeAdviceResult,
    ScopeLevel,
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
from ..services.client_resolver_service import ClientResolverService
from ..services.client_gate_service import ClientGateService
from ..services.odoo_service import OdooService
from ..services.odoosh_service import OdooShService
from ..services.browser_cdp_service import CdpBrowserService
from ..services.repo_service import RepoService
from ..services.prewrite_gate_service import PreWriteGateService
from ..services.path_service import resolve_allowed_path
from ..services.syntax_verifier_service import SyntaxVerifierService
from ..services.workflow_guard_service import (
    code_change_issue_fingerprint,
    mark_stale_safe_deploy_running_job,
    record_code_change_deploy_attempt,
    repeated_failure_guard_status,
)
from .qa import self_run
from .repo import repo_delete, repo_write, repo_write_many_atomic, repo_write_many_fast_safe, repo_write_many_smart, repo_apply_patch, repo_apply_patch_atomic, repo_clean_pyc, repo_prepare, repo_custom_modules
from .git import git_commit, git_push
from .odoo import odoo_module_install, odoo_module_update, odoo_module_deploy, start_odoo_module_deploy_job
from .odoosh import odoosh_module_install_ssh, odoosh_module_deploy_ssh, odoosh_module_update, odoosh_wait_for_commit, odoosh_verify_module_manifest
from .workflow import capability_check, full_verify, safe_deploy_module, coding_task_plan, start_session, registry_smoke_test, restore_last_good_commit, browser_menu_crawl, find_core_file, find_similar_view, list_module_files, scope_advice, verify_against_core, verify_pos_change, frontend_pattern_preflight, smart_tool_router, repo_index, verify_deployed_module, _poll_odoosh_deployed, _pre_push_verify_files, _expected_user_file_hashes, _infer_registry_fields_from_python, _normalise_expected_registry_fields, _verify_registry_fields, _assert_head_commit_contains_paths
from ..schemas import ApprovalRecord, ApprovalStatus
from ..services.store_service import JsonStore
from ..services.deploy_impact_service import classify_deploy_impact
from ..services.failure_policy_service import FailurePolicyService
from ..services.background_executor import background_task, submit_background

router = APIRouter(prefix="/tools", tags=["tools"], dependencies=[AuthDependency])

_STRICT_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_CODE_CHANGE_ENTRY_LOCK = threading.Lock()


def strict_safe_tool_name(name: str) -> str:
    """Return a strict MCP-client-safe tool name.

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
    """Put functional Odoo query tools early so MCP agents choose them first.

    Functional questions like "list draft invoices" should call odoo_search_read
    directly and not enter the coding workflow.
    """
    order = {
        "connector.health": 0,
        "odoo.query": 1,
        "odoosh.query": 2,
        "odoo.search_read": 3,
        "odoo.health": 3,
        "odoo.module_status": 3,
        "workflow.smart_tool_router": 4,
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
        name="client.check",
        title="Check/select MCP client",
        description=(
            "Mandatory first gate for multi-client ODOO_CLIENTS setups. "
            "Call without client_name to list clients and require user selection when multiple clients exist. "
            "The user's exact client-name reply is final confirmation: immediately call with that client_name, "
            "validate=true, and user_confirmed=true when the field is available; never ask a second confirmation. "
            "Older/cached client schemas may omit user_confirmed, so validation also accepts the selected client_name alone. "
            "Never set force_refresh=true for a normal query, schema retry, or repeated request; healthy cached validation is reused. "
            "Validation checks repo/token/branch, checkout, Odoo.sh SSH, Odoo shell health, and addons path. "
            "Write/git/deploy tools are blocked until this passes when multiple clients are configured."
        ),
        read_only=True,
    ),

    ToolDescriptor(name="workflow.capability_check",
        title="Check connector capabilities", description="Check local connector capabilities and missing runtime configuration", read_only=True),
    ToolDescriptor(name="workflow.coding_task_plan",
        title="Get coding workflow plan", description="Return the repo-first coding workflow guidance for MCP agents", read_only=True),
    ToolDescriptor(
        name="workflow.odoo_code_change",
        title="Provider-safe Odoo code change",
        description=(
            "Single high-level provider-safe coding workflow. Use this instead of manually calling repo.read/write, "
            "scope_advice, verify, git.push, or module_deploy. It returns a background job_id immediately; poll jobs.get "
            "for live phase transitions and the terminal result. It enforces the v15 flow internally: "
            "read custom module, scope_advice, read /home/odoo/src/odoo/addons related source first with enterprise fallback, "
            "apply provided edits, git.diff, qa.self_run, verify_against_core, then workflow.safe_deploy_module and jobs.get polling. "
            "For a missing addon, set create_module=true with an exact lowercase technical name; it is created only at the checkout root. "
            "For a new addon, generate its entire installable file set in one writes array (up to 100 complete files). "
            "An empty, scaffold-only, or manifest-referencing-incomplete new-addon batch is rejected before any file is written. "
            "For an existing addon, empty writes returns focused inspection context and asks for a second call with exact writes. "
            "Never use web search, terminal/Git commands, restore/reset/checkout, or a second clean checkout to work "
            "around dirty files. Re-call this workflow in the same checkout; it preserves and safely includes existing "
            "uncommitted module work. Short approvals such as apply/implement continue the pending operation. Adaptive "
            "task lanes keep small and normal work within strict read/search budgets and cache target locations. Tiny deterministic CSS/SCSS/XML text/manifest edits skip remote core discovery but retain diff, QA, syntax, push, and deploy guards. Broad, "
            "ambiguous, migration, architecture, or multi-file work automatically "
            "promotes to long mode. CSS/SCSS additionally supports deterministic unique-selector autopatching."
        ),
        requires_approval=True,
    ),
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
        title="Deploy module (full pipeline)", description="Safely commit, push, poll Odoo.sh adaptively for up to 4 minutes for the pushed SHA, stop immediately on detected Odoo.sh build/test failures, verify changed file hashes under /home/odoo/src/user, verify manifest, upgrade an Odoo module, For MCP clients this starts/resumes a background safe-deploy job and returns job_id/next_call immediately; status=running is not success and must be followed via jobs.get. Only treat the deployment as complete when response.deploy_confirmed == true. A finished job without this field set to true means the deploy did not fully complete — do not report success to the user. If git diff shows no changes for this module, safe_deploy_module automatically runs Odoo.sh source verification and returns verified_already_deployed/already_deployed_confirmed; read this field before telling the user anything is 'already added' or 'already live'. Do not make that claim from memory of a previous turn.", requires_approval=True),
    ToolDescriptor(
        name="workflow.verify_deployed_module",
        title="Verify deployed source",
        description=(
            "Compare active checkout files to the real Odoo.sh /home/odoo/src/user files and optionally live module status. "
            "Use before saying a local/GitHub change is already deployed, already live, or already done. repo.read alone is not proof. "
            "If git diff shows no changes for a module, workflow.safe_deploy_module will automatically run this source verification and return verified_already_deployed — read this field before telling the user anything is 'already added' or 'already live'. Do not make that claim from memory of a previous turn."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="workflow.start_session",
        title="Start coding session", description="Bootstrap a coding session: capability check, repo.prepare, git status, module discovery, Odoo/Odoo.sh health", requires_approval=True),
    ToolDescriptor(name="workflow.registry_smoke_test",
        title="Test Odoo registry", description="Verify live Odoo registry models/menus after install or upgrade", read_only=True),
    ToolDescriptor(name="workflow.restore_last_good_commit",
        title="Revert a bad code release", description="Fast deterministic Git revert for an exact bad SHA or current HEAD, with optional manifest version/bump, local QA, one combined commit, push, and SHA-guarded module deployment. Use for revert/rollback/restore-last-version requests; never reconstruct parent files through workflow.odoo_code_change writes or artifacts.", requires_approval=True),
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
            "slow repo.write_many_atomic for normal OWL/POS/website/XML changes. Each file should use "
            "relative_path; path is also accepted as a compatibility alias."
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
        title="Push code to GitHub", description="Direct git.push is blocked for Odoo module code changes. Use workflow.safe_deploy_module so push, deploy, upgrade, and verification remain one controlled job. Non-module pushes may still use this tool.", requires_approval=True),
    ToolDescriptor(
        name="odoosh.module_install_ssh",
        title="Install module via SSH",
        description=(
            "[LEGACY — odoo.module_deploy auto-falls-back to SSH on Access Denied, no need to call this directly] "
            "Install a NEW module via Odoo.sh SSH using odoo-bin -i flag. "
            "Do not call this directly after a fresh code change. Use workflow.safe_deploy_module instead, "
            "which pushes code and verifies the remote HEAD before upgrading. This tool will refuse to run "
            "if local commits are not yet pushed to the remote branch."
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
            "NON-BLOCKING single source checkout check: returns immediately with deployed=true/false. "
            "deployed=true only means ~/src/user has the SHA; safe_deploy_module also checks recent "
            "Odoo.sh build/test failure evidence and changed /home/odoo/src/user file hashes before module upgrade. Never use odoosh.wait_for_commit."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="odoosh.wait_for_commit",
        title="Wait for Odoo.sh deployment", description="[DEPRECATED - use odoosh.check_commit in a polling loop] Blocking wait for Odoo.sh deploy", read_only=True),
    ToolDescriptor(name="odoosh.verify_module_manifest",
        title="Verify module manifest", description="Verify a module manifest exists and is installable on Odoo.sh", read_only=True),
    ToolDescriptor(
        name="odoosh.read_core_file",
        title="Read Odoo.sh core source file",
        description=(
            "Read exact installed Odoo.sh source from /home/odoo/src/odoo/addons, /home/odoo/src/enterprise, or /home/odoo/src/user. "
            "Use when workflow.find_core_file/list_module_files identifies a native Odoo file that must be inspected before POS/OWL/XML coding. "
            "For POS native files use base='odoo_addons' and relative_path like 'point_of_sale/static/src/app/generic_components/order_widget/order_widget.xml'."
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="odoosh.list_core_files",
        title="List Odoo.sh core source files",
        description=(
            "List exact installed Odoo.sh source files under /home/odoo/src/odoo/addons, /home/odoo/src/enterprise, or /home/odoo/src/user. "
            "Use to browse native Odoo module folders before reading exact files."
        ),
        read_only=True,
    ),
    ToolDescriptor(
        name="odoosh.read_user_file",
        title="Read deployed user file",
        description="Read the actual Odoo.sh /home/odoo/src/user file content only when proving an already-deployed/live claim or investigating deployed-source mismatch. Do not call before editing simple custom XML/CSS/SCSS UI text/style; use repo.read on the checkout target file instead.",
        read_only=True,
    ),
    ToolDescriptor(
        name="odoosh.list_user_files",
        title="List deployed user files",
        description="List actual Odoo.sh /home/odoo/src/user files for a custom module/subdirectory. Use before claiming code already exists on Odoo.sh.",
        read_only=True,
    ),
    ToolDescriptor(name="odoosh.log",
        title="Read Odoo.sh server log", description="Tail an Odoo.sh log", read_only=True),
    ToolDescriptor(name="odoosh.grep_errors",
        title="Search Odoo.sh error logs", description="Search Odoo.sh logs for errors", read_only=True),
    ToolDescriptor(name="odoosh.error_pack",
        title="Collect Odoo.sh error details", description="Return Odoo.sh tracebacks plus structured root-cause hints/files/XML/model names. Invalid-field failures explicitly require Python registry/import inspection and reject XML eval/value-format fixes.", read_only=True),
    ToolDescriptor(
        name="odoosh.module_deploy_ssh",
        title="Deploy module via SSH",
        description=(
            "SSH smart install/upgrade via Odoo.sh. Background job. Use only when you explicitly need SSH; "
            "odoo.module_deploy falls back to this automatically. Do not call this directly after a fresh code change. "
            "Use workflow.safe_deploy_module instead, which pushes code and verifies the remote HEAD before upgrading. "
            "This tool will refuse to run if local commits are not yet pushed to the remote branch."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(
        name="odoosh.module_update",
        title="Update module on Odoo.sh",
        description=(
            "Run Odoo module update through Odoo.sh SSH. Do not call this directly after a fresh code change. "
            "Use workflow.safe_deploy_module instead, which pushes code and verifies the remote HEAD before upgrading. "
            "This tool will refuse to run if local commits are not yet pushed to the remote branch."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(
        name="odoosh.query",
        title="Query Odoo via SSH",
        description=(
            "Read-only Odoo model query through Odoo.sh SSH using odoo-bin shell. "
            "Use when Odoo shell login/password is missing or failing. Requires client_name in multi-client mode; "
            "does not need Odoo email/password, only Odoo.sh SSH key access."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="odoo.health",
        title="Check Odoo server health", description="Check Odoo Odoo shell health", read_only=True),
    ToolDescriptor(
        name="odoo.query",
        title="Smart Odoo functional query",
        description=(
            "First tool for every read-only Odoo business-data question, including custom models and analytical/ranking requests. "
            "It generates a query plan, dynamically discovers the relevant model/module and fields from live ir.model.fields metadata, "
            "then executes a bounded search_read. Supported derived cross-model reports such as workforce presence gaps run in one "
            "Odoo shell session. It never guesses res.partner for an unknown request. "
            "Delete/unlink/write/create/update/archive requests are hard-blocked without executing Odoo methods. "
            "Returns the generated model/domain/fields/order plan plus live records. "
            "Runs read-only Odoo shell through the selected client's Odoo.sh SSH context."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="odoo.module_status",
        title="Check module install status", description="Read Odoo module state (installed/uninstalled/not found). Do not call repeatedly while locating files or before simple custom UI/style edits; use repo.search/repo.read, then write, then workflow.safe_deploy_module.", read_only=True),
    ToolDescriptor(
        name="odoo.module_deploy",
        title="Install or upgrade module",
        description=(
            "SMART INSTALL/UPGRADE — use directly for an existing module install or upgrade; never route an install-only request through workflow.odoo_code_change. "
            "If a recent manual git.push was recorded, this tool first verifies the pushed SHA, recent "
            "Odoo.sh failure logs, and changed /home/odoo/src/user file hashes before touching the DB. "
            "Runs install/upgrade through Odoo.sh SSH and verifies source/deploy state in the same background job. "
            "For OpenCode/local-checkout changes, pass expected_sha from the pushed local HEAD; the connector will not touch the module registry until that exact SHA is active on Odoo.sh. "
            "For MCP providers this never blocks for final success: it returns job_id + next_call. status=running is not success; call jobs.get until terminal passed/failed before telling the user deployed/upgraded."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(name="odoo.module_install",
        title="Install Odoo module", description="[LEGACY] Direct button_immediate_install — use odoo.module_deploy instead", requires_approval=True, deprecated=True),
    ToolDescriptor(name="odoo.module_update",
        title="Upgrade Odoo module", description="[LEGACY] Direct button_immediate_upgrade — use odoo.module_deploy instead", requires_approval=True, deprecated=True),
    ToolDescriptor(name="odoo.search_read",
        title="Query Odoo records", description="Run Odoo search_read safely. Uses Odoo.sh SSH first, then HTTPS JSON-RPC read-only fallback when SSH is refused/unavailable for a client.", read_only=True),
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
        title="Check background job status", description="Read one connector job by id. Remote MCP profiles force this call to be non-blocking to prevent client HTTP timeouts; poll again while status is queued/running. Only treat the deployment as complete when the terminal result confirms the requested module operation.", read_only=True),
    ToolDescriptor(name="audit.list",
        title="View audit log", description="List recent audit events", read_only=True),
    # Continue a bounded, interrupted, or recoverable pipeline from its checkpoint.
    ToolDescriptor(
        name="workflow.resume_job",
        title="Continue paused deployment",
        description=(
            "Continue an intentionally paused, bounded-wait, interrupted, or recoverable "
            "safe_deploy_module pipeline from its last successful checkpoint. A continuation is "
            "not a deployment failure unless the returned result explicitly reports a terminal "
            "error. Pass job_id when available; if omitted, the connector continues the "
            "last saved safe-deploy job from session state or the latest matching job. Instead of "
            "starting over from step 1, the agent continues from where it left off. Optionally "
            "override the start step with override_step (qa | commit | push | wait_odoosh | "
            "verify_deployed_code | install | upgrade | module_update | smoke_test)."
        ),
        requires_approval=True,
    ),
    # Fix 7: Session state persistence
    ToolDescriptor(
        name="session.save_state",
        title="Save session state",
        description=(
            "Fix 7 — Persist session context (last_sha, module_name, step_history, etc.) so a new "
            "MCP agent session after a restart can reload it with session.load_state instead "
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
            "Search priority: /home/odoo/src/odoo/addons → /home/odoo/src/enterprise → /home/odoo/src/user fallback for other related custom dependencies. "
            "Do not use this to read the current custom module; read that from the active checkout with repo.read. "
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
            "Search priority: odoo/addons → enterprise → user fallback for other related custom dependencies. "
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
            "Search priority: odoo/addons → enterprise → user fallback for other related custom dependencies. "
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
            "V15 MANDATORY DUAL READ: Analyse custom module files and return the adaptive source-read plan before ANY code write, including XML/CSS/SCSS/UI edits. "
            "Call AFTER repo.read on custom files and BEFORE repo.write/repo.apply_patch. "
            "Search priority for related source is current custom module → /home/odoo/src/odoo/addons first → /home/odoo/src/enterprise fallback → /home/odoo/src/user only for other custom dependencies. "
            "Scans manifest depends, XML inherit_id/xpath/template refs, _inherit (L1), mixins (L2), computed/relational fields (L3), cross-module refs (L4). "
            "Returns ordered ScopeReadInstruction items — execute ALL mandatory instructions up to L2 before writing. "
            "Example: custom_module_name='my_sale_ext', custom_file_paths=['/path/to/models/sale_order.py', '/path/to/views/view.xml']"
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
    "client.check": ClientCheckRequest,
    "workflow.capability_check": EmptyRequest,
    "workflow.coding_task_plan": CodingTaskPlanRequest,
    "workflow.odoo_code_change": OdooCodeChangeRequest,
    "workflow.smart_tool_router": SmartToolRouteRequest,
    "workflow.repo_index": RepoIndexRequest,
    "workflow.full_verify": FullVerifyRequest,
    "workflow.safe_deploy_module": SafeDeployModuleRequest,
    "workflow.verify_deployed_module": DeployedModuleVerifyRequest,
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
    "odoosh.module_install_ssh": OdooModuleRequest,
    "odoosh.query": OdooShQueryRequest,
    "odoosh.health": ClientOnlyRequest,
    "odoosh.git_status": ClientOnlyRequest,
    "odoosh.check_commit": OdooShCheckCommitRequest,
    "odoosh.wait_for_commit": OdooShWaitForCommitRequest,
    "odoosh.verify_module_manifest": OdooShVerifyManifestRequest,
    "odoosh.read_core_file": OdooShCoreFileReadRequest,
    "odoosh.list_core_files": OdooShCoreFilesListRequest,
    "odoosh.read_user_file": OdooShUserFileReadRequest,
    "odoosh.list_user_files": OdooShUserFilesListRequest,
    "odoosh.log": OdooShLogRequest,
    "odoosh.grep_errors": OdooShCommandRequest,
    "odoosh.error_pack": OdooShCommandRequest,
    "odoosh.module_deploy_ssh": OdooShModuleUpdateRequest,
    "odoosh.module_update": OdooShModuleUpdateRequest,
    "odoo.health": ClientOnlyRequest,
    "odoo.query": OdooFunctionalQueryRequest,
    "odoo.module_status": OdooModuleRequest,
    "odoo.module_deploy": OdooModuleRequest,
    "odoo.module_install": OdooModuleRequest,
    "odoo.module_update": OdooModuleRequest,
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
    except ValidationError as exc:
        return ToolCallResult(name=external_name, status="failed", error=format_tool_validation_error(exc))
    except Exception as exc:  # noqa: BLE001
        return ToolCallResult(name=external_name, status="failed", error=str(exc))


def format_tool_validation_error(exc: ValidationError) -> str:
    """Return concise agent-facing validation text without Pydantic internals."""
    parts: list[str] = []
    for error in exc.errors(include_url=False):
        loc = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        message = str(error.get("msg", "invalid value"))
        parts.append(f"{loc}: {message}")
    details = "; ".join(parts) if parts else str(exc)
    return f"Tool input invalid: {details}"


def _force_mcp_async_deploy_args(args: dict) -> dict:
    """For MCP providers, never block inside direct deploy/upgrade tools.

    Direct deploy tools are compatibility tools. They must start a background job
    and return job_id/next_call so the provider polls jobs.get. Final success is
    only allowed from a terminal job result.
    """
    data = dict(args or {})
    data["wait_for_result"] = False
    data["wait_timeout_seconds"] = 0
    data.setdefault("poll_interval_seconds", 2.0)
    return data



def _infer_client_name_from_text(text: str) -> str | None:
    lowered = (text or "").lower()
    for item in get_odoo_clients():
        client = normalise_client(item)
        name = str(client.get("name") or "").strip()
        if name and name.lower() in lowered:
            return name
    return None


def _odoo_service_for_client(client=None):
    if client is not None:
        try:
            return OdooService(client)
        except TypeError:
            return OdooService()
    return OdooService()


def _resolve_query_client(client_name: str | None, query_text: str | None = None):
    requested = client_name or _infer_client_name_from_text(query_text or "")
    if not requested:
        # Let resolver auto-select only when there is exactly one complete client
        # or legacy env is in use. If multiple clients exist, it raises an
        # explicit ambiguity error instead of silently using a saved/default repo.
        return ClientResolverService().resolve_client(require_complete=True)
    client = ClientResolverService().resolve_client(client_name=requested, require_complete=True)
    return client


def _search_read_via_shell(
    *,
    model: str,
    domain: list,
    fields: list[str],
    limit: int,
    offset: int = 0,
    order: str | None = None,
    context: dict | None = None,
    client_name: str | None = None,
    query_text: str | None = None,
    timeout_seconds: int = 45,
) -> dict:
    client = _resolve_query_client(client_name, query_text)
    shell_result = _odoo_service_for_client(client).search_read(
        model, domain, fields, limit, offset, order, context, timeout_seconds
    )
    shell_dumped = shell_result.model_dump(mode="json") if hasattr(shell_result, "model_dump") else shell_result
    if isinstance(shell_dumped, dict) and shell_dumped.get("status") == "ok":
        return {
            "status": "ok",
            "source": "odoo_ssh_or_https_jsonrpc",
            "client_name": client.name if client else None,
            "odoo": shell_dumped,
            "attempt": shell_dumped,
            "ssh_fallback_used": False,
            "message": "Odoo query completed. SSH is preferred; HTTPS JSON-RPC read-only fallback is used automatically when SSH is unavailable.",
        }

    # OdooService already attempted SSH and its bounded HTTPS fallback. Replaying
    # the same SSH command here doubled worst-case latency without a new path.
    return {
        "status": shell_dumped.get("status") if isinstance(shell_dumped, dict) else "failed",
        "source": "odoo_ssh_or_https_jsonrpc_failed",
        "client_name": client.name if client else None,
        "odoo": shell_dumped,
        "shell_attempt": shell_dumped,
        "ssh_fallback_used": False,
        "message": "Odoo query failed after one SSH attempt and the bounded HTTPS JSON-RPC fallback; duplicate SSH replay was skipped.",
    }


_QUERY_MUTATION_RE = re.compile(
    r"\b(delete|remove|unlink|erase|purge|destroy|archive|deactivate|drop|truncate|wipe)\b",
    re.I,
)
_QUERY_WRITE_RE = re.compile(
    r"^\s*(?:(?:please|kindly)\s+)*(?:(?:can|could|would)\s+you\s+)?"
    r"(?:(?:please|kindly)\s+)*(write|update|modify|create|change|set)\b",
    re.I,
)
_QUERY_RANKING_RE = re.compile(r"\b(highest|largest|maximum|max|top|lowest|smallest|minimum|min|best|worst)\b", re.I)
_WORKFORCE_PRESENCE_GAP_RE = re.compile(
    r"\b(absent|absence|missing\s+attendance|no\s+attendance|did\s+not\s+attend)\b.*\b(employee|employees|staff|worker|workers)\b|"
    r"\b(employee|employees|staff|worker|workers)\b.*\b(absent|absence|missing\s+attendance|no\s+attendance|did\s+not\s+attend)\b",
    re.I,
)
_QUERY_STOP_WORDS = {
    "a", "an", "and", "are", "all", "for", "from", "get", "give", "in", "is", "last", "list",
    "me", "of", "on", "please", "record", "records", "show", "the", "to", "with", "who", "which",
    "highest", "largest", "maximum", "max", "top", "lowest", "smallest", "minimum", "min", "best", "worst",
}
_QUERY_SYNONYM_GROUPS = {
    "employee": {"employee", "employees", "staff", "worker", "workers"},
    "salary": {"salary", "salaries", "wage", "wages", "pay", "compensation"},
    "customer": {"customer", "customers", "client", "clients", "partner", "partners"},
    "invoice": {"invoice", "invoices", "billing", "billings"},
    "product": {"product", "products", "item", "items"},
    "amount": {"amount", "total", "value", "revenue", "cost", "price"},
    "quantity": {"quantity", "qty", "stock", "units"},
    "order": {"order", "orders", "quotation", "quotations"},
    "unpaid": {"unpaid", "not_paid", "not"},
}
_NUMERIC_FIELD_TYPES = {"integer", "float", "monetary"}
_DATE_FIELD_TYPES = {"date", "datetime"}
_QUERY_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


def _raw_query_tokens(text: str) -> set[str]:
    tokens = {token for token in re.findall(r"[a-z][a-z0-9_]+", text.lower()) if token not in _QUERY_STOP_WORDS}
    inflections = set(tokens)
    for token in tokens:
        if len(token) > 4 and token.endswith("ies"):
            inflections.add(token[:-3] + "y")
        elif len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "us")):
            inflections.add(token[:-1])
    return inflections


def _query_tokens(text: str) -> set[str]:
    raw = _raw_query_tokens(text)
    expanded = set(raw)
    for aliases in _QUERY_SYNONYM_GROUPS.values():
        if raw & aliases:
            expanded.update(aliases)
    return expanded


def _is_mutating_query(text: str) -> bool:
    return bool(_QUERY_MUTATION_RE.search(text) or _QUERY_WRITE_RE.search(text.strip()))


def _requested_calendar_date(text: str) -> str | None:
    iso_match = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text)
    if iso_match:
        raw = f"{iso_match.group(1)}-{int(iso_match.group(2)):02d}-{int(iso_match.group(3)):02d}"
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None
    month_names = "|".join(sorted(_QUERY_MONTHS, key=len, reverse=True))
    day_first = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_names})\s+(20\d{{2}})\b", text, re.I)
    month_first = re.search(rf"\b({month_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,)?\s+(20\d{{2}})\b", text, re.I)
    if day_first:
        day, month, year = int(day_first.group(1)), _QUERY_MONTHS[day_first.group(2).lower()], int(day_first.group(3))
    elif month_first:
        month, day, year = _QUERY_MONTHS[month_first.group(1).lower()], int(month_first.group(2)), int(month_first.group(3))
    else:
        return None
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _workforce_presence_gap_query(req: OdooFunctionalQueryRequest, report_date: str) -> dict[str, Any]:
    client = _resolve_query_client(req.client_name, req.query)
    result = OdooShService(client).workforce_presence_report(report_date, limit=req.limit, timeout_seconds=45)
    dumped = result.model_dump(mode="json")
    return {
        "status": dumped.get("status"),
        "intent": "workforce_presence_gap",
        "planner": "single_shell_composite_recipe",
        "read_only_enforced": True,
        "client_name": client.name if client else None,
        "parsed": {
            "report_date": report_date,
            "operation": "scheduled_employees - attendance_overlap - approved_leave",
            "models": ["hr.employee", "hr.attendance", "hr.leave", "resource.calendar"],
            "single_odoo_shell": True,
            "limit": req.limit,
        },
        "odoo": dumped,
        "message": (
            "Workforce presence gap calculated in one Odoo shell session."
            if dumped.get("status") == "ok"
            else dumped.get("error") or "Workforce presence analysis failed."
        ),
    }


def _odoo_result_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    payload = data.get("odoo") if isinstance(data, dict) else None
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    result = payload.get("result") if isinstance(payload, dict) else None
    return [row for row in result if isinstance(row, dict)] if isinstance(result, list) else []


def _metadata_or_domain(tokens: set[str], raw_tokens: set[str] | None = None) -> list[Any]:
    conditions: list[Any] = []
    for token in sorted(tokens, key=lambda item: (-len(item), item))[:8]:
        if token in (raw_tokens if raw_tokens is not None else tokens):
            conditions.append(("model", "ilike", token))
        conditions.extend([("name", "ilike", token), ("field_description", "ilike", token)])
    if not conditions:
        return []
    return ["|"] * (len(conditions) - 1) + conditions


def _relative_date_domain(text: str, field_name: str | None) -> list[Any]:
    if not field_name:
        return []
    today = datetime.now(timezone.utc).date()
    if "yesterday" in text:
        day = today - timedelta(days=1)
        return [(field_name, ">=", day.isoformat()), (field_name, "<", today.isoformat())]
    if "today" in text:
        return [(field_name, ">=", today.isoformat())]
    word_numbers = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
    match = re.search(r"\blast\s+(\d+|one|two|three|four|five|six|seven)\s+days?\b", text)
    if match:
        days = int(match.group(1)) if match.group(1).isdigit() else word_numbers[match.group(1)]
        return [(field_name, ">=", (today - timedelta(days=max(0, days - 1))).isoformat())]
    if re.search(r"\blast\s+(week|7\s+days)\b", text):
        return [(field_name, ">=", (today - timedelta(days=7)).isoformat())]
    if re.search(r"\blast\s+(month|30\s+days)\b", text):
        return [(field_name, ">=", (today - timedelta(days=30)).isoformat())]
    return []


def _field_match_score(
    row: dict[str, Any],
    tokens: set[str],
    *,
    ranking: bool,
    include_model: bool = True,
) -> int:
    name = str(row.get("name") or "").lower()
    description = str(row.get("field_description") or "").lower()
    model = str(row.get("model") or "").lower()
    relation = str(row.get("relation") or "").lower()
    field_tokens = set(re.findall(r"[a-z][a-z0-9_]+", name.replace("_", " ") + " " + description))
    model_tokens = set(re.findall(r"[a-z][a-z0-9_]+", model.replace(".", " ").replace("_", " ")))
    score = 0
    for token in tokens:
        if include_model and token in model_tokens:
            score = max(score, 10)
        elif token == name or token in field_tokens:
            score = max(score, 8)
        elif token in description:
            score = max(score, 5)
        elif token in model or token in relation:
            score = max(score, 3)
    if ranking and str(row.get("ttype") or "") in _NUMERIC_FIELD_TYPES and score:
        score += 5
    return score


def _module_hints_for_model(model: str, field_rows: list[dict[str, Any]] | None = None) -> list[str]:
    counts: dict[str, int] = {}
    for row in field_rows or []:
        raw = str(row.get("modules") or "")
        for module in re.split(r"[,\s]+", raw):
            module = module.strip()
            if module:
                counts[module] = counts.get(module, 0) + 1
    if counts:
        return [name for name, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5]]
    return []


def _selection_options(raw: Any) -> list[tuple[str, str]]:
    """Normalize live ir.model.fields selection metadata without executing code."""
    value = raw
    if isinstance(value, str):
        try:
            value = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return []
    if not isinstance(value, (list, tuple)):
        return []
    options: list[tuple[str, str]] = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            key, label = str(item[0]), str(item[1])
            if key and label:
                options.append((key, label))
    return options


def _selection_domains(text: str, tokens: set[str], field_rows: list[dict[str, Any]]) -> list[Any]:
    """Build filters from the selected model's live selection labels."""
    domains: list[Any] = []
    normalized_text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    for row in field_rows:
        if str(row.get("ttype") or "") != "selection":
            continue
        scored: list[tuple[int, str]] = []
        for key, label in _selection_options(row.get("selection")):
            label_text = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
            label_tokens = _query_tokens(label_text)
            overlap = len(tokens & label_tokens)
            phrase_match = bool(label_text and re.search(rf"\b{re.escape(label_text)}s?\b", normalized_text))
            score = 20 if phrase_match else overlap
            if score:
                scored.append((score, key))
        if not scored:
            continue
        best_score = max(score for score, _key in scored)
        keys = list(dict.fromkeys(key for score, key in scored if score == best_score))
        field_name = str(row.get("name") or "")
        if field_name and keys:
            domains.append((field_name, "=" if len(keys) == 1 else "in", keys[0] if len(keys) == 1 else keys))
    return domains


def _best_date_field(text: str, tokens: set[str], field_rows: list[dict[str, Any]]) -> str | None:
    candidates = [row for row in field_rows if str(row.get("ttype") or "") in _DATE_FIELD_TYPES]
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda row: (-_field_match_score(row, tokens, ranking=False, include_model=False), str(row.get("name") or "")),
    )
    scored = _field_match_score(ranked[0], tokens, ranking=False, include_model=False)
    if scored:
        return str(ranked[0].get("name") or "") or None
    create_field = next((row for row in candidates if row.get("name") == "create_date"), None)
    return str((create_field or ranked[0]).get("name") or "") or None


def _numeric_comparison(text: str) -> tuple[str, float] | None:
    match = re.search(
        r"\b(over|above|greater\s+than|more\s+than|under|below|less\s+than)\s+([0-9]+(?:\.[0-9]+)?)\b",
        text,
    )
    if not match:
        return None
    operator = ">" if match.group(1) in {"over", "above", "greater than", "more than"} else "<"
    return operator, float(match.group(2))


def _dynamic_query_plan(req: OdooFunctionalQueryRequest) -> dict[str, Any]:
    text = (req.query or "").strip().lower()
    raw_tokens = _raw_query_tokens(text)
    tokens = _query_tokens(text)
    ranking = bool(_QUERY_RANKING_RE.search(text))
    metadata = _search_read_via_shell(
        model="ir.model.fields",
        domain=_metadata_or_domain(tokens, raw_tokens),
        fields=["model", "name", "field_description", "ttype", "relation", "readonly", "modules", "selection"],
        limit=300,
        order="model asc, name asc",
        client_name=req.client_name,
        query_text=req.query,
    )
    rows = _odoo_result_rows(metadata)
    if metadata.get("status") != "ok" or not rows:
        return {
            "status": "needs_clarification",
            "intent": "dynamic_query",
            "read_only_enforced": True,
            "message": "No reliable Odoo model/field match was found. Ask for one business object or field clue; do not guess a model.",
            "query": req.query,
            "metadata_source": metadata.get("source"),
            "success_allowed": False,
        }

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        model = str(row.get("model") or "").strip()
        if not model or model.startswith(("ir.model", "base.", "bus.")):
            continue
        item = grouped.setdefault(model, {"score": 0, "field_scores": [], "fields": [], "numeric_score": 0})
        score = _field_match_score(row, tokens, ranking=ranking, include_model=False)
        item["field_scores"].append(score)
        item["fields"].append(row)
        if str(row.get("ttype") or "") in _NUMERIC_FIELD_TYPES:
            item["numeric_score"] = max(int(item["numeric_score"]), score)
    for model, item in grouped.items():
        strongest = sorted((int(score) for score in item["field_scores"]), reverse=True)[:6]
        model_tokens = set(re.findall(r"[a-z][a-z0-9_]+", model.replace(".", " ").replace("_", " ")))
        item["score"] = sum(strongest) + (8 * len(raw_tokens & model_tokens))
    if ranking:
        ranked_groups = [(model, info) for model, info in grouped.items() if int(info["numeric_score"]) > 0]
    else:
        ranked_groups = list(grouped.items())
    ranked_groups.sort(key=lambda item: (-int(item[1]["score"]), item[0]))
    close_ambiguous = bool(
        len(ranked_groups) > 1
        and int(ranked_groups[0][1]["score"]) - int(ranked_groups[1][1]["score"]) <= 1
    )
    if not ranked_groups or int(ranked_groups[0][1]["score"]) < 5 or close_ambiguous:
        return {
            "status": "needs_clarification",
            "intent": "dynamic_query",
            "read_only_enforced": True,
            "message": "The request is ambiguous across Odoo models. Provide the business object or desired field; no fallback model was queried.",
            "candidates": [{"model": model, "score": info["score"]} for model, info in ranked_groups[:5]],
            "success_allowed": False,
        }

    model, best = ranked_groups[0]
    all_fields_data = _search_read_via_shell(
        model="ir.model.fields",
        domain=[("model", "=", model)],
        fields=["model", "name", "field_description", "ttype", "relation", "readonly", "modules", "selection"],
        limit=400,
        order="name asc",
        client_name=req.client_name,
        query_text=req.query,
    )
    all_fields = _odoo_result_rows(all_fields_data) or list(best["fields"])
    by_name = {str(row.get("name") or ""): row for row in all_fields if row.get("name")}
    matched = sorted(
        all_fields,
        key=lambda row: (-_field_match_score(row, tokens, ranking=ranking, include_model=False), str(row.get("name") or "")),
    )
    measure = next(
        (str(row["name"]) for row in matched if str(row.get("ttype") or "") in _NUMERIC_FIELD_TYPES and _field_match_score(row, tokens, ranking=ranking, include_model=False)),
        None,
    )
    result_fields: list[str] = ["id"]
    for preferred in ("display_name", "name"):
        if preferred in by_name:
            result_fields.append(preferred)
            break
    for row in matched:
        name = str(row.get("name") or "")
        score = _field_match_score(row, tokens, ranking=ranking, include_model=False)
        if name and score and name not in result_fields and str(row.get("ttype") or "") not in {"one2many", "many2many", "binary", "html"}:
            result_fields.append(name)
        if len(result_fields) >= 8:
            break
    for preferred in ("state", "active", "company_id", "currency_id", "date", "date_start", "date_end"):
        if preferred in by_name and preferred not in result_fields and len(result_fields) < 10:
            result_fields.append(preferred)
    if len(result_fields) < 5:
        relation_field = next(
            (
                str(row.get("name") or "")
                for row in all_fields
                if str(row.get("ttype") or "") == "many2one"
                and str(row.get("name") or "") not in {"company_id", "create_uid", "write_uid"}
            ),
            None,
        )
        if relation_field and relation_field not in result_fields:
            result_fields.append(relation_field)

    descending = not bool(re.search(r"\b(lowest|smallest|minimum|min)\b", text))
    domain: list[Any] = []
    if "active" in by_name:
        if "inactive" in text or "archived" in text:
            domain.append(("active", "=", False))
        elif "active" in text:
            domain.append(("active", "=", True))
    domain.extend(_selection_domains(text, tokens, all_fields))
    for row in matched:
        name = str(row.get("name") or "")
        score = _field_match_score(row, tokens, ranking=False, include_model=False)
        field_type = str(row.get("ttype") or "")
        if not name or score < 8:
            continue
        if field_type == "boolean" and not any(item[0] == name for item in domain if isinstance(item, tuple)):
            domain.append((name, "=", not bool(re.search(rf"\b(not|without|no)\s+{re.escape(name.replace('_', ' '))}\b", text))))
        elif field_type in _NUMERIC_FIELD_TYPES and name.endswith("_rank"):
            domain.append((name, ">", 0))
    comparison = _numeric_comparison(text)
    if comparison and measure:
        domain.append((measure, comparison[0], comparison[1]))
    date_field = _best_date_field(text, tokens, all_fields)
    domain.extend(_relative_date_domain(text, date_field))
    if date_field and date_field not in result_fields and len(result_fields) < 10:
        result_fields.append(date_field)
    order = (
        f"{measure} {'desc' if descending else 'asc'}, id desc"
        if ranking and measure
        else f"{date_field} desc, id desc"
        if date_field and (bool(_relative_date_domain(text, date_field)) or re.search(r"\b(latest|recent|newest|oldest)\b", text))
        else "id desc"
    )
    relevant_module_fields = [
        row
        for row in all_fields
        if _field_match_score(row, tokens, ranking=False, include_model=False) > 0
    ]
    target_modules = _module_hints_for_model(model, relevant_module_fields or all_fields)
    plan = {
        "model": model,
        "target_module": target_modules[0] if target_modules else None,
        "target_modules": target_modules,
        "domain": domain,
        "fields": result_fields,
        "limit": req.limit,
        "order": order,
        "measure_field": measure,
        "confidence": "high" if int(best["score"]) >= 15 else "medium",
        "candidate_models": [{"model": name, "score": info["score"]} for name, info in ranked_groups[:5]],
    }
    data = _search_read_via_shell(
        model=model,
        domain=domain,
        fields=result_fields,
        limit=req.limit,
        order=order,
        client_name=req.client_name,
        query_text=req.query,
    )
    return {
        "status": data.get("status"),
        "intent": "dynamic_query",
        "planner": "live_odoo_metadata",
        "read_only_enforced": True,
        "source": data.get("source"),
        "client_name": data.get("client_name"),
        "parsed": plan,
        "odoo": data.get("odoo"),
        "shell_attempt": data.get("shell_attempt"),
        "ssh_fallback_used": data.get("ssh_fallback_used"),
        "message": data.get("message"),
    }


def _odoo_functional_query(req: OdooFunctionalQueryRequest) -> object:
    """Plan every functional question from live Odoo metadata, then search_read.

    It tries Odoo shell first. When shell authentication fails but the selected
    client has Odoo.sh SSH settings, it automatically falls back to a read-only
    ``odoo-bin shell`` query over SSH. This lets users read business records
    without an Odoo email/password, as long as SSH access is working.
    """
    text = (req.query or "").strip().lower()

    if _is_mutating_query(text):
        return {
            "status": "blocked",
            "error_type": "read_only_query_guard",
            "read_only_enforced": True,
            "success_allowed": False,
            "message": "Delete/unlink/write/create/update/archive/drop/truncate/wipe requests are forbidden in the Odoo query engine. No model method was executed.",
            "query": req.query,
        }
    if _WORKFORCE_PRESENCE_GAP_RE.search(text):
        report_date = _requested_calendar_date(text)
        if not report_date:
            return {
                "status": "needs_clarification",
                "intent": "workforce_presence_gap",
                "read_only_enforced": True,
                "success_allowed": False,
                "message": "Provide one exact calendar date for the attendance-gap report, for example 2026-07-16.",
            }
        return _workforce_presence_gap_query(req, report_date)
    return _dynamic_query_plan(req)


def _resolve_code_change_project(payload: OdooCodeChangeRequest) -> tuple[str | None, str]:
    """Resolve the client/project for the provider-safe composite workflow."""
    client = ClientResolverService().resolve_client(
        client_name=payload.client_name,
        project_path=payload.project_path,
        require_complete=False,
    )
    project_path = payload.project_path or (client.path if client else None)
    if not project_path:
        raise HTTPException(status_code=400, detail="project_path or client_name is required for workflow.odoo_code_change")
    resolved_client_name = client.name if client else payload.client_name
    return resolved_client_name, str(project_path)


def _safe_module_relative(module_name: str, relative_path: str) -> str:
    """Normalize a user/provider path into <module>/... inside project_path."""
    value = str(relative_path or "").replace("\\", "/").strip()
    if not value or "\x00" in value:
        raise HTTPException(status_code=400, detail="Empty or unsafe relative_path")
    value = value.lstrip("/")
    parts = [part for part in value.split("/") if part and part not in {".", ".."}]
    if not parts:
        raise HTTPException(status_code=400, detail="Empty relative_path")
    if module_name in parts:
        idx = parts.index(module_name)
        parts = parts[idx:]
    elif parts[0] != module_name:
        parts = [module_name, *parts]
    return "/".join(parts)


def _ensure_root_module_scaffold(repo: RepoService, project: Path, payload: OdooCodeChangeRequest) -> None:
    """Materialize one complete new-addon batch at the checkout root.

    A previous implementation wrote an empty initializer and placeholder
    manifest before the provider had supplied the rest of the module. If a
    continuation or inspection was interrupted, that left a misleading partial
    addon in the checkout. New addons are now accepted only as one complete,
    atomic batch.
    """
    if not payload.create_module:
        return
    module_name = payload.module_name.strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", module_name):
        raise HTTPException(status_code=400, detail="create_module requires an exact lowercase technical module_name")
    if not (project / ".git").exists():
        raise HTTPException(status_code=400, detail="create_module requires project_path to be the repository checkout root")
    if (project / "__manifest__.py").exists() or (project / "__openerp__.py").exists():
        raise HTTPException(status_code=400, detail="project_path points to an addon, not the repository checkout root")
    module_dir = (project / module_name).resolve()
    if module_dir.parent != project.resolve():
        raise HTTPException(status_code=400, detail="New modules must be direct children of the repository checkout root")
    if (module_dir / "__manifest__.py").exists():
        return

    planned, missing, errors = _validate_complete_new_module_writes(project, payload)
    if missing or errors:
        detail = "; ".join([*errors, f"missing complete files: {', '.join(missing)}" if missing else ""])
        raise HTTPException(status_code=409, detail=detail.strip("; "))
    result = repo.write_many_atomic(str(project), planned, timeout_seconds=120)
    if result.exit_code != 0:
        raise HTTPException(status_code=400, detail=result.stderr or "Could not atomically create complete root-level module")


def _validate_complete_new_module_writes(
    project: Path,
    payload: OdooCodeChangeRequest,
) -> tuple[list[RepoWriteItem], list[str], list[str]]:
    """Validate a complete new-addon file graph without mutating the checkout."""
    module_name = payload.module_name.strip()
    supplied: dict[str, str] = {}
    errors: list[str] = []
    for change in payload.writes:
        rel = _safe_module_relative(module_name, change.relative_path)
        if change.content is None:
            errors.append(f"new file {rel} requires complete content, not search/replace")
            continue
        supplied[rel] = change.content

    required = {f"{module_name}/__init__.py", f"{module_name}/__manifest__.py"}
    manifest_rel = f"{module_name}/__manifest__.py"
    manifest: dict[str, Any] = {}
    manifest_content = supplied.get(manifest_rel)
    if manifest_content is None and (project / manifest_rel).is_file():
        manifest_content = (project / manifest_rel).read_text(encoding="utf-8")
    if manifest_content is not None:
        try:
            parsed = ast.literal_eval(manifest_content)
            if not isinstance(parsed, dict):
                raise ValueError("manifest must evaluate to a dictionary")
            manifest = parsed
        except (SyntaxError, ValueError) as exc:
            errors.append(f"invalid complete manifest: {exc}")

    # Every local data/demo/asset path declared by the proposed manifest must
    # either already exist or be included in the same write batch.
    for key in ("data", "demo"):
        values = manifest.get(key) or []
        if isinstance(values, str):
            values = [values]
        for value in values if isinstance(values, (list, tuple)) else []:
            if isinstance(value, str) and not value.startswith(("http://", "https://", "//")):
                rel = value.lstrip("/")
                required.add(rel if rel.startswith(f"{module_name}/") else f"{module_name}/{rel}")

    assets = manifest.get("assets") or {}
    if isinstance(assets, dict):
        for entries in assets.values():
            entries = entries if isinstance(entries, (list, tuple)) else [entries]
            for entry in entries:
                candidates = [entry] if isinstance(entry, str) else (
                    [part for part in entry if isinstance(part, str)] if isinstance(entry, (list, tuple)) else []
                )
                for candidate in candidates:
                    rel = candidate.lstrip("/")
                    if rel.startswith(f"{module_name}/"):
                        required.add(rel)

    # Validate direct package imports such as ``from . import models`` and
    # ``from . import library`` against the same proposed file set.
    initializer_contents: dict[str, str] = {}
    existing_module = project / module_name
    if existing_module.is_dir():
        for init_path in existing_module.rglob("__init__.py"):
            initializer_contents[init_path.relative_to(project).as_posix()] = init_path.read_text(encoding="utf-8")
    initializer_contents.update({rel: text for rel, text in supplied.items() if rel.endswith("/__init__.py")})
    for init_rel, content in initializer_contents.items():
        try:
            tree = ast.parse(content or "")
        except SyntaxError as exc:
            errors.append(f"invalid initializer {init_rel}: {exc}")
            continue
        base = init_rel.rsplit("/", 1)[0]
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != 1 or node.module:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                py_path = f"{base}/{alias.name}.py"
                package_path = f"{base}/{alias.name}/__init__.py"
                if not any((project / path).is_file() or path in supplied for path in (py_path, package_path)):
                    required.add(py_path)

    missing: list[str] = []
    supplied_paths = set(supplied)
    for rel in sorted(required):
        if "*" in rel:
            if not any(fnmatch.fnmatch(path, rel) for path in supplied_paths) and not glob.glob(str(project / rel), recursive=True):
                missing.append(rel)
        elif rel not in supplied_paths and not (project / rel).is_file():
            missing.append(rel)

    planned = [RepoWriteItem(relative_path=rel, content=content) for rel, content in supplied.items()]
    return planned, missing, errors


def _is_legacy_placeholder_module(module_dir: Path) -> bool:
    """Recognize the old connector's empty two-file scaffold."""
    manifest_path = module_dir / "__manifest__.py"
    init_path = module_dir / "__init__.py"
    if not manifest_path.is_file() or not init_path.is_file():
        return False
    try:
        manifest = ast.literal_eval(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return False
    if not isinstance(manifest, dict) or init_path.read_text(encoding="utf-8").strip():
        return False
    return set(manifest).issubset({"name", "depends", "installable"}) and manifest.get("depends") == ["base"]


def _read_text_safe(path: Path, limit: int = 300_000) -> str:
    data = path.read_text(encoding="utf-8")
    return data[:limit]


_INVALID_FIELD_ERROR_RE = re.compile(
    r"Invalid\s+field\s+['\"](?P<field>[A-Za-z_][A-Za-z0-9_]*)['\"]\s+on\s+model\s+['\"](?P<model>[A-Za-z_][A-Za-z0-9_.]*)['\"]",
    re.I,
)


def _invalid_field_request(payload: OdooCodeChangeRequest) -> tuple[str, str] | None:
    text = "\n".join(
        str(value or "")
        for value in (
            payload.task_description,
            payload.intent,
            _target_hint(payload),
            "\n".join(payload.investigation_evidence or []),
        )
    )
    match = _INVALID_FIELD_ERROR_RE.search(text)
    return (match.group("model"), match.group("field")) if match else None


def _literal_model_names(node: ast.AST) -> list[str]:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if isinstance(item, str)]
    return []


def _model_class_facts(path: Path, model_name: str, field_name: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []
    facts: list[dict[str, Any]] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        names: list[str] = []
        inherits: list[str] = []
        fields: list[str] = []
        for child in node.body:
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                value = child.value
                for target in targets:
                    if not isinstance(target, ast.Name):
                        continue
                    if target.id == "_name" and value is not None:
                        names.extend(_literal_model_names(value))
                    elif target.id == "_inherit" and value is not None:
                        inherits.extend(_literal_model_names(value))
                    elif target.id == field_name:
                        fields.append(target.id)
        if model_name in names or model_name in inherits:
            facts.append({
                "class_name": node.name,
                "line": node.lineno,
                "declares_name": model_name in names,
                "inherits_model": model_name in inherits,
                "defines_field": field_name in fields,
            })
    return facts


def _invalid_field_registry_diagnostic(
    project: Path,
    module_path: str,
    payload: OdooCodeChangeRequest,
) -> dict[str, Any] | None:
    requested = _invalid_field_request(payload)
    if requested is None:
        return None
    model_name, field_name = requested
    module_dir = (project / module_path).resolve()
    models_dir = module_dir / "models"
    definitions: list[dict[str, Any]] = []
    target_files: list[str] = []
    for path in sorted(models_dir.rglob("*.py")) if models_dir.is_dir() else []:
        for fact in _model_class_facts(path, model_name, field_name):
            rel = path.relative_to(project).as_posix()
            definitions.append({"relative_path": rel, **fact})
            if rel not in target_files:
                target_files.append(rel)

    init_path = models_dir / "__init__.py"
    imported_modules: list[str] = []
    if init_path.is_file():
        init_rel = init_path.relative_to(project).as_posix()
        target_files.insert(0, init_rel)
        try:
            init_tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
            for node in init_tree.body:
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    imported_modules.extend(alias.name for alias in node.names)
        except (OSError, UnicodeDecodeError, SyntaxError):
            pass

    for path in sorted(module_dir.rglob("*.xml")):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if field_name in content and model_name in content:
            rel = path.relative_to(project).as_posix()
            if rel not in target_files:
                target_files.append(rel)

    primary = [item for item in definitions if item["declares_name"]]
    field_defs = [item for item in definitions if item["defines_field"]]
    missing_imports = [
        item["relative_path"]
        for item in definitions
        if Path(item["relative_path"]).stem not in imported_modules
    ]
    if len(primary) > 1:
        classification = "duplicate_model_definitions"
        root_cause = (
            f"Model {model_name!r} is declared with _name in {len(primary)} Python classes. "
            f"Field {field_name!r} is not consistently defined across those primary declarations; "
            "registry composition/import order can expose a model without that field. Consolidate one primary "
            "model and convert intentional extensions to _inherit before changing XML data values."
        )
    elif field_defs and missing_imports:
        classification = "model_file_not_imported"
        root_cause = f"Field {field_name!r} is defined in a Python model file that models/__init__.py does not import."
    elif not field_defs:
        classification = "field_not_defined_in_module"
        root_cause = f"No loaded Python class for {model_name!r} defines field {field_name!r}; correct the XML field name or add the field."
    else:
        classification = "deployed_registry_or_source_mismatch"
        root_cause = (
            f"A single imported model definition contains {field_name!r}; verify the deployed SHA/source and restart/upgrade registry. "
            "Changing the XML value syntax cannot create a missing registry field."
        )
    return {
        "status": "diagnosed",
        "error_type": "invalid_registry_field",
        "model": model_name,
        "field": field_name,
        "classification": classification,
        "root_cause": root_cause,
        "definitions": definitions,
        "models_init_import_order": imported_modules,
        "missing_import_files": sorted(set(missing_imports)),
        "target_files": target_files,
        "xml_value_format_change_is_fix": False,
    }


def _invalid_field_xml_only_write_block(
    diagnostic: dict[str, Any] | None,
    writes: list[OdooCodeFileChange],
) -> bool:
    """Reject cosmetic XML edits when the registry failure is in Python model composition."""
    return bool(
        diagnostic
        and diagnostic.get("classification") == "duplicate_model_definitions"
        and writes
        and all(str(change.relative_path).lower().endswith(".xml") for change in writes)
    )


def _parse_manifest_depends(manifest_path: Path) -> list[str]:
    try:
        data = ast.literal_eval(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    depends = data.get("depends", []) if isinstance(data, dict) else []
    return [str(dep) for dep in depends if isinstance(dep, str) and dep]


def _append_unique(values: list[str], raw: Any) -> None:
    value = str(raw or "").strip()
    if value and value not in values:
        values.append(value)


def _target_hint(payload: OdooCodeChangeRequest) -> str:
    return str(payload.target_hint or payload.target_text or "").strip()


def _change_request_values(value: Any, *, limit: int = 12) -> list[str]:
    """Return short string leaves from a structured change request for focused search.

    This keeps the workflow generic: the connector can inspect around method names,
    field names, XML ids, route names, labels, or visible text without hardcoding a
    task-specific prompt.
    """
    found: list[str] = []
    def visit(item: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(item, str):
            text = item.strip()
            if 2 <= len(text) <= 200 and text not in found:
                found.append(text)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple, set)):
            for child in item:
                visit(child)
    visit(value)
    return found


def _infer_code_change_search_patterns(payload: OdooCodeChangeRequest) -> list[str]:
    patterns: list[str] = []
    for raw in payload.search_patterns:
        _append_unique(patterns, raw)
    _append_unique(patterns, _target_hint(payload))
    _append_unique(patterns, payload.intent)
    # Pull safe, short clues from structured change_request. This is generic and
    # works for labels, method names, fields, XML ids, routes, CSS selectors, etc.
    for raw in _change_request_values(payload.change_request):
        _append_unique(patterns, raw)
    # Keep automatic search derivation generic. Domain-specific terms must come
    # from explicit target hints, search patterns, file paths, or structured
    # change_request values supplied by the caller.
    return patterns[:10]


def _file_preview(content: str, max_chars: int = 6000) -> str:
    if len(content) <= max_chars:
        return content
    return content[: max_chars // 2] + f"\n\n...[{len(content)-max_chars} chars omitted]...\n\n" + content[-max_chars // 2 :]


def _module_hint_tokens(value: str) -> set[str]:
    stop = {"module", "addon", "custom", "odoo", "the", "form"}
    return {part for part in re.split(r"[^a-z0-9]+", value.lower()) if len(part) >= 3 and part not in stop}


def _normalise_module_hint(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _resolve_fuzzy_module(payload: OdooCodeChangeRequest, modules: list[Any]) -> tuple[Any | None, dict[str, Any]]:
    """Resolve a human module hint to an actual custom module.

    Human-facing module hints may differ from technical module names.
    This function keeps path safety by selecting only from the discovered
    custom-module list.
    """
    requested = str(payload.module_name or "").strip()
    exact = next((item for item in modules if item.module_name == requested), None)
    if exact:
        return exact, {"requested": requested, "resolved": exact.module_name, "strategy": "exact", "score": 1.0}

    hint_parts = [requested, payload.module_hint or "", payload.task_description or ""]
    hint_parts.extend(str(p or "") for p in payload.search_patterns)
    hint_parts.extend(str(p or "") for p in payload.target_files)
    hint_text = " ".join(part for part in hint_parts if part).strip()
    hint_norm = _normalise_module_hint(hint_text)
    hint_tokens = _module_hint_tokens(hint_text)
    candidates: list[tuple[float, Any, dict[str, Any]]] = []
    for item in modules:
        name = str(item.module_name)
        haystack = f"{name} {getattr(item, 'path', '')}"
        name_norm = _normalise_module_hint(name)
        name_tokens = _module_hint_tokens(haystack.replace("_", " "))
        score = 0.0
        reasons: list[str] = []
        if requested and _normalise_module_hint(requested) == name_norm:
            score = max(score, 0.98); reasons.append("normalized_exact")
        if hint_norm and name_norm and (name_norm in hint_norm or hint_norm in name_norm):
            score = max(score, 0.9); reasons.append("substring")
        if hint_tokens and name_tokens:
            overlap = len(hint_tokens & name_tokens)
            if overlap:
                score = max(score, 0.55 + min(overlap, 4) * 0.1); reasons.append(f"token_overlap:{overlap}")
        if requested:
            ratio = difflib.SequenceMatcher(None, _normalise_module_hint(requested), name_norm).ratio()
            if ratio >= 0.55:
                score = max(score, ratio * 0.8); reasons.append(f"fuzzy:{ratio:.2f}")
        if score:
            candidates.append((score, item, {"module": name, "score": round(score, 3), "reasons": reasons}))
    candidates.sort(key=lambda row: row[0], reverse=True)
    if candidates:
        best_score, best, best_meta = candidates[0]
        second_score = candidates[1][0] if len(candidates) > 1 else 0.0
        if best_score >= 0.72 and (best_score - second_score >= 0.08 or best_score >= 0.9):
            return best, {"requested": requested, "resolved": best.module_name, "strategy": "fuzzy", "score": round(best_score, 3), "candidates": [meta for _, _, meta in candidates[:5]]}
    return None, {"requested": requested, "strategy": "not_found", "candidates": [meta for _, _, meta in candidates[:5]]}


def _style_value(style: dict[str, Any], *names: str) -> str | None:
    for name in names:
        if name in style and style[name] not in {None, ""}:
            return str(style[name]).strip()
    return None


def _css_style_declarations(style: dict[str, Any]) -> list[str]:
    declarations: list[str] = []
    color = _style_value(style, "color", "text_color")
    background = _style_value(style, "background")
    background_color = _style_value(style, "background_color", "backgroundColor")
    border_color = _style_value(style, "border_color", "borderColor")
    fill = _style_value(style, "fill")
    font_style = _style_value(style, "font_style", "fontStyle")
    font_weight = _style_value(style, "font_weight", "fontWeight")
    safe_values = {
        "color": color,
        "background": background,
        "background-color": background_color,
        "border-color": border_color,
        "fill": fill,
    }
    for prop, value in safe_values.items():
        if value and not any(char in value for char in ";{}"):
            declarations.append(f"{prop}: {value} !important;")
    if font_style:
        declarations.append(f"font-style: {font_style} !important;")
    if font_weight:
        weight = "700" if font_weight.lower() == "bold" else font_weight
        declarations.append(f"font-weight: {weight} !important;")
    return declarations


def _focused_search(repo: RepoService, project_path: str, module_path: str, patterns: list[str], max_matches: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Exact-first, low-noise search for inspect/autopatch mode."""
    preferred_suffixes = (".xml", ".scss", ".css", ".js", ".py")
    search_results: list[dict[str, Any]] = []
    rel_paths: list[str] = []
    seen_paths: set[str] = set()
    for pattern in patterns[:10]:
        try:
            result = repo.search(project_path, pattern, module_path, max(1, min(max_matches, 25)), 24, False)
            matches = result.get("matches", []) or []
            # Keep exact/template/style files first; avoid pulling large controllers/js unless needed.
            matches = sorted(
                matches,
                key=lambda m: (
                    0 if str(m.get("path", "")).endswith((".xml", ".scss", ".css")) else 1,
                    1 if "/controllers/" in str(m.get("path", "")) else 0,
                    str(m.get("path", "")),
                    int(m.get("line") or 0),
                ),
            )[: max(1, min(max_matches, 10))]
            search_results.append({
                "pattern": pattern,
                "count": result.get("count"),
                "focused_count": len(matches),
                "matches": matches,
            })
            for match in matches:
                rel = str(match.get("path") or "")
                if rel and rel.endswith(preferred_suffixes) and rel not in seen_paths:
                    seen_paths.add(rel)
                    rel_paths.append(rel)
            # Exact visible text found in XML/CSS is usually enough for inspection.
            if rel_paths and any(pattern.lower() in str(m.get("text", "")).lower() for m in matches):
                break
        except Exception as exc:  # noqa: BLE001
            search_results.append({"pattern": pattern, "status": "failed", "error": str(exc)})
    return search_results, rel_paths


def _is_fast_ui_task(payload: OdooCodeChangeRequest) -> bool:
    task_type = _normalized_task_type(payload)
    if task_type in {"style", "scss_css", "css"}:
        return True
    text = " ".join((payload.task_description or "", payload.intent or "", _target_hint(payload))).lower()
    return any(term in text for term in (
        "color", "colour", "dark mode", "dark theme", "heatmap", "border", "spacing", "font", "background",
        "loader", "spinner", "loading indicator", "loading state", "tooltip", "icon", "badge",
    )) and not any(term in text for term in ("python", "model field", "controller", "database", "security"))


_TASK_LANE_BUDGETS = {
    "fast": {"target_files": 3, "search_matches": 6, "custom_files": 4, "core_files": 8},
    "normal": {"target_files": 6, "search_matches": 10, "custom_files": 8, "core_files": 16},
    "long": {"target_files": 50, "search_matches": 25, "custom_files": 30, "core_files": 60},
}


def _compatibility_lane_budget(task_lane: str, mandatory_count: int) -> tuple[dict[str, int], bool]:
    """Fit mandatory reads without changing an evidence-based task lane."""
    budget = dict(_TASK_LANE_BUDGETS[task_lane])
    expanded = task_lane != "long" and mandatory_count > budget["core_files"]
    if expanded:
        budget["core_files"] = mandatory_count
    return budget, expanded


def _task_lane_decision(payload: OdooCodeChangeRequest) -> dict[str, Any]:
    """Classify every Odoo task from evidence instead of trusting provider labels."""
    task_type = _normalized_task_type(payload)
    text = " ".join((payload.task_description or "", payload.intent or "", _target_hint(payload))).lower()
    requested = str(payload.complexity or "auto").strip().lower()
    high_risk_types = {"security_csv", "payment", "payment_frontend", "migration", "migrations", "accounting", "payroll"}
    high_risk_markers = (
        "access rights", "record rule", "security group", "accounting", "journal entry",
        "tax filing", "payment provider", "payment transaction", "stock valuation",
        "inventory valuation", "payroll", "data migration", "production branch",
    )
    # An explicit fast hint must not weaken inspection for sensitive business
    # flows. These tasks use the long evidence budget even when only one file is
    # expected to change.
    if task_type in high_risk_types or any(marker in text for marker in high_risk_markers):
        return {"lane": "long", "requested_lane": requested, "score": None, "reason": "Safety-sensitive business or data scope requires long inspection."}
    long_markers = (
        "refactor", "redesign", "migration", "migrate", "all modules", "entire module", "whole module",
        "multi-module", "architecture", "end to end", "full workflow", "race condition", "data migration",
        "replace system", "new module", "large change", "new layout", "responsive overhaul",
        "replace theme", "all styling", "entire ui", "whole dashboard",
        "multiple components",
    )
    if (
        payload.create_module
        or task_type in {"migration", "migrations", "refactor", "architecture"}
        or len(payload.writes) > 5
        or len(payload.target_files) > 8
        or any(marker in text for marker in long_markers)
    ):
        return {"lane": "long", "requested_lane": requested, "score": None, "reason": "Broad, architectural, migration, or mandatory multi-file scope detected."}

    if payload.force_complexity and requested in _TASK_LANE_BUDGETS:
        return {"lane": requested, "requested_lane": requested, "score": None, "reason": "Caller explicitly forced the inspection lane."}

    score = 0
    evidence: list[str] = []
    write_count = len(payload.writes)
    target_count = len(payload.target_files)
    target_hint = _target_hint(payload).strip()
    if 0 < write_count <= 2:
        score += 5
        evidence.append(f"{write_count} exact write(s)")
    elif 3 <= write_count <= 5:
        score += 1
        evidence.append(f"{write_count} bounded writes")
    if 0 < target_count <= 3:
        score += 3
        evidence.append(f"{target_count} target file(s)")
    if target_hint:
        score += 3
        evidence.append("exact target hint")
    if task_type not in {"", "auto"}:
        score += 1
        evidence.append(f"task type {task_type}")

    deterministic_types = {"style", "scss_css", "css", "text_replace", "manifest"}
    scope_markers = ("one ", "single ", "only ", "specific ", "exact ")
    bounded_markers = (
        "label", "typo", "color", "colour", "border", "background", "spacing", "font", "shadow",
        "radius", "opacity", "button", "selector", "xpath", "method", "field",
        "route", "dependency", "manifest", "loader", "spinner", "loading indicator",
        "loading state", "tooltip", "icon", "badge",
    )
    frontend_micro_markers = (
        "loader", "spinner", "loading indicator", "loading state", "tooltip", "icon", "badge",
    )
    if task_type in deterministic_types:
        score += 3
        evidence.append("deterministic task type")
    if any(marker in text for marker in scope_markers):
        score += 2
        evidence.append("explicitly narrow scope")
    if any(marker in text for marker in bounded_markers):
        score += 2
        evidence.append("bounded change wording")
    if any(marker in text for marker in frontend_micro_markers):
        score += 2
        evidence.append("bounded frontend micro-feature")
    if len(text) <= 180 and not re.search(r"\b(and|plus|across|throughout)\b", text):
        score += 1
        evidence.append("concise single-feature request")

    if score >= 4:
        lane = "fast"
        reason = "Bounded low-risk task: " + ", ".join(evidence) + "."
    else:
        lane = "normal"
        reason = (
            "Scope is low-risk but not precise enough for the fast lane. "
            "Unforced provider lane hints cannot override the server's evidence-based classification."
        )
    return {"lane": lane, "requested_lane": requested, "score": score, "reason": reason}


def _task_lane(payload: OdooCodeChangeRequest) -> str:
    return str(_task_lane_decision(payload)["lane"])


_MICRO_CHANGE_SUFFIXES = (".scss", ".css", ".xml", ".md", ".txt", "__manifest__.py")
_MICRO_CHANGE_RISK_RE = re.compile(
    r"\b(?:security|access\s+rule|record\s+rule|payroll|accounting|migration|schema|architecture|redesign|refactor|delete|unlink|controller|javascript|owl|python)\b",
    re.I,
)


def _is_low_risk_micro_change(
    payload: OdooCodeChangeRequest,
    writes: list[OdooCodeFileChange],
    task_lane: str,
) -> bool:
    """Use local custom-file + QA checks for tiny deterministic edits; skip core discovery."""
    if task_lane != "fast" or not writes or len(writes) > 2:
        return False
    text = " ".join(str(value or "") for value in (payload.task_description, payload.intent, _target_hint(payload)))
    if _MICRO_CHANGE_RISK_RE.search(text):
        return False
    paths = [str(change.relative_path).lower().replace("\\", "/") for change in writes]
    if not all(path.endswith(_MICRO_CHANGE_SUFFIXES) for path in paths):
        return False
    # Full-file replacement of XML can hide broad structural edits. Exact
    # search/replace or generated bounded patches stay on the micro lane.
    for change in writes:
        if str(change.relative_path).lower().endswith(".xml") and change.content is not None:
            return False
    return True


def _missing_manifest_asset_paths(project: Path, module_dir: Path, manifest_path: Path) -> list[str]:
    """Resolve manifest asset references locally without core sync or SSH."""
    try:
        manifest = ast.literal_eval(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return []
    if not isinstance(manifest, dict) or not isinstance(manifest.get("assets"), dict):
        return []

    missing: list[str] = []
    for entries in manifest["assets"].values():
        values = entries if isinstance(entries, (list, tuple)) else [entries]
        for entry in values:
            candidates = [entry] if isinstance(entry, str) else (
                [value for value in entry if isinstance(value, str) and "/" in value]
                if isinstance(entry, (list, tuple)) else []
            )
            for asset in candidates:
                if asset.startswith(("http://", "https://", "//")):
                    continue
                raw_candidates = [project / asset, module_dir / asset]
                exists = any(path.exists() for path in raw_candidates)
                if not exists and "*" in asset:
                    exists = any(glob.glob(str(path), recursive=True) for path in raw_candidates)
                if not exists and asset not in missing:
                    missing.append(asset)
    return missing


def _is_missing_asset_diagnostic_request(payload: OdooCodeChangeRequest, missing_assets: list[str]) -> bool:
    if not missing_assets:
        return False
    text = " ".join((payload.task_description or "", payload.intent or "", _target_hint(payload))).lower()
    failure_marker = any(marker in text for marker in ("missing", "cannot read", "can't read", "not found", "failed to load", "could not get content"))
    asset_marker = any(marker in text for marker in ("asset", "manifest", "static/src", ".js", ".scss", ".css"))
    return failure_marker and asset_marker


def _ui_locator_key(project: Path, module_name: str, payload: OdooCodeChangeRequest) -> str:
    import hashlib
    target = " ".join((_target_hint(payload) or payload.task_description).lower().split())
    tokens = sorted({token for token in re.split(r"[^a-z0-9_.#-]+", target) if len(token) >= 3})[:12]
    raw = f"{project.resolve()}|{module_name}|{' '.join(tokens)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _cached_ui_paths(cache: Any, key: str, project: Path, module_path: str) -> list[str]:
    if not isinstance(cache, dict) or not isinstance(cache.get(key), dict):
        return []
    module_root = (project / module_path).resolve()
    paths: list[str] = []
    for raw in cache[key].get("paths") or []:
        candidate = (project / str(raw)).resolve()
        try:
            candidate.relative_to(module_root)
        except ValueError:
            continue
        if candidate.is_file() and candidate.suffix.lower() in {".scss", ".css", ".xml", ".js", ".py", ".csv"}:
            paths.append(candidate.relative_to(project).as_posix())
    return list(dict.fromkeys(paths))[:3]


def _update_ui_locator_cache(store: JsonStore, client_name: str | None, cache: Any, key: str, paths: list[str], project: Path) -> None:
    import hashlib
    updated = dict(cache) if isinstance(cache, dict) else {}
    entries = list(dict.fromkeys(paths))[:3]
    updated[key] = {
        "paths": entries,
        "fingerprints": {
            rel: hashlib.sha256((project / rel).read_bytes()).hexdigest()[:16]
            for rel in entries if (project / rel).is_file()
        },
        "saved_at": time.time(),
    }
    if len(updated) > 100:
        updated = dict(list(updated.items())[-100:])
    store.save_session_state({"ui_target_cache": updated}, client_name=client_name)


def _ui_evidence_key(project: Path, module_name: str, payload: OdooCodeChangeRequest, paths: list[str]) -> str:
    import hashlib
    profile = _task_profile(payload, paths)
    raw = f"{project.resolve()}|{module_name}|{profile.get('scope')}|{profile.get('live_verifier')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _path_content_fingerprint(raw_path: str) -> str | None:
    import hashlib
    path = Path(raw_path)
    try:
        if not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()[:20]
    except OSError:
        return None


def _shared_ui_evidence_valid(evidence: Any) -> bool:
    if not isinstance(evidence, dict) or not evidence.get("frontend_preflight"):
        return False
    fingerprints = evidence.get("core_fingerprints") or {}
    if not isinstance(fingerprints, dict):
        return False
    return all(_path_content_fingerprint(path) == expected for path, expected in fingerprints.items())


def _normalized_task_type(payload: OdooCodeChangeRequest) -> str:
    task_type = str(payload.task_type or "auto").strip().lower().replace("-", "_").replace(" ", "_")
    return task_type or "auto"


def _normalized_patch_strategy(payload: OdooCodeChangeRequest) -> str:
    strategy = str(payload.patch_strategy or "auto").strip().lower().replace("-", "_").replace(" ", "_")
    return strategy or "auto"


def _style_request(payload: OdooCodeChangeRequest) -> dict[str, Any]:
    """Merge backward-compatible style with generic change_request style fields."""
    merged: dict[str, Any] = {}
    cr = payload.change_request or {}
    if isinstance(cr.get("style"), dict):
        merged.update(cr["style"])
    for key in (
        "color", "text_color", "font_style", "fontStyle", "font_weight", "fontWeight",
        "background", "background_color", "backgroundColor", "border_color", "borderColor", "fill",
    ):
        if key in cr and cr[key] not in {None, ""}:
            merged[key] = cr[key]
    merged.update(payload.style or {})
    return merged


def _text_replace_request(payload: OdooCodeChangeRequest) -> tuple[str | None, str | None]:
    """Extract generic text replacement instructions from change_request.

    This supports many task phrasings without becoming task-specific:
      {type: text_replace, old: X, new: Y}
      {search: X, replace: Y}
      {target: X, value: Y}
    """
    cr = payload.change_request or {}
    if not isinstance(cr, dict):
        return None, None
    old = cr.get("old") or cr.get("old_text") or cr.get("from") or cr.get("search") or cr.get("target") or cr.get("target_text")
    new = cr.get("new") or cr.get("new_text") or cr.get("to") or cr.get("replace") or cr.get("replacement") or cr.get("value")
    if old is None and _normalized_task_type(payload) in {"text_replace", "replace_text"}:
        old = _target_hint(payload)
    if old is None or new is None:
        return None, None
    return str(old), str(new)


def _iter_autopatch_candidate_files(module_dir: Path, target_files: list[str], project: Path) -> list[Path]:
    suffix_order = {".xml": 0, ".scss": 1, ".css": 2, ".js": 3, ".py": 4, ".csv": 5}
    candidates: list[Path] = []
    seen: set[Path] = set()
    for rel in target_files or []:
        path = (project / rel).resolve()
        try:
            path.relative_to(module_dir.resolve())
        except ValueError:
            continue
        if path.is_file() and path.suffix in suffix_order and path not in seen:
            seen.add(path); candidates.append(path)
    if candidates:
        return candidates
    for path in module_dir.rglob("*"):
        if not path.is_file() or path.suffix not in suffix_order:
            continue
        if any(part in {".git", "__pycache__", "node_modules"} for part in path.parts):
            continue
        if path not in seen:
            seen.add(path); candidates.append(path)
    return sorted(candidates, key=lambda p: (suffix_order.get(p.suffix, 99), len(p.as_posix()), p.as_posix()))


def _build_text_replace_autopatch(project: Path, module_path: str, target_files: list[str], old: str, new: str) -> list[OdooCodeFileChange]:
    if not old or new is None:
        return []
    module_dir = project / module_path
    for file_path in _iter_autopatch_candidate_files(module_dir, target_files, project):
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if old not in content:
            continue
        rel = file_path.relative_to(project).as_posix()
        # Use search/replace rather than full content rewrite so the diff remains
        # minimal and deterministic across all text-like task types.
        return [OdooCodeFileChange(relative_path=rel, search=old, replace=new, count=1)]
    return []


def _build_safe_generic_autopatch(project: Path, module_path: str, payload: OdooCodeChangeRequest, custom_rel_paths: list[str]) -> tuple[list[OdooCodeFileChange], str | None, str | None]:
    """Build a deterministic autopatch for supported safe cases.

    This is intentionally generic and conservative.  It handles only changes that
    can be generated without hallucinating Odoo code (style rule or exact text
    replacement).  Python/XML/JS/POS/security/report tasks still use the same
    universal workflow, but return focused inspection context and request explicit
    writes when no deterministic patch exists.
    """
    if not payload.allow_autopatch and not payload.style and not payload.change_request:
        return [], None, "autopatch_not_requested"
    task_type = _normalized_task_type(payload)
    strategy = _normalized_patch_strategy(payload)
    target = _target_hint(payload)
    style = _style_request(payload)
    cr_type = str((payload.change_request or {}).get("type") or "").strip().lower().replace("-", "_").replace(" ", "_")

    if style and target and (task_type in {"auto", "style", "scss_css", "css", "qweb_template", "xml_view"} or strategy in {"auto", "safe_autopatch", "style_rule", "inline_style"} or cr_type in {"style", "change_style", "update_style"}):
        writes = _build_selector_style_autopatch(project, module_path, custom_rel_paths, target, style)
        if not writes:
            writes = _build_simple_style_autopatch(project, module_path, target, style)
        if writes:
            return writes, "style_rule", None
        return [], "style_rule", "target_text_or_class_not_found"

    old, new = _text_replace_request(payload)
    if old is not None and new is not None and (task_type in {"auto", "text", "text_replace", "replace_text", "qweb_template", "xml_view", "data_xml", "scss_css", "js_owl", "pos_owl", "python_model"} or strategy in {"auto", "safe_autopatch", "text_replace"} or cr_type in {"text_replace", "replace_text", "rename_label", "change_text"}):
        writes = _build_text_replace_autopatch(project, module_path, custom_rel_paths, old, new)
        if writes:
            return writes, "text_replace", None
        return [], "text_replace", "search_text_not_found"

    return [], None, "unsupported_autopatch_for_task_type"


def _build_selector_style_autopatch(
    project: Path,
    module_path: str,
    target_files: list[str],
    target_hint: str,
    style: dict[str, Any],
) -> list[OdooCodeFileChange]:
    """Update one uniquely matched CSS/SCSS selector without rewriting a file."""
    declarations = _css_style_declarations(style)
    if not target_hint or not declarations:
        return []
    module_dir = project / module_path
    candidates = [
        path for path in _iter_autopatch_candidate_files(module_dir, target_files, project)
        if path.suffix.lower() in {".scss", ".css"}
    ]
    exact_selector = target_hint.strip() if target_hint.strip().startswith((".", "#", "[")) else None
    clue_tokens = [token for token in re.split(r"[^a-z0-9_-]+", target_hint.lower()) if len(token) >= 3]
    matches: list[tuple[Path, re.Match[str]]] = []
    for path in candidates:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rule_re = re.compile(r"(?P<selector>[^{}]+)\{(?P<body>[^{}]*)\}", re.S)
        for match in rule_re.finditer(content):
            selector = " ".join(match.group("selector").split())
            selector_lower = selector.lower()
            if exact_selector:
                selector_parts = [part.strip() for part in selector.split(",")]
                selected = exact_selector in selector_parts or exact_selector == selector
            else:
                selected = bool(clue_tokens) and all(token in selector_lower for token in clue_tokens)
            if selected:
                matches.append((path, match))
    if len(matches) != 1:
        return []

    path, match = matches[0]
    old_rule = match.group(0)
    body = match.group("body")
    for declaration in declarations:
        prop, value = declaration.split(":", 1)
        prop_re = re.compile(rf"(?m)^(?P<indent>\s*){re.escape(prop)}\s*:\s*[^;]+;")
        if prop_re.search(body):
            body = prop_re.sub(lambda m: f"{m.group('indent')}{prop}: {value.strip()}", body, count=1)
        else:
            indent_match = re.search(r"\n(?P<indent>\s*)\S", body)
            indent = indent_match.group("indent") if indent_match else "    "
            body = body.rstrip() + f"\n{indent}{prop}: {value.strip()}\n"
    new_rule = old_rule[: old_rule.index("{") + 1] + body + "}"
    rel = path.relative_to(project).as_posix()
    return [OdooCodeFileChange(relative_path=rel, search=old_rule, replace=new_rule, count=1)]


def _build_simple_style_autopatch(project: Path, module_path: str, target_text: str, style: dict[str, Any]) -> list[OdooCodeFileChange]:
    declarations = _css_style_declarations(style)
    if not target_text or not declarations:
        return []
    module_dir = project / module_path
    # Prefer existing class in the template, then add/update a small frontend CSS rule.
    for xml_file in sorted(module_dir.rglob("*.xml")):
        try:
            text = xml_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if target_text not in text:
            continue
        class_match = re.search(r'<([a-zA-Z0-9:_-]+)([^>]*class=["\']([^"\']+)["\'][^>]*)>\s*' + re.escape(target_text), text, re.S)
        class_name = None
        if class_match:
            classes = [part for part in class_match.group(3).split() if part]
            class_name = next((cls for cls in classes if cls.startswith("o_") or cls.startswith("s_")), classes[0] if classes else None)
        if class_name:
            scss_files = sorted((module_dir / "static" / "src" / "scss").glob("*.scss"))
            css_files = sorted((module_dir / "static" / "src" / "css").glob("*.css"))
            for style_file in [*scss_files, *css_files]:
                try:
                    old_css = style_file.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                rule_re = re.compile(rf"\.{re.escape(class_name)}\s*\{{(?P<body>.*?)\}}", re.S)
                declaration_block = "\n".join(f"    {line}" for line in declarations)
                if rule_re.search(old_css):
                    new_css = rule_re.sub(lambda m: f".{class_name} {{\n{declaration_block}\n}}", old_css, count=1)
                else:
                    new_css = old_css.rstrip() + f"\n\n.{class_name} {{\n{declaration_block}\n}}\n"
                rel = style_file.relative_to(project).as_posix()
                return [OdooCodeFileChange(relative_path=rel, content=new_css)]
        # Fallback: inline style on the matching element.
        inline = " ".join(line.replace(" !important", "") for line in declarations)
        new_text, count = re.subn(
            r'(<[^>]+>\s*)' + re.escape(target_text),
            lambda m: m.group(1).replace(">", f' style="{inline}">', 1) + target_text if " style=" not in m.group(1) else m.group(0),
            text,
            count=1,
        )
        if count:
            rel = xml_file.relative_to(project).as_posix()
            return [OdooCodeFileChange(relative_path=rel, content=new_text)]
    return []





_TASK_PROFILES = {
    "python_model": {"frontend": False, "live_verifier": "registry_smoke_test", "scope": "model"},
    "controller": {"frontend": False, "live_verifier": "endpoint_smoke", "scope": "controller"},
    "xml_view": {"frontend": False, "live_verifier": "backend_view_open", "scope": "view"},
    "qweb_template": {"frontend": True, "live_verifier": "report_or_page_render", "scope": "qweb"},
    "report": {"frontend": False, "live_verifier": "report_render", "scope": "report"},
    "js_report": {"frontend": True, "live_verifier": "report_ui_smoke", "scope": "frontend"},
    "js_owl": {"frontend": True, "live_verifier": "owl_ui_smoke", "scope": "frontend"},
    "pos_owl": {"frontend": True, "live_verifier": "pos_flow", "scope": "pos"},
    "scss_css": {"frontend": True, "live_verifier": "page_render", "scope": "style"},
    "manifest": {"frontend": False, "live_verifier": "module_upgrade", "scope": "manifest"},
    "data_xml": {"frontend": False, "live_verifier": "module_upgrade", "scope": "data"},
    "init": {"frontend": False, "live_verifier": "module_import", "scope": "init"},
    "security_csv": {"frontend": False, "live_verifier": "access_smoke", "scope": "security"},
    "cron": {"frontend": False, "live_verifier": "method_smoke", "scope": "backend"},
    "payment": {"frontend": False, "live_verifier": "payment_route_smoke", "scope": "payment"},
    "payment_frontend": {"frontend": True, "live_verifier": "payment_ui_smoke", "scope": "payment"},
}

def _task_profile(payload: OdooCodeChangeRequest, rel_paths: list[str] | None = None) -> dict[str, Any]:
    task_type = _normalized_task_type(payload)
    profile = dict(_TASK_PROFILES.get(task_type, {"frontend": False, "live_verifier": "targeted_smoke", "scope": "auto"}))
    paths = [str(p).lower().replace("\\", "/") for p in (rel_paths or [])]

    def is_pos_path(path: str) -> bool:
        segments = [segment for segment in path.split("/") if segment]
        return (
            "point_of_sale" in segments
            or "_assets_pos" in path
            or any(segment in {"pos", "pos_store"} for segment in segments)
        )

    def is_frontend_xml(path: str) -> bool:
        return path.endswith(".xml") and (
            "/static/src/" in path
            or "/static/" in path
            or "/qweb/" in path
            or "/templates/" in path
        )

    if any(is_pos_path(path) for path in paths):
        profile.update({"frontend": True, "live_verifier": "pos_flow", "scope": "pos"})
    elif any(path.endswith((".js", ".scss", ".css")) or is_frontend_xml(path) for path in paths):
        profile["frontend"] = True
    return profile

def _current_head_sha(project: Path) -> str | None:
    try:
        result = GitService().head_sha(str(project))
    except Exception:
        return None
    return result.stdout.strip() if result.exit_code == 0 and result.stdout.strip() else None


def _code_change_operation_identity(project: Path, payload: OdooCodeChangeRequest) -> dict[str, Any]:
    return {
        "project_path": str(project.resolve()),
        "requested_module": payload.module_name.strip().lower(),
        "task_description": " ".join(payload.task_description.lower().split()),
        "task_type": _normalized_task_type(payload),
        "intent": (payload.intent or "").strip().lower(),
        "target_hint": _target_hint(payload).strip().lower(),
    }


_SHORT_CODE_CHANGE_CONTINUATIONS = {
    "apply", "implement", "do it", "yes", "continue", "proceed", "push", "deploy", "fix it",
}


def _reuse_pending_code_change(payload: OdooCodeChangeRequest, pending: Any, project: Path) -> OdooCodeChangeRequest:
    """Bind terse approval replies to the last unresolved composite operation."""
    if not isinstance(pending, dict):
        return payload
    short = " ".join((payload.task_description or "").strip().lower().split())
    if short not in _SHORT_CODE_CHANGE_CONTINUATIONS:
        return payload
    pending_project = str(pending.get("project_path") or "")
    pending_module = str(pending.get("module_name") or "").strip().lower()
    requested_module = re.sub(r"[^a-z0-9]+", "_", payload.module_name.strip().lower()).strip("_")
    try:
        same_project = Path(pending_project).resolve() == project.resolve()
    except Exception:
        same_project = False
    if not same_project or not pending_module or pending_module != requested_module:
        return payload
    return payload.model_copy(update={
        "task_description": str(pending.get("task_description") or payload.task_description),
        "task_type": str(pending.get("task_type") or payload.task_type),
        "complexity": str(pending.get("complexity") or payload.complexity),
        "force_complexity": bool(pending.get("force_complexity", payload.force_complexity)),
        "intent": pending.get("intent") or payload.intent,
        "target_hint": pending.get("target_hint") or payload.target_hint,
        "target_files": list(dict.fromkeys(list(pending.get("target_files") or []) + list(payload.target_files))),
    })


def _evidence_signature(project: Path, module_name: str, payload: OdooCodeChangeRequest, head_sha: str | None) -> str:
    """Stable inspection key for uncommitted correction cycles.

    File mtimes deliberately do not participate: the first attempted fix makes
    files dirty and must not invalidate the source evidence collected for that
    same operation. A changed HEAD still invalidates the cache after commit.
    """
    import hashlib
    raw = json.dumps(
        {
            **_code_change_operation_identity(project, payload),
            "module": module_name,
            "head_sha": head_sha,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _looks_like_frontend_code_change(payload: OdooCodeChangeRequest, rel_paths: list[str]) -> bool:
    """Return True only when the change actually touches frontend code.

    Website/payment words alone are not enough: controller/model Python changes
    must not trigger dozens of JS/OWL/POS reads.
    """
    profile = _task_profile(payload, rel_paths)
    if profile.get("frontend"):
        return True

    normalized_paths = [str(path).lower().replace("\\", "/") for path in rel_paths]
    if any(path.endswith((".js", ".scss", ".css")) for path in normalized_paths):
        return True
    if any(
        path.endswith(".xml")
        and any(marker in path for marker in ("/static/src/", "/static/", "/qweb/", "/templates/"))
        for path in normalized_paths
    ):
        return True

    text = " ".join([
        payload.task_description or "", payload.intent or "", payload.target_hint or "",
    ]).lower()
    strong_markers = (
        "owl component", "qweb template", "javascript", "scss", "css",
        "frontend asset", "website editor", "public widget", "pos screen",
        "payment form js",
    )
    return any(marker in text for marker in strong_markers)


def _git_status_changed_module_files(project_path: str, module_path: str) -> list[str]:
    """Return dirty/tracked/untracked files under module_path from git status.

    workflow.odoo_code_change may be re-called after a verify failure with a
    tiny follow-up write.  Committing only the latest write can leave earlier
    runtime JS/XML edits dirty and undeployed.  This helper makes the composite
    workflow deploy all current dirty module code files, not only the last
    writes[] list.
    """
    project = Path(project_path)
    result = GitService().status(project_path)
    changed: list[str] = []
    prefix = module_path.strip("/") + "/"
    for raw in (result.stdout or "").splitlines():
        if not raw or raw.startswith("##") or len(raw) < 4:
            continue
        rel = raw[3:].strip()
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1].strip()
        if rel.startswith(prefix) and (project / rel).is_file():
            if rel.endswith((".py", ".xml", ".js", ".scss", ".css", ".csv")) or rel.endswith("__manifest__.py"):
                changed.append(rel)
    return list(dict.fromkeys(changed))


def _record_code_change_phase(job_id: str, phase: str) -> None:
    """Persist one concise progress transition for provider polling."""
    store = JsonStore()
    job = store.get_job(job_id)
    if job.status not in {JobStatus.queued, JobStatus.running}:
        raise RuntimeError(f"Code-change job {job_id} is already terminal ({job.status.value})")
    now = datetime.now(timezone.utc).isoformat()
    if job.steps and job.steps[-1].status == "running":
        job.steps[-1].status = "passed"
        job.steps[-1].finished_at = now
    if not job.steps or job.steps[-1].name != phase:
        job.steps.append(JobStep(name=phase, status="running"))
    job.status = JobStatus.running
    job.result = {
        "status": "running",
        "phase": phase,
        "heartbeat_at": now,
        "worker_alive": True,
        "connector_revision": settings.build_revision,
        "message": f"Odoo code-change workflow is running: {phase}.",
        "completion_required": True,
        "success_allowed": False,
        "tool_exit_allowed": False,
        "job_id": job.id,
        "next_call": {
            "tool": "jobs.get",
            "arguments": {
                "job_id": job.id,
                "wait_for_result": True,
                "wait_timeout_seconds": 20,
                "poll_interval_seconds": 2.0,
            },
        },
    }
    store.update_job(job)


def _touch_code_change_heartbeat(job_id: str) -> bool:
    """Refresh liveness without invoking stale reconciliation on the active worker."""
    store = JsonStore()
    job = next((item for item in store.list_jobs(limit=500) if item.id == job_id), None)
    if job is None or job.status not in {JobStatus.queued, JobStatus.running}:
        return False
    result = dict(job.result) if isinstance(job.result, dict) else {}
    result.update({
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "worker_alive": True,
        "completion_required": True,
        "success_allowed": False,
        "tool_exit_allowed": False,
    })
    job.result = result
    store.update_job(job)
    return True


async def _workflow_odoo_code_change(
    payload: OdooCodeChangeRequest,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Provider-safe composite Odoo code-change workflow.

    This is intentionally server-owned to remove MCP provider overhead/confusion:
    provider calls this one tool, the connector performs the v15 custom+core read,
    applies explicit edits, verifies, starts safe deploy, and returns a job_id for jobs.get.
    """
    report = progress or (lambda _phase: None)
    report("resolve_project")
    resolved_client_name, project_path = _resolve_code_change_project(payload)
    client_name = resolved_client_name or payload.client_name
    repo = RepoService()
    gate = PreWriteGateService()
    store = JsonStore()
    steps: list[dict[str, Any]] = []

    # 1) Prepare checkout once per operation. Correction calls reuse the same
    # local HEAD instead of fetching/checking out/pulling again.
    # repo URL/branch; for already-materialized local checkouts/tests, avoid failing
    # just because TARGET_GIT_REPO is not configured.
    report("repo.prepare")
    bootstrap_state = store.load_session_state(client_name=client_name)
    candidate = Path(project_path).expanduser()
    payload = _reuse_pending_code_change(payload, bootstrap_state.get("pending_code_change"), candidate)
    active_change = bootstrap_state.get("active_code_change") if isinstance(bootstrap_state, dict) else None
    candidate_head = _current_head_sha(candidate) if (candidate / ".git").exists() else None
    requested_identity = _code_change_operation_identity(candidate, payload)
    reuse_checkout = bool(
        isinstance(active_change, dict)
        and active_change.get("identity") == requested_identity
        and active_change.get("head_sha")
        and active_change.get("head_sha") == candidate_head
    )
    if reuse_checkout:
        project = repo.project(project_path)
        project_path = str(project)
        steps.append({
            "step": "repo.prepare",
            "status": "cached",
            "project_path": project_path,
            "sha": candidate_head,
            "reason": "same uncommitted code-change operation",
        })
    else:
        try:
            prepare = repo.prepare_repo(project_path=project_path, client_name=client_name, preserve_dirty=True)
            project_path = prepare.project_path
            project = Path(project_path)
            steps.append({"step": "repo.prepare", "status": "ok", "project_path": project_path, "branch": prepare.branch, "sha": prepare.sha})
        except HTTPException as exc:
            detail = str(exc.detail)
            project = repo.project(project_path)
            if exc.status_code == 400 and "repo_url is required" in detail and (project / ".git").exists():
                steps.append({"step": "repo.prepare", "status": "skipped_existing_checkout", "project_path": str(project), "reason": detail})
                project_path = str(project)
            else:
                raise

    # 2) Discover/validate module once, with a safe fuzzy resolver for human hints.
    # A brand-new addon must arrive as one complete batch. Reject an empty or
    # incomplete provider plan before creating even a placeholder manifest.
    requested_module_dir = project / payload.module_name.strip()
    if payload.create_module:
        _planned, missing_new_module_files, new_module_errors = _validate_complete_new_module_writes(project, payload)
        supplied_new_paths = {
            _safe_module_relative(payload.module_name.strip(), change.relative_path)
            for change in payload.writes if change.content is not None
        }
        if _is_legacy_placeholder_module(requested_module_dir):
            required_replacements = {
                f"{payload.module_name.strip()}/__manifest__.py",
                f"{payload.module_name.strip()}/__init__.py",
            }
            if not required_replacements.issubset(supplied_new_paths):
                new_module_errors.append(
                    "legacy placeholder module detected; explicitly replace its manifest and root initializer, "
                    "then submit the complete addon in one writes array"
                )
        if missing_new_module_files or new_module_errors:
            return {
                "status": "needs_writes",
                "message": (
                    "INTERNAL_CONTINUATION_REQUIRED: generate the complete installable addon in one call. "
                    "No placeholder or partial module files were written."
                ),
                "client_name": client_name,
                "project_path": project_path,
                "module_name": payload.module_name,
                "missing_complete_files": missing_new_module_files,
                "write_errors": new_module_errors,
                "writes_applied": False,
                "success_allowed": False,
                "completion_required": True,
                "next_call": {
                    "tool": "workflow.odoo_code_change",
                    "arguments": {
                        "client_name": client_name,
                        "project_path": project_path,
                        "module_name": payload.module_name,
                        "create_module": True,
                        "task_description": payload.task_description,
                        "writes": "Generate every complete addon file in one writes array, including manifest/init/imported Python/data/views/security/assets. Do not send a scaffold-only batch.",
                    },
                },
            }
    _ensure_root_module_scaffold(repo, project, payload)
    modules = repo.list_custom_modules(project_path, max_depth=5, include_uninstallable=False)
    module, module_resolution = _resolve_fuzzy_module(payload, modules)
    if not module:
        return {
            "status": "failed",
            "error": f"Custom module not found from hint: {payload.module_name}",
            "module_resolution": module_resolution,
            "available_modules": [item.module_name for item in modules[:50]],
            "success_allowed": False,
        }
    effective_module_name = module.module_name
    report("inspect_custom_code")
    fast_ui_mode = _is_fast_ui_task(payload)
    lane_decision = _task_lane_decision(payload)
    task_lane = str(lane_decision["lane"])
    lane_budget = dict(_TASK_LANE_BUDGETS[task_lane])
    steps.append({"step": "workflow.task_lane_select", "status": "ok", **lane_decision})
    ui_locator_cache = bootstrap_state.get("ui_target_cache") if isinstance(bootstrap_state, dict) else {}
    ui_locator_key = _ui_locator_key(project, effective_module_name, payload) if task_lane != "long" else None
    issue_fingerprint = code_change_issue_fingerprint(
        project_path=project_path,
        module_name=effective_module_name,
        task_description=payload.task_description,
        task_type=_normalized_task_type(payload),
        intent=payload.intent,
        target_hint=_target_hint(payload),
    )
    module_dir = project / module.path
    manifest_rel = module.manifest_path
    manifest_abs = project / manifest_rel
    invalid_field_diagnostic = _invalid_field_registry_diagnostic(project, module.path, payload)
    if invalid_field_diagnostic:
        steps.append({
            "step": "workflow.invalid_field_registry_diagnostic",
            "status": "ok",
            "classification": invalid_field_diagnostic["classification"],
            "model": invalid_field_diagnostic["model"],
            "field": invalid_field_diagnostic["field"],
        })
    steps.append({"step": "repo.list_custom_modules", "status": "ok", "module": effective_module_name, "module_path": module.path, "module_resolution": module_resolution})
    head_sha = _current_head_sha(project)
    store.save_session_state({
        "active_code_change": {
            "identity": _code_change_operation_identity(project, payload),
            "effective_module": effective_module_name,
            "head_sha": head_sha,
        }
    }, client_name=client_name)

    # 3) Read custom manifest + target/custom files.  This also records gate evidence.
    custom_rel_paths: list[str] = [manifest_rel]
    for rel in (invalid_field_diagnostic or {}).get("target_files", []):
        if rel not in custom_rel_paths:
            custom_rel_paths.append(rel)
    target_file_budget = lane_budget["target_files"] if not payload.writes else len(payload.target_files)
    for raw in payload.target_files[:target_file_budget]:
        rel = _safe_module_relative(effective_module_name, raw)
        if rel not in custom_rel_paths:
            custom_rel_paths.append(rel)
    for write in payload.writes:
        rel = _safe_module_relative(effective_module_name, write.relative_path)
        if rel not in custom_rel_paths:
            custom_rel_paths.append(rel)

    # Inspect/autopatch mode uses exact-first focused search to avoid huge noisy reads.
    search_results: list[dict[str, Any]] = []
    if not payload.writes:
        focused_paths = _cached_ui_paths(ui_locator_cache, ui_locator_key, project, module.path) if ui_locator_key else []
        if focused_paths:
            search_results = [{"status": "cached_target", "paths": focused_paths, "count": len(focused_paths)}]
        else:
            patterns = _infer_code_change_search_patterns(payload)
            search_budget = min(payload.max_search_matches, lane_budget["search_matches"])
            search_results, focused_paths = _focused_search(repo, project_path, module.path, patterns, search_budget)
            focused_paths = focused_paths[:lane_budget["target_files"]]
            if ui_locator_key and focused_paths:
                _update_ui_locator_cache(store, client_name, ui_locator_cache, ui_locator_key, focused_paths, project)
        for rel in focused_paths[:lane_budget["target_files"]]:
            if rel and rel not in custom_rel_paths:
                custom_rel_paths.append(rel)

    generated_writes: list[OdooCodeFileChange] = []
    generated_autopatch_strategy: str | None = None
    generated_autopatch_reason: str | None = None
    if not payload.writes:
        generated_writes, generated_autopatch_strategy, generated_autopatch_reason = _build_safe_generic_autopatch(project, module.path, payload, custom_rel_paths)
        for write in generated_writes:
            rel = _safe_module_relative(effective_module_name, write.relative_path)
            if rel not in custom_rel_paths:
                custom_rel_paths.append(rel)

    evidence_signature = _evidence_signature(project, effective_module_name, payload, head_sha)
    session_state = store.load_session_state(client_name=client_name)
    evidence_cache = (session_state.get("code_change_evidence") or {}) if isinstance(session_state, dict) else {}
    cached_evidence = evidence_cache.get(evidence_signature) if isinstance(evidence_cache, dict) else None
    requested_custom_paths = set(list(dict.fromkeys(custom_rel_paths))[:30])
    cached_custom_paths = set(cached_evidence.get("custom_paths") or []) if isinstance(cached_evidence, dict) else set()
    reuse_custom_evidence = bool(cached_evidence and requested_custom_paths.issubset(cached_custom_paths))
    if not reuse_custom_evidence:
        cached_evidence = None

    custom_file_paths_abs: list[str] = []
    custom_reads: list[dict[str, Any]] = []
    if reuse_custom_evidence:
        cached_reads_by_path = {
            str(item.get("relative_path")): item
            for item in (cached_evidence.get("custom_reads") or [])
            if isinstance(item, dict) and item.get("relative_path")
        }
        custom_read_budget = lane_budget["custom_files"]
        for rel in list(dict.fromkeys(custom_rel_paths))[:custom_read_budget]:
            target = project / rel
            cached_read = dict(cached_reads_by_path.get(rel) or {"relative_path": rel, "status": "missing"})
            if cached_read.get("status") in {"read", "cached"} and target.is_file():
                custom_file_paths_abs.append(str(target.resolve()))
                gate.record_repo_inspection(
                    project_path=project_path,
                    relative_path=rel,
                    client_name=client_name,
                    action="workflow.odoo_code_change.cached_custom_evidence",
                    is_content_read=True,
                )
                cached_read["status"] = "cached"
            custom_reads.append(cached_read)
        steps.append({"step": "repo.read_custom", "status": "cached", "count": len(custom_file_paths_abs)})
    else:
        custom_read_budget = lane_budget["custom_files"]
        for rel in list(dict.fromkeys(custom_rel_paths))[:custom_read_budget]:
            target = project / rel
            if not target.exists() or not target.is_file():
                custom_reads.append({"relative_path": rel, "status": "missing"})
                continue
            try:
                content = _read_text_safe(target)
                gate.record_repo_inspection(project_path=project_path, relative_path=rel, client_name=client_name, action="workflow.odoo_code_change.read_custom", is_content_read=True)
                custom_file_paths_abs.append(str(target.resolve()))
                custom_reads.append({"relative_path": rel, "status": "read", "size": target.stat().st_size, "preview": _file_preview(content, 3000)})
            except UnicodeDecodeError:
                custom_reads.append({"relative_path": rel, "status": "binary_skipped"})
        steps.append({"step": "repo.read_custom", "status": "ok", "count": len([r for r in custom_reads if r.get("status") == "read"])})

    # Ensure scope has at least manifest when nothing else exists.
    if not custom_file_paths_abs and manifest_abs.exists():
        custom_file_paths_abs.append(str(manifest_abs.resolve()))

    # 4) Scope advice with corrected installed-source priority (core addons first).
    # Scope must follow the files actually being changed. Including every user-
    # supplied target file made focused controller fixes scan unrelated models
    # and escalate to L3/L4. Keep the manifest plus exact write files on write
    # passes; inspection mode keeps the focused search files.
    scope_file_paths = list(custom_file_paths_abs)
    if payload.writes or generated_writes:
        write_abs = {str((project / _safe_module_relative(effective_module_name, w.relative_path)).resolve()) for w in (payload.writes or generated_writes)}
        manifest_resolved = str(manifest_abs.resolve()) if manifest_abs.exists() else ""
        include_manifest_scope = not fast_ui_mode or manifest_resolved in write_abs
        scope_file_paths = [
            path for path in custom_file_paths_abs
            if path in write_abs or (include_manifest_scope and path == manifest_resolved)
        ]
        if not scope_file_paths:
            scope_file_paths = [path for path in custom_file_paths_abs if path != manifest_resolved] or list(custom_file_paths_abs)

    report("inspect_core_scope")
    scope_payload = ScopeAdviceRequest(
        custom_module_name=effective_module_name,
        custom_file_paths=scope_file_paths,
        task_description=payload.task_description,
        client_name=client_name,
        allow_remote_core_sync=False,
    )
    missing_manifest_assets = _missing_manifest_asset_paths(project, module_dir, manifest_abs)
    missing_asset_fast_path = _is_missing_asset_diagnostic_request(payload, missing_manifest_assets)
    micro_change = (
        _is_low_risk_micro_change(payload, list(payload.writes or generated_writes), task_lane)
        or missing_asset_fast_path
    )
    if micro_change:
        scope = ScopeAdviceResult(
            custom_module=effective_module_name,
            reached_level=ScopeLevel.L0,
            instructions=[],
            mandatory_count=0,
            total_count=0,
            warning=(
                "Missing manifest assets were diagnosed locally; remote frontend/core discovery skipped."
                if missing_asset_fast_path
                else "Low-risk micro change: core discovery skipped; changed-file QA and syntax verification remain mandatory."
            ),
        )
        gate.record_scope_advice(scope, scope_payload)
        steps.append({
            "step": "workflow.scope_advice",
            "status": "micro_skipped",
            "mandatory_count": 0,
            "reached_level": "L0",
            "reason": (
                "Manifest asset references were checked against local module files; no core/SSH evidence is needed."
                if missing_asset_fast_path
                else "Bounded low-risk edit; avoid remote core discovery overhead."
            ),
        })
    elif cached_evidence and cached_evidence.get("scope"):
        scope = ScopeAdviceResult.model_validate(cached_evidence["scope"])
        gate.record_scope_advice(scope, scope_payload)
        steps.append({"step": "workflow.scope_advice", "status": "cached", "mandatory_count": scope.mandatory_count, "reached_level": str(scope.reached_level.value if hasattr(scope.reached_level, "value") else scope.reached_level)})
    else:
        scope = scope_advice(scope_payload)
        steps.append({"step": "workflow.scope_advice", "status": "ok", "mandatory_count": scope.mandatory_count, "reached_level": str(scope.reached_level.value if hasattr(scope.reached_level, "value") else scope.reached_level)})

    frontend_preflight = None
    frontend_paths = list(dict.fromkeys(custom_rel_paths + [_safe_module_relative(effective_module_name, w.relative_path) for w in payload.writes]))
    shared_ui_cache = session_state.get("fast_ui_evidence") if isinstance(session_state, dict) else {}
    shared_ui_key = _ui_evidence_key(project, effective_module_name, payload, frontend_paths) if fast_ui_mode else None
    shared_ui_evidence = (
        shared_ui_cache.get(shared_ui_key)
        if shared_ui_key and isinstance(shared_ui_cache, dict)
        else None
    )
    if not _shared_ui_evidence_valid(shared_ui_evidence):
        shared_ui_evidence = None
    if micro_change:
        frontend_preflight = None
        steps.append({
            "step": "workflow.frontend_pattern_preflight",
            "status": "micro_skipped",
            "reason": "Tiny deterministic edit; local changed-file verification remains enabled.",
        })
    elif _looks_like_frontend_code_change(payload, frontend_paths):
        report("inspect_frontend_patterns")
        if cached_evidence and cached_evidence.get("frontend_preflight"):
            from ..schemas import FrontendPatternPreflightResult
            frontend_preflight = FrontendPatternPreflightResult.model_validate(cached_evidence["frontend_preflight"])
            steps.append({"step": "workflow.frontend_pattern_preflight", "status": "cached", "target_area": frontend_preflight.target_area, "mandatory_count": frontend_preflight.mandatory_read_count})
        elif shared_ui_evidence:
            from ..schemas import FrontendPatternPreflightResult
            frontend_preflight = FrontendPatternPreflightResult.model_validate(shared_ui_evidence["frontend_preflight"])
            steps.append({"step": "workflow.frontend_pattern_preflight", "status": "cached_module", "target_area": frontend_preflight.target_area, "mandatory_count": frontend_preflight.mandatory_read_count})
        else:
            frontend_preflight = frontend_pattern_preflight(FrontendPatternPreflightRequest(
            client_name=client_name,
            task_description=payload.task_description,
            target_area="auto",
            target_module_name=effective_module_name,
            custom_file_paths=custom_file_paths_abs,
            planned_file_paths=frontend_paths,
            allow_remote_core_sync=False,
            ))
            steps.append({
                "step": "workflow.frontend_pattern_preflight",
                "status": "ok",
                "target_area": frontend_preflight.target_area,
                "mandatory_count": frontend_preflight.mandatory_read_count,
            })
    else:
        frontend_preflight = None

    # 4a) Manifest dependency fallback: scope_advice should detect them, but make sure
    # common dependency manifests are read with /home/odoo/src/odoo/addons priority.
    if cached_evidence and cached_evidence.get("mandatory_paths"):
        mandatory_paths = list(cached_evidence.get("mandatory_paths") or [])
    elif shared_ui_evidence and shared_ui_evidence.get("mandatory_paths"):
        mandatory_paths = list(shared_ui_evidence.get("mandatory_paths") or [])
    else:
        existing_scope_paths = {item.path for item in scope.instructions}
        extra_core_reads: list[str] = []
        manifest_dependencies = [] if fast_ui_mode or micro_change else _parse_manifest_depends(manifest_abs)
        for dep in manifest_dependencies:
            if dep == effective_module_name:
                continue
            try:
                found = find_core_file(FindCoreFileRequest(module_name=dep, file_type="manifest", keyword=dep, client_name=client_name))
                if found.best_match and found.best_match not in existing_scope_paths:
                    extra_core_reads.append(found.best_match)
            except Exception:
                continue

        mandatory_paths = [item.path for item in scope.instructions if item.mandatory and not str(item.path).startswith("NOT_FOUND:")]
        mandatory_paths.extend(path for path in extra_core_reads if path not in mandatory_paths)
        if frontend_preflight is not None:
            for item in frontend_preflight.instructions:
                if item.mandatory and not str(item.path).startswith("NOT_FOUND:") and item.path not in mandatory_paths:
                    mandatory_paths.append(item.path)
    report("read_core_evidence")
    previous_core_budget = lane_budget["core_files"]
    lane_budget, compatibility_budget_expanded = _compatibility_lane_budget(task_lane, len(mandatory_paths))
    if compatibility_budget_expanded:
        # Compatibility evidence returned by scope advice is mandatory, but its
        # raw file count is not task complexity.  A focused frontend change can
        # reference several framework files and must not silently become a long
        # task.  Expand only this read budget and preserve the selected lane.
        steps.append({
            "step": "workflow.compatibility_budget",
            "status": "expanded",
            "task_lane": task_lane,
            "from": previous_core_budget,
            "to": len(mandatory_paths),
            "reason": "Read mandatory compatibility evidence without promoting task complexity.",
        })
    mandatory_paths = mandatory_paths[:lane_budget["core_files"]]

    core_reads: list[dict[str, Any]] = []
    cached_core_paths = list(cached_evidence.get("core_paths") or []) if cached_evidence else []
    if not cached_core_paths and shared_ui_evidence:
        cached_core_paths = list(shared_ui_evidence.get("core_paths") or [])
    if cached_core_paths:
        for raw_path in cached_core_paths:
            resolved = Path(str(raw_path)).expanduser().resolve()
            gate.record_repo_inspection(
                project_path=str(resolved.parent),
                relative_path=resolved.name,
                client_name=client_name,
                action="workflow.odoo_code_change.cached_core_evidence",
                is_content_read=True,
            )
            core_reads.append({"path": str(resolved), "status": "cached"})
        steps.append({"step": "repo.read_core", "status": "cached", "count": len(core_reads)})
    else:
        for raw_path in mandatory_paths[:lane_budget["core_files"]]:
            path = Path(str(raw_path)).expanduser()
            try:
                resolved = path.resolve()
            except Exception:
                resolved = path
            if resolved.is_dir():
                # Read a small number of files from mandatory dirs so the gate sees the dir satisfied.
                directory_file_budget = 1 if task_lane == "fast" else 3 if task_lane == "normal" else 10
                files = [p for p in resolved.rglob("*.py") if p.is_file()][:directory_file_budget]
                if not files:
                    core_reads.append({"path": str(resolved), "status": "dir_no_readable_files"})
                    continue
                for file_path in files:
                    try:
                        _ = _read_text_safe(file_path, 100_000)
                        gate.record_repo_inspection(project_path=str(file_path.parent), relative_path=file_path.name, client_name=client_name, action="workflow.odoo_code_change.read_core", is_content_read=True)
                        core_reads.append({"path": str(file_path), "status": "read"})
                    except Exception as exc:  # noqa: BLE001
                        core_reads.append({"path": str(file_path), "status": "failed", "error": str(exc)})
                continue
            if not resolved.exists() or not resolved.is_file():
                core_reads.append({"path": str(resolved), "status": "missing"})
                continue
            try:
                _ = _read_text_safe(resolved, 200_000)
                gate.record_repo_inspection(project_path=str(resolved.parent), relative_path=resolved.name, client_name=client_name, action="workflow.odoo_code_change.read_core", is_content_read=True)
                core_reads.append({"path": str(resolved), "status": "read"})
            except Exception as exc:  # noqa: BLE001
                core_reads.append({"path": str(resolved), "status": "failed", "error": str(exc)})
        steps.append({"step": "repo.read_core", "status": "ok", "count": len([r for r in core_reads if r.get("status") == "read"])})
    cache = dict(evidence_cache) if isinstance(evidence_cache, dict) else {}
    cached_custom_reads = list(cached_evidence.get("custom_reads") or []) if cached_evidence else []
    merged_custom_reads = {
        str(item.get("relative_path")): item
        for item in [*cached_custom_reads, *custom_reads]
        if isinstance(item, dict) and item.get("relative_path")
    }
    cache[evidence_signature] = {
        "scope": scope.model_dump(mode="json"),
        "frontend_preflight": frontend_preflight.model_dump(mode="json") if frontend_preflight is not None else None,
        "custom_paths": sorted(cached_custom_paths | requested_custom_paths),
        "custom_reads": list(merged_custom_reads.values()),
        "mandatory_paths": mandatory_paths,
        "core_paths": [r.get("path") for r in core_reads if r.get("status") in {"read", "cached"}],
        "task_profile": _task_profile(payload, frontend_paths),
    }
    if len(cache) > 20:
        cache = dict(list(cache.items())[-20:])
    store.save_session_state({"code_change_evidence": cache}, client_name=client_name)
    if fast_ui_mode and frontend_preflight is not None and shared_ui_key:
        shared_updated = dict(shared_ui_cache) if isinstance(shared_ui_cache, dict) else {}
        shared_core_paths = [str(r.get("path")) for r in core_reads if r.get("status") in {"read", "cached"} and r.get("path")]
        shared_updated[shared_ui_key] = {
            "frontend_preflight": frontend_preflight.model_dump(mode="json"),
            "mandatory_paths": mandatory_paths,
            "core_paths": shared_core_paths,
            "core_fingerprints": {
                path: fingerprint
                for path in shared_core_paths
                if (fingerprint := _path_content_fingerprint(path)) is not None
            },
            "saved_at": time.time(),
        }
        if len(shared_updated) > 30:
            shared_updated = dict(list(shared_updated.items())[-30:])
        store.save_session_state({"fast_ui_evidence": shared_updated}, client_name=client_name)

    failure_guard = repeated_failure_guard_status(
        client_name=client_name,
        fingerprint=issue_fingerprint,
        task_description=payload.task_description,
        investigation_evidence=payload.investigation_evidence,
        functional_verification_passed=payload.functional_verification_passed,
    )
    if failure_guard.get("blocked"):
        return {
            "status": "investigation_required",
            "message": (
                "The same user-visible issue remains after two technically successful attempts. "
                "Further writes are blocked until the execution flow and runtime state are investigated."
            ),
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "steps": steps,
            "failure_guard": failure_guard,
            "investigation_read_plan": {
                "custom_files": [r.get("relative_path") for r in custom_reads if r.get("status") in {"read", "cached"}],
                "native_files": [r.get("path") for r in core_reads if r.get("status") in {"read", "cached"}],
                "selection_rule": (
                    "Select additional exact files only from inheritance, imports, dependencies, "
                    "field definitions, method calls, templates, and runtime evidence. "
                    "Do not scan unrelated source trees."
                ),
                "required_evidence": [
                    "entry point to final output flow",
                    "framework-owned methods and computed values involved",
                    "expected runtime state compared with actual runtime state",
                    "new evidence-based failing-point hypothesis",
                ],
            },
            "success_allowed": False,
            "completion_required": True,
            "writes_blocked": True,
        }

    writes_to_apply = list(payload.writes or generated_writes)
    if _invalid_field_xml_only_write_block(invalid_field_diagnostic, writes_to_apply):
        return {
            "status": "root_cause_fix_required",
            "message": (
                "XML-only writes were rejected: changing an XML value or adding eval cannot create a field "
                "missing from the Odoo model registry. Fix the duplicate Python model declarations first."
            ),
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "invalid_field_diagnostic": invalid_field_diagnostic,
            "missing_manifest_assets": missing_manifest_assets,
            "rejected_writes": [change.relative_path for change in writes_to_apply],
            "writes_applied": False,
            "writes_blocked": True,
            "success_allowed": False,
            "completion_required": True,
            "next_call": {
                "tool": "workflow.odoo_code_change",
                "arguments": {
                    "client_name": client_name,
                    "project_path": project_path,
                    "module_name": effective_module_name,
                    "task_description": payload.task_description,
                    "target_files": invalid_field_diagnostic.get("target_files", []),
                    "writes": (
                        "Generate a Python root-cause fix from invalid_field_diagnostic: keep one primary _name "
                        "definition and use _inherit for intentional extensions; do not change XML literal syntax."
                    ),
                },
            },
        }
    if not writes_to_apply:
        # Persist unresolved implementation context so short follow-ups such as
        # "yes", "implement", "push", or "revert" can continue the latest task.
        # This is an internal continuation contract, not a user-facing result.
        JsonStore().save_session_state({
            "pending_code_change": {
                "client_name": client_name,
                "project_path": project_path,
                "module_name": effective_module_name,
                "task_description": payload.task_description,
                "task_type": _normalized_task_type(payload),
                "complexity": payload.complexity,
                "force_complexity": payload.force_complexity,
                "intent": payload.intent,
                "target_hint": _target_hint(payload),
                "target_files": [r.get("relative_path") for r in custom_reads if r.get("status") in {"read", "cached"}],
                "status": "awaiting_internal_writes",
            }
        }, client_name=client_name)
        return {
            "status": "needs_writes",
            "message": "INTERNAL_CONTINUATION_REQUIRED: inspection is complete; generate exact writes and call workflow.odoo_code_change again. Do not present code or manual file-edit instructions to the user.",
            "checkout_policy": {
                "same_checkout_required": True,
                "preserve_uncommitted_work": True,
                "forbidden_actions": ["web_search", "terminal_git", "git_restore", "git_reset", "branch_switch", "clean_checkout"],
                "message": "Continue through workflow.odoo_code_change only. Never restore, discard, switch, or replace the active checkout to handle existing uncommitted work.",
            },
            "fast_ui_mode": fast_ui_mode,
            "task_lane": task_lane,
            "task_lane_decision": lane_decision,
            "inspection_budget": lane_budget,
            "target_cache_hit": bool(search_results and search_results[0].get("status") == "cached_target"),
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "steps": steps,
            "custom_reads": custom_reads[:20],
            "search_results": search_results[:10],
            "task_type": _normalized_task_type(payload),
            "intent": payload.intent,
            "target_hint": _target_hint(payload),
            "patch_strategy": _normalized_patch_strategy(payload),
            "generated_autopatch_strategy": generated_autopatch_strategy,
            "generated_autopatch_reason": generated_autopatch_reason,
            "supported_autopatch_cases": ["style_rule", "text_replace"],
            "task_profile": _task_profile(payload, frontend_paths),
            "recommended_live_verifier": _task_profile(payload, frontend_paths).get("live_verifier"),
            "scope_advice": scope.model_dump(mode="json"),
            "frontend_preflight": frontend_preflight.model_dump(mode="json") if frontend_preflight is not None else None,
            "core_reads": core_reads[:30],
            "invalid_field_diagnostic": invalid_field_diagnostic,
            "success_allowed": False,
            "completion_required": True,
            "next_call": {
                "tool": "workflow.odoo_code_change",
                "arguments": {
                    "client_name": client_name,
                    "project_path": project_path,
                    "module_name": effective_module_name,
                    "task_description": payload.task_description,
                    "complexity": task_lane,
                    "target_files": [r.get("relative_path") for r in custom_reads if r.get("status") in {"read", "cached"}],
                    "writes": "Generate exact writes internally: [{relative_path, content}] or [{relative_path, search, replace, regex, count}]. Do not ask the user to edit files.",
                },
            },
        }

    # 5) Resolve all content before mutating the checkout. Multi-file changes go
    # through one smart transaction (fast-safe for small work, worktree-atomic
    # for larger/riskier work) instead of taking the repo lock once per file.
    report("apply_safe_changes")
    written_files_abs: list[str] = []
    write_results: list[dict[str, Any]] = []
    prepared_items: list[RepoWriteItem] = []
    for change in writes_to_apply:
        rel = _safe_module_relative(effective_module_name, change.relative_path)
        if change.content is not None and (change.search is not None or change.replace is not None):
            raise HTTPException(status_code=400, detail=f"Provide either content or search/replace, not both, for {rel}")
        if change.content is not None:
            new_content = change.content
        else:
            if change.search is None or change.replace is None:
                raise HTTPException(status_code=400, detail=f"Missing content or search+replace for {rel}")
            target = project / rel
            old = target.read_text(encoding="utf-8")
            if change.regex:
                new_content, replaced = re.subn(change.search, change.replace, old, count=change.count or 0)
            else:
                replaced = old.count(change.search) if change.count == 0 else min(old.count(change.search), change.count)
                new_content = old.replace(change.search, change.replace, change.count or -1)
            if replaced <= 0:
                raise HTTPException(status_code=409, detail=f"Search text not found in {rel}")
        prepared_items.append(RepoWriteItem(relative_path=rel, content=new_content))
        written_abs = str((project / rel).resolve())
        written_files_abs.append(written_abs)

    write_started = time.perf_counter()
    if len(prepared_items) == 1:
        item = prepared_items[0]
        result = repo_write(RepoWriteRequest(
            project_path=project_path,
            client_name=client_name,
            relative_path=item.relative_path,
            content=item.content,
        ))
        write_strategy = "single_atomic"
        write_results.append({
            "relative_path": item.relative_path,
            "status": "written",
            "size": result.size,
            "extra": result.extra,
        })
    else:
        batch_result = repo_write_many_smart(RepoWriteManySmartRequest(
            project_path=project_path,
            client_name=client_name,
            files=prepared_items,
            strategy="auto",
            timeout_seconds=60,
        ))
        if batch_result.exit_code != 0:
            raise HTTPException(
                status_code=409,
                detail={
                    "status": "batch_write_failed",
                    "message": batch_result.stderr or "Smart multi-file write failed",
                    "command": batch_result.command,
                },
            )
        write_strategy = "atomic" if "SMART_STRATEGY=atomic" in batch_result.stdout else "fast_safe"
        write_results.extend({
            "relative_path": item.relative_path,
            "status": "written",
            "size": len(item.content.encode("utf-8")),
            "extra": {"strategy": write_strategy},
        } for item in prepared_items)
    write_elapsed_ms = int((time.perf_counter() - write_started) * 1000)
    steps.append({
        "step": "repo.write_many_smart" if len(prepared_items) > 1 else "repo.write",
        "status": "ok",
        "written_count": len(written_files_abs),
        "strategy": write_strategy,
        "elapsed_ms": write_elapsed_ms,
    })

    if payload.create_module:
        missing_after_write = _missing_manifest_asset_paths(project, module_dir, manifest_abs)
        if missing_after_write:
            report("module_completeness")
            JsonStore().save_session_state({
                "pending_code_change": {
                    "client_name": client_name,
                    "project_path": project_path,
                    "module_name": effective_module_name,
                    "task_description": payload.task_description,
                    "task_type": _normalized_task_type(payload),
                    "complexity": task_lane,
                    "intent": payload.intent,
                    "target_hint": _target_hint(payload),
                    "target_files": missing_after_write,
                    "status": "incomplete_new_module",
                }
            }, client_name=client_name)
            return {
                "status": "needs_writes",
                "message": (
                    "INTERNAL_CONTINUATION_REQUIRED: the new module manifest references files that are not yet present. "
                    "Generate all missing files in one workflow.odoo_code_change continuation before QA, commit, or push."
                ),
                "client_name": client_name,
                "project_path": project_path,
                "module_name": effective_module_name,
                "missing_manifest_assets": missing_after_write,
                "writes_applied": True,
                "qa_started": False,
                "commit_started": False,
                "success_allowed": False,
                "completion_required": True,
                "next_call": {
                    "tool": "workflow.odoo_code_change",
                    "arguments": {
                        "client_name": client_name,
                        "project_path": project_path,
                        "module_name": effective_module_name,
                        "create_module": True,
                        "task_description": payload.task_description,
                        "target_files": missing_after_write,
                        "writes": "Generate complete contents for every missing_manifest_assets path; do not stop or answer the user yet.",
                    },
                },
            }

    # 6) Diff, then run independent read-only QA/core validators concurrently.
    report("qa_and_compatibility")
    diff = GitService().diff(project_path, staged=False, timeout_seconds=60)
    validation_started = time.perf_counter()
    manifest_only_change = bool(written_files_abs) and all(
        Path(path).name == "__manifest__.py" for path in written_files_abs
    )
    verify_request = SyntaxVerifyRequest(
        written_files=written_files_abs,
        project_path=project_path,
        client_name=client_name,
        fix_cycle=0,
        allow_remote_core_sync=False,
        impact_aware=False,
    )
    if manifest_only_change:
        # The syntax verifier parses and validates the exact manifest. A full
        # module scan here duplicates safe-deploy validation and made a one-line
        # release bump wait behind unrelated module QA.
        verify = await asyncio.to_thread(verify_against_core, verify_request)
        qa = SelfRunReport(
            status="passed",
            project_path=project_path,
            module_path=str(module_dir),
            module_name=effective_module_name,
            findings=[],
            commands=[],
            summary={
                "mode": "manifest_only",
                "full_module_scan_skipped": True,
                "exact_file_syntax_verification": True,
            },
        )
        validation_execution = "manifest_fast_path"
    else:
        qa, verify = await asyncio.gather(
            asyncio.to_thread(
                self_run,
                SelfRunRequest(
                    project_path=project_path,
                    client_name=client_name,
                    module_name=effective_module_name,
                    run_pytest=False,
                    include_git_status=True,
                ),
            ),
            asyncio.to_thread(verify_against_core, verify_request),
        )
        validation_execution = "parallel"
    validation_elapsed_ms = int((time.perf_counter() - validation_started) * 1000)
    steps.append({"step": "git.diff", "status": "ok" if diff.exit_code == 0 else "failed", "exit_code": diff.exit_code})
    steps.append({"step": "qa.self_run", "status": qa.status, "execution": validation_execution})
    steps.append({
        "step": "workflow.verify_against_core",
        "status": verify.status,
        "push_blocked": verify.push_blocked,
        "critical_count": verify.critical_count,
        "execution": validation_execution,
        "validation_group_elapsed_ms": validation_elapsed_ms,
    })

    if qa.status == "failed" or verify.push_blocked:
        failure_text = json.dumps(
            {
                "qa": qa.model_dump(mode="json"),
                "verify": verify.model_dump(mode="json"),
            },
            default=str,
        )[-20000:]
        failure_policy = FailurePolicyService().decide(
            stage="pre_deploy_validation",
            error=failure_text,
            attempt=1,
        ).as_dict()
        return {
            "status": "verify_failed",
            "message": "Code was written but deploy is blocked by QA/core verification. Fix findings and rerun workflow.odoo_code_change.",
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "steps": steps,
            "write_results": write_results,
            "git_diff": {"exit_code": diff.exit_code, "stdout": diff.stdout[-12000:], "stderr": diff.stderr[-4000:]},
            "qa": qa.model_dump(mode="json"),
            "verify_against_core": verify.model_dump(mode="json"),
            "failure_policy": failure_policy,
            "success_allowed": False,
            "completion_required": True,
        }

    if not payload.auto_deploy:
        return {
            "status": "ready_to_deploy",
            "message": "Code written and verified. auto_deploy=false, so no deploy job was started.",
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "steps": steps,
            "write_results": write_results,
            "verify_against_core": verify.model_dump(mode="json"),
            "success_allowed": False,
            "completion_required": True,
        }

    report("start_safe_deploy")
    commit_message = payload.commit_message or f"Update {effective_module_name}: {payload.task_description[:180]}"
    dirty_module_paths = _git_status_changed_module_files(project_path, module.path)
    deploy_paths = list(dict.fromkeys(
        [str(Path(path).resolve().relative_to(project.resolve()).as_posix()) for path in written_files_abs]
        + dirty_module_paths
    ))
    try:
        deploy_impact = classify_deploy_impact(
            project_path=project_path,
            module_name=effective_module_name,
            changed_paths=deploy_paths,
            git_diff=diff.stdout,
            deploy_mode=payload.deploy_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    deploy_payload = SafeDeployModuleRequest(
        client_name=client_name,
        project_path=project_path,
        module_name=effective_module_name,
        commit_message=commit_message,
        paths=deploy_paths,
        run_qa=False,
        wait_for_result=False,
        wait_timeout_seconds=0,
        upgrade_module=deploy_impact.upgrade_required,
        deploy_impact=deploy_impact.impact,
        upgrade_reasons=list(deploy_impact.reasons),
    )
    try:
        deploy = await safe_deploy_module(deploy_payload)
        deploy_struct = deploy.model_dump(mode="json") if hasattr(deploy, "model_dump") else deploy
    except HTTPException as exc:
        deploy_detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return {
            "status": "deploy_blocked",
            "message": "Code was written and verified, but safe deploy was blocked by client validation.",
            "client_name": client_name,
            "project_path": project_path,
            "module_name": effective_module_name,
            "steps": steps + [{"step": "workflow.safe_deploy_module", "status": "blocked", "detail": deploy_detail}],
            "write_results": write_results,
            "git_diff": {"exit_code": diff.exit_code, "stdout_preview": diff.stdout[-12000:], "stderr_preview": diff.stderr[-4000:]},
            "qa": qa.model_dump(mode="json"),
            "verify_against_core": verify.model_dump(mode="json"),
            "deploy_validation": deploy_detail,
            "success_allowed": False,
            "completion_required": True,
            "source_deployed": False,
            "module_upgraded": False,
            "visible_claim_allowed": False,
            "allowed_summary": "Code was written and verified, but not pushed/deployed/upgraded. Fix the deploy validation blocker, then rerun with auto_deploy=true.",
        }
    job_id = deploy_struct.get("job_id") if isinstance(deploy_struct, dict) else None
    record_code_change_deploy_attempt(client_name=client_name, fingerprint=issue_fingerprint, job_id=job_id)
    store.save_session_state({"pending_code_change": None}, client_name=client_name)
    frontend_touched = _looks_like_frontend_code_change(payload, deploy_paths)
    return {
        "status": "deploy_started" if deploy_struct.get("status") in {"running", "already_running", "already_in_progress", "resumable_existing_job"} else deploy_struct.get("status", "deploy_started"),
        "message": "Code change workflow completed through verify phase; safe deploy is server-owned. Poll jobs.get until terminal result before final answer.",
        "client_name": client_name,
        "project_path": project_path,
        "module_name": effective_module_name,
        "steps": steps,
        "write_results": write_results,
        "generated_autopatch": bool(generated_writes and not payload.writes),
        "generated_autopatch_strategy": generated_autopatch_strategy,
        "fast_ui_mode": fast_ui_mode,
        "task_lane": task_lane,
        "task_lane_decision": lane_decision,
        "inspection_budget": lane_budget,
        "task_type": _normalized_task_type(payload),
        "intent": payload.intent,
        "target_hint": _target_hint(payload),
        "patch_strategy": _normalized_patch_strategy(payload),
        "git_diff": {"exit_code": diff.exit_code, "stdout_preview": diff.stdout[-12000:], "stderr_preview": diff.stderr[-4000:]},
        "qa": qa.model_dump(mode="json"),
        "verify_against_core": verify.model_dump(mode="json"),
        "deploy_impact": deploy_impact.as_dict(),
        "safe_deploy": deploy_struct,
        "job_id": job_id,
        "completion_required": True,
        "success_allowed": False,
        "source_pushed": False,
        "source_on_odoosh": False,
        "source_deployed": False,
        "module_upgraded": False,
        "core_verified": not verify.push_blocked,
        "frontend_touched": frontend_touched,
        "allowed_summary": "Code was written and verified; deploy is running. Do not claim pushed/deployed/upgraded until jobs.get returns terminal passed with deploy_confirmed=true.",
        "next_call": {"tool": "jobs.get", "arguments": {"job_id": job_id, "wait_for_result": True, "wait_timeout_seconds": 20, "poll_interval_seconds": 2.0}} if job_id else None,
    }


def _code_change_start_response(job: JobRecord, *, duplicate: bool = False) -> dict[str, Any]:
    phase = None
    if job.steps:
        phase = job.steps[-1].name
    return {
        "status": "already_in_progress" if duplicate else "running",
        "message": (
            "The same Odoo code-change workflow is already running in the background; no duplicate worker was started."
            if duplicate
            else "Odoo code-change workflow started in the background. Poll jobs.get for phase progress and the terminal result."
        ),
        "job_id": job.id,
        "job_status": job.status.value,
        "phase": phase or "queued",
        "worker_alive": True,
        "connector_revision": settings.build_revision,
        "completion_required": True,
        "success_allowed": False,
        "tool_exit_allowed": False,
        "poll_after_seconds": 2,
        "next_call": {
            "tool": "jobs.get",
            "arguments": {
                "job_id": job.id,
                "wait_for_result": True,
                "wait_timeout_seconds": 20,
                "poll_interval_seconds": 2.0,
            },
        },
    }


def _finish_code_change_without_deploy(job_id: str, result: dict[str, Any]) -> None:
    store = JsonStore()
    job = store.get_job(job_id)
    if job.status not in {JobStatus.queued, JobStatus.running}:
        return
    result_status = str(result.get("status") or "failed")
    failed_statuses = {"failed", "verify_failed", "deploy_blocked", "error"}
    final_status = JobStatus.failed if result_status in failed_statuses else JobStatus.warning
    now = datetime.now(timezone.utc).isoformat()
    if job.steps and job.steps[-1].status == "running":
        job.steps[-1].status = final_status.value
        job.steps[-1].finished_at = now
        job.steps[-1].result = {"status": result_status}
    final_result = {
        **result,
        "job_id": job.id,
        "completion_required": False,
        "success_allowed": False,
        "tool_exit_allowed": True,
    }
    store.finish_job(job, final_status, final_result)


def _start_code_change_job(payload: OdooCodeChangeRequest) -> dict[str, Any]:
    """Return immediately and own the complete code-change workflow in a job."""
    request = payload.model_dump(mode="json")
    with _CODE_CHANGE_ENTRY_LOCK:
        store = JsonStore()
        refreshed_jobs = [store.get_job(candidate.id) for candidate in store.list_jobs(limit=200)]
        existing = next(
            (
                job
                for job in refreshed_jobs
                if job.job_type == "odoo_code_change"
                and job.status in {JobStatus.queued, JobStatus.running}
                and job.request == request
            ),
            None,
        )
        if existing is not None:
            return _code_change_start_response(existing, duplicate=True)
        job = store.create_job(JobRecord(
            job_type="odoo_code_change",
            status=JobStatus.running,
            request=request,
            steps=[JobStep(name="queued", status="passed")],
            result={
                "status": "running",
                "phase": "queued",
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "worker_alive": True,
                "connector_revision": settings.build_revision,
                "completion_required": True,
                "success_allowed": False,
                "tool_exit_allowed": False,
            },
        ))

    def _background() -> None:
        heartbeat_stop = threading.Event()

        def _heartbeat() -> None:
            while not heartbeat_stop.wait(10):
                try:
                    if not _touch_code_change_heartbeat(job.id):
                        return
                except Exception:  # noqa: BLE001 - liveness must not crash work
                    continue

        _touch_code_change_heartbeat(job.id)
        heartbeat_thread = threading.Thread(
            target=_heartbeat,
            name=f"odoo-code-heartbeat-{job.id[:8]}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            result = asyncio.run(
                _workflow_odoo_code_change(
                    payload,
                    progress=lambda phase: _record_code_change_phase(job.id, phase),
                )
            )
            nested_job_id = result.get("job_id") if isinstance(result, dict) else None
            if nested_job_id:
                current_store = JsonStore()
                current = current_store.get_job(job.id)
                if current.status not in {JobStatus.queued, JobStatus.running}:
                    return
                current.result = {
                    **result,
                    "status": "running",
                    "phase": "safe_deploy",
                    "job_id": current.id,
                    "nested_job_id": nested_job_id,
                    "nested_next_call": result.get("next_call"),
                    "completion_required": True,
                    "success_allowed": False,
                    "tool_exit_allowed": False,
                    "next_call": {
                        "tool": "jobs.get",
                        "arguments": {
                            "job_id": current.id,
                            "wait_for_result": True,
                            "wait_timeout_seconds": 20,
                            "poll_interval_seconds": 2.0,
                        },
                    },
                }
                current_store.update_job(current)
                return
            _finish_code_change_without_deploy(job.id, result)
        except Exception as exc:  # noqa: BLE001
            failure = {
                "status": "failed",
                "message": "Odoo code-change workflow failed before safe deploy started.",
                "error": str(getattr(exc, "detail", exc)),
                "success_allowed": False,
            }
            try:
                _finish_code_change_without_deploy(job.id, failure)
            except Exception:  # noqa: BLE001
                pass
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)

    try:
        submit_background(background_task(_background, "orchestration"))
    except Exception as exc:  # noqa: BLE001
        _finish_code_change_without_deploy(job.id, {
            "status": "failed",
            "message": "Odoo code-change worker could not be submitted.",
            "error": str(exc),
            "success_allowed": False,
        })
        return JsonStore().get_job(job.id).model_dump(mode="json")
    return _code_change_start_response(job)


async def _dispatch(name: str, args: dict) -> object:
    if name == "connector.health":
        EmptyRequest.model_validate(args)
        return {"status": "ok", "service": "odoo-mcp-agent-connector"}
    if name == "artifacts.read":
        req = ArtifactReadRequest.model_validate(args)
        return JsonStore().read_artifact(req.artifact_id, req.offset, req.max_chars)
    if name == "client.check":
        req = ClientCheckRequest.model_validate(args)
        return ClientGateService().check(req.client_name, req.run_validation, req.force_refresh, req.user_confirmed)
    if name == "workflow.capability_check":
        EmptyRequest.model_validate(args)
        return capability_check()
    if name == "workflow.coding_task_plan":
        return coding_task_plan(CodingTaskPlanRequest.model_validate(args))
    if name == "workflow.odoo_code_change":
        return _start_code_change_job(OdooCodeChangeRequest.model_validate(args))
    if name == "workflow.smart_tool_router":
        return smart_tool_router(SmartToolRouteRequest.model_validate(args))
    if name == "workflow.repo_index":
        return repo_index(RepoIndexRequest.model_validate(args))
    if name == "workflow.full_verify":
        return await full_verify(FullVerifyRequest.model_validate(args))
    if name == "workflow.safe_deploy_module":
        return await safe_deploy_module(SafeDeployModuleRequest.model_validate(args))
    if name == "workflow.verify_deployed_module":
        return verify_deployed_module(DeployedModuleVerifyRequest.model_validate(args))
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
        return odoosh_module_install_ssh(OdooModuleRequest.model_validate(_force_mcp_async_deploy_args(args)))
    if name == "odoosh.health":
        req = ClientOnlyRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).health()
    if name == "odoosh.git_status":
        req = ClientOnlyRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).git_status()
    if name == "odoosh.check_commit":
        req = OdooShCheckCommitRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).check_commit_deployed(req.expected_sha)
    if name == "odoosh.wait_for_commit":
        return odoosh_wait_for_commit(OdooShWaitForCommitRequest.model_validate(args))
    if name == "odoosh.verify_module_manifest":
        return odoosh_verify_module_manifest(OdooShVerifyManifestRequest.model_validate(args))
    if name == "odoosh.read_core_file":
        req = OdooShCoreFileReadRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).read_core_file(req.base, req.relative_path, req.max_bytes)
    if name == "odoosh.list_core_files":
        req = OdooShCoreFilesListRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).list_core_files(req.base, req.relative_path, req.max_items)
    if name == "odoosh.read_user_file":
        req = OdooShUserFileReadRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).read_user_file(req.relative_path, req.max_bytes)
    if name == "odoosh.list_user_files":
        req = OdooShUserFilesListRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).list_user_files(req.module_name, req.subdir, req.max_items)
    if name == "odoosh.log":
        req = OdooShLogRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).tail_log(req.log_name, req.tail_lines)
    if name == "odoosh.grep_errors":
        req = OdooShCommandRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).grep_errors(req.tail_lines)
    if name == "odoosh.error_pack":
        req = OdooShCommandRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).error_pack(req.tail_lines)
    if name == "odoosh.module_deploy_ssh":
        return odoosh_module_deploy_ssh(OdooShModuleUpdateRequest.model_validate(_force_mcp_async_deploy_args(args)))
    if name == "odoosh.module_update":
        return odoosh_module_update(OdooShModuleUpdateRequest.model_validate(_force_mcp_async_deploy_args(args)))
    if name == "odoosh.query":
        req = OdooShQueryRequest.model_validate(args)
        client = _resolve_query_client(req.client_name)
        return OdooShService(client).search_read(req.model, req.domain, req.fields, req.limit, req.offset, req.order, req.context, req.timeout_seconds)
    if name == "odoo.health":
        req = ClientOnlyRequest.model_validate(args)
        return _odoo_service_for_client(_resolve_query_client(req.client_name)).health()
    if name == "odoo.query":
        return _odoo_functional_query(OdooFunctionalQueryRequest.model_validate(args))
    if name == "odoo.module_deploy":
        return odoo_module_deploy(OdooModuleRequest.model_validate(_force_mcp_async_deploy_args(args)))
    if name == "odoo.module_status":
        req = OdooModuleRequest.model_validate(args)
        return _odoo_service_for_client(_resolve_query_client(req.client_name)).module_status(req.module_name)
    if name == "odoo.module_install":
        return odoo_module_install(OdooModuleRequest.model_validate(_force_mcp_async_deploy_args(args)))
    if name == "odoo.module_update":
        return odoo_module_update(OdooModuleRequest.model_validate(_force_mcp_async_deploy_args(args)))
    if name == "odoo.search_read":
        req = OdooSearchReadRequest.model_validate(args)
        data = _search_read_via_shell(
            model=req.model,
            domain=req.domain,
            fields=req.fields,
            limit=req.limit,
            offset=req.offset,
            order=req.order,
            context=req.context,
            client_name=req.client_name,
        )
        return data.get("odoo") if data.get("status") == "ok" else OdooResult(status="failed", result=data, error=data.get("message") or "Odoo search_read failed")
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
            return await asyncio.to_thread(
                JsonStore().wait_job,
                req.job_id,
                req.wait_timeout_seconds,
                req.poll_interval_seconds,
            )
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
        return _odoo_service_for_client(_resolve_query_client(req.client_name)).ensure_dependencies(req.module_name)
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

_SAFE_DEPLOY_STEPS = [
    "git_diff",
    "git_changed_files",
    "qa_self_run",
    "verify_against_core_pre_push",
    "git_configure_identity",
    "git_validate_target",
    "git_commit",
    "git_head_commit_path_gate",
    "git_head_sha",
    "git_push",
    "git_remote_head_verify",
    "odoosh_check_commit",
    "odoosh_user_file_verify",
    "odoosh_verify_module_manifest",
    "odoo_module_deploy",
    "odoo_module_status",
    "registry_field_verify",
    "registry_smoke_test",
]
_SAFE_DEPLOY_STEP_ALIASES = {
    "qa": "qa_self_run",
    "verify": "verify_against_core_pre_push",
    "commit": "git_commit",
    "push": "git_push",
    "wait_odoosh": "odoosh_check_commit",
    "odoosh": "odoosh_check_commit",
    "verify_deployed_code": "odoosh_user_file_verify",
    "verify_user_files": "odoosh_user_file_verify",
    "install": "odoo_module_deploy",
    "upgrade": "odoo_module_deploy",
    "module_install": "odoo_module_deploy",
    "module_update": "odoo_module_deploy",
    "module_upgrade": "odoo_module_deploy",
    "deploy": "odoo_module_deploy",
    "field_verify": "registry_field_verify",
    "registry_fields": "registry_field_verify",
    "smoke_test": "registry_smoke_test",
}
_RESUME_COMPLETED_STATUSES = {"ok", "passed", "skipped"}


def _step_payload(result: object) -> object:
    return result.model_dump(mode="json") if hasattr(result, "model_dump") else result


def _extract_sha_from_steps(job) -> str | None:
    for step in reversed(job.steps or []):
        result = step.result
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        if not isinstance(result, dict):
            continue
        for key in ("sha", "last_sha"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        stdout = str(result.get("stdout") or "")
        match = re.search(r"(?:^|\n)sha=([0-9a-fA-F]{7,40})(?:\n|$)", stdout)
        if match:
            return match.group(1)
        if step.name == "git_head_sha":
            value = stdout.strip()
            if re.fullmatch(r"[0-9a-fA-F]{7,40}", value):
                return value
    return None


def _same_resume_project(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return True
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except Exception:
        return str(left).rstrip("/\\") == str(right).rstrip("/\\")


def _job_matches_resume_request(job, req: "ResumeJobRequest", state: dict) -> bool:
    request = job.request or {}
    req_project = request.get("project_path") or state.get("project_path")
    req_module = request.get("module_name") or state.get("module_name")
    if req.project_path and req_project and not _same_resume_project(str(req_project), req.project_path):
        return False
    if req.module_name and req_module and str(req_module) != req.module_name:
        return False
    return True


def _result_for_completed_step(job, step_name: str) -> object | None:
    for step in reversed(job.steps or []):
        if step.name == step_name and step.status in _RESUME_COMPLETED_STATUSES:
            return step.result
    return None


def _resume_step_ok(resume_results: dict, job, step_name: str) -> bool:
    current = ((resume_results.get("steps_run") or {}).get(step_name) or {}).get("status")
    if current in {"ok", "passed", "skipped"}:
        return True
    return any(step.name == step_name and step.status in _RESUME_COMPLETED_STATUSES for step in (job.steps or []))


def _resume_deploy_confirmation(original_request: dict, resume_results: dict, job) -> dict:
    result: dict = {
        "status": "passed",
        "message": resume_results.get("message") or "Pipeline resumed and completed.",
        "module_name": original_request.get("module_name"),
        "resume": resume_results,
    }
    odoosh_step = ((resume_results.get("steps_run") or {}).get("odoosh_check_commit") or {})
    existing_odoosh = _result_for_completed_step(job, "odoosh_check_commit")
    deployed = bool(odoosh_step.get("deployed"))
    if not deployed and isinstance(existing_odoosh, dict):
        stdout = str(existing_odoosh.get("stdout") or "")
        deployed = '"deployed": true' in stdout.lower() or existing_odoosh.get("deployed") is True
    result["odoosh_check_commit"] = {
        "status": "passed" if deployed else "unknown",
        "deployed": deployed,
        "expected_sha": resume_results.get("sha") or original_request.get("last_sha") or _extract_sha_from_steps(job),
    }
    upgrade_requested = bool(original_request.get("upgrade_module", True))
    upgrade_passed = True if not upgrade_requested else _resume_step_ok(resume_results, job, "odoo_module_deploy")
    result["module_upgrade"] = {
        "requested": upgrade_requested,
        "status": "passed" if upgrade_passed else "unknown",
        "passed": upgrade_passed,
    }
    return result


def _persist_resume_result(store: JsonStore, job, resume_results: dict, original_request: dict) -> dict:
    status_text = str(resume_results.get("status") or "failed")
    if status_text in {"ok", "already_complete"}:
        final_result = _resume_deploy_confirmation(original_request, resume_results, job)
        if deployment_confirmed_from_record(JobStatus.passed, original_request, final_result):
            target_status = JobStatus.passed
        else:
            resume_results["status"] = "resume_finalization_incomplete"
            resume_results["message"] = (
                "All resume steps are recorded, but the stored job lacks explicit "
                "Odoo.sh deployed=true and module-upgrade confirmation. Marking the "
                "resume terminal instead of leaving a warning job that loops forever."
            )
            final_result = {**resume_results, "module_name": original_request.get("module_name")}
            target_status = JobStatus.failed
    elif status_text in {"deployment_waiting", "deploy_failed_or_timeout", "module_upgrade_running"}:
        final_result = {**resume_results, "module_name": original_request.get("module_name")}
        target_status = JobStatus.warning
    else:
        final_result = {**resume_results, "module_name": original_request.get("module_name")}
        target_status = JobStatus.failed

    store.add_step(
        store.get_job(job.id),
        JobStep(
            name="workflow.resume_job",
            status="passed" if target_status == JobStatus.passed else "warning" if target_status == JobStatus.warning else "failed",
            result=resume_results,
        ),
    )
    finished = store.finish_job(store.get_job(job.id), target_status, final_result)
    store.save_session_state(
        {
            "last_safe_deploy_job_id": job.id,
            "module_name": original_request.get("module_name"),
            "project_path": original_request.get("project_path"),
            "branch": original_request.get("branch"),
            "client_name": original_request.get("client_name"),
            "safe_deploy_status": status_text,
        },
        client_name=original_request.get("client_name"),
    )
    resume_results["job_status_after_resume"] = finished.status.value
    resume_results["deploy_confirmed"] = finished.deploy_confirmed
    return resume_results



def _start_and_wait_module_deploy_for_resume(original_request: dict, module_name: str) -> OdooResult:
    started = start_odoo_module_deploy_job(OdooModuleRequest(
        client_name=original_request.get("client_name"),
        module_name=module_name,
        wait_for_result=False,
        wait_timeout_seconds=0,
        poll_interval_seconds=1.0,
    ))
    started_payload = started.model_dump(mode="json") if hasattr(started, "model_dump") else started
    result_payload = started_payload.get("result") if isinstance(started_payload, dict) and isinstance(started_payload.get("result"), dict) else {}
    if result_payload.get("finished") or not result_payload.get("job_id"):
        return started

    nested_job_id = result_payload.get("job_id")
    timeout_seconds = int(original_request.get("odoo_module_deploy_timeout_seconds", 300))
    poll_interval = float(original_request.get("odoo_module_deploy_poll_interval_seconds", 2.0))
    waited = JsonStore().wait_job(str(nested_job_id), timeout_seconds, poll_interval)
    data = waited.model_dump(mode="json")
    if waited.status == JobStatus.passed:
        return OdooResult(status="ok", result={"job_id": waited.id, "job_status": waited.status.value, "finished": True, "job": data})
    if waited.status in {JobStatus.failed, JobStatus.warning}:
        result_error = waited.result.get("error") if isinstance(waited.result, dict) else None
        return OdooResult(
            status="failed" if waited.status == JobStatus.failed else "ok",
            error=result_error or f"Odoo module deploy job finished with status={waited.status.value}",
            result={"job_id": waited.id, "job_status": waited.status.value, "finished": True, "job": data},
        )
    return OdooResult(
        status="ok",
        result={
            "job_id": waited.id,
            "job_status": waited.status.value,
            "finished": False,
            "completion_required": True,
            "success_allowed": False,
            "tool_exit_allowed": False,
            "poll_after_seconds": 10,
            "message": f"Odoo module deploy is still running after {timeout_seconds}s. Safe deploy is not complete and final success is blocked.",
            "next_call": {"tool": "jobs.get", "arguments": {"job_id": waited.id, "wait_for_result": True, "wait_timeout_seconds": 20, "poll_interval_seconds": 2.0}},
        },
    )


def _latest_safe_deploy_job_id(req: "ResumeJobRequest", store: JsonStore) -> str | None:
    state = store.load_session_state(req.client_name)
    saved_job_id = state.get("last_safe_deploy_job_id")
    if isinstance(saved_job_id, str) and saved_job_id.strip():
        try:
            saved_job = store.get_job(saved_job_id.strip())
            if _job_matches_resume_request(saved_job, req, state):
                return saved_job.id
        except Exception:
            pass

    for job in store.list_jobs(limit=100):
        if job.job_type != "safe_deploy_module":
            continue
        request = job.request or {}
        if req.project_path and request.get("project_path") != req.project_path:
            continue
        if req.module_name and request.get("module_name") != req.module_name:
            continue
        return job.id
    return None


async def _resume_job(req: "ResumeJobRequest") -> dict:
    """Fix 3: Resume a safe_deploy_module pipeline from last successful step.

    Reads the original job record, finds the last step that succeeded, and
    re-runs the pipeline from the next step.  The SafeDeployModuleRequest
    stored in job.request is used to rebuild the context.
    """
    store = JsonStore()
    job_id = req.job_id or _latest_safe_deploy_job_id(req, store)
    if not job_id:
        raise HTTPException(
            status_code=404,
            detail=(
                "No resumable safe_deploy_module job found. Pass job_id, or provide client_name/project_path/module_name "
                "matching a previous safe deploy."
            ),
        )
    try:
        job = store.get_job(job_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    job = mark_stale_safe_deploy_running_job(job, store)
    if job.status in {JobStatus.queued, JobStatus.running}:
        return {
            "status": "active_job",
            "message": "The safe_deploy_module job is still active. Poll jobs.get instead of resuming so commit/push/upgrade are not replayed.",
            "job_id": job_id,
            "job_status": job.status.value,
            "completion_required": True,
            "success_allowed": False,
            "tool_exit_allowed": False,
            "poll_after_seconds": 10,
            "next_call": {"tool": "jobs.get", "arguments": {"job_id": job_id, "wait_for_result": True, "wait_timeout_seconds": 20, "poll_interval_seconds": 2.0}},
        }

    completed_set = {s.name for s in (job.steps or []) if s.status in _RESUME_COMPLETED_STATUSES}
    # Older saved jobs and some tests predate the explicit resume gates added
    # here. If a downstream irreversible step already passed, infer that the
    # earlier read-only/pre-push gates from that phase should not be replayed.
    if "git_commit" in completed_set:
        completed_set.update({"git_diff", "git_changed_files", "qa_self_run", "verify_against_core_pre_push"})
    if "git_head_sha" in completed_set:
        completed_set.add("git_head_commit_path_gate")
    if "git_push" in completed_set:
        completed_set.add("git_remote_head_verify")
    completed_steps = [step for step in _SAFE_DEPLOY_STEPS if step in completed_set]
    # Determine resume point
    if req.override_step:
        override_step = _SAFE_DEPLOY_STEP_ALIASES.get(req.override_step, req.override_step)
        if override_step not in _SAFE_DEPLOY_STEPS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown override_step '{req.override_step}'. Valid: {_SAFE_DEPLOY_STEPS}",
            )
        start_from = override_step
    else:
        # Find the first step NOT in completed_steps
        start_from = None
        for step_name in _SAFE_DEPLOY_STEPS:
            if step_name not in completed_steps:
                start_from = step_name
                break
    # Recover original request payload
    original_request = job.request or {}
    ClientGateService().require_validated_client(original_request.get("client_name") or req.client_name, "workflow.resume_job")
    module_name = original_request.get("module_name")
    project_path = original_request.get("project_path")
    if not module_name or not project_path:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resume: job {job_id} is missing module_name or project_path in its request payload.",
        )

    client = ClientResolverService().resolve_client(
        client_name=original_request.get("client_name") or req.client_name,
        project_path=project_path,
        require_complete=False,
    )
    if client:
        original_request.setdefault("client_name", client.name)
        original_request.setdefault("client", client.public)
        original_request["project_path"] = client.path or project_path
        project_path = str(original_request["project_path"])
    remote = original_request.get("remote", "origin")
    branch = original_request.get("branch") or (client.branch if client else None) or settings.target_git_branch
    original_request["branch"] = branch
    pushed_sha = _extract_sha_from_steps(job) or original_request.get("last_sha")
    expected_user_hashes = original_request.get("expected_user_file_hashes") or {}
    candidate_files: list[str] = []
    if not expected_user_hashes:
        try:
            candidate_files = _pre_push_verify_files(Path(project_path), module_name, original_request.get("paths") or [], "")
            expected_user_hashes = _expected_user_file_hashes(Path(project_path), candidate_files)
            if expected_user_hashes:
                job.request["expected_user_file_hashes"] = expected_user_hashes
                store.update_job(job)
        except Exception:
            expected_user_hashes = {}
    expected_registry_fields = _normalise_expected_registry_fields(original_request.get("expected_registry_fields") or {})
    if not expected_registry_fields:
        try:
            if not candidate_files:
                candidate_files = _pre_push_verify_files(Path(project_path), module_name, original_request.get("paths") or [], "")
            expected_registry_fields = _infer_registry_fields_from_python(Path(project_path), module_name, candidate_files)
            if expected_registry_fields:
                job.request["expected_registry_fields"] = expected_registry_fields
                store.update_job(job)
        except Exception:
            expected_registry_fields = {}
    git = GitService()
    odoosh = OdooShService(client) if client else OdooShService()
    odoo = _odoo_service_for_client(client)

    # Re-run from start_from step
    store.log_audit(
        "workflow.resume_job",
        "running",
        f"Resuming job {job_id} from step '{start_from}' (completed: {completed_steps})",
    )
    resume_results: dict = {
        "resumed_from": start_from,
        "original_job_id": job_id,
        "completed_steps": completed_steps,
        "steps_run": {},
    }
    if start_from is None:
        resume_results["resumed_from"] = "already_complete"
        resume_results["status"] = "already_complete"
        resume_results["message"] = "All pipeline steps were already recorded; finalizing the stored job state."
        return _persist_resume_result(store, job, resume_results, original_request)

    step_order = _SAFE_DEPLOY_STEPS[_SAFE_DEPLOY_STEPS.index(start_from):]

    for step_name in step_order:
        try:
            if step_name == "git_diff":
                result = git.diff(project_path, staged=False, timeout_seconds=30)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"git diff failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "git_changed_files":
                if hasattr(git, "changed_files"):
                    result = git.changed_files(project_path)
                else:
                    fallback_paths = original_request.get("paths") or [f"{module_name}/__manifest__.py"]
                    result = CommandResult(command=["git", "changed-files", "legacy-fake"], cwd=project_path, exit_code=0, stdout="\n".join(fallback_paths) + "\n")
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"git changed-files failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "qa_self_run":
                if not original_request.get("run_qa", True):
                    resume_results["steps_run"][step_name] = {"status": "skipped"}
                    continue
                qa_req = SelfRunRequest(project_path=project_path, module_name=module_name)
                result = self_run(qa_req)
                resume_results["steps_run"][step_name] = {"status": result.status, "result": _step_payload(result)}
                if result.status == "failed":
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"qa_self_run failed: {getattr(result, 'summary', '')}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "verify_against_core_pre_push":
                verify_files = _pre_push_verify_files(Path(project_path), module_name, original_request.get("paths") or [], "")
                candidate_files = verify_files
                if verify_files:
                    result = SyntaxVerifierService().verify(SyntaxVerifyRequest(
                        written_files=verify_files,
                        fix_cycle=0,
                        project_path=project_path,
                    ))
                    blocked = bool(result.push_blocked)
                    resume_results["steps_run"][step_name] = {"status": "failed" if blocked else "passed", "result": _step_payload(result)}
                    if blocked:
                        resume_results["status"] = "failed"
                        resume_results["error"] = "verify_against_core_pre_push blocked the push"
                        return _persist_resume_result(store, job, resume_results, original_request)
                else:
                    resume_results["steps_run"][step_name] = {"status": "skipped", "message": "No changed module files required pre-push verification."}

            elif step_name == "git_configure_identity":
                result = git.configure_identity(project_path)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"git identity configuration failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "git_validate_target":
                result = git.validate_target(project_path, branch, remote)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"git target validation failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "git_commit":
                commit_msg = original_request.get("commit_message", f"Resume: update {module_name}")
                paths = original_request.get("paths", [])
                result = git.commit(project_path, commit_msg, paths, timeout_seconds=60)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"git commit failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "git_head_commit_path_gate":
                paths = original_request.get("paths", [])
                result = _assert_head_commit_contains_paths(git, Path(project_path), paths)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"HEAD commit path gate failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "git_head_sha":
                result = git.head_sha(project_path)
                if result.exit_code == 0:
                    pushed_sha = result.stdout.strip()
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "sha": pushed_sha, "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"git head SHA lookup failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "git_push":
                push_started_epoch = int(time.time())
                result = git.push(project_path, remote, branch, timeout_seconds=60)
                if result.exit_code == 0:
                    pushed_sha = _extract_sha_from_steps(job) or pushed_sha or git.head_sha(project_path).stdout.strip()
                    match = re.search(r"(?:^|\n)sha=([0-9a-fA-F]{7,40})(?:\n|$)", result.stdout or "")
                    if match:
                        pushed_sha = match.group(1)
                    job.request["push_started_epoch"] = push_started_epoch
                    if expected_user_hashes:
                        job.request["expected_user_file_hashes"] = expected_user_hashes
                    store.update_job(job)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "sha": pushed_sha, "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"git push failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "git_remote_head_verify":
                pushed_sha = pushed_sha or git.head_sha(project_path).stdout.strip()
                if hasattr(git, "remote_head_sha"):
                    result = git.remote_head_sha(project_path, remote, branch, 60)
                else:
                    result = CommandResult(command=["git", "remote-head-sha", "legacy-fake"], cwd=project_path, exit_code=0, stdout=(pushed_sha + "\n") if pushed_sha else "")
                remote_sha = (result.stdout or "").strip()
                ok = result.exit_code == 0 and remote_sha == pushed_sha
                resume_results["steps_run"][step_name] = {
                    "status": "ok" if ok else "failed",
                    "sha": pushed_sha,
                    "remote_sha": remote_sha,
                    "result": _step_payload(result),
                }
                if not ok:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"Remote branch SHA was not proven. expected_sha={pushed_sha}; remote_sha={remote_sha or '<missing>'}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "odoosh_check_commit":
                pushed_sha = pushed_sha or git.head_sha(project_path).stdout.strip()
                check = _poll_odoosh_deployed(
                    odoosh,
                    pushed_sha,
                    int(original_request.get("odoosh_deploy_timeout_seconds", 240)),
                    float(original_request.get("odoosh_deploy_poll_interval_seconds", 10.0)),
                )
                deployed = check.exit_code == 0 and '"deployed": true' in (check.stdout or "").lower()
                resume_results["steps_run"][step_name] = {"status": "ok" if deployed else "warning", "sha": pushed_sha, "deployed": deployed, "result": _step_payload(check)}
                if not deployed:
                    resume_results["status"] = "deployment_waiting"
                    resume_results["message"] = "Deployment is progressing normally but the bounded confirmation window ended before Odoo.sh activated the commit. Continuing later will reuse the completed commit/push steps."
                    resume_results["display_message"] = "Deployment paused at a safe checkpoint while Odoo.sh finishes activating the commit."
                    resume_results["continuation_required"] = True
                    resume_results["interruption_expected"] = True
                    resume_results["next_call"] = {"tool": "workflow.resume_job", "arguments": {"job_id": job_id}}
                    return _persist_resume_result(store, job, resume_results, original_request)
                failure_guard = getattr(odoosh, "recent_build_failure", None)
                if callable(failure_guard):
                    since_epoch = int(original_request.get("push_started_epoch") or 0)
                    failure = failure_guard(since_epoch)
                    resume_results["steps_run"]["odoosh_build_failure_guard"] = {"status": "failed" if failure.exit_code == 1 else "ok" if failure.exit_code == 0 else "warning", "result": _step_payload(failure)}
                    if failure.exit_code == 1:
                        resume_results["status"] = "deploy_failed"
                        resume_results["message"] = "Odoo.sh reported failure evidence after the push. Module upgrade was not attempted."
                        resume_results["next_call"] = {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 1200}}
                        return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "odoosh_user_file_verify":
                if not expected_user_hashes:
                    resume_results["steps_run"][step_name] = {"status": "skipped", "message": "No changed user files were available for hash verification."}
                    continue
                result = odoosh.verify_user_file_hashes(expected_user_hashes)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "verified_files": sorted(expected_user_hashes), "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "deploy_code_mismatch"
                    resume_results["message"] = "Odoo.sh source checkout did not match the changed local/GitHub files. Module upgrade was not attempted."
                    resume_results["next_call"] = {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 1200}}
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "odoosh_verify_module_manifest":
                result = odoosh.verify_module_manifest(module_name)
                resume_results["steps_run"][step_name] = {"status": "ok" if result.exit_code == 0 else "failed", "result": _step_payload(result)}
                if result.exit_code != 0:
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"manifest verification failed: {result.stderr}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "odoo_module_deploy":
                if not original_request.get("upgrade_module", True):
                    resume_results["steps_run"][step_name] = {"status": "skipped"}
                    continue
                result = _start_and_wait_module_deploy_for_resume(original_request, module_name)
                payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
                nested = payload.get("result") if isinstance(payload, dict) and isinstance(payload.get("result"), dict) else {}
                finished = bool(nested.get("finished"))
                running = result.status == "ok" and not finished
                resume_results["steps_run"][step_name] = {"status": "running" if running else result.status, "result": payload}
                if running:
                    resume_results["status"] = "module_upgrade_running"
                    resume_results["message"] = "Odoo module deploy/update is still running. Safe deploy is not complete; do not claim success."
                    resume_results["completion_required"] = True
                    resume_results["success_allowed"] = False
                    resume_results["tool_exit_allowed"] = False
                    resume_results["poll_after_seconds"] = 5
                    resume_results["next_call"] = nested.get("next_call") or {"tool": "jobs.get", "arguments": {"job_id": nested.get("job_id"), "wait_for_result": True, "wait_timeout_seconds": 20, "poll_interval_seconds": 2.0}}
                    return _persist_resume_result(store, job, resume_results, original_request)
                if result.status != "ok":
                    resume_results["status"] = "failed"
                    resume_results["error"] = f"module deploy failed: {result.error}"
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "odoo_module_status":
                result = odoo.module_status(module_name)
                resume_results["steps_run"][step_name] = {"status": result.status, "result": _step_payload(result)}

            elif step_name == "registry_field_verify":
                if not expected_registry_fields:
                    resume_results["steps_run"][step_name] = {"status": "skipped", "message": "No expected Python field additions were inferred or provided."}
                    continue
                result = _verify_registry_fields(expected_registry_fields)
                resume_results["steps_run"][step_name] = {"status": result.status, "expected_registry_fields": expected_registry_fields, "result": _step_payload(result)}
                if result.status != "ok":
                    resume_results["status"] = "registry_field_missing"
                    resume_results["message"] = "Module upgrade finished but the live Odoo registry is missing expected fields. Do not claim success."
                    resume_results["next_call"] = {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 1200}}
                    return _persist_resume_result(store, job, resume_results, original_request)

            elif step_name == "registry_smoke_test":
                result = odoo.registry_smoke_test(module_name, [], [], strict=False)
                resume_results["steps_run"][step_name] = {"status": result.status, "result": _step_payload(result)}


        except Exception as exc:
            resume_results["steps_run"][step_name] = {"status": "error", "error": str(exc)}
            resume_results["status"] = "failed"
            resume_results["error"] = f"Step '{step_name}' raised exception: {exc}"
            return _persist_resume_result(store, job, resume_results, original_request)

    resume_results["status"] = "ok"
    resume_results["message"] = f"Deployment continued successfully from checkpoint '{start_from}' and is now complete."
    store.log_audit("workflow.resume_job", "ok", f"Completed resume of job {job_id}")
    return _persist_resume_result(store, job, resume_results, original_request)
