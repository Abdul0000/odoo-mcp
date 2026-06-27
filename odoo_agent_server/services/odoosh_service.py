from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from ..schemas import CommandResult, OdooShLogName


@dataclass(frozen=True)
class SshTarget:
    host: str
    user: str
    port: int = 22
    key_path: Path | None = None


class OdooShService:
    """Small, safe SSH connector for Odoo.sh builds.

    Odoo.sh does not need a special deploy trigger in most projects: pushing the
    GitHub branch is what starts a new build. This service is for verification:
    reading logs, checking the build filesystem, and optionally running an
    allowlisted module update command on a development/staging shell.
    """

    def _target(self) -> SshTarget:
        if not settings.odoosh_ssh_host:
            raise ValueError("ODOOSH_SSH_HOST is not configured")
        return SshTarget(
            host=settings.odoosh_ssh_host,
            user=settings.odoosh_ssh_user or "odoo",
            port=settings.odoosh_ssh_port,
            key_path=Path(settings.odoosh_ssh_key_path).expanduser().resolve()
            if settings.odoosh_ssh_key_path
            else None,
        )

    def _base_ssh_command(self, target: SshTarget) -> list[str]:
        command = [
            "ssh",
            "-p",
            str(target.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=15",
        ]
        if target.key_path:
            command.extend(["-i", str(target.key_path), "-o", "IdentitiesOnly=yes"])
        command.append(f"{target.user}@{target.host}")
        return command

    def run_shell(self, remote_script: str, timeout_seconds: int = 60) -> CommandResult:
        target = self._target()
        command = self._base_ssh_command(target) + [remote_script]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            return CommandResult(
                command=self._redacted_command(command),
                cwd="odoosh:ssh",
                exit_code=completed.returncode,
                stdout=completed.stdout[-20000:],
                stderr=completed.stderr[-20000:],
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command=self._redacted_command(command),
                cwd="odoosh:ssh",
                exit_code=124,
                stdout=(exc.stdout or "")[-20000:] if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "")[-20000:] if isinstance(exc.stderr, str) else "",
                timed_out=True,
            )

    def health(self) -> CommandResult:
        script = "set -e; echo ODOOSH_SSH_OK; hostname; pwd; test -d ~/src/user && echo SRC_USER_OK; test -d ~/logs && echo LOGS_OK"
        return self.run_shell(script, timeout_seconds=30)

    def git_status(self) -> CommandResult:
        script = "set -e; cd ~/src/user; git status --short --branch; git rev-parse --abbrev-ref HEAD || true; git rev-parse HEAD"
        return self.run_shell(script, timeout_seconds=30)

    def tail_log(self, log_name: OdooShLogName, tail_lines: int = 200) -> CommandResult:
        safe_lines = max(20, min(tail_lines, 2000))
        filename = f"~/logs/{log_name.value}.log"
        script = f"test -f {filename} && tail -n {safe_lines} {filename} || echo 'Log not found: {filename}'"
        return self.run_shell(script, timeout_seconds=30)

    def grep_errors(self, tail_lines: int = 200) -> CommandResult:
        safe_lines = max(20, min(tail_lines, 2000))
        pattern = r"ERROR|CRITICAL|Traceback|Exception|ValidationError|ParseError|failed|Failed|ModuleNotFoundError|ImportError"
        script = (
            "set +e; "
            f"grep -RniE {shlex.quote(pattern)} ~/logs/*.log 2>/dev/null | tail -n {safe_lines}; "
            "exit 0"
        )
        return self.run_shell(script, timeout_seconds=45)

    def module_update(self, module_name: str, timeout_seconds: int = 600) -> CommandResult:
        # This is intentionally explicit and requires ODOOSH_DB_NAME. It should be
        # used only on development/staging builds unless the user approves production.
        if not settings.odoosh_db_name:
            return CommandResult(
                command=["odoosh_module_update"],
                cwd="odoosh:ssh",
                exit_code=2,
                stderr="ODOOSH_DB_NAME is not configured. Set the exact database name for the target build.",
            )
        safe_module = shlex.quote(module_name)
        safe_db = shlex.quote(settings.odoosh_db_name)
        script = (
            "set -e; "
            "cd ~/src/odoo; "
            f"./odoo-bin -d {safe_db} -u {safe_module} --stop-after-init --log-level=info"
        )
        return self.run_shell(script, timeout_seconds=timeout_seconds)

    @staticmethod
    def _redacted_command(command: list[str]) -> list[str]:
        redacted: list[str] = []
        skip_next = False
        for item in command:
            if skip_next:
                redacted.append("<redacted>")
                skip_next = False
                continue
            redacted.append(item)
            if item == "-i":
                skip_next = True
        return redacted
