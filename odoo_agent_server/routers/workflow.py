from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from threading import Lock

from fastapi import APIRouter, HTTPException, status

from ..auth import AuthDependency
from ..config import settings, get_client_names, mask_repo_url
from ..schemas import (
    CapabilityCheckResult,
    CodingTaskPlanRequest,
    CodingTaskPlanResult,
    CommandResult,
    DeployedModuleVerifyRequest,
    FullVerifyRequest,
    StartSessionRequest,
    RegistrySmokeTestRequest,
    RestoreLastGoodCommitRequest,
    MenuCrawlerRequest,
    JobRecord,
    JobStatus,
    JobStep,
    Severity,
    WorkflowReport,
    SafeDeployModuleRequest,
    SelfRunRequest,
    OdooRpcModuleRequest,
    OdooRpcResult,
    # V14: Self-Learning Agent — Core Discovery
    FindCoreFileRequest,
    FindCoreFileResult,
    FindSimilarViewRequest,
    FindSimilarViewResult,
    ListModuleFilesRequest,
    ListModuleFilesResult,
    # V14+: Pillar 2 — Adaptive Scope Advisor
    ScopeAdviceRequest,
    ScopeAdviceResult,
    # V14+: Pillar 3 — Pre-Push Syntax Verifier
    SyntaxVerifyRequest,
    SyntaxVerifyResult,
    # V17: Combined static + live POS browser verification with bounded fix-cycle
    PosChangeVerifyRequest,
    PosChangeVerifyResult,
    PosFlowCheckRequest,
    # V15: Strict Odoo frontend/OWL/JS/XML preflight
    FrontendPatternPreflightRequest,
    FrontendPatternPreflightResult,
    # V16: fast/correct routing and repo index
    SmartToolRouteRequest,
    SmartToolRouteResult,
    SmartToolRouteStep,
    RepoIndexRequest,
    RepoIndexResult,
)
from ..services.approval_service import ApprovalService
from ..services.client_resolver_service import ClientResolverService
from ..services.git_service import GitService
from ..services.odoo_rpc_service import OdooRpcService
from ..services.odoosh_service import OdooShService
from ..services.path_service import resolve_allowed_path
from ..services.browser_cdp_service import CdpBrowserService
from ..services.repo_service import RepoService
from ..services.store_service import JsonStore
from ..services.background_executor import submit_background
from ..services.core_finder_service import CoreFinderService          # V14
from ..services.syntax_verifier_service import SyntaxVerifierService  # V14+
from ..services.frontend_pattern_service import FrontendPatternService  # V15
from ..services.repo_index_service import RepoIndexService  # V16
from ..services.workflow_guard_service import (
    _ACTIVE_JOB_STATUSES,
    _RESUMABLE_SAFE_DEPLOY_RESULTS,
    find_blocking_safe_deploy,
    safe_deploy_block_payload,
)
from .qa import self_run
from .odoo import start_odoo_module_deploy_job

router = APIRouter(prefix="/workflow", tags=["workflow"], dependencies=[AuthDependency])

# Serializes the safe_deploy_module entry gate inside this process so two
# near-simultaneous retries cannot both pass the duplicate-job check before the
# first JobRecord is persisted. JsonStore still provides the durable state; this
# lock closes the in-process check/create window.
_SAFE_DEPLOY_ENTRY_LOCK = Lock()


def _looks_like_small_patch(task: str) -> bool:
    value = task.lower()
    small_terms = ("add field", "new field", "simple field", "notebook", "tab", "page", "xpath", "label")
    broad_terms = ("new module", "report", "dashboard", "integration", "migration", "workflow", "portal", "api")
    return any(term in value for term in small_terms) and not any(term in value for term in broad_terms)


def _looks_like_frontend_task(task: str) -> bool:
    value = task.lower().replace("-", "_")
    frontend_terms = (
        "js", "javascript", "owl", "component", "qweb", "xml template", "scss", "asset",
        "pos", "point_of_sale", "website", "snippet", "frontend", "editor",
        "payment", "checkout", "provider", "dashboard", "screen", "patch", "registry",
    )
    return any(term in value for term in frontend_terms)


@router.post("/coding-task-plan", response_model=CodingTaskPlanResult)
def coding_task_plan(payload: CodingTaskPlanRequest) -> CodingTaskPlanResult:
    """Return the canonical repo-first coding workflow for MCP agents."""
    client = ClientResolverService().resolve_client(
        client_name=payload.client_name,
        project_path=payload.project_path or settings.target_project_path,
        require_complete=False,
    )
    repo_url = payload.repo_url or (client.repo if client else None) or settings.target_git_repo or "<repo_url>"
    branch = payload.branch or (client.branch if client else None) or settings.target_git_branch or "<target_branch>"
    project_path = payload.project_path or (client.path if client else None) or settings.target_project_path or f"{settings.workspace_root.rstrip('/')}/<repo_name>"
    module_name = payload.module_name or "<custom_module>"
    small_patch = _looks_like_small_patch(payload.task)
    if small_patch:
        steps = [
            "Use the small-patch path: if session.load_state already has project_path/client_name, skip workflow.start_session; otherwise call workflow.start_session with include_odoo=false and include_odoosh=false.",
            "Call repo.prepare once, then read only the target module manifest plus the specific model/view files needed for the requested field or notebook change.",
            "If the patch touches JS/OWL/POS/website/payment/QWeb/XML/SCSS/assets, call workflow.frontend_pattern_preflight after reading custom files and read every mandatory source pattern before coding.",
            "For POS work, never use legacy load_fields('pos.config', ...) / models.load_fields for pos.config; read installed point_of_sale source and use the version's real loader/backend pattern.",
            "MANDATORY Odoo.sh source layer before writing: call workflow.scope_advice after reading custom files from the checkout, then repo.read every mandatory related installed-source path it returns. For editing, do not re-read the same current module from /home/odoo/src/user; /home/odoo/src/user is only for other related custom dependencies. For claims like already deployed/live/done, verify the current module with workflow.verify_deployed_module or odoosh.read_user_file.",
            "For field/view changes, never use direct ORM/Studio writes to ir.model.fields or ir.ui.view unless the user explicitly asked for no-code Studio customization. Normal development must patch Python/XML files in the custom module.",
            "For Odoo Python files, never import pydantic or create Pydantic BaseModel classes. Use Odoo ORM models.Model, fields, api, and XML views only.",
            "Patch the smallest file set with repo.apply_patch_atomic or repo.write_many_smart; use repo.write_many_atomic only for large/risky refactors. Do not run broad module discovery again after the target module is known.",
            "Run git.diff and qa.self_run with run_pytest=false unless Python tests are present or the patch touches shared logic.",
            "MANDATORY before deploy: run workflow.verify_against_core for changed .py/.xml/.js/__manifest__.py files. If push_blocked=true, fix and re-run; only then continue.",
            "Call workflow.safe_deploy_module with module_name, commit_message, changed paths, run_qa=false if qa.self_run already passed, wait_for_odoosh=true, upgrade_module=true, and expected_registry_fields for new fields when known. Do not manually chain git.commit/git.push/odoosh.check_commit/odoosh.module_update/odoo.module_deploy for the normal path; those lower-level tools are blocked once safe_deploy starts.",
            "If the repo already contains the requested change and no edit is needed, do not say done from repo.read alone; call workflow.verify_deployed_module and continue deploy/resume if /home/odoo/src/user does not match.",
            "If safe deploy returns retry_later, wait briefly and call workflow.resume_job with the job_id. If it returns deploy_running, call jobs.get on the nested job. Do not repeat commit/push.",
            "Verify the exact field with odoo.search_read or workflow.registry_smoke_test; if the user asks for visible UI proof, pass the exact Odoo web path in verify_ui_paths during deploy or call workflow.browser_menu_crawl once.",
        ]
        required_tools = [
            "session.load_state",
            "repo.prepare",
            "repo.read",
            "workflow.scope_advice",
            "repo.apply_patch_atomic",
            "repo.write_many_smart",
            "git.diff",
            "qa.self_run",
            "workflow.verify_against_core",
            "workflow.frontend_pattern_preflight",
            "workflow.safe_deploy_module",
            "workflow.verify_deployed_module",
            "workflow.resume_job",
            "jobs.get",
            "odoo.search_read",
        ]
        summary = "Fast path for a small targeted Odoo code patch."
    else:
        steps = [
            "Call repo.prepare with the target repo and branch. This clones if missing, otherwise fetches, checks out, and pulls the branch with --ff-only.",
            "Call repo.list_custom_modules on the returned project_path; choose the target custom module from __manifest__.py files, not from Odoo's installed module list only.",
            f"Read the target module files first: {module_name}/__manifest__.py, models, views, security, and relevant static files.",
            "For JS/OWL/POS/website/payment/QWeb/XML/SCSS/assets, call workflow.frontend_pattern_preflight with task_description, target_module_name, custom_file_paths, and planned_file_paths; then repo.read all mandatory pattern files returned before implementation.",
            "For POS work, never use legacy load_fields('pos.config', ...) / models.load_fields for pos.config; read installed point_of_sale source and use the version's real loader/backend pattern.",
            "MANDATORY Odoo.sh source layer before writing: call workflow.scope_advice after reading custom files from the checkout, then repo.read every mandatory related installed-source path it returns. For editing, do not re-read the same current module from /home/odoo/src/user; /home/odoo/src/user is only for other related custom dependencies. For claims like already deployed/live/done, verify the current module with workflow.verify_deployed_module or odoosh.read_user_file.",
            "For field/view changes, never use direct ORM/Studio writes to ir.model.fields or ir.ui.view unless the user explicitly asked for no-code Studio customization. Normal development must patch Python/XML files in the custom module.",
            "For Odoo Python files, never import pydantic or create Pydantic BaseModel classes. Use Odoo ORM models.Model, fields, api, and XML views only.",
            "Apply edits safely: use repo.apply_patch_atomic for multi-file diffs or repo.write_many_smart for normal multi-file writes. Use repo.write_many_atomic only for large/risky refactors, and repo.apply_patch/repo.write only for small single-file changes. Do not edit Odoo core, enterprise, .git, or unrelated modules.",
            "Run git.diff and qa.self_run. Treat pytest environment failures separately from real module errors.",
            "MANDATORY before deploy: run workflow.verify_against_core for changed .py/.xml/.js/__manifest__.py files. If push_blocked=true, fix and re-run; do not deploy while blocked.",
            "Call workflow.safe_deploy_module with module_name, commit_message, changed paths, wait_for_odoosh=true, upgrade_module=true, and exact verify_ui_paths for UI/POS/website/view tasks. It owns commit, push, non-blocking Odoo.sh check, module deploy, module status, smoke test, and optional browser verification.",
            "If the repo already contains the requested change and no edit is needed, do not say done from repo.read alone; call workflow.verify_deployed_module and continue deploy/resume if /home/odoo/src/user does not match.",
            "If safe_deploy returns retry_later, wait briefly and call workflow.resume_job with the job_id. If it returns deploy_running, call jobs.get on the nested job. Do not repeat commit/push.",
            "Finish only after safe deploy/resume reports passed, or report the exact blocker and next_call returned by the workflow.",
        ]
        required_tools = [
            "repo.prepare",
            "repo.list_custom_modules",
            "repo.read",
            "workflow.frontend_pattern_preflight",
            "workflow.scope_advice",
            "repo.apply_patch_atomic",
            "repo.write_many_smart",
            "repo.apply_patch",
            "git.diff",
            "qa.self_run",
            "workflow.verify_against_core",
            "workflow.safe_deploy_module",
            "workflow.verify_deployed_module",
            "workflow.resume_job",
            "jobs.get",
        ]
        summary = "Canonical repo-first coding workflow for Odoo MCP development."
    return CodingTaskPlanResult(
        summary=summary,
        ordered_steps=steps,
        required_tools=required_tools,
        guardrails=[
            "Always prepare/pull the repo before reading or patching code.",
            "Always use the configured target branch; never push to default/main by assumption.",
            "Discover custom modules from repo manifests before relying on Odoo database state.",
            "Use atomic multi-file tools for broad changes so incomplete states are not left in the checkout.",
            "Only patch the requested module and related files.",
            "Do not upgrade Odoo.sh until the expected pushed SHA is deployed.",
            "Do not answer by printing MCP JSON blocks for the user/client to execute; call the MCP tools yourself.",
            "Do not create fields/views by direct ir.model.fields or ir.ui.view database writes for normal module-code requests.",
            "For small patches, avoid repeated tool discovery, broad source reads, full health checks, and browser diagnostics unless they are needed to prove the requested change.",
            "For hard UI/POS/website/accounting view tasks, verify exact affected views in a browser after upgrade using verify_ui_paths or workflow.browser_menu_crawl.",
            "For JS/OWL/POS/website/payment/QWeb/XML/SCSS/assets, frontend_pattern_preflight is blocking: do not write code until real framework source patterns are read.",
        ],
        defaults={
            "repo_url": repo_url,
            "branch": branch,
            "project_path": project_path,
            "client_name": client.name if client else payload.client_name,
            "module_name": module_name,
            "task": payload.task,
            "small_patch_fast_path": small_patch,
        },
    )


