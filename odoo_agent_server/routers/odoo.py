from __future__ import annotations

import json
import re
import time

from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import CommandResult, JobRecord, JobStatus, OdooModuleRequest, OdooResult, OdooSearchReadRequest
from ..services.approval_service import ApprovalService
from ..services.odoo_service import OdooService
from ..services.odoosh_service import OdooShService
from ..services.store_service import JsonStore
from ..services.workflow_guard_service import safe_deploy_shell_block
from ..services.client_gate_service import ClientGateService
from ..services.client_resolver_service import ClientResolverService
from ..services.background_executor import background_task, submit_background

router = APIRouter(prefix="/odoo", tags=["odoo"], dependencies=[AuthDependency])

_TIMEOUT_RESULT_RE = re.compile(r"\b(?:timed?\s*out|timeout|deadline exceeded)\b", re.I)


def _odoo_service_for_client(client=None):
    if client is not None:
        try:
            return OdooService(client)
        except TypeError:
            return OdooService()
    return OdooService()


def _module_state_value(result: OdooResult) -> str | None:
    rows = result.result if result.status == "ok" and isinstance(result.result, list) else []
    if not rows or not isinstance(rows[0], dict):
        return None
    return str(rows[0].get("state") or "").strip().lower() or None


def _module_deploy_with_timeout_recovery(service: OdooService, payload: OdooModuleRequest) -> OdooResult:
    """Retry one idempotent smart deploy only after checking live module state."""
    try:
        first = service.module_deploy(payload.module_name, timeout_seconds=payload.operation_timeout_seconds)
    except TypeError:
        first = service.module_deploy(payload.module_name)
    first_text = json.dumps(first.model_dump(mode="json"), ensure_ascii=False, default=str)
    if first.status == "ok" or not _TIMEOUT_RESULT_RE.search(first_text):
        return first

    status_after_timeout = service.module_status(payload.module_name)
    state = _module_state_value(status_after_timeout)
    if state == "installed":
        return OdooResult(
            status="ok",
            result={
                "module_name": payload.module_name,
                "state": state,
                "timeout_recovered": True,
                "attempts": 1,
                "message": "The install command response timed out, but live Odoo status confirms the module is installed.",
                "first_attempt": first.model_dump(mode="json"),
                "status_probe": status_after_timeout.model_dump(mode="json"),
            },
        )

    try:
        retry = service.module_deploy(payload.module_name, timeout_seconds=payload.operation_timeout_seconds)
    except TypeError:
        retry = service.module_deploy(payload.module_name)
    if retry.status == "ok":
        data = retry.result if isinstance(retry.result, dict) else {"result": retry.result}
        retry.result = {
            **data,
            "timeout_recovered": True,
            "attempts": 2,
            "first_attempt": first.model_dump(mode="json"),
            "status_probe": status_after_timeout.model_dump(mode="json"),
        }
        return retry

    final_status = service.module_status(payload.module_name)
    final_state = _module_state_value(final_status)
    if final_state == "installed":
        return OdooResult(
            status="ok",
            result={
                "module_name": payload.module_name,
                "state": final_state,
                "timeout_recovered": True,
                "attempts": 2,
                "message": "The retry response was inconclusive, but live Odoo status confirms installation.",
                "first_attempt": first.model_dump(mode="json"),
                "retry_attempt": retry.model_dump(mode="json"),
                "final_status_probe": final_status.model_dump(mode="json"),
            },
        )
    return OdooResult(
        status="failed",
        error=retry.error or first.error or "Module install/upgrade timed out after one verified retry.",
        result={
            "module_name": payload.module_name,
            "timeout_recovery_attempted": True,
            "attempts": 2,
            "first_attempt": first.model_dump(mode="json"),
            "status_probe": status_after_timeout.model_dump(mode="json"),
            "retry_attempt": retry.model_dump(mode="json"),
            "final_status_probe": final_status.model_dump(mode="json"),
        },
    )


def _prepare_validated_client(payload: OdooModuleRequest, action: str):
    ClientGateService().require_validated_client(payload.client_name, action)
    if payload.client_name:
        client = ClientResolverService().resolve_client(client_name=payload.client_name, require_complete=True)
        return client
    return None


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


