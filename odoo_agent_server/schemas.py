from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, Field, computed_field, field_validator


class Severity(str, Enum):
    error = "error"
    warning = "warning"
    info = "info"


class Finding(BaseModel):
    severity: Severity
    code: str
    message: str
    file: str | None = None
    line: int | None = None
    hint: str | None = None


class CommandResult(BaseModel):
    command: list[str]
    cwd: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class FileContent(BaseModel):
    path: str
    content: str
    size: int
    extra: dict[str, Any] = Field(default_factory=dict)


class FileListItem(BaseModel):
    path: str
    kind: Literal["file", "dir"]
    size: int | None = None


class BaseProjectRequest(BaseModel):
    project_path: str = Field(..., description="Absolute path to the checked-out Odoo repo")


class ApprovalMixin(BaseModel):
    approval_id: str | None = Field(default=None, description="Required when approval gating is enabled for this action")




class EmptyRequest(BaseModel):
    """Empty argument object for no-input tools."""


class LimitRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=2000)


class AuditListRequest(BaseModel):
    limit: int = Field(default=200, ge=1, le=2000)


class JobGetRequest(BaseModel):
    job_id: str = Field(..., min_length=1)
    wait_for_result: bool = Field(default=True, description="Wait server-side until the job finishes or the wait timeout expires")
    wait_timeout_seconds: int = Field(default=20, ge=0, le=25, description="Maximum server-side wait. Kept under Render request timeout.")
    poll_interval_seconds: float = Field(default=2.0, ge=0.25, le=10.0, description="Delay between job status checks while waiting")


class ArtifactReadRequest(BaseModel):
    artifact_id: str = Field(..., min_length=1, max_length=120, description="Artifact id returned by a previous tool response")
    offset: int = Field(default=0, ge=0, description="Zero-based character offset for text artifacts")
    max_chars: int = Field(default=12000, ge=1, le=50000, description="Maximum characters to return")


class SelfRunRequest(BaseProjectRequest):
    module_name: str = Field(..., description="Custom module folder name inside the repo")
    run_pytest: bool = Field(default=True)
    include_git_status: bool = Field(default=True)
    timeout_seconds: int = Field(default=90, ge=5, le=600)

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        return validate_safe_name(value, "module_name")


class SelfRunReport(BaseModel):
    status: Literal["passed", "failed", "warning"]
    project_path: str
    module_path: str
    module_name: str
    findings: list[Finding]
    commands: list[CommandResult] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == Severity.error)




class RepoPrepareRequest(ApprovalMixin):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. When omitted, the active client is auto-resolved when possible.")
    repo_url: str | None = Field(default=None, description="Git repository URL. Defaults to TARGET_GIT_REPO.")
    branch: str | None = Field(default=None, description="Target branch. Defaults to TARGET_GIT_BRANCH.")
    project_path: str | None = Field(default=None, description="Exact checkout path. Defaults to selected ODOO_CLIENTS path or TARGET_PROJECT_PATH.")
    repo_name: str | None = Field(default=None, description="Folder name under workspace_root. Defaults to repo URL name or TARGET_PROJECT_PATH name.")
    workspace_root: str | None = Field(default=None, description="Workspace root for clones. Defaults to WORKSPACE_ROOT.")
    remote: str = Field(default="origin")
    timeout_seconds: int = Field(default=180, ge=30, le=900)


class RepoPrepareResult(BaseModel):
    project_path: str
    repo_url: str
    branch: str
    sha: str
    status: str
    already_cloned: bool
    steps: list[CommandResult] = Field(default_factory=list)


class RepoCustomModulesRequest(BaseProjectRequest):
    max_depth: int = Field(default=5, ge=1, le=10)
    include_uninstallable: bool = Field(default=False)


class CustomModuleItem(BaseModel):
    module_name: str
    path: str
    manifest_path: str
    installable: bool | None = None
    name: str | None = None
    version: str | None = None
    author: str | None = None
    application: bool | None = None


class CodingTaskPlanRequest(BaseModel):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. When omitted, the active client is auto-resolved when possible.")
    repo_url: str | None = Field(default=None, description="Git repository URL. Defaults to TARGET_GIT_REPO.")
    branch: str | None = Field(default=None, description="Target branch. Defaults to TARGET_GIT_BRANCH.")
    module_name: str | None = Field(default=None, description="Target custom module folder name, if known.")
    task: str = Field(..., min_length=1, description="Natural-language coding task.")
    project_path: str | None = Field(default=None, description="Existing checkout path, if already known.")


class CodingTaskPlanResult(BaseModel):
    summary: str
    ordered_steps: list[str]
    required_tools: list[str]
    guardrails: list[str]
    defaults: dict[str, Any] = Field(default_factory=dict)


class RepoListRequest(BaseProjectRequest):
    relative_path: str = Field(default=".")
    max_depth: int = Field(default=3, ge=1, le=8)
    max_items: int = Field(default=500, ge=1, le=3000)


class RepoReadRequest(BaseProjectRequest):
    relative_path: str
    max_bytes: int = Field(default=300_000, ge=100, le=2_000_000)


class RepoReadRangeRequest(BaseProjectRequest):
    relative_path: str
    start_line: int = Field(default=1, ge=1, description="1-based first line to read")
    max_lines: int = Field(default=200, ge=1, le=2000, description="Maximum number of lines to return")


class RepoSearchRequest(BaseProjectRequest):
    pattern: str = Field(..., min_length=1, max_length=500, description="Literal text or regex pattern to search for")
    relative_path: str = Field(default=".", description="Directory or file inside project_path to search")
    max_matches: int = Field(default=100, ge=1, le=1000)
    context_lines: int = Field(default=2, ge=0, le=10)
    regex: bool = Field(default=False, description="Treat pattern as a Python regular expression instead of literal text")