@router.get("/capability-check", response_model=CapabilityCheckResult)
def capability_check() -> CapabilityCheckResult:
    checks: dict[str, dict] = {}
    checks["allowed_roots"] = {"ok": bool(settings.allowed_roots), "value": [str(p) for p in settings.allowed_roots]}
    checks["git"] = {"ok": shutil.which("git") is not None, "path": shutil.which("git")}
    checks["ssh"] = {"ok": shutil.which("ssh") is not None, "path": shutil.which("ssh")}
    checks["odoo_rpc_config"] = {"ok": all([
        os.environ.get("ODOO_BASE_URL") or settings.odoo_base_url,
        os.environ.get("ODOO_DB_NAME") or settings.odoo_db_name,
        os.environ.get("ODOO_LOGIN") or settings.odoo_login,
        os.environ.get("ODOO_PASSWORD") or settings.odoo_password,
    ])}
    checks["odoosh_config"] = {"ok": bool(settings.odoosh_ssh_host), "host_configured": bool(settings.odoosh_ssh_host)}
    checks["state_dir"] = {"ok": settings.state_path.exists(), "path": str(settings.state_path)}
    checks["browser_artifact_dir"] = {"ok": Path(settings.screenshot_dir).expanduser().resolve().parent.exists(), "path": settings.screenshot_dir}
    cdp_url = os.environ.get("ODOO_AGENT_CDP_URL") or settings.cdp_url
    checks["cdp_browser"] = {
        "ok": bool(cdp_url),
        "url_configured": bool(cdp_url),
        "note": "Set ODOO_AGENT_CDP_URL to a Chrome remote debugging endpoint for UI console diagnostics.",
    }
    failed = [key for key, item in checks.items() if not item.get("ok")]
    if not failed:
        status = "ready"
    elif len(failed) < len(checks):
        status = "partial"
    else:
        status = "failed"
    return CapabilityCheckResult(status=status, checks=checks)




@router.post("/start-session", response_model=WorkflowReport)
def start_session(payload: StartSessionRequest) -> WorkflowReport:
    """V15 Multi-Client bootstrap for every MCP agent session.

    Pass client_name to auto-resolve repo, branch, path, token, and Odoo creds
    from ODOO_CLIENTS env var. Falls back to TARGET_GIT_* env vars if no client_name.

    Also confirms runtime capability, prepares the repo, reads git status,
    lists custom modules, and checks Odoo/Odoo.sh connectivity.
    """
    # ── Multi-Client Resolution ──────────────────────────────────────────
    resolver = ClientResolverService()
    client = resolver.resolve_client(
        client_name=payload.client_name,
        project_path=settings.target_project_path,
        require_complete=False,
    )
    resolver.apply_odoo_env(client)
    if client:
        payload = payload.model_copy(update={
            "client_name": client.name,
            "repo_url": payload.repo_url or client.repo,
            "branch": payload.branch or client.branch,
            "repo_name": payload.repo_name or Path(client.path).name,
            "workspace_root": payload.workspace_root or str(Path(client.path).parent),
        })

    store = JsonStore()
    job = store.create_job(JobRecord(job_type="start_session", status=JobStatus.running, request=payload.model_dump(mode="json")))

    def step(name: str, status_: str, result=None, error: str | None = None) -> None:
        current = store.get_job(job.id)
        store.add_step(current, JobStep(name=name, status=status_, finished_at=datetime.now(timezone.utc).isoformat(), result=result.model_dump(mode="json") if hasattr(result, "model_dump") else result, error=error))

    flags: list[str] = []
    cap = capability_check()
    step("capability_check", cap.status, cap)
    if cap.status != "ready":
        flags.append("warning")

    prepared = None
    try:
        prepared = RepoService().prepare_repo(
            payload.repo_url,
            payload.branch,
            payload.repo_name,
            payload.workspace_root,
            payload.remote,
            payload.timeout_seconds,
            client.path if client else settings.target_project_path,
            client.name if client else payload.client_name,
        )
        step("repo_prepare", "passed", prepared)
        status_result = GitService().status(prepared.project_path)
        step("git_status", "passed" if status_result.exit_code == 0 else "warning", status_result)
        modules = RepoService().list_custom_modules(prepared.project_path, payload.max_depth, include_uninstallable=False)
        step("repo_list_custom_modules", "passed", {"count": len(modules), "modules": [m.model_dump(mode="json") for m in modules]})
    except Exception as exc:  # noqa: BLE001
        step("repo_prepare", "failed", error=str(exc))
        flags.append("failed")

    if payload.include_odoo:
        try:
            odoo_health = OdooRpcService().health()
            step("odoo_health", "passed" if odoo_health.status == "ok" else "warning", odoo_health)
            if odoo_health.status != "ok":
                flags.append("warning")
        except Exception as exc:  # noqa: BLE001
            step("odoo_health", "warning", error=str(exc))
            flags.append("warning")

    if payload.include_odoosh:
        try:
            sh_health = OdooShService().health()
            step("odoosh_health", "passed" if sh_health.exit_code == 0 else "warning", sh_health)
            if sh_health.exit_code != 0:
                flags.append("warning")
        except Exception as exc:  # noqa: BLE001
            step("odoosh_health", "warning", error=str(exc))
            flags.append("warning")

    final = "failed" if "failed" in flags else "warning" if "warning" in flags else "passed"
    final_job_status = JobStatus.failed if final == "failed" else JobStatus.warning if final == "warning" else JobStatus.passed
    finished = store.finish_job(store.get_job(job.id), final_job_status, {"status": final, "project_path": prepared.project_path if prepared else None})
    # Fix 7: persist session state so a new session after Render restart can reload it
    store.save_session_state({
        "last_job_id": job.id,
        "last_sha": prepared.sha if prepared else None,
        "project_path": prepared.project_path if prepared else None,
        "branch": prepared.branch if prepared else None,
        "start_session_status": final,
        "client_name": client.name if client else payload.client_name or None,
        "client": client.public if client else None,
        "repo_url_safe": mask_repo_url(payload.repo_url or "") if payload.repo_url else None,
        "odoo_url": client.odoo_url if client else os.environ.get("ODOO_BASE_URL") or settings.odoo_base_url,
        "odoo_db": client.odoo_db if client else os.environ.get("ODOO_DB_NAME") or settings.odoo_db_name,
    }, client_name=client.name if client else payload.client_name or None)
    return WorkflowReport(status=final, job_id=job.id, steps=finished.steps, summary={"status": final, "project_path": prepared.project_path if prepared else None, "sha": prepared.sha if prepared else None})


@router.post("/registry-smoke-test", response_model=OdooRpcResult)
def registry_smoke_test(payload: RegistrySmokeTestRequest) -> OdooRpcResult:
    return OdooRpcService().registry_smoke_test(payload.module_name, payload.model_names, payload.menu_names, payload.strict)