def _direct_module_deploy_guard(payload: OdooModuleRequest, store: JsonStore) -> OdooResult | None:
    """Block direct install/upgrade after manual git.push until Odoo.sh is verified."""
    # A local-first MCP client such as OpenCode pushes from the user's PC, so
    # this server has no local Git-push session record. The explicit pushed SHA
    # is the cross-machine trust boundary: never upgrade until Odoo.sh proves
    # that exact commit is active in ~/src/user.
    if payload.expected_sha:
        client = _prepare_validated_client(payload, "odoo.module_deploy_expected_sha_guard")
        odoosh = OdooShService(client) if client else OdooShService()
        check = _poll_manual_push_deployed(
            odoosh,
            payload.expected_sha,
            payload.deploy_guard_timeout_seconds,
            payload.deploy_guard_poll_interval_seconds,
        )
        if check.exit_code != 0:
            return OdooResult(
                status="failed",
                error="Module deploy paused: Odoo.sh has not activated the pushed expected_sha, so install/upgrade would target stale source.",
                result={
                    "status": "deployment_waiting",
                    "display_message": "The local push is complete; waiting for Odoo.sh to activate the exact commit before module install/upgrade.",
                    "continuation_required": True,
                    "interruption_expected": True,
                    "sha": payload.expected_sha,
                    "guard": check.model_dump(mode="json"),
                    "next_call": {
                        "tool": "odoo.module_deploy",
                        "arguments": {
                            "client_name": payload.client_name,
                            "module_name": payload.module_name,
                            "expected_sha": payload.expected_sha,
                            "wait_for_result": False,
                        },
                    },
                },
            )
        store.save_session_state({
            "opencode_expected_sha": {
                "sha": payload.expected_sha,
                "module_name": payload.module_name,
                "deployed": True,
            }
        }, client_name=payload.client_name)
        return None

    state = store.load_session_state(client_name=payload.client_name)
    push = state.get("last_git_push")
    if not isinstance(push, dict) or push.get("verified_for_module_deploy"):
        return None

    pushed_sha = str(push.get("sha") or "").strip()
    module_names = set(push.get("module_names") or [])
    if module_names and payload.module_name not in module_names:
        return None
    if not pushed_sha:
        return OdooResult(
            status="failed",
            error="Direct module deploy blocked: a recent git.push was recorded without a pushed SHA. Use workflow.safe_deploy_module or workflow.resume_job.",
            result={"status": "deploy_guard_missing_sha", "next_call": {"tool": "workflow.resume_job", "arguments": {"module_name": payload.module_name}}},
        )

    client = _prepare_validated_client(payload, "odoo.module_deploy_guard")
    odoosh = OdooShService(client) if client else OdooShService()
    check = _poll_manual_push_deployed(
        odoosh,
        pushed_sha,
        payload.deploy_guard_timeout_seconds,
        payload.deploy_guard_poll_interval_seconds,
    )
    if check.exit_code != 0:
        return OdooResult(
            status="failed",
            error="Direct module deploy blocked: Odoo.sh has not deployed the recent git.push SHA, so module upgrade would target stale code.",
            result={
                "status": "deployment_waiting",
                "display_message": "Module upgrade is paused safely until Odoo.sh activates the pushed commit.",
                "continuation_required": True,
                "interruption_expected": True,
                "sha": pushed_sha,
                "guard": check.model_dump(mode="json"),
                "next_call": {"tool": "workflow.resume_job", "arguments": {"module_name": payload.module_name}},
            },
        )

    failure = odoosh.recent_build_failure(int(push.get("started_epoch") or 0))
    if failure.exit_code == 1:
        return OdooResult(
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
            return OdooResult(
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
    }, client_name=payload.client_name)
    return None


def _wait_until_terminal(store: JsonStore, job_id: str, poll_interval_seconds: float) -> JobRecord:
    """Block until a background Odoo job reaches a terminal state.

    `wait_for_result=true` is an explicit request for a final answer. Returning
    a polite "started / check back" payload from that mode caused agents to
    report unconfirmed upgrades as completed. Non-blocking behavior remains
    available by calling the tool with `wait_for_result=false`.
    """
    interval = max(min(float(poll_interval_seconds or 2.0), 10.0), 0.25)
    while True:
        current = store.get_job(job_id)
        if current.status not in {JobStatus.queued, JobStatus.running}:
            return current
        time.sleep(interval)


