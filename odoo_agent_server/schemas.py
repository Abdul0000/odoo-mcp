from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


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
    repo_url: str | None = Field(default=None, description="Git repository URL. Defaults to TARGET_GIT_REPO.")
    branch: str | None = Field(default=None, description="Target branch. Defaults to TARGET_GIT_BRANCH.")
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


class RepoWriteRequest(BaseProjectRequest, ApprovalMixin):
    relative_path: str
    content: str
    create_dirs: bool = True
    # Chunked write support for large files (>300 lines)
    # chunk_index: 0-based index of this chunk
    # total_chunks: total number of chunks to expect
    # When total_chunks > 1, content is appended to existing file chunks
    chunk_index: int = Field(default=0, ge=0, description="0-based chunk index for large file writes")
    total_chunks: int = Field(default=1, ge=1, description="Total chunks. Use 1 for normal writes. >1 enables chunked mode.")


class RepoWriteItem(BaseModel):
    relative_path: str
    content: str
    create_dirs: bool = True


class RepoWriteManyAtomicRequest(BaseProjectRequest, ApprovalMixin):
    files: list[RepoWriteItem] = Field(..., min_length=1, max_length=200, description="Files to write as one atomic multi-file transaction")
    check_only: bool = Field(default=False, description="Validate the transaction without changing the real checkout")
    timeout_seconds: int = Field(default=60, ge=5, le=300)


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

    @field_validator("module_name")
    @classmethod
    def module_name_must_be_safe(cls, value: str) -> str:
        return validate_safe_name(value, "module_name")


class OdooSearchReadRequest(BaseModel):
    model: str
    domain: list[Any] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=200)


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
    console_errors: list[str] = Field(default_factory=list)
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


class JobStep(BaseModel):
    name: str
    status: str
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    result: Any = None
    error: str | None = None


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
    module_name: str = Field(..., description="Technical module folder/name to deploy and update")
    commit_message: str = Field(..., min_length=3, max_length=300)
    paths: list[str] = Field(default_factory=list, description="Relative paths to add before commit. Empty means git add -A.")
    remote: str = Field(default="origin")
    branch: str | None = Field(default=None, description="Defaults to current branch or TARGET_GIT_BRANCH")
    run_qa: bool = Field(default=True)
    wait_for_odoosh: bool = Field(default=True)
    upgrade_module: bool = Field(default=True)
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
    repo_url: str | None = Field(default=None, description="Git repository URL. Defaults to TARGET_GIT_REPO.")
    branch: str | None = Field(default=None, description="Target branch. Defaults to TARGET_GIT_BRANCH.")
    repo_name: str | None = Field(default=None, description="Folder name under workspace_root. Defaults to configured target project.")
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
    status: Literal["passed", "failed", "warning", "running"]
    job_id: str
    steps: list[JobStep]
    summary: dict[str, Any]


def validate_safe_name(value: str, field_name: str) -> str:
    if "/" in value or "\\" in value or value.strip() != value or not value:
        raise ValueError(f"{field_name} must be a safe folder/technical name")
    if value in {".", ".."}:
        raise ValueError(f"{field_name} must not be a relative path token")
    return value


class OdooShCheckCommitRequest(BaseModel):
    expected_sha: str = Field(..., min_length=7, description="Git SHA to check on Odoo.sh ~/src/user")


# ---------------------------------------------------------------------------
# Fix 3: Resume job from last successful step
# ---------------------------------------------------------------------------

class ResumeJobRequest(BaseModel):
    """Fix 3: Resume a safe_deploy_module job from its last successful step.

    Useful when a long multi-step pipeline (10+ steps) fails partway through.
    Instead of starting over from step 1, the agent calls workflow.resume_job
    with the job_id from the failed run.
    """
    job_id: str = Field(..., description="Job ID returned by a previous safe_deploy_module or start_session call")
    override_step: str | None = Field(
        default=None,
        description=(
            "If provided, start from this step name instead of the auto-detected last successful step. "
            "Supported values: 'qa', 'commit', 'push', 'wait_odoosh', 'upgrade', 'smoke_test'."
        ),
    )


# ---------------------------------------------------------------------------
# Fix 7: Session state request schemas
# ---------------------------------------------------------------------------

class SessionStateSaveRequest(BaseModel):
    """Fix 7: Save arbitrary key/value pairs to the session state store."""
    state: dict[str, Any] = Field(..., description="Key/value pairs to merge into the session state")


class SessionStateLoadRequest(BaseModel):
    """Fix 7: Load the last persisted session state."""
    pass  # no input needed


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
