from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..auth import AuthDependency
from ..config import settings
from ..schemas import (
    ApprovalCreateRequest,
    ApprovalDecisionRequest,
    AuditListRequest,
    BaseProjectRequest,
    BrowserCheckRequest,
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
    OdooShCheckCommitRequest,
    OdooShCommandRequest,
    OdooShLogRequest,
    OdooShModuleUpdateRequest,
    OdooShVerifyManifestRequest,
    OdooShWaitForCommitRequest,
    RepoCleanPycRequest,
    RepoCustomModulesRequest,
    RepoDeleteRequest,
    RepoListRequest,
    RepoPatchRequest,
    RepoPrepareRequest,
    RepoReadRequest,
    RepoWriteRequest,
    SelfRunRequest,
    ToolCallRequest,
    ToolCallResult,
    ToolDescriptor,
)
from ..services.git_service import GitService
from ..services.odoo_rpc_service import OdooRpcService
from ..services.odoosh_service import OdooShService
from ..services.playwright_service import PlaywrightService
from ..services.repo_service import RepoService
from .qa import self_run
from .repo import repo_delete, repo_write, repo_apply_patch, repo_clean_pyc, repo_prepare, repo_custom_modules
from .git import git_commit, git_push
from .odoo import odoo_module_install, odoo_module_update
from .odoosh import odoosh_module_update, odoosh_wait_for_commit, odoosh_verify_module_manifest
from .workflow import capability_check, full_verify, safe_deploy_module, coding_task_plan
from ..schemas import ApprovalRecord, ApprovalStatus
from ..services.store_service import JsonStore

router = APIRouter(prefix="/tools", tags=["tools"], dependencies=[AuthDependency])

_TOOL_DESCRIPTORS = [
    ToolDescriptor(name="connector.health", description="Check connector service health", read_only=True),
    ToolDescriptor(name="workflow.capability_check", description="Check local connector capabilities and missing runtime configuration", read_only=True),
    ToolDescriptor(name="workflow.coding_task_plan", description="Return the repo-first coding workflow guidance for ChatGPT MCP clients", read_only=True),
    ToolDescriptor(name="workflow.full_verify", description="Run the full self-verification workflow for an Odoo module", read_only=True),
    ToolDescriptor(name="workflow.safe_deploy_module", description="Safely commit, push, wait for Odoo.sh deployment, verify manifest, and upgrade an Odoo module", requires_approval=True),
    ToolDescriptor(name="qa.self_run", description="Run Odoo custom module static QA and optional pytest/git status", read_only=True),
    ToolDescriptor(name="repo.prepare", description="Clone or refresh target repo and checkout/pull the target branch before coding", requires_approval=True),
    ToolDescriptor(name="repo.list_custom_modules", description="Discover custom Odoo modules from repo __manifest__.py files", read_only=True),
    ToolDescriptor(name="repo.list", description="List files inside an allowed project path", read_only=True),
    ToolDescriptor(name="repo.read", description="Read a UTF-8 file inside an allowed project path", read_only=True),
    ToolDescriptor(name="repo.write", description="Write a file inside an allowed project path", requires_approval=True),
    ToolDescriptor(name="repo.delete", description="Delete a file inside an allowed project path", requires_approval=True),
    ToolDescriptor(name="repo.apply_patch", description="Check/apply a unified diff patch", requires_approval=True),
    ToolDescriptor(name="repo.clean_pyc", description="Remove .pyc and __pycache__ from a project", requires_approval=True),
    ToolDescriptor(name="git.status", description="Read git status", read_only=True),
    ToolDescriptor(name="git.current_branch", description="Read current git branch", read_only=True),
    ToolDescriptor(name="git.diff", description="Read git diff", read_only=True),
    ToolDescriptor(name="git.commit", description="Create a git commit", requires_approval=True),
    ToolDescriptor(name="git.push", description="Push current branch to remote", requires_approval=True),
    ToolDescriptor(name="odoosh.health", description="Check Odoo.sh SSH health", read_only=True),
    ToolDescriptor(name="odoosh.git_status", description="Read Odoo.sh deployed git status", read_only=True),
    ToolDescriptor(
        name="odoosh.check_commit",
        description=(
            "NON-BLOCKING single check: returns immediately with deployed=true/false. "
            "Use this in a polling loop after git.push: call once, read the 'deployed' field. "
            "If false, wait 15 seconds then call again. Repeat max 20 times. "
            "When deployed=true proceed to odoosh.module_update. Never use odoosh.wait_for_commit."
        ),
        read_only=True,
    ),
    ToolDescriptor(name="odoosh.wait_for_commit", description="[DEPRECATED - use odoosh.check_commit in a polling loop] Blocking wait for Odoo.sh deploy", read_only=True),
    ToolDescriptor(name="odoosh.verify_module_manifest", description="Verify a module manifest exists and is installable on Odoo.sh", read_only=True),
    ToolDescriptor(name="odoosh.log", description="Tail an Odoo.sh log", read_only=True),
    ToolDescriptor(name="odoosh.grep_errors", description="Search Odoo.sh logs for errors", read_only=True),
    ToolDescriptor(name="odoosh.module_update", description="Run Odoo module update through Odoo.sh SSH", requires_approval=True),
    ToolDescriptor(name="odoo.health", description="Check Odoo XML-RPC health", read_only=True),
    ToolDescriptor(name="odoo.module_status", description="Read Odoo module status", read_only=True),
    ToolDescriptor(name="odoo.module_install", description="Install an Odoo module via XML-RPC", requires_approval=True),
    ToolDescriptor(name="odoo.module_update", description="Upgrade an Odoo module via XML-RPC", requires_approval=True),
    ToolDescriptor(name="odoo.search_read", description="Run Odoo search_read via XML-RPC", read_only=True),
    ToolDescriptor(name="browser.screenshot", description="Capture Odoo page screenshot with console/network errors", read_only=True),
    ToolDescriptor(name="approvals.list", description="List pending/recent approval records", read_only=True),
    ToolDescriptor(name="approvals.request", description="Create an approval request", requires_approval=False),
    ToolDescriptor(name="approvals.approve", description="Approve an approval request", requires_approval=True),
    ToolDescriptor(name="approvals.reject", description="Reject an approval request", requires_approval=True),
    ToolDescriptor(name="jobs.list", description="List recent connector jobs", read_only=True),
    ToolDescriptor(name="jobs.get", description="Read one connector job by id", read_only=True),
    ToolDescriptor(name="audit.list", description="List recent audit events", read_only=True),
]