def _background_job_response(store: JsonStore, job: JobRecord, payload: OdooModuleRequest, started_message: str) -> OdooResult:
    """Return a final result for wait_for_result=true; otherwise return job_id.

    A wait-for-result deploy/upgrade must never hand back an "in progress" or
    "check back later" message. It either blocks until the module deploy job
    passes/fails, or callers can opt into the old asynchronous behavior with
    `wait_for_result=false`.
    """
    if not payload.wait_for_result:
        return OdooResult(
            status="failed",
            error="Deploy/upgrade job started but is not complete. Poll jobs.get until terminal passed/failed.",
            result={
                "job_id": job.id,
                "job_status": job.status.value,
                "status": "running",
                "finished": False,
                "message": f"{started_message} This is not a confirmed deployment. Poll jobs.get until terminal passed/failed.",
                "completion_required": True,
                "success_allowed": False,
                "tool_exit_allowed": False,
                "poll_after_seconds": 5,
                "next_call": {
                    "tool": "jobs.get",
                    "arguments": {
                        "job_id": job.id,
                        "wait_for_result": True,
                        "wait_timeout_seconds": 20,
                        "poll_interval_seconds": 2.0,
                    },
                },
            },
        )

    waited = _wait_until_terminal(store, job.id, payload.poll_interval_seconds)
    data = waited.model_dump(mode="json")
    if waited.status == JobStatus.passed:
        return OdooResult(
            status="ok",
            result={
                "job_id": waited.id,
                "job_status": waited.status.value,
                "finished": True,
                "message": "Upgrade confirmed successful after 1 attempt(s).",
                "job": data,
            },
        )
    if waited.status in {JobStatus.failed, JobStatus.warning}:
        result_error = None
        if isinstance(waited.result, dict):
            result_error = waited.result.get("error") or waited.result.get("message")
        final_error = result_error or f"Job finished with status={waited.status.value}"
        return OdooResult(
            status="failed" if waited.status == JobStatus.failed else "ok",
            result={
                "job_id": waited.id,
                "job_status": waited.status.value,
                "finished": True,
                "message": f"Upgrade failed after 1 attempt(s), last error: {final_error}" if waited.status == JobStatus.failed else f"Job finished with status={waited.status.value}",
                "job": data,
            },
            error=final_error,
        )

    # _wait_until_terminal cannot return an active job, but keep this defensive
    # failure branch so future enum changes never produce success-sounding text.
    final_error = f"Module deploy did not produce a terminal result; job_status={waited.status.value}"
    return OdooResult(
        status="failed",
        result={"job_id": waited.id, "job_status": waited.status.value, "finished": True, "message": f"Upgrade failed after 1 attempt(s), last error: {final_error}", "job": data},
        error=final_error,
    )


def _existing_post_push_job_response(store: JsonStore, payload: OdooModuleRequest) -> OdooResult | None:
    """Return the existing post-push deploy job instead of starting a duplicate direct deploy.

    After raw git.push auto-starts a follow-up upgrade job, direct odoo.module_deploy
    must not create a second deploy job or report success. It must direct the agent
    to jobs.get until the existing job is terminal.
    """
    state = store.load_session_state(client_name=payload.client_name)
    push = state.get("last_git_push")
    if not isinstance(push, dict):
        return None
    job_id = str(push.get("follow_up_job_id") or "").strip()
    if not job_id:
        return None
    module_names = set(push.get("module_names") or [])
    if module_names and payload.module_name not in module_names:
        return None
    try:
        job = store.get_job(job_id)
    except Exception:
        return None
    data = job.model_dump(mode="json")
    if job.status in {JobStatus.queued, JobStatus.running}:
        return OdooResult(
            status="failed",
            error="Deploy/upgrade is still running. This is not confirmed success; poll jobs.get.",
            result={
                "status": "running",
                "job_id": job.id,
                "job_status": job.status.value,
                "finished": False,
                "completion_required": True,
                "success_allowed": False,
                "tool_exit_allowed": False,
                "poll_after_seconds": 5,
                "message": "Existing post-push deploy/upgrade job is still running. Do not report deployed/upgraded yet.",
                "job": data,
                "next_call": {
                    "tool": "jobs.get",
                    "arguments": {
                        "job_id": job.id,
                        "wait_for_result": True,
                        "wait_timeout_seconds": 20,
                        "poll_interval_seconds": 2.0,
                    },
                },
            },
        )
    if job.status == JobStatus.passed:
        return OdooResult(status="ok", result={"status": "passed", "job_id": job.id, "job_status": job.status.value, "finished": True, "success_allowed": True, "job": data})
    return OdooResult(status="failed", error="Existing post-push deploy/upgrade job failed or ended with warning.", result={"status": job.status.value, "job_id": job.id, "job_status": job.status.value, "finished": True, "success_allowed": False, "job": data})


@router.post("/health", response_model=OdooResult)
def odoo_health() -> OdooResult:
    client = ClientResolverService().resolve_client(require_complete=False)
    result = _odoo_service_for_client(client).health()
    JsonStore().log_audit("odoo.health", result.status, "Odoo shell health")
    return result


@router.post("/module-status", response_model=OdooResult)
def odoo_module_status(payload: OdooModuleRequest) -> OdooResult:
    client = _prepare_validated_client(payload, "odoo.module_status")
    result = _odoo_service_for_client(client).module_status(payload.module_name)
    JsonStore().log_audit("odoo.module_status", result.status, payload.module_name)
    return result


