from __future__ import annotations

import subprocess
from pathlib import Path

from ..schemas import CommandResult


class TestRunner:
    """Safe command runner for bounded QA commands."""

    def run(self, command: list[str], cwd: Path, timeout_seconds: int) -> CommandResult:
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            return CommandResult(
                command=command,
                cwd=str(cwd),
                exit_code=completed.returncode,
                stdout=completed.stdout[-12000:],
                stderr=completed.stderr[-12000:],
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command=command,
                cwd=str(cwd),
                exit_code=124,
                stdout=(exc.stdout or "")[-12000:] if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "")[-12000:] if isinstance(exc.stderr, str) else "",
                timed_out=True,
            )
        except OSError as exc:
            # Keep QA/reporting endpoints deterministic when a stale session
            # points at a missing checkout.  subprocess.run raises before it
            # can return a CompletedProcess if cwd does not exist; surface that
            # as a normal failed command so the agent cannot treat the step as
            # verified.
            return CommandResult(
                command=command,
                cwd=str(cwd),
                exit_code=127,
                stdout="",
                stderr=f"Command could not start in cwd {cwd}: {exc}",
            )

    def git_status(self, cwd: Path, timeout_seconds: int = 20) -> CommandResult:
        return self.run(["git", "status", "--short", "--branch"], cwd, timeout_seconds)

    def pytest(self, cwd: Path, timeout_seconds: int = 90) -> CommandResult:
        return self.run(["python", "-m", "pytest", "-q"], cwd, timeout_seconds)