_TOOL_INPUT_MODELS = {
    "connector.health": EmptyRequest,
    "workflow.capability_check": EmptyRequest,
    "workflow.coding_task_plan": CodingTaskPlanRequest,
    "workflow.full_verify": FullVerifyRequest,
    "workflow.safe_deploy_module": SafeDeployModuleRequest,
    "qa.self_run": SelfRunRequest,
    "repo.prepare": RepoPrepareRequest,
    "repo.list_custom_modules": RepoCustomModulesRequest,
    "repo.list": RepoListRequest,
    "repo.read": RepoReadRequest,
    "repo.write": RepoWriteRequest,
    "repo.delete": RepoDeleteRequest,
    "repo.apply_patch": RepoPatchRequest,
    "repo.clean_pyc": RepoCleanPycRequest,
    "git.status": BaseProjectRequest,
    "git.current_branch": BaseProjectRequest,
    "git.diff": GitDiffRequest,
    "git.commit": GitCommitRequest,
    "git.push": GitPushRequest,
    "odoosh.health": EmptyRequest,
    "odoosh.git_status": EmptyRequest,
    "odoosh.check_commit": OdooShCheckCommitRequest,
    "odoosh.wait_for_commit": OdooShWaitForCommitRequest,
    "odoosh.verify_module_manifest": OdooShVerifyManifestRequest,
    "odoosh.log": OdooShLogRequest,
    "odoosh.grep_errors": OdooShCommandRequest,
    "odoosh.module_update": OdooShModuleUpdateRequest,
    "odoo.health": EmptyRequest,
    "odoo.module_status": OdooRpcModuleRequest,
    "odoo.module_install": OdooRpcModuleRequest,
    "odoo.module_update": OdooRpcModuleRequest,
    "odoo.search_read": OdooSearchReadRequest,
    "browser.screenshot": BrowserCheckRequest,
    "approvals.list": LimitRequest,
    "approvals.request": ApprovalCreateRequest,
    "approvals.approve": ApprovalDecisionRequest,
    "approvals.reject": ApprovalDecisionRequest,
    "jobs.list": LimitRequest,
    "jobs.get": JobGetRequest,
    "audit.list": AuditListRequest,
}


