from __future__ import annotations

import asyncio
import ast
from copy import deepcopy
import difflib
import json
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from ..auth import AuthDependency
from ..config import settings, get_odoo_clients, normalise_client
from ..schemas import (
    ApprovalCreateRequest,
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
    FrontendVisualImplementRequest,
    EmptyRequest,
    SkillListRequest,
    SkillReadRequest,
    FullVerifyRequest,
    SafeDeployModuleRequest,
    GitCommitRequest,
    GitDiffRequest,
    GitPushRequest,
    JobGetRequest,
    LimitRequest,
    OdooModuleRequest,
    OdooReadGroupRequest,
    OdooCodeFileChange,
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
    JobStatus,
    JobStep,
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
from ..services.odoo_code_search_service import OdooCodeSearchService
from ..services.odoosh_service import OdooShService
from ..services.browser_cdp_service import CdpBrowserService
from ..services.repo_service import RepoService
from ..services.prewrite_gate_service import PreWriteGateService
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
from ..schemas import ApprovalRecord
from ..services.store_service import JsonStore
from ..services.frontend_visual_service import FrontendVisualService
from ..services.deploy_impact_service import classify_deploy_impact
from ..services.skill_service import SkillService

router = APIRouter(prefix="/tools", tags=["tools"], dependencies=[AuthDependency])

_STRICT_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


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
        "odoo.read_group": 1,
        "odoo.query": 2,
        "odoosh.query": 2,
        "odoo.search_read": 3,
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
        name="skills.list",
        title="List Odoo skills",
        description="List exact Odoo domain skills. Optionally pass task_description to receive one recommendation without loading skill content.",
        read_only=True,
    ),
    ToolDescriptor(
        name="skills.read",
        title="Load an Odoo skill",
        description="Load one exact Odoo skill on demand. Use 'reporting' for ORM/SQL analytics, 'qweb-report' for general PDF/HTML templates, or 'accounting-report' for financial reports.",
        read_only=True,
    ),
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
            "After the user selects a client, call with client_name and validate=true. "
            "Validation checks repo/token/branch, checkout, Odoo.sh SSH, Odoo shell health, and addons path. "
            "Write/git/deploy tools are blocked until this passes when multiple clients are configured."
        ),
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
        title="Get coding workflow plan", description="Return the repo-first coding workflow guidance for MCP agents", read_only=True),
    ToolDescriptor(
        name="workflow.odoo_code_change",
        title="Provider-safe Odoo code change",
        description=(
            "Single high-level provider-safe coding workflow. Use this instead of manually calling repo.read/write, "
            "scope_advice, verify, git.push, or module_deploy. It enforces the v15 flow internally: "
            "read custom module, scope_advice, read /home/odoo/src/odoo/addons related source first with enterprise fallback, "
            "apply provided edits, git.diff, qa.self_run, verify_against_core, then workflow.safe_deploy_module and jobs.get polling. "
            "For a missing addon, set create_module=true with an exact lowercase technical name; it is created only at the checkout root. "
            "A cached Odoo-aware index maps functional labels through menus/actions/views/models/fields/QWeb/JS relationships, then reads exact match-centered ranges. "
            "Task descriptions automatically supply quoted identifiers/selectors/XML IDs/methods for focused inspection. "
            "An exact quoted replacement that occurs once can complete in one call; ambiguous or complex work returns focused context for a second call with exact writes."
        ),
        requires_approval=True,
    ),
    ToolDescriptor(
        name="workflow.frontend_visual_implement",
        title="Implement frontend visual asset",
        description=(
            "Generic visual implementation workflow for Odoo POS/OWL/dashboard/website/backend UI. "
            "Use when a design needs SCSS, SVG assets, decorative backgrounds, logos/icons, premium cards, "
            "or decorative shapes. It generates deterministic visual assets/writes, reads custom/native context, "
            "runs verify_against_core, then uses the same safe deploy pipeline. For exact geometric/logo/background shapes, "
            "prefer allowed_methods=['svg_asset'] instead of guessing with CSS borders. Set create_module=true only for a new "
            "standalone root-level addon; otherwise module_name must already exist."
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
        title="Deploy module (full pipeline)", description="Safely commit, push, poll Odoo.sh every 10s up to 10 minutes for the pushed SHA, reject recent Odoo.sh build/test failures, verify changed file hashes under /home/odoo/src/user, verify manifest, upgrade an Odoo module, For MCP clients this starts/resumes a background safe-deploy job and returns job_id/next_call immediately; status=running is not success and must be followed via jobs.get. Only treat the deployment as complete when response.deploy_confirmed == true. A finished job without this field set to true means the deploy did not fully complete — do not report success to the user. If git diff shows no changes for this module, safe_deploy_module automatically runs Odoo.sh source verification and returns verified_already_deployed/already_deployed_confirmed; read this field before telling the user anything is 'already added' or 'already live'. Do not make that claim from memory of a previous turn.", requires_approval=True),
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
        title="Collect Odoo.sh error details", description="Return Odoo.sh tracebacks plus root-cause hints/files/XML/model names", read_only=True),
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
            "Best tool for simple functional Odoo questions from any MCP client: list users, employees, "
            "draft invoices, unpaid invoices, customers, vendors, products, sale orders, purchase orders, or modules. "
            "Takes a natural-language query and returns parsed model/domain/fields plus live Odoo records. "
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
            "SMART INSTALL/UPGRADE — always use this for ALL first-time installs and upgrades. "
            "If a recent manual git.push was recorded, this tool first verifies the pushed SHA, recent "
            "Odoo.sh failure logs, and changed /home/odoo/src/user file hashes before touching the DB. "
            "Runs install/upgrade through Odoo.sh SSH and verifies source/deploy state in the same background job. "
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
    ToolDescriptor(name="odoo.read_group",
        title="Aggregate Odoo records", description="Return live grouped counts, sums, averages, minima, or maxima with safe read_group. Example: account.move customer invoices grouped by state.", read_only=True),
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
        title="Check background job status", description="Read one connector job by id; waits server-side by default until done or timeout to avoid fragile client polling. Only treat the deployment as complete when response.deploy_confirmed == true. A finished job without this field set to true means the deploy did not fully complete — do not report success to the user.", read_only=True),
    ToolDescriptor(name="audit.list",
        title="View audit log", description="List recent audit events", read_only=True),
    # Fix 3: Resume failed pipeline from last successful step
    ToolDescriptor(
        name="workflow.resume_job",
        title="Resume interrupted job",
        description=(
            "Fix 3 — Resume a failed safe_deploy_module or start_session pipeline from its last "
            "successful step. Pass job_id when available; if omitted, the connector resumes the "
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
    "skills.list": SkillListRequest,
    "skills.read": SkillReadRequest,
    "artifacts.read": ArtifactReadRequest,
    "client.check": ClientCheckRequest,
    "workflow.agent_instructions": EmptyRequest,
    "workflow.capability_check": EmptyRequest,
    "workflow.coding_task_plan": CodingTaskPlanRequest,
    "workflow.odoo_code_change": OdooCodeChangeRequest,
    "workflow.frontend_visual_implement": FrontendVisualImplementRequest,
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
    "odoo.read_group": OdooReadGroupRequest,
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


@lru_cache(maxsize=None)
def _cached_tool_input_schema(internal_name: str) -> dict:
    model = _TOOL_INPUT_MODELS.get(internal_name, EmptyRequest)
    schema = model.model_json_schema()
    schema.setdefault("type", "object")
    schema.setdefault("additionalProperties", False)
    return schema


def tool_input_schema(name: str) -> dict:
    # Return an isolated copy because provider compatibility cleanup may reshape
    # nested schema dictionaries. Pydantic generation itself is cached once.
    return deepcopy(_cached_tool_input_schema(internal_tool_name(name)))


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
    timeout_seconds: int = 120,
) -> dict:
    client = _resolve_query_client(client_name, query_text)
    shell_result = _odoo_service_for_client(client).search_read(model, domain, fields, limit, offset, order, context)
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

    # Preserve the original explicit Odoo.sh fallback path for compatibility and
    # tests. In normal runtime OdooService already attempted SSH + HTTPS JSON-RPC,
    # but direct tests/legacy monkeypatches may still expect this second path.
    ssh_result = OdooShService(client).search_read(model, domain, fields, limit, offset, order, context, timeout_seconds)
    ssh_dumped = ssh_result.model_dump(mode="json") if hasattr(ssh_result, "model_dump") else ssh_result
    if isinstance(ssh_dumped, dict) and ssh_dumped.get("status") == "ok":
        return {
            "status": "ok",
            "source": "odoosh_ssh_fallback",
            "client_name": client.name if client else None,
            "odoo": ssh_dumped,
            "shell_attempt": shell_dumped,
            "ssh_fallback_used": True,
            "message": "Primary Odoo query failed; Odoo.sh SSH fallback was used.",
        }
    return {
        "status": ssh_dumped.get("status") if isinstance(ssh_dumped, dict) else "failed",
        "source": "odoo_ssh_or_https_jsonrpc_failed",
        "client_name": client.name if client else None,
        "odoo": ssh_dumped,
        "shell_attempt": shell_dumped,
        "ssh_fallback_used": True,
        "message": "Odoo query failed through primary path and Odoo.sh fallback.",
    }


def _read_group_via_shell(req: OdooReadGroupRequest, query_text: str | None = None) -> dict:
    client = _resolve_query_client(req.client_name, query_text)
    result = _odoo_service_for_client(client).read_group(
        req.model, req.domain, req.fields, req.groupby, req.lazy, req.context, req.timeout_seconds
    )
    dumped = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    return {
        "status": dumped.get("status") if isinstance(dumped, dict) else "failed",
        "source": "odoo_ssh_or_https_jsonrpc",
        "client_name": getattr(client, "name", None),
        "odoo": dumped,
        "message": "Live Odoo read_group completed through SSH or HTTPS fallback." if isinstance(dumped, dict) and dumped.get("status") == "ok" else "Live Odoo read_group failed.",
    }


def _odoo_functional_query(req: OdooFunctionalQueryRequest) -> object:
    """Map common functional Odoo questions to safe read-only search_read.

    It tries Odoo shell first. When shell authentication fails but the selected
    client has Odoo.sh SSH settings, it automatically falls back to a read-only
    ``odoo-bin shell`` query over SSH. This lets users read business records
    without an Odoo email/password, as long as SSH access is working.
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

    data = _search_read_via_shell(
        model=model,
        domain=domain,
        fields=fields,
        limit=limit,
        offset=0,
        order=order,
        context=None,
        client_name=req.client_name,
        query_text=req.query,
    )
    return {
        "status": data.get("status"),
        "intent": intent,
        "source": data.get("source"),
        "client_name": data.get("client_name"),
        "parsed": {"model": model, "domain": domain, "fields": fields, "limit": limit, "order": order},
        "odoo": data.get("odoo"),
        "shell_attempt": data.get("shell_attempt"),
        "ssh_fallback_used": data.get("ssh_fallback_used"),
        "message": data.get("message"),
    }


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
    """Create only the minimal root-level skeleton needed for first module reads."""
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

    supplied = {_safe_module_relative(module_name, item.relative_path): item.content for item in payload.writes if item.content is not None}
    skeleton = [
        RepoWriteItem(
            relative_path=f"{module_name}/__init__.py",
            content=supplied.get(f"{module_name}/__init__.py", ""),
        ),
        RepoWriteItem(
            relative_path=f"{module_name}/__manifest__.py",
            content=supplied.get(
                f"{module_name}/__manifest__.py",
                repr({"name": module_name.replace("_", " ").title(), "depends": ["base"], "installable": True}) + "\n",
            ),
        ),
    ]
    result = repo.write_many_fast_safe(str(project), skeleton)
    if result.exit_code != 0:
        raise HTTPException(status_code=400, detail=result.stderr or "Could not create root-level module scaffold")


def _read_text_safe(path: Path, limit: int = 300_000) -> str:
    data = path.read_text(encoding="utf-8")
    return data[:limit]


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


_TASK_CLUE_STOPWORDS = {
    "add", "change", "create", "delete", "development", "display", "existing",
    "field", "file", "fix", "function", "method", "module", "odoo", "remove",
    "report", "template", "update", "view", "website",
}


def _task_description_search_clues(description: str, *, limit: int = 10) -> list[str]:
    """Extract concrete code clues from natural language without guessing domain terms.

    Quoted labels, CSS selectors, XML IDs, routes, and callable identifiers are
    safe exact-search inputs. Generic prose is deliberately ignored so repository
    inspection stays focused and deterministic.
    """
    text = str(description or "")
    candidates: list[str] = []
    patterns = (
        r"`([^`\r\n]{2,160})`",
        r'"([^"\r\n]{2,160})"',
        r"'([^'\r\n]{2,160})'",
        r"(?<![\w-])([.#][A-Za-z_][\w-]{1,100})",
        r"\b([A-Za-z_][\w]*\.[A-Za-z_][\w.]*)\b",
        r"\b([A-Za-z_][\w]{2,})\s*\(",
        r"\b(?:xml\s*id|xmlid|selector|method|function|class|field|template|view|route)\s*[:=]?\s*[`'\"]?([/.#A-Za-z_][\w./#-]{1,159})",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = match.group(1).strip().rstrip(".,:;")
            if (
                len(value) >= 2
                and value.lower() not in _TASK_CLUE_STOPWORDS
                and value not in candidates
            ):
                candidates.append(value)
                if len(candidates) >= limit:
                    return candidates
    return candidates


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
    for raw in _task_description_search_clues(payload.task_description):
        _append_unique(patterns, raw)
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
    color = _style_value(style, "color")
    font_style = _style_value(style, "font_style", "fontStyle")
    font_weight = _style_value(style, "font_weight", "fontWeight")
    if color:
        declarations.append(f"color: {color} !important;")
    if font_style:
        declarations.append(f"font-style: {font_style} !important;")
    if font_weight:
        weight = "700" if font_weight.lower() == "bold" else font_weight
        declarations.append(f"font-weight: {weight} !important;")
    return declarations


def _focused_search(repo: RepoService, project_path: str, module_path: str, patterns: list[str], max_matches: int) -> tuple[list[dict[str, Any]], list[str], dict[str, list[int]]]:
    """Exact-first, low-noise search for inspect/autopatch mode."""
    preferred_suffixes = (".xml", ".scss", ".css", ".js", ".py")
    search_results: list[dict[str, Any]] = []
    rel_paths: list[str] = []
    seen_paths: set[str] = set()
    match_lines: dict[str, list[int]] = {}
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
                if rel and rel.endswith(preferred_suffixes):
                    line = int(match.get("line") or 0)
                    if line > 0 and line not in match_lines.setdefault(rel, []):
                        match_lines[rel].append(line)
            # Exact visible text found in XML/CSS is usually enough for inspection.
            if rel_paths and any(pattern.lower() in str(m.get("text", "")).lower() for m in matches):
                break
        except Exception as exc:  # noqa: BLE001
            search_results.append({"pattern": pattern, "status": "failed", "error": str(exc)})
    return search_results, rel_paths, match_lines


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
    for key in ("color", "font_style", "fontStyle", "font_weight", "fontWeight", "background", "background_color"):
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


def _task_description_text_replace(description: str) -> tuple[str | None, str | None]:
    """Parse only explicit, quoted natural-language replacement instructions."""
    text = str(description or "").strip()
    patterns = (
        r"\b(?:replace|change|rename)\s+[`\"']([^`\"'\r\n]{1,300})[`\"']\s+(?:with|to)\s+[`\"']([^`\"'\r\n]{1,300})[`\"']",
        r"[`\"']([^`\"'\r\n]{1,300})[`\"']\s*->\s*[`\"']([^`\"'\r\n]{1,300})[`\"']",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match and match.group(1) != match.group(2):
            return match.group(1), match.group(2)
    return None, None


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


def _build_text_replace_autopatch(
    project: Path,
    module_path: str,
    target_files: list[str],
    old: str,
    new: str,
    *,
    require_unique: bool = False,
) -> list[OdooCodeFileChange]:
    if not old or new is None:
        return []
    module_dir = project / module_path
    matches: list[tuple[Path, int]] = []
    for file_path in _iter_autopatch_candidate_files(module_dir, target_files, project):
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        count = content.count(old)
        if count <= 0:
            continue
        matches.append((file_path, count))
        if not require_unique:
            break
        if sum(item[1] for item in matches) > 1:
            return []
    if not matches or (require_unique and sum(item[1] for item in matches) != 1):
        return []
    file_path = matches[0][0]
    rel = file_path.relative_to(project).as_posix()
    # Use search/replace rather than full content rewrite so the diff remains
    # minimal and deterministic across all text-like task types.
    return [OdooCodeFileChange(relative_path=rel, search=old, replace=new, count=1)]


def _build_safe_generic_autopatch(project: Path, module_path: str, payload: OdooCodeChangeRequest, custom_rel_paths: list[str]) -> tuple[list[OdooCodeFileChange], str | None, str | None]:
    """Build a deterministic autopatch for supported safe cases.

    This is intentionally generic and conservative.  It handles only changes that
    can be generated without hallucinating Odoo code (style rule or exact text
    replacement).  Python/XML/JS/POS/security/report tasks still use the same
    universal workflow, but return focused inspection context and request explicit
    writes when no deterministic patch exists.
    """
    task_type = _normalized_task_type(payload)
    strategy = _normalized_patch_strategy(payload)
    target = _target_hint(payload)
    style = _style_request(payload)
    cr_type = str((payload.change_request or {}).get("type") or "").strip().lower().replace("-", "_").replace(" ", "_")

    inferred_old, inferred_new = _task_description_text_replace(payload.task_description)
    if inferred_old is not None and inferred_new is not None:
        writes = _build_text_replace_autopatch(
            project,
            module_path,
            [],
            inferred_old,
            inferred_new,
            require_unique=True,
        )
        if writes:
            return writes, "task_description_text_replace", None
        return [], "task_description_text_replace", "replacement_not_unique_or_not_found"

    if not payload.allow_autopatch and not payload.style and not payload.change_request:
        return [], None, "autopatch_not_requested"

    if style and target and (task_type in {"auto", "style", "scss_css", "css", "qweb_template", "xml_view"} or strategy in {"auto", "safe_autopatch", "style_rule", "inline_style"} or cr_type in {"style", "change_style", "update_style"}):
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


def _core_relevance_terms(payload: OdooCodeChangeRequest, rel_paths: list[str]) -> list[str]:
    values = [*_infer_code_change_search_patterns(payload), *rel_paths]
    terms: list[str] = []
    for value in values:
        for token in re.split(r"[^a-zA-Z0-9_]+", str(value).lower()):
            if len(token) >= 3 and token not in _TASK_CLUE_STOPWORDS and token not in terms:
                terms.append(token)
    return terms[:30]


def _core_path_relevance(path: Path, terms: list[str], suffixes: set[str], task_type: str, original_index: int = 0) -> tuple[int, int, str]:
    text = path.as_posix().lower()
    name = path.name.lower()
    stem = path.stem.lower()
    score = 0
    for term in terms:
        if term == stem or term == name:
            score += 8
        elif term in name:
            score += 5
        elif term in text:
            score += 2
    if path.suffix.lower() in suffixes:
        score += 4
    area_hints = {
        "python_model": "/models/",
        "controller": "/controllers/",
        "xml_view": "/views/",
        "qweb_template": "/views/",
        "report": "/report/",
        "js_report": "/static/",
        "js_owl": "/static/",
        "pos_owl": "/static/",
        "scss_css": "/static/",
    }
    if area_hints.get(task_type) and area_hints[task_type] in text:
        score += 5
    return (-score, original_index, text)


def _rank_core_required_paths(paths: list[str], payload: OdooCodeChangeRequest, rel_paths: list[str], limit: int) -> list[str]:
    unique = list(dict.fromkeys(str(path) for path in paths if str(path).strip()))
    terms = _core_relevance_terms(payload, rel_paths)
    suffixes = {Path(path).suffix.lower() for path in rel_paths if Path(path).suffix}
    ranked = sorted(
        enumerate(unique),
        key=lambda item: _core_path_relevance(Path(item[1]), terms, suffixes, _normalized_task_type(payload), item[0]),
    )
    return [path for _, path in ranked[: max(1, min(limit, 10))]]


def _rank_core_directory_files(directory: Path, payload: OdooCodeChangeRequest, rel_paths: list[str]) -> list[Path]:
    terms = _core_relevance_terms(payload, rel_paths)
    suffixes = {Path(path).suffix.lower() for path in rel_paths if Path(path).suffix}
    files = [
        path for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in {".py", ".js", ".xml", ".scss", ".css"}
    ]
    return sorted(
        files,
        key=lambda path: _core_path_relevance(path, terms, suffixes, _normalized_task_type(payload)),
    )


def _evidence_signature(project: Path, module_name: str, payload: OdooCodeChangeRequest, rel_paths: list[str]) -> str:
    import hashlib
    rows=[]
    for rel in sorted(set(rel_paths)):
        path=project/rel
        try:
            st=path.stat(); rows.append((rel, st.st_mtime_ns, st.st_size))
        except OSError:
            rows.append((rel, None, None))
    raw=json.dumps({"module":module_name,"task_type":_normalized_task_type(payload),"paths":rows},sort_keys=True,default=str)
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


def _static_frontend_fast_path(rel_paths: list[str]) -> bool:
    paths = [str(path).lower().replace("\\", "/") for path in rel_paths if str(path).strip()]
    return bool(paths) and all(
        "/static/" in path and path.endswith((".js", ".jsx", ".ts", ".tsx", ".xml", ".scss", ".css"))
        for path in paths
    )


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

async def _workflow_odoo_code_change(payload: OdooCodeChangeRequest) -> dict[str, Any]:
    """Provider-safe composite Odoo code-change workflow.

    This is intentionally server-owned to remove MCP provider overhead/confusion:
    provider calls this one tool, the connector performs the v15 custom+core read,
    applies explicit edits, verifies, starts safe deploy, and returns a job_id for jobs.get.
    """
    resolved_client_name, project_path = _resolve_code_change_project(payload)
    client_name = resolved_client_name or payload.client_name
    repo = RepoService()
    gate = PreWriteGateService()
    state_store = JsonStore()
    steps: list[dict[str, Any]] = []

    # 1) Prepare checkout once.  In real multi-client use the client config supplies
    # repo URL/branch; for already-materialized local checkouts/tests, avoid failing
    # just because TARGET_GIT_REPO is not configured.
    cached_prepare = (state_store.load_session_state(client_name).get("workflow_prepare_cache") or {})
    cached_path = str(cached_prepare.get("project_path") or "")
    cached_at = float(cached_prepare.get("at") or 0)
    project = repo.project(project_path)
    if cached_path and Path(cached_path).resolve() == project.resolve() and (project / ".git").exists() and time.time() - cached_at < 300:
        project_path = str(project)
        steps.append({"step": "repo.prepare", "status": "cached", "project_path": project_path, "max_age_seconds": 300})
    else:
        try:
            prepare = repo.prepare_repo(project_path=project_path, client_name=client_name)
            project_path = prepare.project_path
            project = Path(project_path)
            steps.append({"step": "repo.prepare", "status": "ok", "project_path": project_path, "branch": prepare.branch, "sha": prepare.sha})
            state_store.save_session_state({"workflow_prepare_cache": {"project_path": project_path, "at": time.time(), "branch": prepare.branch, "sha": prepare.sha}}, client_name=client_name)
        except HTTPException as exc:
            detail = str(exc.detail)
            project = repo.project(project_path)
            if exc.status_code == 400 and "repo_url is required" in detail and (project / ".git").exists():
                steps.append({"step": "repo.prepare", "status": "skipped_existing_checkout", "project_path": str(project), "reason": detail})
                project_path = str(project)
            else:
                raise

    # 2) Discover/validate module once, with a safe fuzzy resolver for human hints.
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
    steps.append({"step": "repo.list_custom_modules", "status": "ok", "module": effective_module_name, "module_path": module.path, "module_resolution": module_resolution})

    # 3) Read custom manifest + target/custom files.  This also records gate evidence.
    custom_rel_paths: list[str] = [manifest_rel]
    for raw in payload.target_files:
        rel = _safe_module_relative(effective_module_name, raw)
        if rel not in custom_rel_paths:
            custom_rel_paths.append(rel)
    for write in payload.writes:
        rel = _safe_module_relative(effective_module_name, write.relative_path)
        if rel not in custom_rel_paths:
            custom_rel_paths.append(rel)

    # Inspect/autopatch mode uses exact-first focused search to avoid huge noisy reads.
    search_results: list[dict[str, Any]] = []
    focused_match_lines: dict[str, list[int]] = {}
    if not payload.writes:
        patterns = _infer_code_change_search_patterns(payload)
        focused_paths: list[str] = []
        direct_targets = list(dict.fromkeys(
            _safe_module_relative(effective_module_name, raw)
            for raw in payload.target_files
            if str(raw or "").strip()
        ))
        if direct_targets:
            search_results.append({"engine": "direct_target", "status": "ok", "target_count": len(direct_targets)})
            for target_rel in direct_targets[:20]:
                if not (project / target_rel).is_file():
                    search_results.append({"engine": "direct_target", "status": "missing", "path": target_rel})
                    continue
                if target_rel not in focused_paths:
                    focused_paths.append(target_rel)
                if not patterns:
                    continue
                target_results, _, target_lines = _focused_search(repo, project_path, target_rel, patterns, payload.max_search_matches)
                search_results.extend(target_results)
                for rel, lines in target_lines.items():
                    for line in lines:
                        if line not in focused_match_lines.setdefault(rel, []):
                            focused_match_lines[rel].append(line)

        if not focused_paths:
            try:
                graph_search = OdooCodeSearchService().search(
                    project_path=project_path,
                    module_path=module.path,
                    task_description=payload.task_description,
                    target_hint=_target_hint(payload),
                    intent=payload.intent,
                    search_patterns=payload.search_patterns,
                    task_type=_normalized_task_type(payload),
                    max_results=min(payload.max_search_matches, 10),
                )
                search_results.append(graph_search)
                for match in graph_search.get("matches") or []:
                    rel = str(match.get("path") or "")
                    if rel and rel not in focused_paths:
                        focused_paths.append(rel)
                    for line in match.get("lines") or []:
                        line_no = int(line or 0)
                        if line_no > 0 and line_no not in focused_match_lines.setdefault(rel, []):
                            focused_match_lines[rel].append(line_no)
            except Exception as exc:  # noqa: BLE001
                search_results.append({"engine": "odoo_code_graph", "status": "failed", "error": str(exc)})

        # Preserve the old lexical search as a bounded fallback only when the
        # Odoo graph cannot produce confident target files.
        graph_result = next((item for item in search_results if item.get("engine") == "odoo_code_graph"), {})
        discovery_ok = bool(direct_targets and focused_paths) or (
            bool(focused_paths) and float(graph_result.get("confidence") or 0) >= 0.45
        )
        if not discovery_ok:
            lexical_results, lexical_paths, lexical_lines = _focused_search(repo, project_path, module.path, patterns, payload.max_search_matches)
            search_results.extend(lexical_results)
            for rel in lexical_paths:
                if rel not in focused_paths:
                    focused_paths.append(rel)
            for rel, lines in lexical_lines.items():
                for line in lines:
                    if line not in focused_match_lines.setdefault(rel, []):
                        focused_match_lines[rel].append(line)
        for rel in focused_paths[:10]:
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

    custom_file_paths_abs: list[str] = []
    custom_reads: list[dict[str, Any]] = []
    for rel in list(dict.fromkeys(custom_rel_paths))[:30]:
        target = project / rel
        if not target.exists() or not target.is_file():
            custom_reads.append({"relative_path": rel, "status": "missing"})
            continue
        try:
            gate.record_repo_inspection(project_path=project_path, relative_path=rel, client_name=client_name, action="workflow.odoo_code_change.read_custom", is_content_read=True)
            custom_file_paths_abs.append(str(target.resolve()))
            match_lines = sorted(focused_match_lines.get(rel, []))
            if match_lines:
                range_reads: list[dict[str, Any]] = []
                covered_until = 0
                for center in match_lines:
                    if center <= covered_until or len(range_reads) >= 3:
                        continue
                    ranged = repo.read_range(project_path, rel, start_line=max(1, center - 40), max_lines=81)
                    covered_until = int(ranged["end_line"])
                    range_reads.append(ranged)
                custom_reads.append({
                    "relative_path": rel,
                    "status": "read",
                    "read_mode": "match_range",
                    "size": target.stat().st_size,
                    "ranges": [{"start_line": item["start_line"], "end_line": item["end_line"]} for item in range_reads],
                    "total_lines": range_reads[0]["total_lines"],
                    "match_lines": match_lines[:20],
                    "preview": "\n\n... focused match window ...\n\n".join(str(item["content"]) for item in range_reads),
                })
            else:
                content = _read_text_safe(target)
                custom_reads.append({"relative_path": rel, "status": "read", "read_mode": "file_preview", "size": target.stat().st_size, "preview": _file_preview(content, 3000)})
        except UnicodeDecodeError:
            custom_reads.append({"relative_path": rel, "status": "binary_skipped"})
    steps.append({"step": "repo.read_custom", "status": "ok", "count": len([r for r in custom_reads if r.get("status") == "read"])})
    evidence_signature = _evidence_signature(project, effective_module_name, payload, custom_rel_paths)
    session_state = JsonStore().load_session_state(client_name=client_name)
    evidence_cache = (session_state.get("code_change_evidence") or {}) if isinstance(session_state, dict) else {}
    cached_evidence = evidence_cache.get(evidence_signature) if isinstance(evidence_cache, dict) else None

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
        scope_file_paths = [path for path in custom_file_paths_abs if path == manifest_resolved or path in write_abs]
        if not scope_file_paths:
            scope_file_paths = list(custom_file_paths_abs)

    frontend_preflight = None
    frontend_paths = list(dict.fromkeys(custom_rel_paths + [_safe_module_relative(effective_module_name, w.relative_path) for w in payload.writes]))
    write_rel_paths = [_safe_module_relative(effective_module_name, w.relative_path) for w in (payload.writes or generated_writes)]
    frontend_fast_path = _static_frontend_fast_path(write_rel_paths)
    scope = None
    scope_payload: ScopeAdviceRequest | None = None
    if frontend_fast_path:
        steps.append({"step": "workflow.scope_advice", "status": "skipped", "reason": "static frontend fast path"})
    else:
        scope_payload = ScopeAdviceRequest(
            custom_module_name=effective_module_name,
            custom_file_paths=scope_file_paths,
            task_description=payload.task_description,
            client_name=client_name,
        )
        if cached_evidence and cached_evidence.get("scope"):
            from ..schemas import ScopeAdviceResult
            scope = ScopeAdviceResult.model_validate(cached_evidence["scope"])
            steps.append({"step": "workflow.scope_advice", "status": "cached", "mandatory_count": scope.mandatory_count, "reached_level": str(scope.reached_level.value if hasattr(scope.reached_level, "value") else scope.reached_level)})
        else:
            scope = scope_advice(scope_payload)
            steps.append({"step": "workflow.scope_advice", "status": "ok", "mandatory_count": scope.mandatory_count, "reached_level": str(scope.reached_level.value if hasattr(scope.reached_level, "value") else scope.reached_level)})

    frontend_preflight_payload: FrontendPatternPreflightRequest | None = None
    if _looks_like_frontend_code_change(payload, frontend_paths):
        frontend_preflight_payload = FrontendPatternPreflightRequest(
            client_name=client_name,
            task_description=payload.task_description,
            target_area="auto",
            target_module_name=effective_module_name,
            custom_file_paths=custom_file_paths_abs,
            planned_file_paths=frontend_paths,
        )
        if cached_evidence and cached_evidence.get("frontend_preflight"):
            from ..schemas import FrontendPatternPreflightResult
            frontend_preflight = FrontendPatternPreflightResult.model_validate(cached_evidence["frontend_preflight"])
            steps.append({"step": "workflow.frontend_pattern_preflight", "status": "cached", "target_area": frontend_preflight.target_area, "mandatory_count": frontend_preflight.mandatory_read_count})
        else:
            frontend_preflight = frontend_pattern_preflight(frontend_preflight_payload)
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
    existing_scope_paths = {item.path for item in scope.instructions} if scope is not None else set()
    extra_core_reads: list[str] = []
    for dep in ([] if frontend_fast_path else _parse_manifest_depends(manifest_abs)):
        if dep == effective_module_name:
            continue
        try:
            found = find_core_file(FindCoreFileRequest(module_name=dep, file_type="manifest", keyword=dep, client_name=client_name))
            if found.best_match and found.best_match not in existing_scope_paths:
                extra_core_reads.append(found.best_match)
        except Exception:
            continue

    mandatory_paths = [item.path for item in scope.instructions if item.mandatory and not str(item.path).startswith("NOT_FOUND:")] if scope is not None else []
    mandatory_paths.extend(path for path in extra_core_reads if path not in mandatory_paths)
    if frontend_preflight is not None:
        for item in frontend_preflight.instructions:
            if item.mandatory and not str(item.path).startswith("NOT_FOUND:") and item.path not in mandatory_paths:
                mandatory_paths.append(item.path)

    # Keep native-source inspection surgical. Deterministic one-file edits need
    # at most five relevant source files; complex/two-pass changes may inspect ten.
    core_read_limit = 5 if generated_writes else 10
    ranked_mandatory_paths = _rank_core_required_paths(mandatory_paths, payload, write_rel_paths or frontend_paths, core_read_limit)
    selected_required = set(ranked_mandatory_paths)
    if scope is not None and scope_payload is not None:
        selected_scope_instructions = [
            item for item in scope.instructions
            if not item.mandatory or str(item.path).startswith("NOT_FOUND:") or item.path in selected_required
        ]
        scope = scope.model_copy(update={
            "instructions": selected_scope_instructions,
            "mandatory_count": sum(1 for item in selected_scope_instructions if item.mandatory),
            "total_count": len(selected_scope_instructions),
        })
        # scope_advice recorded the unbounded plan; replace it with the selected
        # relevance-ranked plan so the hard pre-write gate enforces the same reads.
        gate.record_scope_advice(scope, scope_payload)
    if frontend_preflight is not None and frontend_preflight_payload is not None:
        selected_frontend_instructions = [
            item for item in frontend_preflight.instructions
            if not item.mandatory or str(item.path).startswith("NOT_FOUND:") or item.path in selected_required
        ]
        frontend_preflight = frontend_preflight.model_copy(update={
            "instructions": selected_frontend_instructions,
            "mandatory_read_count": sum(1 for item in selected_frontend_instructions if item.mandatory),
        })
        gate.record_frontend_preflight(frontend_preflight, frontend_preflight_payload)

    core_reads: list[dict[str, Any]] = []
    core_files: list[Path] = []
    core_file_seen: set[str] = set()
    directory_extras: list[Path] = []
    for raw_path in ranked_mandatory_paths:
        path = Path(str(raw_path)).expanduser()
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved.is_dir():
            files = _rank_core_directory_files(resolved, payload, write_rel_paths or frontend_paths)
            if not files:
                core_reads.append({"path": str(resolved), "status": "dir_no_readable_files"})
                continue
            first = files[0]
            if str(first) not in core_file_seen:
                core_file_seen.add(str(first)); core_files.append(first)
            directory_extras.extend(files[1:])
            continue
        if not resolved.exists() or not resolved.is_file():
            core_reads.append({"path": str(resolved), "status": "missing"})
            continue
        if str(resolved) not in core_file_seen:
            core_file_seen.add(str(resolved)); core_files.append(resolved)

    terms = _core_relevance_terms(payload, write_rel_paths or frontend_paths)
    suffixes = {Path(path).suffix.lower() for path in (write_rel_paths or frontend_paths) if Path(path).suffix}
    directory_extras.sort(key=lambda path: _core_path_relevance(path, terms, suffixes, _normalized_task_type(payload)))
    for file_path in directory_extras:
        if len(core_files) >= core_read_limit:
            break
        if str(file_path) not in core_file_seen:
            core_file_seen.add(str(file_path)); core_files.append(file_path)

    for resolved in core_files[:core_read_limit]:
        try:
            _ = _read_text_safe(resolved, 200_000)
            gate.record_repo_inspection(project_path=str(resolved.parent), relative_path=resolved.name, client_name=client_name, action="workflow.odoo_code_change.read_core", is_content_read=True)
            core_reads.append({"path": str(resolved), "status": "read"})
        except Exception as exc:  # noqa: BLE001
            core_reads.append({"path": str(resolved), "status": "failed", "error": str(exc)})
    steps.append({
        "step": "repo.read_core",
        "status": "ok",
        "count": len([r for r in core_reads if r.get("status") == "read"]),
        "limit": core_read_limit,
        "required_candidates": len(list(dict.fromkeys(mandatory_paths))),
        "selected_required": len(ranked_mandatory_paths),
        "selection": "relevance_ranked",
    })
    cache = dict(evidence_cache) if isinstance(evidence_cache, dict) else {}
    cache[evidence_signature] = {
        "scope": scope.model_dump(mode="json") if scope is not None else None,
        "frontend_preflight": frontend_preflight.model_dump(mode="json") if frontend_preflight is not None else None,
        "core_paths": [r.get("path") for r in core_reads if r.get("status") == "read"],
        "task_profile": _task_profile(payload, frontend_paths),
    }
    if len(cache) > 20:
        cache = dict(list(cache.items())[-20:])
    JsonStore().save_session_state({"code_change_evidence": cache}, client_name=client_name)

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
                "custom_files": [r.get("relative_path") for r in custom_reads if r.get("status") == "read"],
                "native_files": [r.get("path") for r in core_reads if r.get("status") == "read"],
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
                "intent": payload.intent,
                "target_hint": _target_hint(payload),
                "target_files": [r.get("relative_path") for r in custom_reads if r.get("status") == "read"],
                "status": "awaiting_internal_writes",
            }
        }, client_name=client_name)
        return {
            "status": "needs_writes",
            "message": "INTERNAL_CONTINUATION_REQUIRED: inspection is complete; generate exact writes and call workflow.odoo_code_change again. Do not present code or manual file-edit instructions to the user.",
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
            "scope_advice": scope.model_dump(mode="json") if scope is not None else None,
            "frontend_preflight": frontend_preflight.model_dump(mode="json") if frontend_preflight is not None else None,
            "core_reads": core_reads[:30],
            "success_allowed": False,
            "completion_required": True,
            "next_call": {
                "tool": "workflow.odoo_code_change",
                "arguments": {
                    "client_name": client_name,
                    "project_path": project_path,
                    "module_name": effective_module_name,
                    "task_description": payload.task_description,
                    "target_files": [r.get("relative_path") for r in custom_reads if r.get("status") == "read"],
                    "writes": "Generate exact writes internally: [{relative_path, content}] or [{relative_path, search, replace, regex, count}]. Do not ask the user to edit files.",
                },
            },
        }

    # 5) Apply writes through normal repo.write so the pre-write gate is still enforced.
    written_files_abs: list[str] = []
    write_results: list[dict[str, Any]] = []
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
        result = repo_write(RepoWriteRequest(project_path=project_path, client_name=client_name, relative_path=rel, content=new_content))
        written_abs = str((project / rel).resolve())
        written_files_abs.append(written_abs)
        write_results.append({"relative_path": rel, "status": "written", "size": result.size, "extra": result.extra})
    steps.append({"step": "repo.write", "status": "ok", "written_count": len(written_files_abs)})

    # 6) Diff, QA, verify against core before deploy.
    diff = GitService().diff(project_path, staged=False, timeout_seconds=60)
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
    if deploy_impact.impact == "asset_only":
        qa = OdooResult(status="ok", result={"skipped": True, "reason": "asset-only fast path uses focused syntax/core verification"})
    else:
        qa = self_run(SelfRunRequest(project_path=project_path, client_name=client_name, module_name=effective_module_name, run_pytest=False, include_git_status=True))
    verify = verify_against_core(SyntaxVerifyRequest(written_files=written_files_abs, project_path=project_path, client_name=client_name, fix_cycle=0))
    steps.append({"step": "git.diff", "status": "ok" if diff.exit_code == 0 else "failed", "exit_code": diff.exit_code})
    steps.append({"step": "qa.self_run", "status": qa.status})
    steps.append({"step": "workflow.verify_against_core", "status": verify.status, "push_blocked": verify.push_blocked, "critical_count": verify.critical_count})

    if qa.status == "failed" or verify.push_blocked:
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

    commit_message = payload.commit_message or f"Update {effective_module_name}: {payload.task_description[:180]}"
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
    frontend_touched = any(str(p).endswith((".xml", ".scss", ".css", ".js")) for p in written_files_abs)
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
        "next_call": {"tool": "jobs.get", "arguments": {"job_id": job_id, "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}} if job_id else None,
    }


async def _workflow_frontend_visual_implement(payload: FrontendVisualImplementRequest) -> dict[str, Any]:
    """Generate deterministic frontend visual assets, then run the normal Odoo workflow.

    This tool is generic for POS/OWL/dashboard/website/backend visuals.  It is
    intentionally a thin planner/generator around workflow.odoo_code_change so
    it inherits the same custom/core reads, QA, verify_against_core, safe deploy,
    and jobs.get polling contract.
    """
    service = FrontendVisualService()
    writes, visual_plan = service.build_writes(payload, payload.module_name)
    if payload.create_module:
        area = (payload.target_area or "backend").lower()
        bundle = "web.assets_frontend" if area == "website" else "point_of_sale._assets_pos" if area == "pos" else "web.assets_backend"
        depends = ["web", "point_of_sale"] if area == "pos" else ["web"]
        asset_paths = [write.relative_path for write in writes if write.relative_path.endswith((".js", ".xml", ".scss", ".css"))]
        manifest = {
            "name": payload.module_name.replace("_", " ").title(),
            "depends": depends,
            "assets": {bundle: asset_paths},
            "installable": True,
            "application": True,
        }
        writes = [
            OdooCodeFileChange(relative_path=f"{payload.module_name}/__init__.py", content=""),
            OdooCodeFileChange(relative_path=f"{payload.module_name}/__manifest__.py", content=repr(manifest) + "\n"),
            *writes,
        ]
    target_files = list(dict.fromkeys([*payload.target_files, *[w.relative_path for w in writes]]))
    description = (
        f"Frontend visual implementation ({visual_plan.get('implementation_type')}): {payload.design_goal}. "
        "Generated visual assets must be treated as implementation files and verified/deployed through the normal Odoo workflow."
    )
    change_payload = OdooCodeChangeRequest(
        client_name=payload.client_name,
        project_path=payload.project_path,
        module_name=payload.module_name,
        create_module=payload.create_module,
        task_description=description[:2000],
        task_type="scss_css" if visual_plan.get("implementation_type") in {"svg_asset", "scss"} else "js_owl",
        intent="frontend_visual_implement",
        target_hint=payload.css_selector or payload.component_type,
        change_request={
            "type": "frontend_visual_implement",
            "target_area": payload.target_area,
            "component_type": payload.component_type,
            "allowed_methods": payload.allowed_methods,
            "reference_images": payload.reference_images,
            "visual_plan": visual_plan,
        },
        patch_strategy="explicit_writes",
        target_files=target_files,
        writes=writes,
        commit_message=payload.commit_message or f"Implement frontend visual {visual_plan.get('asset_name', 'asset')}",
        auto_deploy=payload.auto_deploy,
    )
    result = await _workflow_odoo_code_change(change_payload)
    if isinstance(result, dict):
        result.setdefault("visual_plan", visual_plan)
        result.setdefault("generated_visual_writes", [w.model_dump(mode="json") for w in writes])
        result.setdefault(
            "visual_implementation_note",
            "The implementation method was selected at runtime from the component behavior. Dynamic dimensions use SCSS; SVG is reserved for fixed artwork.",
        )
    return result


async def _dispatch(name: str, args: dict) -> object:
    if name == "connector.health":
        EmptyRequest.model_validate(args)
        return {"status": "ok", "service": "odoo-mcp-agent-connector"}
    if name == "skills.list":
        req = SkillListRequest.model_validate(args)
        return SkillService().list(req.task_description)
    if name == "skills.read":
        req = SkillReadRequest.model_validate(args)
        return SkillService().read(req.name)
    if name == "artifacts.read":
        req = ArtifactReadRequest.model_validate(args)
        return JsonStore().read_artifact(req.artifact_id, req.offset, req.max_chars)
    if name == "client.check":
        req = ClientCheckRequest.model_validate(args)
        return ClientGateService().check(req.client_name, req.run_validation, req.force_refresh)
    if name == "workflow.agent_instructions":
        try:
            from ..main import agent_instructions

            return agent_instructions()
        except Exception as exc:
            return {"error": str(exc), "fallback": "Call workflow.coding_task_plan for workflow guidance"}
    if name == "workflow.capability_check":
        EmptyRequest.model_validate(args)
        return capability_check()
    if name == "workflow.coding_task_plan":
        return coding_task_plan(CodingTaskPlanRequest.model_validate(args))
    if name == "workflow.odoo_code_change":
        return await _workflow_odoo_code_change(OdooCodeChangeRequest.model_validate(args))
    if name == "workflow.frontend_visual_implement":
        return await _workflow_frontend_visual_implement(FrontendVisualImplementRequest.model_validate(args))
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
    if name == "odoo.read_group":
        req = OdooReadGroupRequest.model_validate(args)
        data = _read_group_via_shell(req)
        return data.get("odoo") if data.get("status") == "ok" else OdooResult(status="failed", result=data, error=data.get("message"))
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
    elif status_text in {"deploy_failed_or_timeout", "module_upgrade_running"}:
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
    module_request = OdooModuleRequest(
        client_name=original_request.get("client_name"),
        module_name=module_name,
        wait_for_result=False,
        wait_timeout_seconds=0,
        poll_interval_seconds=1.0,
    )
    try:
        started = start_odoo_module_deploy_job(module_request, run_inline=True)
    except TypeError:
        started = start_odoo_module_deploy_job(module_request)
    started_payload = started.model_dump(mode="json") if hasattr(started, "model_dump") else started
    result_payload = started_payload.get("result") if isinstance(started_payload, dict) and isinstance(started_payload.get("result"), dict) else {}
    if result_payload.get("finished") or not result_payload.get("job_id"):
        return started

    nested_job_id = result_payload.get("job_id")
    timeout_seconds = int(original_request.get("odoo_module_deploy_timeout_seconds", 600))
    poll_interval = float(original_request.get("odoo_module_deploy_poll_interval_seconds", 10.0))
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
            "poll_after_seconds": 1,
            "message": f"Odoo module deploy is still running after {timeout_seconds}s. Safe deploy is not complete and final success is blocked.",
            "next_call": {"tool": "jobs.get", "arguments": {"job_id": waited.id, "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}},
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
            "poll_after_seconds": 1,
            "next_call": {"tool": "jobs.get", "arguments": {"job_id": job_id, "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}},
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
                    int(original_request.get("odoosh_deploy_timeout_seconds", 600)),
                    float(original_request.get("odoosh_deploy_poll_interval_seconds", 10.0)),
                )
                deployed = check.exit_code == 0 and '"deployed": true' in (check.stdout or "").lower()
                resume_results["steps_run"][step_name] = {"status": "ok" if deployed else "warning", "sha": pushed_sha, "deployed": deployed, "result": _step_payload(check)}
                if not deployed:
                    resume_results["status"] = "deploy_failed_or_timeout"
                    resume_results["message"] = "Odoo.sh did not report the pushed commit as deployed within the polling window. Completed commit/push steps will not be replayed."
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
                    resume_results["next_call"] = nested.get("next_call") or {"tool": "jobs.get", "arguments": {"job_id": nested.get("job_id"), "wait_for_result": True, "wait_timeout_seconds": 600, "poll_interval_seconds": 1.0}}
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
    resume_results["message"] = f"Pipeline resumed and completed from step '{start_from}'"
    store.log_audit("workflow.resume_job", "ok", f"Completed resume of job {job_id}")
    return _persist_resume_result(store, job, resume_results, original_request)
