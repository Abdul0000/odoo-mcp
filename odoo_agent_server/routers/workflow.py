from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from ..auth import AuthDependency
from ..config import settings
from ..schemas import (
    CapabilityCheckResult,
    CommandResult,
    FullVerifyRequest,
    JobRecord,
    JobStatus,
    JobStep,
    Severity,
    WorkflowReport,
    SafeDeployModuleRequest,
    SelfRunRequest,
)
from ..services.approval_service import ApprovalService
from ..services.git_service import GitService
from ..services.odoo_rpc_service import OdooRpcService
from ..services.odoosh_service import OdooShService
from ..services.path_service import resolve_allowed_path
from ..services.playwright_service import PlaywrightService
from ..services.repo_service import RepoService
from ..services.store_service import JsonStore
from .qa import self_run

router = APIRouter(prefix="/workflow", tags=["workflow"], dependencies=[AuthDependency])


@router.get("/capability-check", response_model=CapabilityCheckResult)
def capability_check() -> CapabilityCheckResult:
    checks: dict[str, dict] = {}
    checks["allowed_roots"] = {"ok": bool(settings.allowed_roots), "value": [str(p) for p in settings.allowed_roots]}
    checks["git"] = {"ok": shutil.which("git") is not None, "path": shutil.which("git")}
    checks["ssh"] = {"ok": shutil.which("ssh") is not None, "path": shutil.which("ssh")}
    checks["odoo_rpc_config"] = {"ok": all([settings.odoo_base_url, settings.odoo_db_name, settings.odoo_login, settings.odoo_password])}
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
def safe_deploy_module(payload: SafeDeployModuleRequest) -> WorkflowReport:
    """Atomic safe path: commit, push, wait for Odoo.sh deployment, then update module.

    This endpoint intentionally performs the deployment as one approved workflow
    so tools cannot accidentally upgrade stale Odoo.sh code after a push.
    """
    approval = ApprovalService()
    approval.ensure_allowed("workflow.safe_deploy_module", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))

    store = JsonStore()
    job = store.create_job(JobRecord(job_type="safe_deploy_module", status=JobStatus.running, request=payload.model_dump(mode="json")))
    git = GitService()
    odoosh = OdooShService()

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

    def fail(name: str, result=None, error: str | None = None) -> WorkflowReport:
        step(name, "failed", result, error)
        fresh = store.get_job(job.id)
        finished = store.finish_job(fresh, JobStatus.failed, {"failed_step": name, "module_name": payload.module_name})
        approval.mark_failed(payload.approval_id, f"workflow.safe_deploy_module failed at {name}")
        return WorkflowReport(status="failed", job_id=job.id, steps=finished.steps, summary={"failed_step": name, "module_name": payload.module_name})

    try:
        project_path = resolve_allowed_path(payload.project_path)
        module_path = project_path / payload.module_name
        if not module_path.exists():
            return fail("resolve_project", error=f"Module path not found: {module_path}")
        step("resolve_project", "passed", {"project_path": str(project_path), "module_path": str(module_path)})
    except Exception as exc:  # noqa: BLE001
        return fail("resolve_project", error=str(exc))

    status_result = git.status(str(project_path))
    if status_result.exit_code != 0:
        return fail("git_status", status_result)
    step("git_status", "passed", status_result)

    diff_result = git.diff(str(project_path), staged=False, timeout_seconds=30)
    if diff_result.exit_code != 0:
        return fail("git_diff", diff_result)
    step("git_diff", "passed", diff_result)

    if payload.run_qa:
        qa_report = self_run(SelfRunRequest(project_path=str(project_path), module_name=payload.module_name, run_pytest=True, include_git_status=True, timeout_seconds=min(payload.timeout_seconds, 600)))
        step("qa_self_run", qa_report.status, qa_report)
        if qa_report.status == "failed":
            return fail("qa_self_run", qa_report)

    identity_result = git.configure_identity(str(project_path))
    if identity_result.exit_code != 0:
        return fail("git_configure_identity", identity_result)
    step("git_configure_identity", "passed", identity_result)

    branch = payload.branch or settings.target_git_branch
    target_result = git.validate_target(str(project_path), branch, payload.remote)
    if target_result.exit_code != 0:
        return fail("git_validate_target", target_result)
    step("git_validate_target", "passed", target_result)

    commit_result = git.commit(str(project_path), payload.commit_message, payload.paths, min(payload.timeout_seconds, 300))
    if commit_result.exit_code != 0:
        return fail("git_commit", commit_result)
    step("git_commit", "passed", commit_result)

    sha_result = git.head_sha(str(project_path))
    if sha_result.exit_code != 0:
        return fail("git_head_sha", sha_result)
    pushed_sha = sha_result.stdout.strip()
    step("git_head_sha", "passed", sha_result)

    push_result = git.push(str(project_path), payload.remote, branch, True, min(payload.timeout_seconds, 600))
    if push_result.exit_code != 0:
        return fail("git_push", push_result)
    step("git_push", "passed", push_result)

    if payload.wait_for_odoosh:
        wait_result = odoosh.wait_for_commit(pushed_sha, min(payload.timeout_seconds, 1800), settings.odoosh_wait_interval_seconds)
        if wait_result.exit_code != 0:
            return fail("odoosh_wait_for_commit", wait_result)
        step("odoosh_wait_for_commit", "passed", wait_result)

    manifest_result = odoosh.verify_module_manifest(payload.module_name)
    if manifest_result.exit_code != 0:
        return fail("odoosh_verify_module_manifest", manifest_result)
    step("odoosh_verify_module_manifest", "passed", manifest_result)

    if payload.upgrade_module:
        update_result = odoosh.module_update(payload.module_name, min(payload.timeout_seconds, 1800))
        if update_result.exit_code != 0:
            return fail("odoosh_module_update", update_result)
        step("odoosh_module_update", "passed", update_result)

    module_status = OdooRpcService().module_status(payload.module_name)
    status_ = "passed" if module_status.status == "ok" else "warning"
    step("odoo_module_status", status_, module_status)

    fresh = store.get_job(job.id)
    finished = store.finish_job(fresh, JobStatus.passed, {"module_name": payload.module_name, "sha": pushed_sha})
    approval.mark_consumed(payload.approval_id, "workflow.safe_deploy_module succeeded")
    return WorkflowReport(
        status="passed",
        job_id=job.id,
        steps=finished.steps,
        summary={"module_name": payload.module_name, "sha": pushed_sha, "result": "passed"},
    )