@router.post("/restore-last-good-commit", response_model=WorkflowReport)
def restore_last_good_commit(payload: RestoreLastGoodCommitRequest) -> WorkflowReport:
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="restore_last_good_commit", status=JobStatus.running, request=payload.model_dump(mode="json")))
    git = GitService()

    def step(name: str, status_: str, result=None, error: str | None = None) -> None:
        current = store.get_job(job.id)
        store.add_step(current, JobStep(name=name, status=status_, finished_at=datetime.now(timezone.utc).isoformat(), result=result.model_dump(mode="json") if hasattr(result, "model_dump") else result, error=error))

    try:
        project_path = str(resolve_allowed_path(payload.project_path))
        step("resolve_project", "passed", {"project_path": project_path})
        target = git.validate_target(project_path, payload.branch or settings.target_git_branch, payload.remote)
        step("git_validate_target", "passed" if target.exit_code == 0 else "failed", target)
        if target.exit_code != 0:
            finished = store.finish_job(store.get_job(job.id), JobStatus.failed, {"failed_step": "git_validate_target"})
            return WorkflowReport(status="failed", job_id=job.id, steps=finished.steps, summary={"failed_step": "git_validate_target"})
        revert = git.revert_commit(project_path, payload.bad_sha, payload.timeout_seconds)
        step("git_revert_bad_sha", "passed" if revert.exit_code == 0 else "failed", revert)
        if revert.exit_code != 0:
            finished = store.finish_job(store.get_job(job.id), JobStatus.failed, {"failed_step": "git_revert_bad_sha"})
            return WorkflowReport(status="failed", job_id=job.id, steps=finished.steps, summary={"failed_step": "git_revert_bad_sha"})
        if payload.push:
            pushed = git.push(project_path, payload.remote, payload.branch or settings.target_git_branch, True, payload.timeout_seconds)
            step("git_push_restore", "passed" if pushed.exit_code == 0 else "failed", pushed)
            if pushed.exit_code != 0:
                finished = store.finish_job(store.get_job(job.id), JobStatus.failed, {"failed_step": "git_push_restore"})
                return WorkflowReport(status="failed", job_id=job.id, steps=finished.steps, summary={"failed_step": "git_push_restore"})
    except Exception as exc:  # noqa: BLE001
        step("exception", "failed", error=str(exc))
        finished = store.finish_job(store.get_job(job.id), JobStatus.failed, {"error": str(exc)})
        return WorkflowReport(status="failed", job_id=job.id, steps=finished.steps, summary={"error": str(exc)})
    finished = store.finish_job(store.get_job(job.id), JobStatus.passed, {"bad_sha": payload.bad_sha, "pushed": payload.push})
    return WorkflowReport(status="passed", job_id=job.id, steps=finished.steps, summary={"bad_sha": payload.bad_sha, "pushed": payload.push})


@router.post("/browser-menu-crawl", response_model=WorkflowReport)
async def browser_menu_crawl(payload: MenuCrawlerRequest) -> WorkflowReport:
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="browser_menu_crawl", status=JobStatus.running, request=payload.model_dump(mode="json")))
    flags: list[str] = []
    failed = False
    for index, path in enumerate(payload.paths, start=1):
        result = await CdpBrowserService().screenshot_odoo_page(payload.base_url, payload.login, payload.password, path, f"{payload.screenshot_prefix}_{index}.png", payload.timeout_seconds, full_page=True)
        status_ = "passed" if result.status == "ok" and not result.console_errors and not result.network_errors else "failed" if result.status == "failed" else "warning"
        if status_ == "failed":
            failed = True
        elif status_ != "passed":
            flags.append("warning")
        store.add_step(store.get_job(job.id), JobStep(name=f"browser_path:{path}", status=status_, finished_at=datetime.now(timezone.utc).isoformat(), result=result.model_dump(mode="json")))
    final = "failed" if failed else "warning" if flags else "passed"
    job_status = JobStatus.failed if failed else JobStatus.warning if flags else JobStatus.passed
    finished = store.finish_job(store.get_job(job.id), job_status, {"status": final, "paths": payload.paths})
    return WorkflowReport(status=final, job_id=job.id, steps=finished.steps, summary={"status": final, "paths": payload.paths})


@router.post("/full-verify", response_model=WorkflowReport)
async def full_verify(payload: FullVerifyRequest) -> WorkflowReport:
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="full_verify", status=JobStatus.running, request=payload.model_dump(mode="json")))

    def step(name: str, status_: str, result=None, error: str | None = None) -> None:
        current_job = store.get_job(job.id)
        store.add_step(
            current_job,
            JobStep(
                name=name,
                status=status_,
                finished_at=datetime.now(timezone.utc).isoformat(),
                result=result.model_dump(mode="json") if hasattr(result, "model_dump") else result,
                error=error,
            ),
        )

    status_flags: list[str] = []

    try:
        project_path = resolve_allowed_path(payload.project_path)
        module_path = project_path / payload.module_name
        step("resolve_project", "passed", {"project_path": str(project_path), "module_path": str(module_path)})
    except Exception as exc:  # noqa: BLE001
        step("resolve_project", "failed", error=str(exc))
        finished = store.finish_job(job, JobStatus.failed, {"reason": "resolve_project failed"})
        return WorkflowReport(status="failed", job_id=job.id, steps=finished.steps, summary={"failed_step": "resolve_project"})

    if payload.clean_pyc:
        try:
            result = RepoService().clean_pyc(str(project_path), payload.module_name)
            step("clean_pyc", "passed", result)
        except Exception as exc:  # noqa: BLE001
            step("clean_pyc", "failed", error=str(exc))
            status_flags.append("failed")

    qa_report = self_run(payload)
    step("qa_self_run", qa_report.status, qa_report)
    if qa_report.status == "failed":
        status_flags.append("failed")
    elif qa_report.status == "warning":
        status_flags.append("warning")

    if payload.include_git_diff:
        git_result = GitService().diff(str(project_path), staged=False, timeout_seconds=30)
        step("git_diff", "passed" if git_result.exit_code == 0 else "warning", git_result)
        if git_result.exit_code != 0:
            status_flags.append("warning")

    if payload.include_odoosh_errors:
        try:
            err_result = OdooShService().grep_errors(tail_lines=300)
            status_ = "passed" if err_result.exit_code == 0 else "warning"
            step("odoosh_grep_errors", status_, err_result)
            if "Traceback" in err_result.stdout or "ERROR" in err_result.stdout:
                status_flags.append("warning")
        except Exception as exc:  # noqa: BLE001
            step("odoosh_grep_errors", "warning", error=str(exc))
            status_flags.append("warning")

    if payload.include_odoo_module_status:
        result = OdooRpcService().module_status(payload.module_name)
        step("odoo_module_status", "passed" if result.status == "ok" else "warning", result)
        if result.status != "ok":
            status_flags.append("warning")

    if payload.take_browser_screenshot:
        result = await CdpBrowserService().screenshot_odoo_page(
            None,
            None,
            None,
            payload.browser_start_path,
            payload.screenshot_name,
            timeout_seconds=45,
            full_page=True,
        )
        step("browser_screenshot", "passed" if result.status == "ok" else "warning", result)
        if result.status != "ok" or result.console_errors or result.network_errors:
            status_flags.append("warning")

    final_status = "failed" if "failed" in status_flags else "warning" if "warning" in status_flags else "passed"
    final_job_status = JobStatus.failed if final_status == "failed" else JobStatus.warning if final_status == "warning" else JobStatus.passed
    fresh_job = store.get_job(job.id)
    finished = store.finish_job(
        fresh_job,
        final_job_status,
        {"status": final_status, "module_name": payload.module_name, "project_path": str(project_path)},
    )
    return WorkflowReport(
        status=final_status,
        job_id=job.id,
        steps=finished.steps,
        summary={
            "module_name": payload.module_name,
            "project_path": str(project_path),
            "step_count": len(finished.steps),
            "result": final_status,
        },
    )


@router.post("/safe-deploy-module", response_model=WorkflowReport)
async def safe_deploy_module(payload: SafeDeployModuleRequest) -> WorkflowReport:
    """Start the single owner deploy pipeline for one project/module.

    This entry point is intentionally idempotent for the same
    ``project_path + module_name``. Some MCP clients retry a tool call when the
    HTTP stream times out even though the first request already persisted a
    background job. Before creating a new JobRecord, we look for an existing
    active or resumable safe_deploy_module job and return that job instead of
    spawning a second thread that could race git commit/push or Odoo.sh module
    upgrade.
    """

    def _existing_job_report(job: JobRecord) -> WorkflowReport:
        request = job.request or {}
        result = job.result if isinstance(job.result, dict) else {}
        result_status = result.get("status")
        job_status = getattr(job.status, "value", str(job.status))
        block_payload = safe_deploy_block_payload("safe_deploy_module", job)

        if job.status in _ACTIVE_JOB_STATUSES:
            report_status = "already_in_progress"
            message = (
                "A workflow.safe_deploy_module job is already in progress for this "
                "project_path + module_name. No new deploy thread was started; "
                "poll the existing job instead of replaying commit/push/upgrade."
            )
            next_call = {"tool": "jobs.get", "arguments": {"job_id": job.id, "wait_for_result": True}}
            blocking_reason = "active_job"
        elif result_status in _RESUMABLE_SAFE_DEPLOY_RESULTS:
            report_status = "resumable_existing_job"
            message = (
                "A previous workflow.safe_deploy_module job for this project_path + "
                "module_name stopped in a resumable state. No new deploy thread was "
                "started; resume the existing job, or call safe_deploy_module with "
                "force_new_deploy=true if a fresh deploy is genuinely required."
            )
            next_call = {"tool": "workflow.resume_job", "arguments": {"job_id": job.id}}
            blocking_reason = "resumable_job"
        else:
            report_status = "already_in_progress"
            # Defensive fallback; find_blocking_safe_deploy should only return
            # active/resumable safe deploy jobs.
            message = block_payload.get("message") or "Existing safe deploy job found."
            next_call = block_payload.get("next_call") or {"tool": "jobs.get", "arguments": {"job_id": job.id}}
            blocking_reason = "existing_job"

        return WorkflowReport(
            status=report_status,
            job_id=job.id,
            steps=job.steps,
            summary={
                **block_payload,
                **result,
                "status": report_status,
                "blocking_reason": blocking_reason,
                "message": message,
                "job_id": job.id,
                "job_status": job_status,
                "result_status": result_status,
                "module_name": request.get("module_name") or payload.module_name,
                "project_path": request.get("project_path") or payload.project_path,
                "force_new_deploy": False,
                "next_call": next_call,
            },
        )

    try:
        approval = ApprovalService()
        approval.ensure_allowed("workflow.safe_deploy_module", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))

        with _SAFE_DEPLOY_ENTRY_LOCK:
            existing_job = None
            if not payload.force_new_deploy:
                existing_job = find_blocking_safe_deploy(
                    project_path=payload.project_path,
                    module_name=payload.module_name,
                    client_name=payload.client_name,
                )
            if existing_job is not None:
                return _existing_job_report(existing_job)

            store = JsonStore()
            job = store.create_job(JobRecord(
                job_type="safe_deploy_module",
                status=JobStatus.running,
                request=payload.model_dump(mode="json"),
            ))
            store.save_session_state({
                "last_safe_deploy_job_id": job.id,
                "module_name": payload.module_name,
                "project_path": payload.project_path,
                "branch": payload.branch,
                "client_name": payload.client_name,
                "safe_deploy_status": "running",
            }, client_name=payload.client_name)

        def _background():
            try:
                _run_safe_deploy(payload, job.id)
            except Exception as exc:
                try:
                    store2 = JsonStore()
                    j = store2.get_job(job.id)
                    if j.status == JobStatus.running:
                        store2.finish_job(j, JobStatus.failed, {"error": f"Unhandled exception: {exc}"})
                except Exception:
                    # The foreground response already returned the job id. Avoid
                    # leaking background exception handling back to the MCP stream.
                    pass

        submit_background(_background)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return WorkflowReport(
            status="failed",
            job_id="",
            steps=[
                JobStep(
                    name="safe_deploy_start",
                    status="failed",
                    error=str(exc),
                )
            ],
            summary={
                "message": "Safe deploy could not start. No commit or push was attempted.",
                "error": str(exc),
                "module_name": payload.module_name,
            },
        )

    if payload.wait_for_result:
        waited = store.wait_job(job.id, payload.wait_timeout_seconds, payload.poll_interval_seconds)
        if waited.status not in {JobStatus.queued, JobStatus.running}:
            final_status: Literal["passed", "failed", "warning", "running"] = "passed" if waited.status == JobStatus.passed else "failed" if waited.status == JobStatus.failed else "warning"
            result = waited.result if isinstance(waited.result, dict) else {"result": waited.result}
            return WorkflowReport(
                status=final_status,
                job_id=job.id,
                steps=waited.steps,
                summary={
                    **result,
                    "job_id": job.id,
                    "module_name": payload.module_name,
                    "message": result.get("message") or f"Safe deploy job finished with status {waited.status.value}.",
                },
            )

    return WorkflowReport(
        status="running",
        job_id=job.id,
        steps=[],
        summary={
            "status": "running",
            "message": "Safe deploy is still running. Do not tell the user it is complete yet; call jobs.get with this job_id until status is passed, failed, or warning with next_call.",
            "job_id": job.id,
            "module_name": payload.module_name,
            "next_call": {"tool": "jobs.get", "arguments": {"job_id": job.id, "wait_for_result": True}},
        },
    )


