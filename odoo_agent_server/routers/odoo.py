from __future__ import annotations

import json
import time

from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import CommandResult, JobRecord, JobStatus, OdooRpcModuleRequest, OdooRpcResult, OdooSearchReadRequest
from ..services.approval_service import ApprovalService
from ..services.odoo_rpc_service import OdooRpcService
from ..services.odoosh_service import OdooShService
from ..services.store_service import JsonStore
from ..services.workflow_guard_service import safe_deploy_rpc_block
from ..services.background_executor import submit_background

router = APIRouter(prefix="/odoo", tags=["odoo"], dependencies=[AuthDependency])


def _poll_manual_push_deployed(odoosh: OdooShService, pushed_sha: str, timeout_seconds: int, interval_seconds: float) -> CommandResult:
    deadline = time.monotonic() + max(timeout_seconds, 0)
    attempts = 0
    recent_checks: list[dict] = []
    while True:
        attempts += 1
        result = odoosh.check_commit_deployed(pushed_sha)
        deployed = result.exit_code == 0 and '"deployed": true' in (result.stdout or "").lower()
        recent_checks.append({"attempt": attempts, "exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr})
        if deployed:
            return result.model_copy(update={
                "command": ["odoosh.check_commit_poll", pushed_sha],
                "stdout": json.dumps({"deployed": True, "expected_sha": pushed_sha, "attempts": attempts, "last_check": result.stdout}),
            })
        if time.monotonic() >= deadline:
            return CommandResult(
                command=["odoosh.check_commit_poll", pushed_sha],
                cwd="odoosh:ssh",
                exit_code=124,
                stdout=json.dumps({"deployed": False, "expected_sha": pushed_sha, "attempts": attempts, "recent_checks": recent_checks[-10:]}),
                stderr=f"Odoo.sh did not deploy pushed commit {pushed_sha} within {timeout_seconds}s.",
                timed_out=True,
            )
        time.sleep(min(interval_seconds, max(deadline - time.monotonic(), 0.0)))


def _direct_module_deploy_guard(payload: OdooRpcModuleRequest, store: JsonStore) -> OdooRpcResult | None:
    """Block direct install/upgrade after manual git.push until Odoo.sh is verified."""
    state = store.load_session_state()
    push = state.get("last_git_push")
    if not isinstance(push, dict) or push.get("verified_for_module_deploy"):
        return None

    pushed_sha = str(push.get("sha") or "").strip()
    module_names = set(push.get("module_names") or [])
    if module_names and payload.module_name not in module_names:
        return None
    if not pushed_sha:
        return OdooRpcResult(
            status="failed",
            error="Direct module deploy blocked: a recent git.push was recorded without a pushed SHA. Use workflow.safe_deploy_module or workflow.resume_job.",
            result={"status": "deploy_guard_missing_sha", "next_call": {"tool": "workflow.resume_job", "arguments": {"module_name": payload.module_name}}},
        )

    odoosh = OdooShService()
    check = _poll_manual_push_deployed(
        odoosh,
        pushed_sha,
        payload.deploy_guard_timeout_seconds,
        payload.deploy_guard_poll_interval_seconds,
    )
    if check.exit_code != 0:
        return OdooRpcResult(
            status="failed",
            error="Direct module deploy blocked: Odoo.sh has not deployed the recent git.push SHA, so module upgrade would target stale code.",
            result={
                "status": "deploy_failed_or_timeout",
                "sha": pushed_sha,
                "guard": check.model_dump(mode="json"),
                "next_call": {"tool": "workflow.resume_job", "arguments": {"module_name": payload.module_name}},
            },
        )

    failure = odoosh.recent_build_failure(int(push.get("started_epoch") or 0))
    if failure.exit_code == 1:
        return OdooRpcResult(
            status="failed",
            error="Direct module deploy blocked: Odoo.sh logs show build/test failure after the recent git.push.",
            result={
                "status": "deploy_failed",
                "sha": pushed_sha,
                "guard": failure.model_dump(mode="json"),
                "next_call": {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 1200}},
            },
        )

    expected_hashes = {
        rel: digest
        for rel, digest in (push.get("expected_user_file_hashes") or {}).items()
        if isinstance(rel, str) and rel.startswith(f"{payload.module_name}/")
    }
    if expected_hashes:
        hash_result = odoosh.verify_user_file_hashes(expected_hashes)
        if hash_result.exit_code != 0:
            return OdooRpcResult(
                status="failed",
                error="Direct module deploy blocked: /home/odoo/src/user files do not match the recent pushed code.",
                result={
                    "status": "deploy_code_mismatch",
                    "sha": pushed_sha,
                    "verified_files": sorted(expected_hashes),
                    "guard": hash_result.model_dump(mode="json"),
                    "next_call": {"tool": "odoosh.error_pack", "arguments": {"tail_lines": 1200}},
                },
            )

    push["verified_for_module_deploy"] = True
    push["verified_module_name"] = payload.module_name
    store.save_session_state({
        "last_git_push": push,
        "safe_deploy_status": "manual_push_verified_for_module_deploy",
    })
    return None


def _background_job_response(store: JsonStore, job: JobRecord, payload: OdooRpcModuleRequest, started_message: str) -> OdooRpcResult:
    """Return a final result when the job finishes quickly, otherwise a clean running state.

    This avoids fragile client-side polling loops and keeps every HTTP request
    below Render's practical timeout window.
    """
    if not payload.wait_for_result:
        return OdooRpcResult(status="ok", result={"job_id": job.id, "job_status": job.status.value, "finished": False, "message": started_message})

    waited = store.wait_job(job.id, payload.wait_timeout_seconds, payload.poll_interval_seconds)
    data = waited.model_dump(mode="json")
    if waited.status == JobStatus.passed:
        return OdooRpcResult(status="ok", result={"job_id": waited.id, "job_status": waited.status.value, "finished": True, "job": data})
    if waited.status in {JobStatus.failed, JobStatus.warning}:
        result_error = None
        if isinstance(waited.result, dict):
            result_error = waited.result.get("error")
        return OdooRpcResult(
            status="failed" if waited.status == JobStatus.failed else "ok",
            result={"job_id": waited.id, "job_status": waited.status.value, "finished": True, "job": data},
            error=result_error or f"Job finished with status={waited.status.value}",
        )
    return OdooRpcResult(
        status="ok",
        result={
            "job_id": waited.id,
            "job_status": waited.status.value,
            "finished": False,
            "message": f"{started_message} Still running after {payload.wait_timeout_seconds}s server-side wait. Call jobs.get with this job_id; jobs.get also waits by default.",
            "next_call": {"tool": "jobs.get", "arguments": {"job_id": waited.id, "wait_for_result": True}},
        },
    )


@router.post("/health", response_model=OdooRpcResult)
def odoo_health() -> OdooRpcResult:
    result = OdooRpcService().health()
    JsonStore().log_audit("odoo.health", result.status, "Odoo RPC health")
    return result


@router.post("/module-status", response_model=OdooRpcResult)
def odoo_module_status(payload: OdooRpcModuleRequest) -> OdooRpcResult:
    result = OdooRpcService().module_status(payload.module_name)
    JsonStore().log_audit("odoo.module_status", result.status, payload.module_name)
    return result


@router.post("/module-install", response_model=OdooRpcResult)
def odoo_module_install(payload: OdooRpcModuleRequest) -> OdooRpcResult:
    """Background install — returns job_id immediately, poll jobs.get for result."""
    blocked = safe_deploy_rpc_block("odoo.module_install", module_name=payload.module_name)
    if blocked:
        return blocked
    approval = ApprovalService()
    approval.ensure_allowed("odoo.module_install", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="odoo.module_install", status=JobStatus.running, request={"module_name": payload.module_name}))

    def _bg():
        try:
            guarded = _direct_module_deploy_guard(payload, store)
            result = guarded or OdooRpcService().module_install(payload.module_name)
        except Exception as exc:
            result = OdooRpcResult(status="failed", error=f"Unexpected error: {exc}")
        j = store.get_job(job.id)
        store.finish_job(j, JobStatus.passed if result.status == "ok" else JobStatus.failed, result.model_dump(mode="json"))
        approval.finalize_status(payload.approval_id, result.status == "ok", "odoo.module_install succeeded", "odoo.module_install failed")
        store.log_audit("odoo.module_install", result.status, payload.module_name)

    submit_background(_bg)
    return _background_job_response(store, job, payload, "Install started.")


