from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
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
    FAILURE_PATTERNS = [
        "not installable",
        "inconsistent states",
        "Traceback",
        "CRITICAL",
        "ParseError",
        "ValidationError",
        "ModuleNotFoundError",
        "ImportError",
        "Failed to load registry",
    ]

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
            "-p", str(target.port),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15",
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

    def deployed_sha(self) -> CommandResult:
        script = "set -e; cd ~/src/user; git rev-parse HEAD"
        return self.run_shell(script, timeout_seconds=30)

    # NON-BLOCKING single check — ChatGPT polls this instead of blocking wait
    def check_commit_deployed(self, expected_sha: str) -> CommandResult:
        """Return immediately with deployed=true/false. Never blocks.
        ChatGPT should call this in a polling loop with 15-second delays."""
        result = self.deployed_sha()
        current = result.stdout.strip()
        deployed = (
            result.exit_code == 0
            and bool(current)
            and (current.startswith(expected_sha) or expected_sha.startswith(current))
        )
        payload = json.dumps({
            "deployed": deployed,
            "current_sha": current or "unknown",
            "expected_sha": expected_sha,
            "message": "Deployed! Proceed to module update." if deployed
                       else "Not yet deployed. Wait 15 seconds then call odoosh.check_commit again.",
        })
        return CommandResult(
            command=["odoosh.check_commit", expected_sha],
            cwd="odoosh:ssh",
            exit_code=0 if deployed else 1,
            stdout=payload,
        )

    # Kept for safe_deploy_module internal use only (not exposed as MCP tool)
    def wait_for_commit(self, expected_sha: str, timeout_seconds: int | None = None, interval_seconds: int | None = None) -> CommandResult:
        timeout_seconds = timeout_seconds or settings.odoosh_wait_timeout_seconds
        interval_seconds = interval_seconds or settings.odoosh_wait_interval_seconds
        deadline = time.time() + timeout_seconds
        expected = expected_sha.strip()
        attempts: list[str] = []
        while time.time() < deadline:
            result = self.deployed_sha()
            current = result.stdout.strip()
            attempts.append(current or f"exit={result.exit_code}:{result.stderr.strip()[:120]}")
            if result.exit_code == 0 and current and (current.startswith(expected) or expected.startswith(current)):
                return CommandResult(
                    command=["odoosh.wait_for_commit", expected],
                    cwd="odoosh:ssh",
                    exit_code=0,
                    stdout=f"ODOOSH_DEPLOYED_COMMIT_OK\nexpected={expected}\ncurrent={current}\n",
                )
            time.sleep(interval_seconds)
        return CommandResult(
            command=["odoosh.wait_for_commit", expected],
            cwd="odoosh:ssh",
            exit_code=124,
            stdout="\n".join(attempts[-10:]),
            stderr=f"Odoo.sh did not deploy expected commit within {timeout_seconds}s. Expected {expected}.",
            timed_out=True,
        )

    def verify_module_manifest(self, module_name: str) -> CommandResult:
        safe_module = shlex.quote(module_name)
        user_addons = shlex.quote(settings.odoosh_user_addons_path)
        script = (
            "set -e; "
            f"test -f {user_addons}/{safe_module}/__manifest__.py; "
            f"grep -E \"['\\\"']installable['\\\"'] *: *True\" {user_addons}/{safe_module}/__manifest__.py >/dev/null; "
            f"echo MODULE_MANIFEST_OK:{module_name}"
        )
        return self.run_shell(script, timeout_seconds=30)

    def tail_log(self, log_name: OdooShLogName, tail_lines: int = 200) -> CommandResult:
        safe_lines = max(20, min(tail_lines, 2000))
        filename = f"~/logs/{log_name.value}.log"
        script = f"test -f {filename} && tail -n {safe_lines} {filename} || echo 'Log not found: {filename}'"
        return self.run_shell(script, timeout_seconds=30)

    def grep_errors(self, tail_lines: int = 500) -> CommandResult:
        """Return full recent traceback/error blocks, not just matching lines.

        Odoo errors often span many lines. Grepping only keywords hides the real
        cause, so this tails recent logs and extracts complete Python traceback
        blocks plus nearby ERROR/CRITICAL windows.
        """
        safe_lines = max(100, min(tail_lines, 2000))
        script = (
            "set +e; "
            "for f in ~/logs/*.log; do "
            "  [ -f \"$f\" ] || continue; "
            "  echo __ODOO_MCP_LOG_FILE__:$f; "
            f"  tail -n {safe_lines} \"$f\"; "
            "done; exit 0"
        )
        result = self.run_shell(script, timeout_seconds=45)
        if result.exit_code != 0:
            return result
        blocks = self._extract_error_blocks(result.stdout)
        summary = "\n\n".join(blocks[-20:]) if blocks else "No recent traceback/error blocks found in tailed Odoo.sh logs."
        return result.model_copy(update={"stdout": summary[-20000:]})

    def _extract_error_blocks(self, text: str) -> list[str]:
        lines = text.splitlines()
        blocks: list[str] = []
        current_file = "unknown"
        i = 0
        trigger = re.compile(r"(Traceback \(most recent call last\)|ERROR|CRITICAL|ParseError|ValidationError|ModuleNotFoundError|ImportError|Failed to load registry|Exception)", re.I)
        while i < len(lines):
            line = lines[i]
            if line.startswith("__ODOO_MCP_LOG_FILE__:"):
                current_file = line.split(":", 1)[1]
                i += 1
                continue
            if "Traceback (most recent call last)" in line:
                start = max(0, i - 3)
                j = i + 1
                while j < len(lines):
                    if lines[j].startswith("__ODOO_MCP_LOG_FILE__:"):
                        break
                    if j > i + 2 and re.match(r"^\d{4}-\d{2}-\d{2} ", lines[j]) and not lines[j].lstrip().startswith(('File ', 'raise ', 'return ', 'if ', 'elif ', 'else:', 'for ', 'while ', 'class ', 'def ')):
                        # stop when next timestamped log record begins after traceback body
                        break
                    j += 1
                blocks.append(f"### {current_file}\n" + "\n".join(lines[start:j]))
                i = j
                continue
            if trigger.search(line):
                start = max(0, i - 6)
                end = min(len(lines), i + 18)
                blocks.append(f"### {current_file}\n" + "\n".join(lines[start:end]))
                i = end
                continue
            i += 1
        # de-duplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for block in blocks:
            key = block[-1000:]
            if key not in seen:
                unique.append(block)
                seen.add(key)
        return unique


    def error_pack(self, tail_lines: int = 800) -> CommandResult:
        """Return traceback blocks plus lightweight root-cause hints."""
        base = self.grep_errors(tail_lines)
        text = f"{base.stdout}\n{base.stderr}"
        hints: list[str] = []
        file_matches = sorted(set(re.findall(r"File \"([^\"]+)\"", text)))
        xml_matches = sorted(set(re.findall(r"while parsing ([^:]+):(\d+)", text, flags=re.I)))
        missing_model = sorted(set(re.findall(r"(?:Object|Model|KeyError: )['\"]?([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z0-9_]+)['\"]?", text)))
        if "ModuleNotFoundError" in text or "ImportError" in text:
            hints.append("Python import failure: check __init__.py imports, renamed files, and manifest dependencies.")
        if "ParseError" in text or xml_matches:
            hints.append("XML parse/load failure: check the referenced XML file/line and make sure actions are defined before menus that reference them.")
        if "Access Denied" in text:
            hints.append("Access rights failure: use SSH module update or add missing ir.model.access.csv rows/groups.")
        if missing_model:
            hints.append("Missing registry model: module likely failed to load/upgrade; fix earlier traceback then rerun odoosh.module_update.")
        pack = {
            "summary": "Odoo.sh traceback/error pack",
            "files": file_matches[-20:],
            "xml_locations": [f"{f}:{line}" for f, line in xml_matches[-20:]],
            "models_mentioned": missing_model[-20:],
            "hints": hints or ["No strong pattern detected; inspect traceback blocks below."],
        }
        stdout = "MCP_ERROR_PACK\n" + json.dumps(pack, indent=2) + "\n\nTRACEBACK_BLOCKS\n" + base.stdout
        return base.model_copy(update={"command": ["odoosh.error_pack", str(tail_lines)], "stdout": stdout[-20000:]})

    def _dependency_guard(self, module_name: str) -> CommandResult | None:
        try:
            from .odoo_rpc_service import OdooRpcService

            deps = OdooRpcService().module_dependencies_status(module_name)
        except Exception:
            return None
        if deps.status != "ok" or not isinstance(deps.result, dict):
            return None
        missing = deps.result.get("missing") or []
        if missing:
            names = ", ".join(sorted({str(row.get("name")) for row in missing}))
            return CommandResult(
                command=["odoosh.dependency_guard", module_name],
                cwd="odoo:xmlrpc",
                exit_code=2,
                stderr=f"Missing/uninstalled dependencies for {module_name}: {names}",
            )
        return None

    def module_install_ssh(self, module_name: str, timeout_seconds: int = 600) -> CommandResult:
        """Install module via SSH using -i flag (first-time install only)."""
        dep_error = self._dependency_guard(module_name)
        if dep_error:
            return dep_error
        return self._run_module_cli(module_name, "-i", timeout_seconds)

    def module_update(self, module_name: str, timeout_seconds: int = 600) -> CommandResult:
        dep_error = self._dependency_guard(module_name)
        if dep_error:
            return dep_error
        if not settings.odoosh_db_name:
            return CommandResult(
                command=["odoosh_module_update"],
                cwd="odoosh:ssh",
                exit_code=2,
                stderr="ODOOSH_DB_NAME is not configured. Set the exact database name for the target build.",
            )
        return self._run_module_cli(module_name, "-u", timeout_seconds)

    def module_deploy_ssh(self, module_name: str, timeout_seconds: int = 600) -> CommandResult:
        """Install or upgrade a module via Odoo.sh SSH without XML-RPC admin rights.

        This is the no-blockage path used when XML-RPC update_list or module
        buttons fail with Access Denied. It first tries to read module state via
        XML-RPC (module_status usually works for non-admin users). If the module
        is installed, it uses -u. If it is uninstalled or not listed yet, it uses
        -i. If XML-RPC state cannot be read, it uses a remote odoo-bin shell
        probe and falls back to -u only when the probe is inconclusive, because
        that is the safest operation for already-installed modules.
        """
        dep_error = self._dependency_guard(module_name)
        if dep_error:
            return dep_error
        state = self._module_state_best_effort(module_name)
        flag = "-u" if state in {"installed", "to upgrade", "to remove"} else "-i"
        result = self._run_module_cli(module_name, flag, timeout_seconds)
        prefix = f"MCP_SSH_DEPLOY mode={'upgrade' if flag == '-u' else 'install'} state={state or 'unknown'} module={module_name}\n"
        return result.model_copy(update={"command": ["odoosh.module_deploy_ssh", module_name, flag], "stdout": prefix + (result.stdout or "")})

    def _module_state_best_effort(self, module_name: str) -> str | None:
        try:
            from .odoo_rpc_service import OdooRpcService

            status = OdooRpcService().module_status(module_name)
            if status.status == "ok" and status.result:
                return (status.result[0] or {}).get("state")
            if status.status == "ok" and status.result == []:
                return "not_found"
        except Exception:
            pass

        if not settings.odoosh_db_name:
            return None
        probe = self._run_module_state_probe(module_name)
        match = re.search(r"MCP_MODULE_STATE=([^\s]+)", probe.stdout or "")
        return match.group(1) if match else None

    def _run_module_state_probe(self, module_name: str) -> CommandResult:
        safe_db = shlex.quote(settings.odoosh_db_name or "")
        odoo_path = shlex.quote(settings.odoosh_odoo_path)
        safe_addons = shlex.quote(self._addons_path())
        module_literal = repr(module_name)
        script = (
            "set +e; "
            f"cd {odoo_path}; "
            f"./odoo-bin shell -d {safe_db} --addons-path={safe_addons} --no-http --log-level=error <<'PY'\n"
            f"module_name = {module_literal}\n"
            "mod = env['ir.module.module'].search([('name', '=', module_name)], limit=1)\n"
            "print('MCP_MODULE_STATE=%s' % (mod.state if mod else 'not_found'))\n"
            "PY"
        )
        return self.run_shell(script, timeout_seconds=120)

    def _addons_path(self) -> str:
        return ",".join([
            f"{settings.odoosh_odoo_path}/odoo/addons",
            f"{settings.odoosh_odoo_path}/addons",
            settings.odoosh_user_addons_path,
        ])

    def _run_module_cli(self, module_name: str, flag: str, timeout_seconds: int) -> CommandResult:
        if flag not in {"-i", "-u"}:
            raise ValueError("flag must be -i or -u")
        if not settings.odoosh_db_name:
            return CommandResult(
                command=["odoosh_module_cli", flag, module_name],
                cwd="odoosh:ssh",
                exit_code=2,
                stderr="ODOOSH_DB_NAME is not configured. Set the exact database name for the target build.",
            )
        safe_module = shlex.quote(module_name)
        safe_db = shlex.quote(settings.odoosh_db_name)
        odoo_path = shlex.quote(settings.odoosh_odoo_path)
        safe_addons = shlex.quote(self._addons_path())
        script = (
            "set -e; "
            f"cd {odoo_path}; "
            f"./odoo-bin -d {safe_db} --addons-path={safe_addons} {flag} {safe_module} --stop-after-init --log-level=info"
        )
        result = self.run_shell(script, timeout_seconds=timeout_seconds)
        return self._classify_module_update_result(result)

    def _classify_module_update_result(self, result: CommandResult) -> CommandResult:
        text = f"{result.stdout}\n{result.stderr}"
        matched = [pattern for pattern in self.FAILURE_PATTERNS if pattern in text]
        if result.exit_code == 0 and matched:
            return result.model_copy(update={
                "exit_code": 3,
                "stderr": (result.stderr or "") + "\nMCP_CLASSIFIED_FAILURE: " + ", ".join(matched),
            })
        return result

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