def _pre_push_verify_files(project: Path, module_name: str, paths: list[str], status_output: str) -> list[str]:
    """Return completed written module files that must pass pre-push verification."""
    allowed_suffixes = (".py", ".xml", ".js", ".scss", "__manifest__.py")
    module_prefix = f"{module_name}/"

    def status_candidates() -> list[str]:
        found: list[str] = []
        for raw_line in status_output.splitlines():
            if not raw_line or raw_line.startswith("##"):
                continue
            path_part = raw_line[3:].strip() if len(raw_line) > 3 else raw_line.strip()
            if " -> " in path_part:
                path_part = path_part.rsplit(" -> ", 1)[-1].strip()
            if path_part:
                found.append(path_part)
        return found

    def collect(candidates: list[str]) -> list[str]:
        written: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            rel = candidate.replace("\\", "/").strip()
            if not rel or rel.startswith(".git/"):
                continue
            target = (project / rel).resolve()
            if not (target == project or project in target.parents):
                continue
            targets = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file()) if target.is_dir() else []
            for file_target in targets:
                rel_posix = file_target.relative_to(project).as_posix()
                if not (rel_posix == f"{module_name}/__manifest__.py" or rel_posix.startswith(module_prefix)):
                    continue
                if not rel_posix.endswith(allowed_suffixes):
                    continue
                value = str(file_target)
                if value not in seen:
                    seen.add(value)
                    written.append(value)
        return written

    written = collect(paths) if paths else []
    if written:
        return written
    return collect(status_candidates())


def _expected_user_file_hashes(project: Path, verify_files: list[str]) -> dict[str, str]:
    """Return repo-relative SHA-256 hashes for changed files deployed to ~/src/user."""
    expected: dict[str, str] = {}
    root = project.resolve()
    for raw_path in verify_files:
        file_path = Path(raw_path).resolve()
        if not file_path.is_file() or not (file_path == root or root in file_path.parents):
            continue
        rel = file_path.relative_to(root).as_posix()
        if not rel or rel.startswith(".git/"):
            continue
        expected[rel] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return expected


def _deployed_compare_files(project: Path, module_name: str, paths: list[str], max_files: int) -> list[str]:
    if paths:
        return _pre_push_verify_files(project, module_name, paths, "")
    allowed_suffixes = (".py", ".xml", ".js", ".scss", "__manifest__.py")
    module_path = (project / module_name).resolve()
    if not module_path.is_dir() or not (module_path == project or project in module_path.parents):
        return []
    files: list[str] = []
    for file_path in sorted(p for p in module_path.rglob("*") if p.is_file()):
        rel = file_path.relative_to(project).as_posix()
        if rel.startswith(".git/") or not rel.endswith(allowed_suffixes):
            continue
        files.append(str(file_path))
        if len(files) >= max_files:
            break
    return files


@router.post("/verify-deployed-module")
def verify_deployed_module(payload: DeployedModuleVerifyRequest) -> dict:
    """Compare active checkout files to real Odoo.sh /home/odoo/src/user files.

    This is the source-of-truth check for claims like "already added", "already
    deployed", or "live now". repo.read only proves the GitHub/local checkout;
    this proves the Odoo.sh user source.
    """
    project = resolve_allowed_path(payload.project_path)
    compare_files = _deployed_compare_files(project, payload.module_name, payload.paths, payload.max_files)
    expected_hashes = _expected_user_file_hashes(project, compare_files)
    odoosh = OdooShService()
    list_result = odoosh.list_user_files(payload.module_name, max_items=min(payload.max_files, 2000))
    if expected_hashes:
        hash_result = odoosh.verify_user_file_hashes(expected_hashes)
        source_status = "matched" if hash_result.exit_code == 0 else "mismatch"
    else:
        hash_result = CommandResult(
            command=["workflow.verify_deployed_module"],
            cwd=str(project),
            exit_code=2,
            stderr="No local module code/XML/assets files were found to compare.",
        )
        source_status = "no_local_files"
    module_status = None
    if payload.include_module_status:
        module_status = OdooRpcService().module_status(payload.module_name).model_dump(mode="json")
    return {
        "status": "passed" if source_status == "matched" else "failed",
        "source_status": source_status,
        "message": (
            "Odoo.sh /home/odoo/src/user matches the active checkout for the compared files."
            if source_status == "matched"
            else "Odoo.sh /home/odoo/src/user does not prove the active checkout is deployed. Do not claim live/deployed/already done."
        ),
        "module_name": payload.module_name,
        "project_path": str(project),
        "compared_files": sorted(expected_hashes),
        "odoosh_user_files": list_result,
        "hash_check": hash_result.model_dump(mode="json"),
        "module_status": module_status,
    }



def _infer_registry_fields_from_python(project: Path, module_name: str, verify_files: list[str]) -> dict[str, list[str]]:
    """Infer Odoo model fields from changed Python files for post-upgrade smoke.

    This catches the exact class of false success where a field was committed but
    the live registry never loaded it.  It intentionally only reads the active
    checkout and verifies after upgrade through ir.model.fields.
    """
    root = project.resolve()
    inferred: dict[str, set[str]] = {}
    for raw_path in verify_files:
        file_path = Path(raw_path).resolve()
        if file_path.suffix != ".py" or not file_path.is_file() or not (file_path == root or root in file_path.parents):
            continue
        rel = file_path.relative_to(root).as_posix()
        if not rel.startswith(f"{module_name}/"):
            continue
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            model_name: str | None = None
            for stmt in node.body:
                if not isinstance(stmt, ast.Assign):
                    continue
                targets = [target.id for target in stmt.targets if isinstance(target, ast.Name)]
                if not targets:
                    continue
                if any(target in {"_inherit", "_name"} for target in targets):
                    value = stmt.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        model_name = value.value
                    elif isinstance(value, (ast.List, ast.Tuple)):
                        names = [elt.value for elt in value.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
                        if names:
                            model_name = names[0]
                    continue
            if not model_name:
                continue
            for stmt in node.body:
                if not isinstance(stmt, ast.Assign):
                    continue
                is_odoo_field = isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Attribute) and isinstance(stmt.value.func.value, ast.Name) and stmt.value.func.value.id == "fields"
                if not is_odoo_field:
                    continue
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        inferred.setdefault(model_name, set()).add(target.id)
    return {model: sorted(fields) for model, fields in sorted(inferred.items()) if fields}


def _normalise_expected_registry_fields(fields_by_model: dict[str, list[str]] | None) -> dict[str, list[str]]:
    normalised: dict[str, list[str]] = {}
    for model, names in (fields_by_model or {}).items():
        if not isinstance(model, str) or not model.strip():
            continue
        clean_names = sorted({str(name).strip() for name in names or [] if str(name).strip()})
        if clean_names:
            normalised[model.strip()] = clean_names
    return normalised


def _verify_registry_fields(fields_by_model: dict[str, list[str]]) -> OdooRpcResult:
    rpc = OdooRpcService()
    missing: dict[str, list[str]] = {}
    found: dict[str, list[str]] = {}
    raw_results: dict[str, object] = {}
    for model, field_names in sorted(fields_by_model.items()):
        result = rpc.search_read(
            "ir.model.fields",
            [("model", "=", model), ("name", "in", field_names)],
            ["model", "name", "ttype", "field_description"],
            limit=max(len(field_names) + 5, 20),
            offset=0,
            order="name asc",
            context=None,
        )
        raw_results[model] = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        if result.status != "ok":
            return OdooRpcResult(status="failed", error=f"Could not verify registry fields for {model}: {result.error}", result={"raw_results": raw_results})
        records = result.result or []
        present = sorted({record.get("name") for record in records if isinstance(record, dict) and record.get("name")})
        found[model] = present
        absent = sorted(set(field_names) - set(present))
        if absent:
            missing[model] = absent
    if missing:
        return OdooRpcResult(
            status="failed",
            error="Live Odoo registry is missing expected fields after module upgrade.",
            result={"expected": fields_by_model, "found": found, "missing": missing, "raw_results": raw_results},
        )
    return OdooRpcResult(status="ok", result={"expected": fields_by_model, "found": found, "raw_results": raw_results})