@router.post("/module-update", response_model=OdooRpcResult)
def odoo_module_update(payload: OdooRpcModuleRequest) -> OdooRpcResult:
    """Background upgrade — returns job_id immediately, poll jobs.get for result."""
    blocked = safe_deploy_rpc_block("odoo.module_update", module_name=payload.module_name)
    if blocked:
        return blocked
    approval = ApprovalService()
    approval.ensure_allowed("odoo.module_update", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="odoo.module_update", status=JobStatus.running, request={"module_name": payload.module_name}))

    def _bg():
        try:
            guarded = _direct_module_deploy_guard(payload, store)
            result = guarded or OdooRpcService().module_update(payload.module_name)
        except Exception as exc:
            result = OdooRpcResult(status="failed", error=f"Unexpected error: {exc}")
        j = store.get_job(job.id)
        store.finish_job(j, JobStatus.passed if result.status == "ok" else JobStatus.failed, result.model_dump(mode="json"))
        approval.finalize_status(payload.approval_id, result.status == "ok", "odoo.module_update succeeded", "odoo.module_update failed")
        store.log_audit("odoo.module_update", result.status, payload.module_name)

    submit_background(_bg)
    return _background_job_response(store, job, payload, "Upgrade started.")


def start_odoo_module_deploy_job(payload: OdooRpcModuleRequest, approval: ApprovalService | None = None, enforce_deploy_guard: bool = False) -> OdooRpcResult:
    """Start smart deploy in the existing background-job path.

    Public router calls pass an ApprovalService after approval succeeds. Internal
    workflows that already enforced their own approval can reuse the same job
    path without asking for a second, mismatched approval.
    """
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="odoo.module_deploy", status=JobStatus.running, request={"module_name": payload.module_name}))

    def _bg():
        try:
            guarded = _direct_module_deploy_guard(payload, store) if enforce_deploy_guard else None
            result = guarded or OdooRpcService().module_deploy(payload.module_name)
        except Exception as exc:
            result = OdooRpcResult(status="failed", error=f"Unexpected error: {exc}")
        j = store.get_job(job.id)
        store.finish_job(j, JobStatus.passed if result.status == "ok" else JobStatus.failed, result.model_dump(mode="json"))
        if approval:
            approval.finalize_status(payload.approval_id, result.status == "ok", "odoo.module_deploy succeeded", "odoo.module_deploy failed")
        store.log_audit("odoo.module_deploy", result.status, payload.module_name)

    submit_background(_bg)
    return _background_job_response(store, job, payload, "Deploy started.")


@router.post("/module-deploy", response_model=OdooRpcResult)
def odoo_module_deploy(payload: OdooRpcModuleRequest) -> OdooRpcResult:
    """Smart deploy: update_list → check state → install/upgrade. Background job."""
    blocked = safe_deploy_rpc_block("odoo.module_deploy", module_name=payload.module_name)
    if blocked:
        return blocked
    approval = ApprovalService()
    approval.ensure_allowed("odoo.module_deploy", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    return start_odoo_module_deploy_job(payload, approval, enforce_deploy_guard=True)


@router.post("/search-read", response_model=OdooRpcResult)
def odoo_search_read(payload: OdooSearchReadRequest) -> OdooRpcResult:
    result = OdooRpcService().search_read(payload.model, payload.domain, payload.fields, payload.limit, payload.offset, payload.order, payload.context)
    JsonStore().log_audit("odoo.search_read", result.status, payload.model, {"fields": payload.fields, "limit": payload.limit})
    return result
