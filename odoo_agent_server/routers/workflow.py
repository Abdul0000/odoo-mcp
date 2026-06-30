from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from ..auth import AuthDependency
from ..config import settings, get_client_names, mask_repo_url
from ..schemas import (
    CapabilityCheckResult,
    CodingTaskPlanRequest,
    CodingTaskPlanResult,
    CommandResult,
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
)
from ..services.approval_service import ApprovalService
from ..services.client_resolver_service import ClientResolverService
from ..services.git_service import GitService
from ..services.odoo_rpc_service import OdooRpcService
from ..services.odoosh_service import OdooShService
from ..services.path_service import resolve_allowed_path
from ..services.playwright_service import PlaywrightService
from ..services.repo_service import RepoService
from ..services.store_service import JsonStore
from ..services.background_executor import submit_background
from ..services.core_finder_service import CoreFinderService          # V14
from ..services.syntax_verifier_service import SyntaxVerifierService  # V14+
from .qa import self_run

router = APIRouter(prefix="/workflow", tags=["workflow"], dependencies=[AuthDependency])


def _looks_like_small_patch(task: str) -> bool:
    value = task.lower()
    small_terms = ("add field", "new field", "simple field", "notebook", "tab", "page", "xpath", "label")
    broad_terms = ("new module", "report", "dashboard", "integration", "migration", "workflow", "portal", "api")
    return any(term in value for term in small_terms) and not any(term in value for term in broad_terms)