def _normalise_safe_deploy_paths(project: Path, module_name: str, paths: list[str]) -> list[str]:
    """Validate deploy paths against the exact checkout safe_deploy will commit/push.

    This prevents the failure mode where the agent edits one checkout (for example
    /repos/test_18) but calls safe_deploy_module with another checkout (for example
    /workspace/repos/test_18). Absolute paths are accepted only when they resolve
    inside the selected project, then they are converted to repo-relative paths.
    """
    clean: list[str] = []
    seen: set[str] = set()
    for raw in paths or []:
        value = str(raw or "").strip()
        if not value:
            continue
        if "\x00" in value:
            raise ValueError("Deploy path contains a NUL byte")
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if not (resolved == project or project in resolved.parents):
                raise ValueError(
                    "PATH_MISMATCH: deploy path is outside project_path. "
                    f"project_path={project}; deploy_path={resolved}. "
                    "Edit and deploy must use the same checkout returned by repo.prepare."
                )
            rel = resolved.relative_to(project).as_posix()
        else:
            resolved = (project / candidate).resolve()
            if not (resolved == project or project in resolved.parents):
                raise ValueError(f"Deploy path escapes project_path: {value}")
            rel = resolved.relative_to(project).as_posix()
        if rel in {"", "."}:
            continue
        if rel == ".git" or rel.startswith(".git/"):
            raise ValueError("Deploy path inside .git is blocked")
        # Safe deploy is module-scoped. Refuse accidental unrelated files unless the
        # caller intentionally passes no paths and lets git add -A capture all changes.
        if module_name and not (rel == module_name or rel.startswith(f"{module_name}/")):
            raise ValueError(
                "DEPLOY_PATH_OUTSIDE_MODULE: safe_deploy_module received a path outside the target module. "
                f"module_name={module_name}; path={rel}. Use the correct module or split the deploy."
            )
        if rel not in seen:
            seen.add(rel)
            clean.append(rel)
    return clean


def _changed_file_set(result: CommandResult) -> set[str]:
    return {line.strip() for line in (result.stdout or "").splitlines() if line.strip()}


def _paths_touch_changed_files(deploy_paths: list[str], changed_files: set[str]) -> bool:
    if not deploy_paths:
        return bool(changed_files)
    for rel in deploy_paths:
        if rel in changed_files:
            return True
        prefix = rel.rstrip("/") + "/"
        if any(item.startswith(prefix) for item in changed_files):
            return True
    return False


def _assert_head_commit_contains_paths(git: GitService, project_path: Path, deploy_paths: list[str]) -> CommandResult:
    if not hasattr(git, "head_changed_files"):
        return CommandResult(command=["git", "head-commit-path-gate", "legacy-fake"], cwd=str(project_path), exit_code=0, stdout="Skipped only because GitService test double has no head_changed_files().\n")
    changed = git.head_changed_files(str(project_path))
    if changed.exit_code != 0:
        return changed
    head_files = {line.strip() for line in (changed.stdout or "").splitlines() if line.strip()}
    if not head_files:
        return CommandResult(command=["git", "diff-tree", "--name-only", "HEAD"], cwd=str(project_path), exit_code=1, stderr="HEAD commit contains no changed files; refusing deploy.")
    if deploy_paths and not _paths_touch_changed_files(deploy_paths, head_files):
        return CommandResult(
            command=["git", "diff-tree", "--name-only", "HEAD"],
            cwd=str(project_path),
            exit_code=1,
            stdout="\n".join(sorted(head_files)) + "\n",
            stderr=(
                "HEAD commit does not contain the requested deploy paths. "
                "This usually means the agent edited a different checkout than project_path."
            ),
        )
    return CommandResult(command=["git", "head-commit-path-gate"], cwd=str(project_path), exit_code=0, stdout="HEAD commit contains expected deploy files.\n")

def _start_and_wait_module_deploy(payload: SafeDeployModuleRequest) -> OdooRpcResult:
    started = start_odoo_module_deploy_job(OdooRpcModuleRequest(
        module_name=payload.module_name,
        wait_for_result=False,
        wait_timeout_seconds=0,
        poll_interval_seconds=1.0,
    ))
    started_payload = started.model_dump(mode="json") if hasattr(started, "model_dump") else started
    result_payload = started_payload.get("result") if isinstance(started_payload, dict) and isinstance(started_payload.get("result"), dict) else {}
    if result_payload.get("finished") or not result_payload.get("job_id"):
        return started

    nested_job_id = result_payload.get("job_id")
    waited = JsonStore().wait_job(
        str(nested_job_id),
        payload.odoo_module_deploy_timeout_seconds,
        payload.odoo_module_deploy_poll_interval_seconds,
    )
    data = waited.model_dump(mode="json")
    if waited.status == JobStatus.passed:
        return OdooRpcResult(status="ok", result={"job_id": waited.id, "job_status": waited.status.value, "finished": True, "job": data})
    if waited.status in {JobStatus.failed, JobStatus.warning}:
        result_error = waited.result.get("error") if isinstance(waited.result, dict) else None
        return OdooRpcResult(
            status="failed" if waited.status == JobStatus.failed else "ok",
            error=result_error or f"Odoo module deploy job finished with status={waited.status.value}",
            result={"job_id": waited.id, "job_status": waited.status.value, "finished": True, "job": data},
        )
    return OdooRpcResult(
        status="ok",
        result={
            "job_id": waited.id,
            "job_status": waited.status.value,
            "finished": False,
            "message": (
                f"Odoo module deploy is still running after {payload.odoo_module_deploy_timeout_seconds}s. "
                "Safe deploy is not complete and final success is blocked."
            ),
            "next_call": {"tool": "jobs.get", "arguments": {"job_id": waited.id, "wait_for_result": True}},
        },
    )


def _poll_odoosh_deployed(
    odoosh: OdooShService,
    pushed_sha: str,
    timeout_seconds: int,
    interval_seconds: float,
    progress_cb=None,
) -> CommandResult:
    """Poll Odoo.sh for the pushed SHA.

    Runs only from the background safe-deploy worker. ``progress_cb`` is called
    after every poll attempt so jobs.get shows fresh timestamps/progress for
    impatient MCP clients while Odoo.sh is still building.
    """
    deadline = time.monotonic() + max(timeout_seconds, 0)
    attempts: list[dict[str, object]] = []
    attempt = 0

    while True:
        attempt += 1
        check_result = odoosh.check_commit_deployed(pushed_sha)
        deployed = check_result.exit_code == 0 and '"deployed": true' in (check_result.stdout or "").lower()
        attempt_payload = {
            "attempt": attempt,
            "exit_code": check_result.exit_code,
            "stdout": check_result.stdout,
            "stderr": check_result.stderr,
            "deployed": deployed,
            "expected_sha": pushed_sha,
            "poll_interval_seconds": interval_seconds,
        }
        attempts.append(attempt_payload)
        if progress_cb is not None:
            try:
                progress_cb(attempt_payload)
            except Exception:
                pass
        if deployed:
            return check_result.model_copy(update={
                "command": ["odoosh.check_commit_poll", pushed_sha],
                "stdout": json.dumps({
                    "deployed": True,
                    "expected_sha": pushed_sha,
                    "attempts": attempt,
                    "poll_interval_seconds": interval_seconds,
                    "message": "Odoo.sh deployed the pushed commit. Proceeding to module upgrade.",
                    "last_check": check_result.stdout,
                }),
            })
        if time.monotonic() >= deadline:
            return CommandResult(
                command=["odoosh.check_commit_poll", pushed_sha],
                cwd="odoosh:ssh",
                exit_code=124,
                stdout=json.dumps({
                    "deployed": False,
                    "expected_sha": pushed_sha,
                    "attempts": attempt,
                    "poll_interval_seconds": interval_seconds,
                    "recent_checks": attempts[-10:],
                }),
                stderr=f"Odoo.sh did not deploy pushed commit {pushed_sha} within {timeout_seconds}s.",
                timed_out=True,
            )
        time.sleep(min(interval_seconds, max(deadline - time.monotonic(), 0.0)))