@router.post("/module-install", response_model=OdooResult)
def odoo_module_install(payload: OdooModuleRequest) -> OdooResult:
    client = _prepare_validated_client(payload, "odoo.module_install")
    """Background install — returns job_id immediately, poll jobs.get for result."""
    blocked = safe_deploy_shell_block("odoo.module_install", module_name=payload.module_name)
    if blocked:
        return blocked
    approval = ApprovalService()
    approval.ensure_allowed("odoo.module_install", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="odoo.module_install", status=JobStatus.running, request={"module_name": payload.module_name}))

    def _bg():
        try:
            guarded = _direct_module_deploy_guard(payload, store)
            result = guarded or _odoo_service_for_client(client).module_install(payload.module_name)
        except Exception as exc:
            result = OdooResult(status="failed", error=f"Unexpected error: {exc}")
        j = store.get_job(job.id)
        store.finish_job(j, JobStatus.passed if result.status == "ok" else JobStatus.failed, result.model_dump(mode="json"))
        approval.finalize_status(payload.approval_id, result.status == "ok", "odoo.module_install succeeded", "odoo.module_install failed")
        store.log_audit("odoo.module_install", result.status, payload.module_name)

    submit_background(background_task(_bg, "deploy"))
    return _background_job_response(store, job, payload, "Install started.")


@router.post("/module-update", response_model=OdooResult)
def odoo_module_update(payload: OdooModuleRequest) -> OdooResult:
    client = _prepare_validated_client(payload, "odoo.module_update")
    """Background upgrade — returns job_id immediately, poll jobs.get for result."""
    blocked = safe_deploy_shell_block("odoo.module_update", module_name=payload.module_name)
    if blocked:
        return blocked
    approval = ApprovalService()
    approval.ensure_allowed("odoo.module_update", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    store = JsonStore()
    job = store.create_job(JobRecord(job_type="odoo.module_update", status=JobStatus.running, request={"module_name": payload.module_name}))

    def _bg():
        try:
            guarded = _direct_module_deploy_guard(payload, store)
            result = guarded or _odoo_service_for_client(client).module_update(payload.module_name)
        except Exception as exc:
            result = OdooResult(status="failed", error=f"Unexpected error: {exc}")
        j = store.get_job(job.id)
        store.finish_job(j, JobStatus.passed if result.status == "ok" else JobStatus.failed, result.model_dump(mode="json"))
        approval.finalize_status(payload.approval_id, result.status == "ok", "odoo.module_update succeeded", "odoo.module_update failed")
        store.log_audit("odoo.module_update", result.status, payload.module_name)

    submit_background(background_task(_bg, "deploy"))
    return _background_job_response(store, job, payload, "Upgrade started.")


def start_odoo_module_deploy_job(payload: OdooModuleRequest, approval: ApprovalService | None = None, enforce_deploy_guard: bool = False) -> OdooResult:
    client = _prepare_validated_client(payload, "odoo.module_deploy")
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
            if guarded:
                result = guarded
            else:
                service = _odoo_service_for_client(client)
                result = _module_deploy_with_timeout_recovery(service, payload)
        except Exception as exc:
            result = OdooResult(status="failed", error=f"Unexpected error: {exc}")
        j = store.get_job(job.id)
        store.finish_job(j, JobStatus.passed if result.status == "ok" else JobStatus.failed, result.model_dump(mode="json"))
        if approval:
            approval.finalize_status(payload.approval_id, result.status == "ok", "odoo.module_deploy succeeded", "odoo.module_deploy failed")
        store.log_audit("odoo.module_deploy", result.status, payload.module_name)

    submit_background(background_task(_bg, "deploy"))
    return _background_job_response(store, job, payload, "Deploy started.")


@router.post("/module-deploy", response_model=OdooResult)
def odoo_module_deploy(payload: OdooModuleRequest) -> OdooResult:
    _prepare_validated_client(payload, "odoo.module_deploy")
    """Smart deploy: update_list → check state → install/upgrade. Background job."""
    blocked = safe_deploy_shell_block("odoo.module_deploy", module_name=payload.module_name)
    if blocked:
        return blocked
    store = JsonStore()
    existing = _existing_post_push_job_response(store, payload)
    if existing:
        return existing
    approval = ApprovalService()
    approval.ensure_allowed("odoo.module_deploy", payload.approval_id, payload.model_dump(exclude={"approval_id"}, mode="json"))
    return start_odoo_module_deploy_job(payload, approval, enforce_deploy_guard=True)


@router.post("/search-read", response_model=OdooResult)
def odoo_search_read(payload: OdooSearchReadRequest) -> OdooResult:
    client = ClientResolverService().resolve_client(client_name=payload.client_name, require_complete=True) if payload.client_name else ClientResolverService().resolve_client(require_complete=False)
    result = _odoo_service_for_client(client).search_read(payload.model, payload.domain, payload.fields, payload.limit, payload.offset, payload.order, payload.context)
    JsonStore().log_audit("odoo.search_read", result.status, payload.model, {"fields": payload.fields, "limit": payload.limit})
    return result