class RepoWriteRequest(BaseProjectRequest, ApprovalMixin):
    relative_path: str
    content: str
    create_dirs: bool = True
    # Chunked write support for large files (>1000 lines)
    # chunk_index: 0-based index of this chunk
    # total_chunks: total number of chunks to expect
    # When total_chunks > 1, content is appended to existing file chunks
    chunk_index: int = Field(default=0, ge=0, description="0-based chunk index for large file writes")
    total_chunks: int = Field(default=1, ge=1, description="Total chunks. Use 1 for normal writes. >1 enables chunked mode.")


class RepoWriteItem(BaseModel):
    relative_path: str = Field(
        validation_alias=AliasChoices("relative_path", "path"),
        description="Path relative to project_path. The alias 'path' is accepted for compatibility with MCP clients.",
    )
    content: str
    create_dirs: bool = True


class RepoWriteManyAtomicRequest(BaseProjectRequest, ApprovalMixin):
    files: list[RepoWriteItem] = Field(..., min_length=1, max_length=200, description="Files to write as one atomic multi-file transaction")
    check_only: bool = Field(default=False, description="Validate the transaction without changing the real checkout")
    timeout_seconds: int = Field(default=60, ge=5, le=300)


class RepoWriteManyFastSafeRequest(BaseProjectRequest, ApprovalMixin):
    files: list[RepoWriteItem] = Field(..., min_length=1, max_length=50, description="Small related files to write using lock + backup + rollback, without git worktree overhead")
    check_only: bool = Field(default=False, description="Validate paths, limits, and parent directories without changing the checkout")
    timeout_seconds: int = Field(default=30, ge=5, le=180, description="Lock timeout for the fast-safe write")


class RepoWriteManySmartRequest(RepoWriteManyAtomicRequest):
    strategy: Literal["auto", "fast_safe", "atomic"] = Field(
        default="auto",
        description=(
            "auto selects fast_safe for small safe writes and atomic for large/risky writes; "
            "fast_safe forces lock+backup+rollback; atomic forces temp git-worktree transaction"
        ),
    )


class RepoDeleteRequest(BaseProjectRequest, ApprovalMixin):
    relative_path: str


class RepoPatchRequest(BaseProjectRequest, ApprovalMixin):
    patch: str = Field(..., description="Unified diff to apply inside project_path")
    check_only: bool = Field(default=False)
    timeout_seconds: int = Field(default=60, ge=5, le=300)


class RepoAtomicPatchRequest(RepoPatchRequest):
    """Apply a multi-file patch through an isolated temporary worktree/branch.

    The real checkout is not changed until the whole patch validates in the
    isolated worktree and validates again against the current checkout.
    """


class RepoCleanPycRequest(BaseProjectRequest, ApprovalMixin):
    relative_path: str = Field(default=".")


class RepoCleanPycResult(BaseModel):
    project_path: str
    scanned_path: str
    removed_files: list[str] = Field(default_factory=list)
    removed_dirs: list[str] = Field(default_factory=list)


class GitDiffRequest(BaseProjectRequest):
    staged: bool = Field(default=False)
    timeout_seconds: int = Field(default=30, ge=5, le=300)


class GitCommitRequest(BaseProjectRequest, ApprovalMixin):
    message: str = Field(..., min_length=3, max_length=300)
    paths: list[str] = Field(default_factory=list, description="Relative paths to add before commit. Empty means git add -A.")
    timeout_seconds: int = Field(default=60, ge=5, le=300)


class GitPushRequest(BaseProjectRequest, ApprovalMixin):
    remote: str = Field(default="origin")
    branch: str | None = Field(default=None, description="Defaults to the current branch")
    set_upstream: bool = Field(default=True)
    timeout_seconds: int = Field(default=120, ge=10, le=600)


class OdooShLogName(str, Enum):
    install = "install"
    odoo = "odoo"
    update = "update"
    pip = "pip"


class OdooShLogRequest(BaseModel):
    log_name: OdooShLogName = Field(default=OdooShLogName.odoo)
    tail_lines: int = Field(default=200, ge=20, le=2000)


class OdooShCommandRequest(BaseModel):
    tail_lines: int = Field(default=200, ge=20, le=2000)


class OdooShWaitForCommitRequest(BaseModel):
    expected_sha: str = Field(..., min_length=7, description="Git SHA expected on Odoo.sh ~/src/user")
    timeout_seconds: int = Field(default=600, ge=30, le=1800)
    interval_seconds: int = Field(default=10, ge=5, le=60)


class OdooShVerifyManifestRequest(BaseModel):
    module_name: str = Field(..., description="Technical module folder/name to verify on Odoo.sh")

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        return validate_safe_name(value, "module_name")


class OdooShUserFileReadRequest(BaseModel):
    relative_path: str = Field(
        ...,
        description="Path relative to /home/odoo/src/user, e.g. sale_order_notebook_tab/models/purchase_order.py",
    )
    max_bytes: int = Field(default=300000, ge=1, le=1000000)

    @field_validator("relative_path")
    @classmethod
    def relative_path_must_be_safe(cls, value: str) -> str:
        rel = value.replace("\\", "/").strip("/")
        if not rel or rel.startswith("../") or "/../" in rel or rel.startswith(".git/") or rel == ".git":
            raise ValueError("relative_path must stay inside /home/odoo/src/user")
        return rel


class OdooShUserFilesListRequest(BaseModel):
    module_name: str = Field(..., description="Custom module folder under /home/odoo/src/user")
    subdir: str = Field(default="", description="Optional module subdirectory such as models, views, static/src")
    max_items: int = Field(default=200, ge=1, le=2000)

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        return validate_safe_name(value, "module_name")

    @field_validator("subdir")
    @classmethod
    def subdir_must_be_safe(cls, value: str) -> str:
        rel = value.replace("\\", "/").strip("/")
        if rel.startswith("../") or "/../" in rel or rel.startswith(".git/") or rel == ".git":
            raise ValueError("subdir must stay inside the module")
        return rel


