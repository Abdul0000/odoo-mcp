"""Phase 1 — Lazy on-demand sync of Odoo.sh core source into local cache paths.

Problem this solves:
  core_finder_service.py and syntax_verifier_service.py both read
  /home/odoo/src/{user,enterprise,odoo} via plain local Path() calls. On a
  Render deployment that tree does not exist locally — it only exists on the
  Odoo.sh build server, reachable over the SSH key already configured for
  OdooShService (ODOOSH_SSH_HOST / ODOOSH_SSH_KEY_PATH).

Fix:
  Before either service reports "not found" for a module directory, it calls
  ensure_module_synced() here. On first miss this rsyncs just that module's
  directory from the Odoo.sh build host into the *same local path* the rest
  of the code already expects — so no other file needs to know sync exists.
  Subsequent reads for the same module are local-disk fast until the cached
  copy goes stale (tracked via the live Odoo.sh deployed sha, falling back to
  a simple TTL if the sha can't be read).

Safety:
  - Fully best-effort. Any failure (no SSH host configured, rsync missing,
    network hiccup, timeout) just returns False / leaves the directory as it
    was — callers fall through to their existing "not found" path exactly as
    before this feature existed. Nothing here can break the current flow.
  - Disabled entirely via CORE_SYNC_ENABLED=false.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from threading import Lock

from ..config import settings

_registry_lock = Lock()
_module_locks: dict[str, Lock] = {}
_sha_cache: dict[str, tuple[str | None, float]] = {}
_SHA_CACHE_TTL = 30.0  # don't hit odoosh.deployed_sha more than once per 30s


def _module_lock(remote_root: str, module_name: str) -> Lock:
    """Per-(root, module) lock — concurrent syncs of *different* modules must
    not block behind each other. Only concurrent requests for the *same*
    module serialize, which is the actual race we need to prevent."""
    key = f"{remote_root}::{module_name}"
    with _registry_lock:
        lock = _module_locks.get(key)
        if lock is None:
            lock = Lock()
            _module_locks[key] = lock
        return lock


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", value)


def _deployed_sha() -> str | None:
    """Best-effort fetch of the currently deployed commit sha on Odoo.sh.

    Cached briefly so a burst of ensure_module_synced() calls in one
    scope_advice/verify_against_core pass doesn't open a new SSH connection
    per module.
    """
    host = settings.odoosh_ssh_host or ""
    now = time.time()
    cached = _sha_cache.get(host)
    if cached and (now - cached[1]) < _SHA_CACHE_TTL:
        return cached[0]
    sha: str | None = None
    try:
        from .odoosh_service import OdooShService

        result = OdooShService().deployed_sha()
        if result.exit_code == 0 and result.stdout:
            lines = [ln.strip() for ln in result.stdout.strip().splitlines() if ln.strip()]
            sha = lines[-1] if lines else None
    except Exception:  # noqa: BLE001
        sha = None
    _sha_cache[host] = (sha, now)
    return sha


def ensure_module_synced(remote_root: str, module_name: str, timeout_seconds: int = 45) -> bool:
    """Best-effort: make sure {remote_root}/{module_name} exists and is fresh locally.

    remote_root must be one of the existing hardcoded roots
    (/home/odoo/src/user|enterprise|odoo) — the local cache path is the
    identical string, so no other code needs to change.

    Returns True if the local directory exists by the time this returns
    (whether already-fresh, freshly synced, or pre-existing). Never raises.
    """
    if not settings.core_sync_enabled:
        return False
    if not settings.odoosh_ssh_host:
        return False
    if not module_name or "/" in module_name or module_name.startswith("."):
        return False

    local_dir = Path(remote_root) / module_name
    marker = local_dir / ".synced_sha"
    sha = _deployed_sha()

    with _module_lock(remote_root, module_name):
        if local_dir.exists() and local_dir.is_dir():
            has_content = any(local_dir.iterdir())
            if sha and has_content:
                try:
                    cached_sha = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
                except OSError:
                    cached_sha = None
                if cached_sha == sha:
                    return True
            elif not sha and has_content:
                try:
                    age = time.time() - local_dir.stat().st_mtime
                except OSError:
                    age = 0.0
                if age < settings.core_sync_max_age_seconds:
                    return True
            # Empty dir (e.g. a prior sync attempt found nothing) always
            # falls through to retry below, rather than being cached as
            # "fresh" — avoids a transient failure looking permanent.

        try:
            local_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return local_dir.exists()

        ssh_opts = [
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
            "-p", str(settings.odoosh_ssh_port),
        ]
        if settings.odoosh_ssh_key_path:
            ssh_opts += ["-i", settings.odoosh_ssh_key_path]
        remote = f"{settings.odoosh_ssh_user}@{settings.odoosh_ssh_host}:{remote_root}/{module_name}/"
        cmd = [
            "rsync", "-az", "--delete", "--timeout", str(timeout_seconds),
            "-e", "ssh " + " ".join(ssh_opts),
            remote, str(local_dir) + "/",
        ]
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_seconds + 5,
            )
        except (subprocess.TimeoutExpired, OSError):
            return local_dir.exists() and any(local_dir.iterdir()) if local_dir.exists() else False

        if completed.returncode != 0:
            # Sync failed (module may genuinely not exist on that root, or
            # transient network issue) — leave whatever was there before and
            # let the caller's normal not-found path handle it.
            return local_dir.exists() and any(local_dir.iterdir()) if local_dir.exists() else False

        if sha:
            try:
                marker.write_text(sha, encoding="utf-8")
            except OSError:
                pass
        return True