def _run_safe_deploy(payload: SafeDeployModuleRequest, job_id: str) -> None:
    """Background worker — runs the full commit→push→wait→upgrade pipeline."""
    store = JsonStore()
    git = GitService()
    odoosh = OdooShService()
    approval = ApprovalService()
    pushed_sha: str | None = None
    rollback_attempted = False

    def _step(name: str, status_: str, result=None, error: str | None = None) -> None:
        job = store.get_job(job_id)
        store.add_step(
            job,
            JobStep(
                name=name,
                status=status_,
                finished_at=datetime.now(timezone.utc).isoformat(),
                result=result.model_dump(mode="json") if hasattr(result, "model_dump") else result,
                error=error,
            ),
        )

    def _rollback_after_failure(reason: str) -> None:
        nonlocal rollback_attempted
        if rollback_attempted or not pushed_sha:
            return
        rollback_attempted = True
        try:
            revert_result = git.revert_commit(str(project_path), pushed_sha, 180)  # type: ignore[name-defined]
            _step("rollback_git_revert", "passed" if revert_result.exit_code == 0 else "failed", revert_result)
            if revert_result.exit_code == 0:
                push_back = git.push(str(project_path), payload.remote, branch, True, min(payload.timeout_seconds, 600))  # type: ignore[name-defined]
                _step("rollback_git_push", "passed" if push_back.exit_code == 0 else "failed", push_back)
        except Exception as exc:  # noqa: BLE001
            _step("rollback_exception", "failed", error=f"{reason}: {exc}")

    def _fail(name: str, result=None, error: str | None = None) -> None:
        _step(name, "failed", result, error)
        if name in {"odoosh_verify_module_manifest", "odoo_module_deploy", "odoo_module_status", "registry_field_verify", "browser_ui_verify"}:
            _rollback_after_failure(name)
        job = store.get_job(job_id)
        store.finish_job(job, JobStatus.failed, {"failed_step": name, "module_name": payload.module_name, "rollback_attempted": rollback_attempted})
        approval.mark_failed(payload.approval_id, f"workflow.safe_deploy_module failed at {name}")

    try:
        resolver = ClientResolverService()
        client = resolver.resolve_client(
            client_name=payload.client_name,
            project_path=payload.project_path,
            require_complete=False,
        )
        resolver.apply_odoo_env(client)
        project_path = resolve_allowed_path(client.path if client else payload.project_path)
    except Exception as exc:
        _fail("resolve_project", error=str(exc))
        return

    module_path = project_path / payload.module_name
    if not module_path.exists():
        _fail("resolve_project", error=f"Module path not found: {module_path}")
        return
    try:
        deploy_paths = _normalise_safe_deploy_paths(project_path, payload.module_name, payload.paths)
    except Exception as exc:  # noqa: BLE001
        _fail("deploy_path_gate", error=str(exc))
        return
    _step("resolve_project", "passed", {"project_path": str(project_path), "module_path": str(module_path), "deploy_paths": deploy_paths})

    status_result = git.status(str(project_path))
    if status_result.exit_code != 0:
        _fail("git_status", status_result)
        return
    _step("git_status", "passed", status_result)

    diff_result = git.diff(str(project_path), staged=False, timeout_seconds=30)
    if diff_result.exit_code != 0:
        _fail("git_diff", diff_result)
        return
    _step("git_diff", "passed", diff_result)

    if hasattr(git, "changed_files"):
        changed_files_result = git.changed_files(str(project_path))
    else:
        fallback_paths = deploy_paths or [f"{payload.module_name}/__manifest__.py"]
        changed_files_result = CommandResult(command=["git", "changed-files", "legacy-fake"], cwd=str(project_path), exit_code=0, stdout="\n".join(fallback_paths) + "\n")
    if changed_files_result.exit_code != 0:
        _fail("git_changed_files", changed_files_result)
        return
    changed_files = _changed_file_set(changed_files_result)
    _step("git_changed_files", "passed", changed_files_result)
    idempotent_clean_retry = False
    idempotent_head_path_gate: CommandResult | None = None
    if not _paths_touch_changed_files(deploy_paths, changed_files):
        # Defense-in-depth: duplicate safe_deploy invocations may arrive after a
        # previous request already committed the expected files but before the
        # client observed the job_id. In that case the working tree is clean for
        # the requested paths. Do not create a second commit/push; prove HEAD
        # already contains those paths and continue to Odoo.sh polling with HEAD.
        idempotent_head_path_gate = _assert_head_commit_contains_paths(git, project_path, deploy_paths)
        if idempotent_head_path_gate.exit_code == 0:
            idempotent_clean_retry = True
            _step(
                "idempotent_commit_push_skip",
                "passed",
                {
                    "message": "No working-tree changes matched deploy paths; HEAD already contains the requested paths. Skipping duplicate commit/push and continuing with HEAD SHA.",
                    "deploy_paths": deploy_paths,
                    "changed_files": sorted(changed_files),
                },
            )
        else:
            _fail(
                "deploy_dirty_path_gate",
                changed_files_result,
                (
                    "No changed files in the selected project_path match the requested deploy paths, and HEAD did not prove an already-committed retry. "
                    "Refusing commit/push because the edits may be in another checkout. "
                    f"project_path={project_path}; deploy_paths={deploy_paths}; changed_files={sorted(changed_files)}"
                ),
            )
            return

    if payload.run_qa:
        qa_report = self_run(SelfRunRequest(
            project_path=str(project_path),
            module_name=payload.module_name,
            run_pytest=True,
            include_git_status=True,
            timeout_seconds=min(payload.timeout_seconds, 600),
        ))
        _step("qa_self_run", qa_report.status, qa_report)
        if qa_report.status == "failed":
            _fail("qa_self_run", qa_report)
            return

    verify_files = _pre_push_verify_files(project_path, payload.module_name, deploy_paths, status_result.stdout)
    if verify_files:
        verify_result = SyntaxVerifierService().verify(SyntaxVerifyRequest(
            written_files=verify_files,
            fix_cycle=0,
            project_path=str(project_path),
        ))
        _step("verify_against_core_pre_push", "failed" if verify_result.push_blocked else "passed", verify_result)
        if verify_result.push_blocked:
            _fail("verify_against_core_pre_push", verify_result)
            return
    else:
        _step(
            "verify_against_core_pre_push",
            "warning",
            {
                "message": (
                    "No changed module .py/.xml/.js/.scss/__manifest__.py files were detected for pre-push verification. "
                    "Push gate passed only because there were no verifiable code files in this deploy request."
                )
            },
        )
    expected_user_hashes = _expected_user_file_hashes(project_path, verify_files)
    expected_registry_fields = _normalise_expected_registry_fields(payload.expected_registry_fields) or _infer_registry_fields_from_python(project_path, payload.module_name, verify_files)

    identity_result = git.configure_identity(str(project_path))
    if identity_result.exit_code != 0:
        _fail("git_configure_identity", identity_result)
        return
    _step("git_configure_identity", "passed", identity_result)

    branch = payload.branch or (client.branch if client else None) or settings.target_git_branch
    target_result = git.validate_target(str(project_path), branch, payload.remote)
    if target_result.exit_code != 0:
        _fail("git_validate_target", target_result)
        return
    _step("git_validate_target", "passed", target_result)

    if idempotent_clean_retry:
        _step("git_commit", "skipped", {"message": "Skipped duplicate commit because requested paths are already represented in HEAD."})
        head_path_gate = idempotent_head_path_gate or _assert_head_commit_contains_paths(git, project_path, deploy_paths)
        _step("git_head_commit_path_gate", "passed", head_path_gate)
        sha_result = git.head_sha(str(project_path))
        if sha_result.exit_code != 0:
            _fail("git_head_sha", sha_result)
            return
        pushed_sha = sha_result.stdout.strip()
        _step("git_head_sha", "passed", sha_result)
        push_started_epoch = int(time.time())
        _step("git_push", "skipped", {"message": "Skipped duplicate push; remote SHA will be verified before Odoo.sh polling.", "sha": pushed_sha})
    else:
        commit_result = git.commit(str(project_path), payload.commit_message, deploy_paths, min(payload.timeout_seconds, 300))
        if commit_result.exit_code != 0:
            _fail("git_commit", commit_result)
            return
        _step("git_commit", "passed", commit_result)

        head_path_gate = _assert_head_commit_contains_paths(git, project_path, deploy_paths)
        if head_path_gate.exit_code != 0:
            _fail("git_head_commit_path_gate", head_path_gate)
            return
        _step("git_head_commit_path_gate", "passed", head_path_gate)

        sha_result = git.head_sha(str(project_path))
        if sha_result.exit_code != 0:
            _fail("git_head_sha", sha_result)
            return
        pushed_sha = sha_result.stdout.strip()
        _step("git_head_sha", "passed", sha_result)

        push_started_epoch = int(time.time())
        push_result = git.push(str(project_path), payload.remote, branch, True, min(payload.timeout_seconds, 600))
        if push_result.exit_code != 0:
            _fail("git_push", push_result)
            return
        _step("git_push", "passed", push_result)

    if hasattr(git, "remote_head_sha"):
        remote_head = git.remote_head_sha(str(project_path), payload.remote, branch, 60)
    else:
        remote_ok = "PUSH_OK" in (push_result.stdout or "") and f"sha={pushed_sha}" in (push_result.stdout or "")
        remote_head = CommandResult(
            command=["git", "remote-head-sha", "legacy-fake"],
            cwd=str(project_path),
            exit_code=0 if remote_ok else 1,
            stdout=(pushed_sha + "\n") if remote_ok else "",
            stderr="GitService test double has no remote_head_sha()." if not remote_ok else "",
        )
    remote_sha = (remote_head.stdout or "").strip()
    if remote_head.exit_code != 0 or remote_sha != pushed_sha:
        _fail(
            "git_remote_head_verify",
            remote_head,
            f"Push was not proven on remote branch. expected_sha={pushed_sha}; remote_sha={remote_sha or '<missing>'}",
        )
        return
    _step("git_remote_head_verify", "passed", remote_head)

    pushed_job = store.get_job(job_id)
    pushed_job.request["push_started_epoch"] = push_started_epoch
    pushed_job.request["expected_user_file_hashes"] = expected_user_hashes
    pushed_job.request["expected_registry_fields"] = expected_registry_fields
    store.update_job(pushed_job)

    if payload.wait_for_odoosh:
        def _odoosh_poll_progress(attempt_payload: dict[str, object]) -> None:
            _step("odoosh_check_commit_poll", "passed" if attempt_payload.get("deployed") else "running", attempt_payload)

        check_result = _poll_odoosh_deployed(
            odoosh,
            pushed_sha,
            payload.odoosh_deploy_timeout_seconds,
            payload.odoosh_deploy_poll_interval_seconds,
            progress_cb=_odoosh_poll_progress,
        )
        deployed = check_result.exit_code == 0 and '"deployed": true' in (check_result.stdout or "").lower()
        _step("odoosh_check_commit", "passed" if deployed else "failed", check_result)
        if not deployed:
            job = store.get_job(job_id)
            result = {
                "status": "deploy_failed_or_timeout",
                "message": "Odoo.sh did not report the pushed commit as deployed within the polling window. Module upgrade was not attempted.",
                "module_name": payload.module_name,
                "sha": pushed_sha,
                "poll_timeout_seconds": payload.odoosh_deploy_timeout_seconds,
                "poll_interval_seconds": payload.odoosh_deploy_poll_interval_seconds,
                "next_call": {"tool": "workflow.resume_job", "arguments": {"job_id": job_id}},
            }
            store.finish_job(job, JobStatus.failed, result)
            store.save_session_state({
                "last_safe_deploy_job_id": job_id,
                "last_sha": pushed_sha,
                "module_name": payload.module_name,
                "project_path": str(project_path),
                "branch": branch,
                "client_name": client.name if client else payload.client_name,
                "safe_deploy_status": "deploy_failed_or_timeout",
            }, client_name=client.name if client else payload.client_name or None)
            return
        failure_guard = getattr(odoosh, "recent_build_failure", None)
        if callable(failure_guard):
            failure_result = failure_guard(push_started_epoch)
            failure_status = "failed" if failure_result.exit_code == 1 else "passed" if failure_result.exit_code == 0 else "warning"
            _step("odoosh_build_failure_guard", failure_status, failure_result)
            if failure_result.exit_code == 1:
                job = store.get_job(job_id)
                result = {
                    "status": "deploy_failed",
                    "message": "Odoo.sh reported failure evidence after this push. Module upgrade was not attempted.",
                    "module_name": payload.module_name,
                    "sha": pushed_sha,
                    "next_call": {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 1200}},
                }
                store.finish_job(job, JobStatus.failed, result)
                store.save_session_state({
                    "last_safe_deploy_job_id": job_id,
                    "last_sha": pushed_sha,
                    "module_name": payload.module_name,
                    "project_path": str(project_path),
                    "branch": branch,
                    "client_name": client.name if client else payload.client_name,
                    "safe_deploy_status": "deploy_failed",
                }, client_name=client.name if client else payload.client_name or None)
                return

        if expected_user_hashes:
            code_verify = odoosh.verify_user_file_hashes(expected_user_hashes)
            _step("odoosh_user_file_verify", "passed" if code_verify.exit_code == 0 else "failed", code_verify)
            if code_verify.exit_code != 0:
                job = store.get_job(job_id)
                result = {
                    "status": "deploy_code_mismatch",
                    "message": "Odoo.sh source checkout did not match the changed local/GitHub files. Module upgrade was not attempted.",
                    "module_name": payload.module_name,
                    "sha": pushed_sha,
                    "verified_files": sorted(expected_user_hashes),
                    "next_call": {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 1200}},
                }
                store.finish_job(job, JobStatus.failed, result)
                store.save_session_state({
                    "last_safe_deploy_job_id": job_id,
                    "last_sha": pushed_sha,
                    "module_name": payload.module_name,
                    "project_path": str(project_path),
                    "branch": branch,
                    "client_name": client.name if client else payload.client_name,
                    "safe_deploy_status": "deploy_code_mismatch",
                }, client_name=client.name if client else payload.client_name or None)
                return

    manifest_result = odoosh.verify_module_manifest(payload.module_name)
    if manifest_result.exit_code != 0:
        _fail("odoosh_verify_module_manifest", manifest_result)
        return
    _step("odoosh_verify_module_manifest", "passed", manifest_result)

    if payload.upgrade_module:
        _step("db_rollback_checkpoint", "warning", {"note": "Code rollback is automatic, but database schema/data rollback requires an Odoo.sh backup or duplicate staging before upgrade. Create/confirm backup before destructive migrations."})
        deploy_result = _start_and_wait_module_deploy(payload)
        deploy_payload = deploy_result.model_dump(mode="json") if hasattr(deploy_result, "model_dump") else deploy_result
        deploy_finished = bool(isinstance(deploy_payload.get("result"), dict) and deploy_payload["result"].get("finished"))
        deploy_running = deploy_result.status == "ok" and not deploy_finished
        _step("odoo_module_deploy", "failed" if deploy_running else "passed" if deploy_result.status == "ok" else "failed", deploy_result)
        if deploy_running:
            job = store.get_job(job_id)
            nested_result = deploy_payload.get("result") if isinstance(deploy_payload.get("result"), dict) else {}
            result = {
                "status": "module_upgrade_running",
                "message": "Odoo module deploy/update is still running. Safe deploy is not complete; do not claim success. Resume or poll the nested job.",
                "module_name": payload.module_name,
                "sha": pushed_sha,
                "odoo_module_deploy": nested_result,
                "next_call": nested_result.get("next_call") or {"tool": "jobs.get", "arguments": {"job_id": nested_result.get("job_id"), "wait_for_result": True}},
            }
            store.finish_job(job, JobStatus.failed, result)
            store.save_session_state({
                "last_safe_deploy_job_id": job_id,
                "last_sha": pushed_sha,
                "module_name": payload.module_name,
                "project_path": str(project_path),
                "branch": branch,
                "client_name": client.name if client else payload.client_name,
                "safe_deploy_status": "module_upgrade_running",
            }, client_name=client.name if client else payload.client_name or None)
            return
        if deploy_result.status != "ok":
            _fail("odoo_module_deploy", deploy_result)
            return

    module_status = OdooRpcService().module_status(payload.module_name)
    status_ = "passed" if module_status.status == "ok" else "warning"
    _step("odoo_module_status", status_, module_status)

    if expected_registry_fields:
        field_verify = _verify_registry_fields(expected_registry_fields)
        _step("registry_field_verify", "passed" if field_verify.status == "ok" else "failed", field_verify)
        if field_verify.status != "ok":
            job = store.get_job(job_id)
            result = {
                "status": "registry_field_missing",
                "message": "Module upgrade finished but the live Odoo registry is missing expected fields. Do not claim success.",
                "module_name": payload.module_name,
                "sha": pushed_sha,
                "expected_registry_fields": expected_registry_fields,
                "registry_field_verify": field_verify.model_dump(mode="json"),
                "next_call": {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 1200}},
            }
            store.finish_job(job, JobStatus.failed, result)
            store.save_session_state({
                "last_safe_deploy_job_id": job_id,
                "last_sha": pushed_sha,
                "module_name": payload.module_name,
                "project_path": str(project_path),
                "branch": branch,
                "client_name": client.name if client else payload.client_name,
                "safe_deploy_status": "registry_field_missing",
            }, client_name=client.name if client else payload.client_name or None)
            return
    else:
        _step("registry_field_verify", "skipped", {"message": "No expected Python field additions were inferred or provided."})

    smoke = OdooRpcService().registry_smoke_test(payload.module_name, [], [], strict=False)
    _step("registry_smoke_test", "passed" if smoke.status == "ok" else "warning", smoke)

    if payload.verify_ui_paths:
        ui_results = []
        for index, path in enumerate(payload.verify_ui_paths, start=1):
            result = asyncio.run(CdpBrowserService().screenshot_odoo_page(
                None,
                None,
                None,
                path,
                f"{payload.verify_ui_screenshot_prefix}_{index}.png",
                payload.verify_ui_timeout_seconds,
                full_page=True,
            ))
            ui_results.append({"path": path, "result": result.model_dump(mode="json")})
            status_ = "passed" if result.status == "ok" and not result.console_errors and not result.network_errors else "failed" if result.status == "failed" else "warning"
            _step(f"browser_ui_path:{path}", status_, result)
            if status_ == "failed":
                _fail("browser_ui_verify", {"failed_path": path, "result": result.model_dump(mode="json")})
                return
        if any(item["result"]["console_errors"] or item["result"]["network_errors"] for item in ui_results):
            _step("browser_ui_verify", "warning", {"paths": payload.verify_ui_paths, "results": ui_results})
        else:
            _step("browser_ui_verify", "passed", {"paths": payload.verify_ui_paths, "results": ui_results})

    fresh_job = store.get_job(job_id)
    store.finish_job(fresh_job, JobStatus.passed, {"module_name": payload.module_name, "sha": pushed_sha, "verify_ui_paths": payload.verify_ui_paths, "expected_registry_fields": expected_registry_fields})
    approval.mark_consumed(payload.approval_id, "workflow.safe_deploy_module succeeded")
    # Fix 7: persist milestone so resume_job and future sessions know where we are
    store.save_session_state({
        "last_safe_deploy_job_id": job_id,
        "last_sha": pushed_sha,
        "module_name": payload.module_name,
        "project_path": str(project_path),
        "branch": branch,
        "client_name": client.name if client else payload.client_name,
        "client": client.public if client else None,
        "safe_deploy_status": "passed",
    }, client_name=client.name if client else payload.client_name or None)