class OdooShModuleUpdateRequest(ApprovalMixin):
    module_name: str = Field(..., description="Technical module folder/name to update")
    timeout_seconds: int = Field(default=600, ge=30, le=1800)
    expected_sha: str | None = Field(default=None, description="Optional pushed Git SHA that must be deployed on Odoo.sh before update")
    wait_for_result: bool = Field(default=True, description="Wait server-side for background SSH job until done or wait timeout")
    wait_timeout_seconds: int = Field(default=20, ge=0, le=25, description="Maximum server-side wait. Kept under Render request timeout.")
    poll_interval_seconds: float = Field(default=2.0, ge=0.25, le=10.0, description="Delay between job status checks while waiting")

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        return validate_safe_name(value, "module_name")


class OdooRpcModuleRequest(ApprovalMixin):
    module_name: str = Field(..., description="Technical module name")
    wait_for_result: bool = Field(default=True, description="For background install/upgrade/deploy, wait server-side until the job finishes or times out")
    wait_timeout_seconds: int = Field(default=20, ge=0, le=25, description="Maximum server-side wait. Kept under Render request timeout.")
    poll_interval_seconds: float = Field(default=2.0, ge=0.25, le=10.0, description="Delay between job status checks while waiting")
    deploy_guard_timeout_seconds: int = Field(default=300, ge=0, le=600, description="Maximum server-side wait for a recent manual git.push SHA to appear on Odoo.sh before install/upgrade")
    deploy_guard_poll_interval_seconds: float = Field(default=10.0, ge=0.25, le=60.0, description="Polling interval for the deploy guard used by direct module install/upgrade calls")

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        return validate_safe_name(value, "module_name")


class DeployedModuleVerifyRequest(BaseProjectRequest):
    module_name: str = Field(..., description="Custom module folder/name to compare against /home/odoo/src/user")
    paths: list[str] = Field(default_factory=list, description="Repo-relative files to compare. Empty compares module code/XML/assets up to max_files.")
    max_files: int = Field(default=200, ge=1, le=1000, description="Maximum module files to compare when paths is empty")
    include_module_status: bool = Field(default=True, description="Also read live Odoo module installation state")

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        return validate_safe_name(value, "module_name")


class OdooSearchReadRequest(BaseModel):
    model: str
    domain: list[Any] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    order: str | None = Field(default=None, description="Optional Odoo order string, e.g. 'create_date desc'")
    context: dict[str, Any] | None = Field(default=None, description="Optional Odoo context passed to search_read")


class OdooFunctionalQueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Natural-language Odoo functional read query, e.g. list users, draft invoices, unpaid invoices, top customers")
    limit: int = Field(default=10, ge=1, le=100, description="Maximum records to return")


class OdooRpcResult(BaseModel):
    status: Literal["ok", "failed"]
    base_url: str | None = None
    database: str | None = None
    result: Any = None
    error: str | None = None


class BrowserCheckRequest(BaseModel):
    base_url: str | None = Field(default=None)
    login: str | None = Field(default=None)
    password: str | None = Field(default=None)
    start_path: str = Field(default="/web")
    screenshot_name: str = Field(default="odoo_page.png")
    timeout_seconds: int = Field(default=45, ge=10, le=180)
    full_page: bool = True


class BrowserCheckResult(BaseModel):
    status: Literal["ok", "failed"]
    url: str | None = None
    title: str | None = None
    screenshot_path: str | None = None
    console_errors: list[str] = Field(default_factory=list, description="console.error/warning + pageerror entries.")
    console_all: list[str] = Field(default_factory=list, description="EVERY console message (log/info/debug/warning/error), in order. Always populated — read this for full console output on every run, not just when something fails.")
    network_errors: list[str] = Field(default_factory=list)
    error: str | None = None


class PosFlowCheckRequest(BaseModel):
    """Run a real multi-step POS interaction inside one persistent session,
    instead of a single cold-load screenshot. Steps are fixed (open POS ->
    open session if needed -> click first product -> open payment screen ->
    validate order -> receipt screen) because POS component/state bugs only
    surface once you actually interact with the app, not on initial mount.
    """
    base_url: str | None = Field(default=None)
    login: str | None = Field(default=None)
    password: str | None = Field(default=None)
    pos_config_name: str | None = Field(default=None, description="Optional POS config display name to open; if omitted, opens the first available POS.")
    product_name: str | None = Field(default=None, description="Optional exact/partial product name to click; if omitted, clicks the first product tile found.")
    payment_method_name: str | None = Field(default=None, description="Optional payment method button label; if omitted, clicks the first payment method tile found.")
    screenshot_prefix: str = Field(default="pos_flow")
    timeout_seconds: int = Field(default=60, ge=15, le=180)


class PosFlowStepResult(BaseModel):
    step: str
    status: Literal["ok", "failed", "skipped"]
    screenshot_path: str | None = None
    console_errors: list[str] = Field(default_factory=list, description="console.error/warning + pageerror entries for this step only.")
    console_all: list[str] = Field(default_factory=list, description="EVERY console message (log/info/debug/warning/error) for this step, in order. Read this whenever you need full console output, not just errors.")
    network_errors: list[str] = Field(default_factory=list)
    error: str | None = None


class PosFlowCheckResult(BaseModel):
    status: Literal["ok", "failed"]
    steps: list[PosFlowStepResult] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list, description="All console errors/warnings across the whole session, deduplicated.")
    console_all: list[str] = Field(default_factory=list, description="EVERY console message across the whole session (log/info/debug/warning/error), in order. Always populated regardless of pass/fail — read this for full console output on every run.")
    network_errors: list[str] = Field(default_factory=list)
    error: str | None = None


class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    running = "running"
    rejected = "rejected"
    consumed = "consumed"
    failed = "failed"


class ApprovalCreateRequest(BaseModel):
    action: str
    summary: str
    payload_preview: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecisionRequest(BaseModel):
    approval_id: str
    reason: str | None = None


class ApprovalRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    action: str
    summary: str
    payload_preview: dict[str, Any] = Field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.pending
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    decided_at: str | None = None
    reason: str | None = None


class AuditEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    action: str
    status: str
    summary: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    passed = "passed"
    warning = "warning"
    failed = "failed"


_STEP_STATUS_ICON = {
    "passed": "\u2705",   # ✅
    "ok": "\u2705",
    "running": "\u23f3",  # ⏳
    "queued": "\u23f3",
    "warning": "\u26a0\ufe0f",  # ⚠️
    "failed": "\u274c",   # ❌
    "skipped": "\u23ed\ufe0f",  # ⏭️
}


class JobStep(BaseModel):
    name: str
    status: str
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    result: Any = None
    error: str | None = None

    @computed_field  # type: ignore[misc]
    @property
    def label(self) -> str:
        """Icon-prefixed display label, e.g. '✅ git.push' — ready to show directly to the user."""
        icon = _STEP_STATUS_ICON.get(self.status.lower(), "\u2022")
        return f"{icon} {self.name}"


class JobRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    job_type: str
    status: JobStatus = JobStatus.queued
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    steps: list[JobStep] = Field(default_factory=list)
    result: Any = None


class ToolDescriptor(BaseModel):
    name: str
    description: str
    read_only: bool = False
    requires_approval: bool = False
    deprecated: bool = Field(
        default=False,
        description=(
            "If True, this tool is hidden from tools/list to reduce clutter and "
            "prevent the agent from choosing the wrong (legacy) tool. The underlying "
            "function still works if called directly — kept for backward compatibility "
            "and as an internal fallback target."
        ),
    )
    title: str | None = Field(
        default=None,
        description=(
            "Human-readable action label shown in MCP/client tool-call UI "
            "(e.g. 'Push code to GitHub' instead of raw name 'git.push'). "
            "Falls back to a humanized version of `name` if not set."
        ),
    )


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolCallResult(BaseModel):
    name: str
    status: Literal["ok", "failed"]
    result: Any = None
    error: str | None = None


class CapabilityCheckResult(BaseModel):
    status: Literal["ready", "partial", "failed"]
    checks: dict[str, dict[str, Any]]


class SafeDeployModuleRequest(BaseProjectRequest, ApprovalMixin):
    client_name: str | None = Field(default=None, description="Client name from ODOO_CLIENTS. When omitted, resolves from project_path when possible.")
    module_name: str = Field(..., description="Technical module folder/name to deploy and update")
    commit_message: str = Field(..., min_length=3, max_length=300)
    paths: list[str] = Field(default_factory=list, description="Relative paths to add before commit. Empty means git add -A.")
    remote: str = Field(default="origin")
    branch: str | None = Field(default=None, description="Defaults to current branch or TARGET_GIT_BRANCH")
    run_qa: bool = Field(default=True)
    wait_for_odoosh: bool = Field(default=True)
    upgrade_module: bool = Field(default=True)
    verify_ui_paths: list[str] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "Optional Odoo web paths to open after upgrade for live UI breakage checks, "
            "for example /web#action=123&model=sale.order&view_type=form."
        ),
    )
    verify_ui_screenshot_prefix: str = Field(default="safe_deploy_ui", description="Screenshot filename prefix for verify_ui_paths")
    verify_ui_timeout_seconds: int = Field(default=45, ge=10, le=180, description="Per-page browser timeout for verify_ui_paths")
    wait_for_result: bool = Field(default=True, description="Wait server-side for the safe deploy job until done or wait_timeout_seconds expires")
    wait_timeout_seconds: int = Field(default=300, ge=0, le=300, description="Maximum server-side wait before returning job_id/next_call")
    poll_interval_seconds: float = Field(default=2.0, ge=0.25, le=10.0, description="Delay between safe deploy job polls while waiting")
    odoosh_deploy_timeout_seconds: int = Field(default=300, ge=0, le=600, description="Maximum time to poll Odoo.sh after git push for the pushed SHA to become deployed")
    odoosh_deploy_poll_interval_seconds: float = Field(default=10.0, ge=0.25, le=60.0, description="Polling interval for Odoo.sh deployed SHA checks")
    odoo_module_deploy_timeout_seconds: int = Field(default=300, ge=0, le=900, description="Maximum time for the internal Odoo module install/upgrade job to finish before returning a non-success resumable state")
    odoo_module_deploy_poll_interval_seconds: float = Field(default=10.0, ge=0.25, le=60.0, description="Polling interval for the internal Odoo module install/upgrade job")
    expected_registry_fields: dict[str, list[str]] = Field(default_factory=dict, description="Optional live registry fields to verify after upgrade, e.g. {'purchase.order': ['grok_y']}. If omitted, safe_deploy infers Python fields from changed addon files.")
    force_new_deploy: bool = Field(
        default=False,
        description=(
            "When False, duplicate workflow.safe_deploy_module calls for the same "
            "project_path + module_name return the existing running/resumable job instead "
            "of spawning a concurrent commit/push/upgrade. Set True only to intentionally "
            "restart after a resumable deploy failure."
        ),
    )
    timeout_seconds: int = Field(default=900, ge=60, le=2400)

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        return validate_safe_name(value, "module_name")


class FullVerifyRequest(SelfRunRequest):
    clean_pyc: bool = False
    include_git_diff: bool = True
    include_odoosh_errors: bool = False
    include_odoo_module_status: bool = False
    take_browser_screenshot: bool = False
    browser_start_path: str = "/web"
    screenshot_name: str = "full_verify.png"


class StartSessionRequest(BaseModel):
    client_name: str | None = Field(
        default=None,
        description=(
            "V15 Multi-Client: name of client from ODOO_CLIENTS env var. "
            "e.g. 'Client A'. When set, repo/branch/path/token/Odoo creds "
            "are resolved automatically. No other fields needed."
        ),
    )
    repo_url: str | None = Field(default=None, description="Git repo URL. Auto-resolved from client_name or falls back to TARGET_GIT_REPO.")
    branch: str | None = Field(default=None, description="Target branch. Auto-resolved from client_name or falls back to TARGET_GIT_BRANCH.")
    repo_name: str | None = Field(default=None, description="Folder name under workspace_root.")
    workspace_root: str | None = Field(default=None, description="Workspace root for clones. Defaults to WORKSPACE_ROOT.")
    remote: str = Field(default="origin")
    max_depth: int = Field(default=5, ge=1, le=10)
    include_odoosh: bool = True
    include_odoo: bool = True
    timeout_seconds: int = Field(default=180, ge=30, le=900)


