from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import Finding, SelfRunReport, SelfRunRequest, Severity
from ..services.path_service import resolve_allowed_path
from ..services.test_runner import TestRunner
from ..services.validator_service import OdooModuleValidator

router = APIRouter(prefix="/qa", tags=["qa"], dependencies=[AuthDependency])


@router.post("/self-run", response_model=SelfRunReport)
def self_run(payload: SelfRunRequest) -> SelfRunReport:
    project_path = resolve_allowed_path(payload.project_path)
    module_path = project_path / payload.module_name

    findings: list[Finding] = []
    commands = []

    validator = OdooModuleValidator()
    findings.extend(validator.validate(module_path))

    runner = TestRunner()
    if payload.include_git_status:
        git_result = runner.git_status(project_path)
        commands.append(git_result)
        if git_result.exit_code not in (0, 128):
            findings.append(
                Finding(
                    severity=Severity.warning,
                    code="GIT_STATUS_FAILED",
                    message="Could not read git status.",
                    hint=git_result.stderr or git_result.stdout,
                )
            )

    if payload.run_pytest:
        has_tests = (module_path / "tests").exists() or (project_path / "tests").exists()
        if has_tests:
            pytest_result = runner.pytest(project_path, timeout_seconds=payload.timeout_seconds)
            commands.append(pytest_result)
            if pytest_result.exit_code != 0:
                findings.append(
                    Finding(
                        severity=Severity.error,
                        code="PYTEST_FAILED",
                        message="pytest failed.",
                        hint=(pytest_result.stderr or pytest_result.stdout)[-4000:],
                    )
                )
        else:
            findings.append(
                Finding(
                    severity=Severity.info,
                    code="NO_TESTS_FOUND",
                    message="No tests folder found. Static validation completed only.",
                )
            )

    error_count = sum(1 for item in findings if item.severity == Severity.error)
    warning_count = sum(1 for item in findings if item.severity == Severity.warning)
    status = "failed" if error_count else "warning" if warning_count else "passed"

    return SelfRunReport(
        status=status,
        project_path=str(project_path),
        module_path=str(module_path),
        module_name=payload.module_name,
        findings=findings,
        commands=commands,
        summary={
            "errors": error_count,
            "warnings": warning_count,
            "infos": sum(1 for item in findings if item.severity == Severity.info),
            "commands_run": len(commands),
        },
    )