@router.post("/coding-task-plan", response_model=CodingTaskPlanResult)
def coding_task_plan(payload: CodingTaskPlanRequest) -> CodingTaskPlanResult:
    """Return the canonical repo-first coding workflow for ChatGPT/MCP clients."""
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
            "For a field on an existing inherited model, read the matching core model/view once with workflow.find_core_file or workflow.find_similar_view only when the existing custom file does not already prove the pattern.",
            "Patch the smallest file set with repo.apply_patch_atomic or repo.write_many_atomic; do not run broad module discovery again after the target module is known.",
            "Run git.diff and qa.self_run with run_pytest=false unless Python tests are present or the patch touches shared logic.",
            "Run workflow.verify_against_core only for the changed .py/.xml/__manifest__.py files, then git.commit and git.push.",
            "After push, poll odoosh.check_commit for the pushed SHA, then call odoo.module_deploy with wait_timeout_seconds=5; use jobs.get only if it returns a running job.",
            "Verify the exact field with odoo.search_read or workflow.registry_smoke_test; skip browser.screenshot unless the user specifically asks for UI proof.",
        ]
        required_tools = [
            "session.load_state",
            "repo.prepare",
            "repo.read",
            "repo.apply_patch_atomic",
            "repo.write_many_atomic",
            "git.diff",
            "qa.self_run",
            "workflow.verify_against_core",
            "git.commit",
            "git.push",
            "odoosh.check_commit",
            "odoo.module_deploy",
            "odoo.search_read",
        ]
        summary = "Fast path for a small targeted Odoo code patch."
    else:
        steps = [
            "Call repo.prepare with the target repo and branch. This clones if missing, otherwise fetches, checks out, and pulls the branch with --ff-only.",
            "Call repo.list_custom_modules on the returned project_path; choose the target custom module from __manifest__.py files, not from Odoo's installed module list only.",
            f"Read the target module files first: {module_name}/__manifest__.py, models, views, security, and relevant static files.",
            "Apply edits atomically: use repo.apply_patch_atomic for multi-file diffs or repo.write_many_atomic for multi-file/new-module writes. Use repo.apply_patch/repo.write only for small single-file changes. Do not edit Odoo core, enterprise, .git, or unrelated modules.",
            "Run git.diff and qa.self_run. Treat pytest environment failures separately from real module errors.",
            "Commit only related paths with git.commit using a clear message.",
            "Push to the exact target branch with git.push; branch must match TARGET_GIT_BRANCH when configured. Note the 'sha' in the push response.",
            "Poll Odoo.sh deploy using odoosh.check_commit with the pushed sha. It returns immediately with deployed=true/false. If false, wait 15 seconds and call again. Max 20 retries.",
            "When odoosh.check_commit returns deployed=true, call odoosh.verify_module_manifest then odoosh.module_update.",
            "Finish with odoo.module_status, odoosh.grep_errors, and browser.screenshot for UI tasks.",
        ]
        required_tools = [
            "repo.prepare",
            "repo.list_custom_modules",
            "repo.read",
            "repo.apply_patch_atomic",
            "repo.write_many_atomic",
            "repo.apply_patch",
            "git.diff",
            "qa.self_run",
            "git.commit",
            "git.push",
            "odoosh.check_commit",
            "odoosh.verify_module_manifest",
            "odoosh.module_update",
        ]
        summary = "Canonical repo-first coding workflow for ChatGPT MCP Odoo development."
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
            "For small patches, avoid repeated tool discovery, broad source reads, full health checks, and screenshots unless they are needed to prove the requested change.",
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
    checks["screenshots"] = {"ok": Path(settings.screenshot_dir).expanduser().resolve().parent.exists(), "path": settings.screenshot_dir}
    try:
        import playwright  # noqa: F401

        checks["playwright_python"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        checks["playwright_python"] = {"ok": False, "error": str(exc)}
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
    """V15 Multi-Client bootstrap for every ChatGPT/Claude session.

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
    for index, path in enumerate(payload.paths, start=1):
        result = await PlaywrightService().screenshot_odoo_page(payload.base_url, payload.login, payload.password, path, f"{payload.screenshot_prefix}_{index}.png", payload.timeout_seconds, full_page=True)
        status_ = "passed" if result.status == "ok" and not result.console_errors and not result.network_errors else "warning"
        if status_ != "passed":
            flags.append("warning")
        store.add_step(store.get_job(job.id), JobStep(name=f"browser_path:{path}", status=status_, finished_at=datetime.now(timezone.utc).isoformat(), result=result.model_dump(mode="json")))
    final = "warning" if flags else "passed"
    finished = store.finish_job(store.get_job(job.id), JobStatus.warning if flags else JobStatus.passed, {"status": final, "paths": payload.paths})
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
        result = await PlaywrightService().screenshot_odoo_page(
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
    """Non-blocking deploy: spawns a background thread and returns job_id immediately.

    ChatGPT must poll /jobs/{job_id} via jobs.get tool until status != running.
    This prevents Render/Koyeb HTTP timeouts from killing long-running deploys.
    """
    approval = ApprovalService()
    approval.ensure_allowed("workflow.safe_deploy_module", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))

    store = JsonStore()
    job = store.create_job(JobRecord(
        job_type="safe_deploy_module",
        status=JobStatus.running,
        request=payload.model_dump(mode="json"),
    ))

    def _background():
        try:
            _run_safe_deploy(payload, job.id)
        except Exception as exc:
            store2 = JsonStore()
            j = store2.get_job(job.id)
            if j.status == JobStatus.running:
                store2.finish_job(j, JobStatus.failed, {"error": f"Unhandled exception: {exc}"})

    submit_background(_background)

    return WorkflowReport(
        status="running",
        job_id=job.id,
        steps=[],
        summary={
            "message": "Deployment started in background. Poll jobs.get with this job_id until status is passed or failed.",
            "job_id": job.id,
            "module_name": payload.module_name,
        },
    )


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
        if name in {"odoosh_wait_for_commit", "odoosh_verify_module_manifest", "odoosh_module_update", "odoo_module_status"}:
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
    _step("resolve_project", "passed", {"project_path": str(project_path), "module_path": str(module_path)})

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

    commit_result = git.commit(str(project_path), payload.commit_message, payload.paths, min(payload.timeout_seconds, 300))
    if commit_result.exit_code != 0:
        _fail("git_commit", commit_result)
        return
    _step("git_commit", "passed", commit_result)

    sha_result = git.head_sha(str(project_path))
    if sha_result.exit_code != 0:
        _fail("git_head_sha", sha_result)
        return
    pushed_sha = sha_result.stdout.strip()
    _step("git_head_sha", "passed", sha_result)

    push_result = git.push(str(project_path), payload.remote, branch, True, min(payload.timeout_seconds, 600))
    if push_result.exit_code != 0:
        _fail("git_push", push_result)
        return
    _step("git_push", "passed", push_result)

    if payload.wait_for_odoosh:
        wait_result = odoosh.wait_for_commit(pushed_sha, min(payload.timeout_seconds, 1800), settings.odoosh_wait_interval_seconds)
        if wait_result.exit_code != 0:
            _fail("odoosh_wait_for_commit", wait_result)
            return
        _step("odoosh_wait_for_commit", "passed", wait_result)

    manifest_result = odoosh.verify_module_manifest(payload.module_name)
    if manifest_result.exit_code != 0:
        _fail("odoosh_verify_module_manifest", manifest_result)
        return
    _step("odoosh_verify_module_manifest", "passed", manifest_result)

    if payload.upgrade_module:
        _step("db_rollback_checkpoint", "warning", {"note": "Code rollback is automatic, but database schema/data rollback requires an Odoo.sh backup or duplicate staging before upgrade. Create/confirm backup before destructive migrations."})
        update_result = odoosh.module_update(payload.module_name, min(payload.timeout_seconds, 1800))
        if update_result.exit_code != 0:
            _fail("odoosh_module_update", update_result)
            return
        _step("odoosh_module_update", "passed", update_result)

    module_status = OdooRpcService().module_status(payload.module_name)
    status_ = "passed" if module_status.status == "ok" else "warning"
    _step("odoo_module_status", status_, module_status)

    smoke = OdooRpcService().registry_smoke_test(payload.module_name, [], [], strict=False)
    _step("registry_smoke_test", "passed" if smoke.status == "ok" else "warning", smoke)

    fresh_job = store.get_job(job_id)
    store.finish_job(fresh_job, JobStatus.passed, {"module_name": payload.module_name, "sha": pushed_sha})
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

    Search priority (mirrors Odoo.sh module load order):
      1. /home/odoo/src/user       — custom addons
      2. /home/odoo/src/enterprise — Odoo enterprise
      3. /home/odoo/src/odoo       — Odoo community

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

    Search priority: user → enterprise → odoo (same as find_core_file).

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

    Search priority: user → enterprise → odoo (same as find_core_file).

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