class RegistrySmokeTestRequest(BaseModel):
    module_name: str = Field(..., description="Technical module name to validate in the live Odoo registry")
    model_names: list[str] = Field(default_factory=list, description="Models that must exist and answer a limit=1 search_read. Empty means infer common module prefix.")
    menu_names: list[str] = Field(default_factory=list, description="Menu labels that should be present. Empty skips menu check.")
    strict: bool = Field(default=True, description="Fail if any requested model/menu check fails")

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        return validate_safe_name(value, "module_name")


class RestoreLastGoodCommitRequest(BaseProjectRequest, ApprovalMixin):
    bad_sha: str = Field(..., min_length=7, description="Bad pushed commit to revert")
    remote: str = Field(default="origin")
    branch: str | None = Field(default=None, description="Defaults to current branch or TARGET_GIT_BRANCH")
    push: bool = Field(default=True, description="Push the revert commit after creating it")
    timeout_seconds: int = Field(default=300, ge=30, le=900)


class MenuCrawlerRequest(BaseModel):
    base_url: str | None = Field(default=None)
    login: str | None = Field(default=None)
    password: str | None = Field(default=None)
    paths: list[str] = Field(default_factory=lambda: ["/web"], max_length=50)
    screenshot_prefix: str = Field(default="menu_crawl")
    timeout_seconds: int = Field(default=45, ge=10, le=180)


class WorkflowReport(BaseModel):
    status: Literal["passed", "failed", "warning", "running", "already_running"]
    job_id: str
    steps: list[JobStep]
    summary: dict[str, Any]

    @computed_field  # type: ignore[misc]
    @property
    def status_label(self) -> str:
        """Icon-prefixed overall status, e.g. '✅ passed' — ready to show directly to the user."""
        icon = _STEP_STATUS_ICON.get(self.status.lower(), "\u2022")
        return f"{icon} {self.status}"

    @computed_field  # type: ignore[misc]
    @property
    def steps_summary(self) -> str:
        """One-line icon summary of all steps, capped at last 12 to avoid context bloat
        in long-running multi-step pipelines (e.g. '... ✅ git.push ⏳ odoosh.check_commit')."""
        labels = [step.label for step in self.steps]
        if len(labels) > 12:
            labels = ["…"] + labels[-12:]
        return " ".join(labels)


def validate_safe_name(value: str, field_name: str) -> str:
    if "/" in value or "\\" in value or value.strip() != value or not value:
        raise ValueError(f"{field_name} must be a safe folder/technical name")
    if value in {".", ".."}:
        raise ValueError(f"{field_name} must not be a relative path token")
    return value


class OdooShCheckCommitRequest(BaseModel):
    expected_sha: str = Field(..., min_length=7, description="Git SHA to check on Odoo.sh ~/src/user")


# ---------------------------------------------------------------------------
# V14: Self-Learning Agent — Core File Discovery Schemas
# ---------------------------------------------------------------------------

class FindCoreFileRequest(BaseModel):
    """V14: Locate an Odoo core source file by module + type + keyword.

    Search priority:
      1. /home/odoo/src/enterprise
      2. /home/odoo/src/odoo
      3. /home/odoo/src/user       (fallback for other related custom dependencies)

    Use before any _inherit, view inherit, or depends to read exact
    fields/methods/xpath structure directly from source.
    """
    module_name: str = Field(
        ...,
        description="Odoo module technical name, e.g. 'sale', 'stock', 'account'",
    )
    file_type: str = Field(
        ...,
        description=(
            "Type of file to search: 'model', 'view', 'wizard', 'controller', "
            "'report', 'data', 'security', 'static', 'manifest', or 'any'"
        ),
    )
    keyword: str = Field(
        ...,
        min_length=1,
        description="Keyword to match in filename, e.g. 'order', 'invoice', 'picking'",
    )
    search_roots: list[str] = Field(
        default_factory=list,
        description=(
            "Override search roots (absolute paths). Defaults to "
            "['/home/odoo/src/enterprise', '/home/odoo/src/odoo', '/home/odoo/src/user']"
        ),
    )

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        return validate_safe_name(value, "module_name")

    @field_validator("file_type")
    @classmethod
    def file_type_must_be_valid(cls, value: str) -> str:
        allowed = {"model", "view", "wizard", "controller", "report", "data", "security", "static", "manifest", "any"}
        if value not in allowed:
            raise ValueError(f"file_type must be one of: {', '.join(sorted(allowed))}")
        return value


class CoreFileMatch(BaseModel):
    """A single match returned by workflow.find_core_file."""
    path: str = Field(..., description="Absolute path to the found file")
    relative_path: str = Field(..., description="Path relative to its search root")
    root: str = Field(..., description="Which search root this file was found in")
    priority: int = Field(..., description="1=enterprise, 2=odoo, 3=user fallback — lower is higher priority")
    size: int = Field(..., description="File size in bytes")


class FindCoreFileResult(BaseModel):
    """Result from workflow.find_core_file."""
    module_name: str
    file_type: str
    keyword: str
    matches: list[CoreFileMatch] = Field(default_factory=list)
    best_match: str | None = Field(default=None, description="Absolute path of the highest-priority match (read this with repo.read)")
    searched_roots: list[str] = Field(default_factory=list)
    total_found: int = 0