# ---------------------------------------------------------------------------
# V14 — Self-Learning Agent: Core File Discovery
# ---------------------------------------------------------------------------

@router.post("/find-core-file", response_model=FindCoreFileResult)
def find_core_file(payload: FindCoreFileRequest) -> FindCoreFileResult:
    """V14: Locate an Odoo core source file by module + type + keyword.

    Search priority for installed-source reads:
      1. /home/odoo/src/enterprise — Odoo enterprise
      2. /home/odoo/src/odoo       — Odoo community
      3. /home/odoo/src/user       — fallback for other related custom dependencies

    Use before any ``_inherit`` so the agent reads the exact field
    definitions, method signatures, and model structure from source
    rather than hallucinating them.

    Example:
        module_name="sale", file_type="model", keyword="order"
        → /home/odoo/src/odoo/addons/sale/models/sale_order.py
    """
    return CoreFinderService().find_core_file(
        module_name=payload.module_name,
        file_type=payload.file_type,
        keyword=payload.keyword,
        search_roots=payload.search_roots or None,
    )


@router.post("/find-similar-view", response_model=FindSimilarViewResult)
def find_similar_view(payload: FindSimilarViewRequest) -> FindSimilarViewResult:
    """V14: Locate an Odoo core view XML file by model + view_type.

    Use before writing any ``<xpath expr="...">`` block so the agent
    reads the exact attribute names, field references, and node structure
    from the real source file rather than guessing.

    Search priority: enterprise → odoo → user fallback (same as find_core_file).

    Example:
        model="sale.order", view_type="form"
        → /home/odoo/src/enterprise/sale/views/sale_order_views.xml
    """
    return CoreFinderService().find_similar_view(
        model=payload.model,
        view_type=payload.view_type,
        search_roots=payload.search_roots or None,
    )


@router.post("/list-module-files", response_model=ListModuleFilesResult)
def list_module_files(payload: ListModuleFilesRequest) -> ListModuleFilesResult:
    """V14+ gap-fix: List all files inside a core or custom module directory.

    Use when you don't know the exact filename/keyword to search for yet —
    browse what exists first, then call find_core_file with a keyword, or
    repo.read directly on a file you spot here.

    Search priority: enterprise → odoo → user fallback (same as find_core_file).

    Example:
        module_name="sale", subdir="models"
        → lists every file under sale/models/ in whichever root has it first.
    """
    return CoreFinderService().list_module_files(
        module_name=payload.module_name,
        subdir=payload.subdir,
        search_roots=payload.search_roots or None,
    )


# ===========================================================================
# V14+ — Pillar 2: Adaptive Scope Advisor
# ===========================================================================