def tool_input_schema(name: str) -> dict:
    model = _TOOL_INPUT_MODELS.get(name, EmptyRequest)
    schema = model.model_json_schema()
    schema.setdefault("type", "object")
    schema.setdefault("additionalProperties", False)
    return schema


@router.get("/list", response_model=list[ToolDescriptor])
def list_tools() -> list[ToolDescriptor]:
    required = settings.approval_required_actions
    return [item.model_copy(update={"requires_approval": item.name in required}) for item in _TOOL_DESCRIPTORS]


@router.post("/call", response_model=ToolCallResult)
async def call_tool(payload: ToolCallRequest) -> ToolCallResult:
    try:
        result = await _dispatch(payload.name, payload.arguments)
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        return ToolCallResult(name=payload.name, status="ok", result=result)
    except HTTPException as exc:
        return ToolCallResult(name=payload.name, status="failed", error=str(exc.detail))
    except Exception as exc:  # noqa: BLE001
        return ToolCallResult(name=payload.name, status="failed", error=str(exc))


async def _dispatch(name: str, args: dict) -> object:
    if name == "connector.health":
        EmptyRequest.model_validate(args)
        return {"status": "ok", "service": "odoo-chatgpt-direct-connector"}
    if name == "workflow.capability_check":
        EmptyRequest.model_validate(args)
        return capability_check()
    if name == "workflow.coding_task_plan":
        return coding_task_plan(CodingTaskPlanRequest.model_validate(args))
    if name == "workflow.full_verify":
        return await full_verify(FullVerifyRequest.model_validate(args))
    if name == "workflow.safe_deploy_module":
        return safe_deploy_module(SafeDeployModuleRequest.model_validate(args))
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
    if name == "repo.write":
        return repo_write(RepoWriteRequest.model_validate(args))
    if name == "repo.delete":
        return repo_delete(RepoDeleteRequest.model_validate(args))
    if name == "repo.apply_patch":
        return repo_apply_patch(RepoPatchRequest.model_validate(args))
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
    if name == "odoosh.module_update":
        return odoosh_module_update(OdooShModuleUpdateRequest.model_validate(args))
    if name == "odoo.health":
        return OdooRpcService().health()
    if name == "odoo.module_status":
        req = OdooRpcModuleRequest.model_validate(args)
        return OdooRpcService().module_status(req.module_name)
    if name == "odoo.module_install":
        return odoo_module_install(OdooRpcModuleRequest.model_validate(args))
    if name == "odoo.module_update":
        return odoo_module_update(OdooRpcModuleRequest.model_validate(args))
    if name == "odoo.search_read":
        req = OdooSearchReadRequest.model_validate(args)
        return OdooRpcService().search_read(req.model, req.domain, req.fields, req.limit)
    if name == "browser.screenshot":
        req = BrowserCheckRequest.model_validate(args)
        return await PlaywrightService().screenshot_odoo_page(
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
    if name == "approvals.approve":
        req = ApprovalDecisionRequest.model_validate(args)
        return JsonStore().set_approval_status(req.approval_id, ApprovalStatus.approved, req.reason)
    if name == "approvals.reject":
        req = ApprovalDecisionRequest.model_validate(args)
        return JsonStore().set_approval_status(req.approval_id, ApprovalStatus.rejected, req.reason)
    if name == "jobs.list":
        req = LimitRequest.model_validate(args)
        return JsonStore().list_jobs(limit=req.limit)
    if name == "jobs.get":
        req = JobGetRequest.model_validate(args)
        return JsonStore().get_job(req.job_id)
    if name == "audit.list":
        req = AuditListRequest.model_validate(args)
        return JsonStore().list_audit(limit=req.limit)
    raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")