class FindSimilarViewRequest(BaseModel):
    """V14: Locate an Odoo core view XML file by model + view_type.

    Use before inheriting/extending any view so the agent reads the
    exact xpath structure, field names, and string attributes before
    writing a <xpath expr="..."> element.

    Search priority: enterprise → odoo → user fallback (same as find_core_file).
    """
    model: str = Field(
        ...,
        description="Odoo model technical name, e.g. 'sale.order', 'account.move', 'stock.picking'",
    )
    view_type: str = Field(
        ...,
        description="View type: 'form', 'tree', 'list', 'kanban', 'search', 'pivot', 'graph', 'calendar', 'any'",
    )
    search_roots: list[str] = Field(
        default_factory=list,
        description="Override search roots. Defaults to enterprise → odoo → user fallback.",
    )

    @field_validator("view_type")
    @classmethod
    def view_type_must_be_valid(cls, value: str) -> str:
        allowed = {"form", "tree", "list", "kanban", "search", "pivot", "graph", "calendar", "activity", "any"}
        if value not in allowed:
            raise ValueError(f"view_type must be one of: {', '.join(sorted(allowed))}")
        return value


class SimilarViewMatch(BaseModel):
    """A single match returned by workflow.find_similar_view."""
    path: str = Field(..., description="Absolute path to the view XML file")
    relative_path: str = Field(..., description="Relative path within its search root")
    root: str = Field(..., description="Which search root this was found in")
    priority: int = Field(..., description="1=enterprise, 2=odoo, 3=user fallback")
    model_hint: str = Field(..., description="Model name extracted from file/path heuristic")
    size: int


class FindSimilarViewResult(BaseModel):
    """Result from workflow.find_similar_view."""
    model: str
    view_type: str
    matches: list[SimilarViewMatch] = Field(default_factory=list)
    best_match: str | None = Field(default=None, description="Absolute path of best match — read with repo.read before writing xpath")
    searched_roots: list[str] = Field(default_factory=list)
    total_found: int = 0


class ListModuleFilesRequest(BaseModel):
    """V14+ gap-fix: List all files inside a core or custom module directory.

    Use when you don't know the exact filename/keyword to search for and
    need to browse what exists first (e.g. before find_core_file).
    """
    module_name: str = Field(..., description="Odoo module technical name")
    subdir: str = Field(
        default="",
        description="Optional subdirectory to narrow listing, e.g. 'models', 'views', 'static/src'. Empty = whole module.",
    )
    search_roots: list[str] = Field(
        default_factory=list,
        description="Override search roots. Defaults to enterprise → odoo → user fallback.",
    )

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        return validate_safe_name(value, "module_name")


class ModuleFileEntry(BaseModel):
    path: str
    relative_path: str
    size: int
    ext: str


class ListModuleFilesResult(BaseModel):
    module_name: str
    subdir: str
    found: bool
    module_path: str | None = None
    root: str | None = None
    priority: int | None = None
    files: list[ModuleFileEntry] = Field(default_factory=list)
    total_found: int = 0
    message: str | None = None


# ---------------------------------------------------------------------------
# Fix 3: Resume job from last successful step
# ---------------------------------------------------------------------------

class ResumeJobRequest(BaseModel):
    """Fix 3: Resume a safe_deploy_module job from its last successful step.

    Useful when a long multi-step pipeline (10+ steps) fails partway through.
    Instead of starting over from step 1, the agent calls workflow.resume_job
    with the job_id from the failed run.
    """
    job_id: str | None = Field(
        default=None,
        description=(
            "Job ID returned by a previous safe_deploy_module or start_session call. "
            "When omitted, the connector uses the last saved safe-deploy job from session state, "
            "or the latest matching safe_deploy_module job."
        ),
    )
    client_name: str | None = Field(default=None, description="Optional client session to load when job_id is omitted")
    project_path: str | None = Field(default=None, description="Optional project path filter when selecting the latest job without job_id")
    module_name: str | None = Field(default=None, description="Optional module filter when selecting the latest job without job_id")
    override_step: str | None = Field(
        default=None,
        description=(
            "If provided, start from this step name instead of the auto-detected last successful step. "
            "Supported values include actual safe-deploy step names such as "
            "'qa_self_run', 'git_commit', 'git_push', 'odoosh_check_commit', "
            "'odoo_module_deploy', and 'registry_smoke_test'. Aliases "
            "'qa', 'commit', 'push', 'wait_odoosh', 'install', 'upgrade', "
            "'module_install', 'module_update', and 'smoke_test' are accepted."
        ),
    )


# ---------------------------------------------------------------------------
# Fix 7: Session state request schemas
# ---------------------------------------------------------------------------

class SessionStateSaveRequest(BaseModel):
    """Fix 7: Save arbitrary key/value pairs to the session state store."""
    state: dict[str, Any] = Field(..., description="Key/value pairs to merge into the session state")
    client_name: str | None = Field(default=None, description="Phase 3: isolate this save to a specific client's session file. Omit for legacy global behavior.")


class SessionStateLoadRequest(BaseModel):
    """Fix 7: Load the last persisted session state."""
    client_name: str | None = Field(default=None, description="Phase 3: load this client's isolated session state instead of the legacy global one. Recommended once more than one client is configured.")


# ---------------------------------------------------------------------------
# Fix 10: Explicit ensure-dependencies tool request
# ---------------------------------------------------------------------------

class EnsureDependenciesRequest(ApprovalMixin):
    """Fix 10: Auto-install any missing/uninstalled Odoo module dependencies."""
    module_name: str = Field(..., description="Technical module name whose dependencies should be satisfied")

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        return validate_safe_name(value, "module_name")


# ===========================================================================
# V14+ — Pillar 2: Adaptive Scope Advisor
# ===========================================================================

class ScopeLevel(str, Enum):
    """Adaptive scope level reached during analysis."""
    L0 = "L0"  # Baseline: custom module already read from checkout
    L1 = "L1"  # _inherit chain: read parent model
    L2 = "L2"  # Mixin/abstract: read all mixins
    L3 = "L3"  # Computed fields / ir.model: scan full models/ dir
    L4 = "L4"  # Cross-module comodel: read related module core too