@router.post("/scope-advice", response_model=ScopeAdviceResult)
def scope_advice(payload: ScopeAdviceRequest) -> ScopeAdviceResult:
    """V14+: Analyse custom module files and return adaptive mandatory read plan.

    MANDATORY: Call this AFTER reading custom module files and BEFORE writing
    any code. The service scans the provided custom file paths for _inherit
    declarations, mixin usage, computed/relational fields, and cross-module
    comodel references. It then returns an ordered list of ScopeReadInstruction
    objects specifying everything that must be read from Odoo core before coding.

    Adaptive scope levels triggered automatically:
      L0 — always: custom module manifest (baseline, always included)
      L1 — _inherit detected → parent core model file(s)
      L2 — mixin / abstract detected → mail.thread, portal.mixin, etc.
      L3 — computed or relational fields / ir.model → full models/ dir scan
      L4 — cross-module comodel → that module's core manifest + model

    Each ScopeReadInstruction includes:
      - level: which level triggered it
      - reason: why it must be read
      - path: absolute path → call repo.read(path) on each mandatory instruction
      - mandatory: True = blocking (must be done before any code write)

    Returns:
      reached_level: highest level triggered
      mandatory_count: number of blocking reads required
      warning: "Push BLOCKED until verify_against_core passes"

    Example:
        custom_module_name='my_sale_ext',
        custom_file_paths=['/workspace/repos/repo/my_sale_ext/models/sale_order.py'],
        task_description='Add custom field to sale.order'
    """
    return CoreFinderService().scope_advice(
        custom_module_name=payload.custom_module_name,
        custom_file_paths=payload.custom_file_paths,
        task_description=payload.task_description,
        search_roots=payload.search_roots or None,
    )


# ===========================================================================
# V14+ — Pillar 3: Pre-Push Syntax Verifier
# ===========================================================================

@router.post("/verify-against-core", response_model=SyntaxVerifyResult)
def verify_against_core(payload: SyntaxVerifyRequest) -> SyntaxVerifyResult:
    """V14+: Pre-push cross-check of written files against Odoo core source.

    MANDATORY: Call this BEFORE every git.push. If push_blocked=True, fix all
    CRITICAL findings and call verify_against_core again (max 3 fix cycles).

    What is verified (per file type):

    Python model files (.py):
      - _inherit model name exists in core (warns if not found)
      - depends=[] field names exist in the inherited core model (CRITICAL if not)
      - comodel_name module exists in /home/odoo/src/* (CRITICAL if not)
      - related= first field exists in core model (CRITICAL if not)
      - compute= method is defined in the same file (CRITICAL if missing)

    XML view files (.xml):
      - xpath @name nodes found in core view source (CRITICAL if not)
      - <field name=...> cross-checked against core model fields (WARNING if not)

    Manifest files (__manifest__.py):
      - depends[] module names exist in /home/odoo/src/* roots (CRITICAL if not)

    Push logic:
      push_blocked=True  → Fix CRITICAL findings, then call verify_against_core again
      push_blocked=False → Proceed with git.push
      fix_cycle >= 3     → Report to user and STOP retrying

    Example:
        written_files=[
            '/workspace/repos/repo/my_sale_ext/__manifest__.py',
            '/workspace/repos/repo/my_sale_ext/models/sale_order.py',
            '/workspace/repos/repo/my_sale_ext/views/sale_order_views.xml',
        ],
        fix_cycle=0
    """
    return SyntaxVerifierService().verify(payload)


_POS_FILE_MARKERS = ("point_of_sale", "/pos/", "pos_", "_pos.")


def _looks_pos_related(written_files: list[str]) -> bool:
    lowered = [f.lower() for f in written_files]
    return any(any(marker in f for marker in _POS_FILE_MARKERS) for f in lowered)


@router.post("/verify-pos-change", response_model=PosChangeVerifyResult)
async def verify_pos_change(payload: PosChangeVerifyRequest) -> PosChangeVerifyResult:
    """V17: Bounded fix-loop for POS changes — static check AND live browser check.

    verify_against_core alone (even with the real node --check parse added in
    V17) only proves the JS/XML/manifest are syntactically and pattern-wise
    sound. It cannot prove the POS screen actually renders, that clicking a
    product still works, or that the payment/validate flow doesn't throw at
    runtime. This endpoint runs BOTH under one fix_cycle counter, exactly
    mirroring the existing verify_against_core contract:

      push_blocked=True, fix_cycle < 3  → fix CRITICAL/failed findings, call
                                            again with fix_cycle+1
      push_blocked=True, fix_cycle >= 3 → STOP retrying, report to user
      push_blocked=False                → safe to git.push

    The live POS flow only runs if written_files look POS-related (or
    run_pos_flow is forced True) — non-POS changes skip straight to the
    static-only result to avoid unnecessary browser cost.
    """
    static_request = SyntaxVerifyRequest(
        written_files=payload.written_files,
        fix_cycle=payload.fix_cycle,
        project_path=payload.project_path,
    )
    static_result = SyntaxVerifierService().verify(static_request)

    pos_flow_result = None
    is_pos = _looks_pos_related(payload.written_files)

    # Never bother driving a real browser if static checks already found
    # CRITICAL issues — fix those first, they're cheaper and faster to loop on.
    if payload.run_pos_flow and is_pos and static_result.critical_count == 0:
        flow_req = payload.pos_flow_request or PosFlowCheckRequest()
        pos_flow_result = await CdpBrowserService().run_pos_flow(flow_req)

    push_blocked = static_result.push_blocked or (pos_flow_result is not None and pos_flow_result.status == "failed")

    if payload.fix_cycle >= 3 and push_blocked:
        message = (
            f"STOP: fix_cycle={payload.fix_cycle} reached the max of 3 with issues still unresolved. "
            "Report the remaining findings/console errors to the user instead of retrying again."
        )
    elif not is_pos:
        message = "No POS-related files detected in written_files — static verification only was run."
    elif static_result.critical_count > 0:
        message = "Static CRITICAL findings exist — fix those first, then call verify_pos_change again to run the live POS flow."
    elif pos_flow_result is None:
        message = "POS flow check was skipped (run_pos_flow=False)."
    elif pos_flow_result.status == "failed":
        failed_steps = [s.step for s in pos_flow_result.steps if s.status == "failed"]
        message = f"Live POS flow FAILED at step(s): {', '.join(failed_steps) or 'unknown'}. Review console_errors/console_all for that step, fix, and call again with fix_cycle+1."
    else:
        message = "Static checks and live POS flow both passed. Safe to git.push."

    return PosChangeVerifyResult(
        push_blocked=push_blocked,
        status="failed" if push_blocked else "passed",
        static_result=static_result,
        pos_flow_result=pos_flow_result,
        fix_cycle=payload.fix_cycle,
        message=message,
    )


# ===========================================================================
# V15 — Strict Odoo Frontend / OWL / JS / XML Preflight
# ===========================================================================

@router.post("/frontend-pattern-preflight", response_model=FrontendPatternPreflightResult)
def frontend_pattern_preflight(payload: FrontendPatternPreflightRequest) -> FrontendPatternPreflightResult:
    """V15: Blocking source exploration plan for Odoo frontend work.

    MANDATORY for any POS, OWL, JS, website, payment, QWeb/XML template,
    frontend SCSS, or asset-bundle task before writing code.  It returns the
    exact source areas/files the agent must read so the implementation matches
    the installed Odoo framework pattern instead of guessing.
    """
    return FrontendPatternService().preflight(payload)


# ===========================================================================
# V16 — Smart router + repo index
# ===========================================================================

@router.post("/smart-tool-router", response_model=SmartToolRouteResult)
def smart_tool_router(payload: SmartToolRouteRequest) -> SmartToolRouteResult:
    """Return a fast but strict tool plan for the current task.

    This is a planner only; it does not mutate code. It prevents the agent from
    calling heavy tools for simple Odoo queries while still enforcing frontend
    preflight, smart writes, changed-file verification, and browser checks when
    needed.
    """
    text = (payload.task_description or "").lower()
    planned = [p.lower() for p in payload.planned_file_paths]
    frontend = _looks_like_frontend_task(payload.task_description) or any(
        any(token in p for token in ("static/src", ".js", ".scss", "qweb", "website", "point_of_sale", "pos"))
        for p in planned
    )
    data_query = any(term in text for term in ("list ", "show ", "find ", "search ", "count ", "invoices", "employees")) and not any(
        term in text for term in ("fix", "implement", "module", "code", "js", "xml", "view", "write")
    )
    high_risk = payload.risk_level == "high" or any(term in text for term in (
        "new module", "migration", "large refactor", "rewrite", "multi module", "security", "access rights", "accounting", "payment provider"
    ))
    file_count = payload.changed_file_count if payload.changed_file_count is not None else len(payload.planned_file_paths)

    steps: list[SmartToolRouteStep] = []
    order = 1
    def add(tool: str, reason: str, required: bool = True) -> None:
        nonlocal order
        steps.append(SmartToolRouteStep(order=order, tool=tool, reason=reason, required=required))
        order += 1

    if data_query:
        add("odoo.search_read", "Direct live-data query; no repo or frontend preflight needed.")
        return SmartToolRouteResult(
            task_type="odoo_data_query",
            frontend_required=False,
            recommended_write_tool="none",
            validation_scope="none",
            browser_check="none",
            steps=steps,
            fast_reason="Fastest path: no code write requested.",
        )

    add("repo.prepare", "Ensure the correct target repo/branch is available before coding.")
    add("workflow.repo_index", "Use cached repo index to find manifests/assets/templates without full filesystem scans.", required=False)
    add("repo.read", "Read the exact custom files involved before changing them.")
    if frontend:
        add("workflow.frontend_pattern_preflight", "Mandatory targeted OWL/POS/website/payment pattern read before JS/XML/SCSS writes.")
    else:
        add("workflow.scope_advice", "Mandatory backend/model/view source-scope advice before Python/XML writes.")

    write_tool = "repo.write_many_atomic" if high_risk or file_count > settings.fast_write_max_files else "repo.write_many_smart"
    add(write_tool, "Use atomic only for high-risk/large changes; otherwise smart writer keeps lock+backup+rollback without worktree overhead.")
    add("git.diff", "Review exact changes before validation.")
    add("workflow.verify_against_core", "Validate changed files with impact-aware related companions only.")
    if frontend:
        add("browser.screenshot", "Smoke-test affected POS/website/backend page and inspect console/network errors after deploy.", required=False)

    return SmartToolRouteResult(
        task_type="frontend_code" if frontend else "backend_code",
        frontend_required=frontend,
        recommended_write_tool=write_tool,
        validation_scope="impact-aware changed files",
        browser_check="required for frontend changes" if frontend else "only if views/UI changed",
        steps=steps,
        fast_reason="Strict validation remains; speed comes from repo index cache, pattern cache, smart writes, and changed-files-only checks.",
    )


@router.post("/repo-index", response_model=RepoIndexResult)
def repo_index(payload: RepoIndexRequest) -> RepoIndexResult:
    """Build or return a cached lightweight Odoo repo index."""
    return RepoIndexService().index(payload.project_path, force_refresh=payload.force_refresh, max_files=payload.max_files)