class ScopeReadInstruction(BaseModel):
    """A single mandatory read instruction returned by scope_advice."""
    level: ScopeLevel = Field(..., description="Scope level that triggered this instruction")
    reason: str = Field(..., description="Why this file must be read")
    read_type: str = Field(
        ...,
        description=(
            "Instruction type: core_model | core_model_missing | "
            "core_mixin | core_models_dir_scan | cross_module_manifest"
        ),
    )
    path: str = Field(..., description="Absolute path (use repo.read on this path)")
    mandatory: bool = Field(True, description="If True, must be completed before any code is written")


class ScopeAdviceRequest(BaseModel):
    """V14+: Request adaptive scope analysis for a coding task.

    Call this AFTER reading custom module files from the active repo checkout
    and BEFORE writing any code. The service scans those custom files for
    _inherit, mixins, computed fields, and cross-module refs, then returns an
    ordered list of additional installed-source reads. It should not duplicate
    the current custom module from /home/odoo/src/user.
    """
    custom_module_name: str = Field(
        ...,
        description="Technical name of the custom module being developed, e.g. 'my_sale_ext'",
    )
    custom_file_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Absolute paths of custom Python files already read by the agent "
            "(e.g. /workspace/repos/myrepo/my_module/models/sale_order.py). "
            "Used to detect _inherit, mixins, computed fields for adaptive level detection."
        ),
    )
    task_description: str = Field(
        default="",
        description="Brief description of the coding task (used for logging only)",
    )
    search_roots: list[str] = Field(
        default_factory=list,
        description="Override search roots. Defaults to enterprise → odoo → user fallback.",
    )


class ScopeAdviceResult(BaseModel):
    """Adaptive scope advice — ordered list of mandatory read instructions."""
    custom_module: str
    reached_level: ScopeLevel = Field(
        ...,
        description="Highest scope level triggered by the analysis",
    )
    instructions: list[ScopeReadInstruction] = Field(
        default_factory=list,
        description="Ordered list of reads the agent MUST execute before writing code",
    )
    mandatory_count: int = Field(0, description="Number of mandatory (blocking) instructions")
    total_count: int = Field(0, description="Total instructions including optional")
    warning: str = Field(
        "",
        description="Always-present warning: push blocked until verify_against_core passes",
    )


# ===========================================================================
# V14+ — Pillar 3: Pre-Push Syntax Verifier
# ===========================================================================

class VerifySeverity(str, Enum):
    """Severity of a syntax verify finding."""
    CRITICAL = "CRITICAL"  # Push is BLOCKED until fixed
    WARNING  = "WARNING"   # Should be reviewed but does not block push
    INFO     = "INFO"      # Informational — cross-check passed


class VerifyFinding(BaseModel):
    """A single finding from verify_against_core."""
    file: str = Field(..., description="Absolute path of the file containing the issue")
    severity: VerifySeverity = Field(..., description="CRITICAL blocks push; WARNING is advisory")
    check: str = Field(..., description="Name of the check that fired")
    message: str = Field(..., description="Human-readable description of the mismatch")
    fix_hint: str = Field("", description="What to do to fix this issue")
    line: int | None = Field(default=None, description="Line number in `file` where the issue was found, when known (e.g. real JS parse errors).")

    @computed_field  # type: ignore[misc]
    @property
    def code(self) -> str:
        """Backward-compatible alias used by older tests/clients."""
        return self.check


class SyntaxVerifyRequest(BaseModel):
    """V14+: Pre-push cross-check of written files against Odoo core source.

    Call this BEFORE every git.push. If push_blocked=True in the response,
    fix all CRITICAL findings and call again (max 3 fix cycles).

    Checks performed:
      Python (.py):
        - _inherit model exists in core
        - depends=[] field names exist in core model
        - comodel_name module exists in src roots
        - related= first field exists in core model
        - compute= method defined in the same file

      XML (.xml):
        - xpath @name nodes exist in core view source
        - <field name=...> fields checked against core model

      Manifest (__manifest__.py):
        - depends[] module names exist in /home/odoo/src/* roots
        - asset paths exist and are in the right Odoo bundle

      Frontend assets (.js/.scss/.xml QWeb):
        - JS/OWL module header, import aliases, legacy/modern mixing, template names
        - QWeb t-name/t-inherit/t-call/xpath source existence
        - SCSS patterns that often break Odoo sass compilation
    """
    written_files: list[str] = Field(
        ...,
        description=(
            "Absolute paths of files written by the agent that need pre-push verification. "
            "Include ALL .py, .xml, .js, .scss, and __manifest__.py files changed in this task."
        ),
    )
    fix_cycle: int | None = Field(
        default=0,
        description="Current fix cycle number (0-indexed). After 3 cycles, the service reports and stops.",
    )
    project_path: str | None = Field(
        default=None,
        description="Optional repo root. When provided, impact-aware validation can include only directly related companion files.",
    )
    impact_aware: bool | None = Field(
        default=None,
        description="Default true via env. Adds only related companion files for template/assets checks; does not scan the full repo.",
    )
    max_related_files: int = Field(
        default=20, ge=0, le=100,
        description="Maximum related files to add during impact-aware validation.",
    )


class SyntaxVerifyResult(BaseModel):
    """Result of workflow.verify_against_core pre-push check."""
    push_blocked: bool = Field(
        ...,
        description="True = push BLOCKED, fix CRITICAL findings first. False = push is allowed.",
    )
    status: str = Field(..., description="Human-readable summary of the verification result")
    files_checked: list[str] = Field(default_factory=list, description="Files that were successfully verified")
    findings: list[VerifyFinding] = Field(default_factory=list, description="All findings (CRITICAL + WARNING + INFO)")
    critical_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    fix_cycle: int = 0


class PosChangeVerifyRequest(BaseModel):
    """Combined static + live-browser verification for a POS-touching change.

    This exists because verify_against_core alone only catches static
    pattern mistakes (regex + now real JS parse errors); it never proves the
    POS UI actually still works. This wraps verify_against_core AND
    browser.pos_flow behind one bounded fix-cycle counter, mirroring the
    existing verify_against_core contract (push_blocked / fix_cycle / max 3).
    """
    written_files: list[str] = Field(..., description="Absolute paths of files changed in this task (.py/.xml/.js/.scss/__manifest__.py).")
    fix_cycle: int = Field(default=0, description="Current fix cycle number (0-indexed). After 3 cycles, stop and report to the user.")
    project_path: str | None = Field(default=None)
    run_pos_flow: bool = Field(default=True, description="If true and any written_files look POS-related, also run a live browser.pos_flow check.")
    pos_flow_request: "PosFlowCheckRequest | None" = Field(default=None, description="Optional overrides (base_url/login/product_name/etc.) for the POS flow step.")


class PosChangeVerifyResult(BaseModel):
    push_blocked: bool
    status: str
    static_result: SyntaxVerifyResult
    pos_flow_result: "PosFlowCheckResult | None" = None
    fix_cycle: int = 0
    message: str

# ===========================================================================
# V15 — Strict Odoo Frontend / OWL / JS / XML Preflight
# ===========================================================================

class FrontendReadInstruction(BaseModel):
    """One source file or module area that must be explored before frontend coding."""
    source: str = Field(..., description="custom | odoo_source")
    module: str = Field(..., description="Odoo module technical name, or '(custom)'")
    subdir: str = Field("", description="Module subdirectory to inspect, e.g. static/src/app")
    reason: str = Field(..., description="Why this pattern source must be read")
    path: str = Field(..., description="Absolute path to read, or NOT_FOUND hint if the source was not found")
    mandatory: bool = Field(True, description="True means the agent must read this before writing code")


class FrontendPatternPreflightRequest(BaseModel):
    """Blocking preflight for Odoo JS/OWL/POS/website/payment/XML asset work.

    Call this after reading the target custom files and before writing any JS,
    OWL component, QWeb/XML template, SCSS asset, POS screen/store patch,
    website snippet/editor code, or payment frontend code.
    """
    task_description: str = Field(..., description="User task or short implementation description")
    target_area: str = Field(
        default="auto",
        description="auto | pos | website | web | payment. Use auto unless the area is known.",
    )
    target_module_name: str | None = Field(
        default=None,
        description="Custom or core module being edited, if known. Used to inspect local static/src and views first.",
    )
    custom_file_paths: list[str] = Field(
        default_factory=list,
        description="Absolute paths of existing custom files already identified/read for the task.",
    )
    planned_file_paths: list[str] = Field(
        default_factory=list,
        description="Planned changed/new file paths, useful for area auto-detection.",
    )
    search_roots: list[str] = Field(
        default_factory=list,
        description="Override source roots. Defaults to enterprise → odoo → user fallback.",
    )


class FrontendPatternPreflightResult(BaseModel):
    """Strict frontend preflight result consumed by the agent before writing."""
    target_area: str = Field(..., description="Detected target area: pos | website | web | payment | none")
    is_frontend_task: bool = Field(..., description="True when strict JS/OWL/XML preflight applies")
    push_blocked_until_done: bool = Field(..., description="True means do not write/push until mandatory reads are completed")
    mandatory_read_count: int = Field(0, description="Number of mandatory read instructions")
    instructions: list[FrontendReadInstruction] = Field(default_factory=list)
    explorer_calls: list[dict[str, str]] = Field(
        default_factory=list,
        description="Suggested workflow.list_module_files calls used/needed for source exploration",
    )
    required_asset_bundles: list[str] = Field(default_factory=list, description="Allowed/expected asset bundles for this area")
    strict_rules: list[str] = Field(default_factory=list, description="Rules the agent must follow while implementing")
    validation_steps: list[str] = Field(default_factory=list, description="Checks to run after implementation")
    message: str = Field(..., description="Human-readable preflight summary")


# ===========================================================================
# V16 — Fast/correct router, repo index, impact-aware validation
# ===========================================================================

class SmartToolRouteRequest(BaseModel):
    """Return the fastest safe MCP workflow for the current task."""
    task_description: str = Field(..., description="User task or implementation summary")
    project_path: str | None = Field(default=None, description="Repo path if already known")
    planned_file_paths: list[str] = Field(default_factory=list, description="Files likely to be read/written")
    changed_file_count: int | None = Field(default=None, ge=0, description="Expected number of files to change")
    risk_level: Literal["auto", "low", "medium", "high"] = Field(default="auto")


class SmartToolRouteStep(BaseModel):
    order: int
    tool: str
    reason: str
    required: bool = True


class SmartToolRouteResult(BaseModel):
    task_type: str
    frontend_required: bool
    recommended_write_tool: str
    validation_scope: str
    browser_check: str
    steps: list[SmartToolRouteStep] = Field(default_factory=list)
    fast_reason: str = ""


class RepoIndexRequest(BaseProjectRequest):
    force_refresh: bool = Field(default=False, description="Rebuild even if a fresh cached index exists")
    max_files: int = Field(default=5000, ge=100, le=50000, description="Safety cap for indexed files")


class RepoIndexModule(BaseModel):
    name: str
    path: str
    manifest_path: str
    depends: list[str] = Field(default_factory=list)
    assets: dict[str, list[str]] = Field(default_factory=dict)


class RepoIndexResult(BaseModel):
    project_path: str
    cached: bool
    generated_at: str
    total_files: int = 0
    modules: list[RepoIndexModule] = Field(default_factory=list)
    python_files: list[str] = Field(default_factory=list)
    xml_files: list[str] = Field(default_factory=list)
    js_files: list[str] = Field(default_factory=list)
    scss_files: list[str] = Field(default_factory=list)
    manifest_files: list[str] = Field(default_factory=list)
    template_names: dict[str, str] = Field(default_factory=dict, description="template name -> file path")
    notes: list[str] = Field(default_factory=list)


class QwenShortTokenResult(BaseModel):
    token: str
    expires_at: str
    ttl_seconds: int
    qwen_mcp_url: str
    note: str
