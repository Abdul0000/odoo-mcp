"""Glue between the framework-free workspace engine and the connector's client config."""
from __future__ import annotations

import hashlib
import re
from typing import Any, Callable

from fastapi import HTTPException

from ..config import settings
from .client_resolver_service import ClientResolverService
from .github_commit_service import GitHubError
from .repo_provider_service import build_repo, detect_provider, parse_hosts
from .workspace_service import ClientTarget, WorkspaceError, WorkspaceService


def _resolve(name: str | None) -> ClientTarget:
    client = ClientResolverService().resolve_client(name or None, require_complete=True)
    if client is None:
        raise WorkspaceError("client_missing", "no client configured in ODOO_CLIENTS")
    hosts = parse_hosts(settings.gitlab_hosts_raw)
    try:
        kind = detect_provider(client.repo, client.provider, hosts)
    except ValueError as exc:
        raise WorkspaceError("repo_provider_unknown", str(exc), repo=client.public.get("repo")) from exc
    token = client.token or (settings.gitlab_token if kind == "gitlab" else settings.github_token)
    if not token:
        hint = "GITLAB_TOKEN with api scope (Developer+ role)" if kind == "gitlab" else "GITHUB_TOKEN with contents:write"
        raise WorkspaceError(f"{kind}_token_missing", f"client '{client.name}' has no {kind.title()} token (set 'token' in ODOO_CLIENTS or {hint})")
    try:
        repo = _repo(client.repo, token, kind, client.api_url, hosts)
    except ValueError as exc:
        raise WorkspaceError("repo_url_invalid", str(exc), repo=client.public.get("repo")) from exc
    return ClientTarget(client.name, repo, client.branch)


_REPOS: dict[tuple[str, str, str, str], Any] = {}


def _repo(url: str, token: str, kind: str, api_url: str | None, hosts: frozenset[str]) -> Any:
    """One engine per repo+token so its known-blob/tree caches survive across tool calls."""
    clean = re.sub(r"//[^@/]+@", "//", (url or "").strip())
    key = (kind, clean, hashlib.sha256(token.encode()).hexdigest()[:16], api_url or "")
    found = _REPOS.get(key)
    if found is None:
        if len(_REPOS) > 32:
            _REPOS.clear()
        found = _REPOS[key] = build_repo(url, token, provider=kind, api_url=api_url, gitlab_hosts=hosts, github_token=settings.github_token)
    return found


def workspace_service() -> WorkspaceService:
    mode = "sync" if str(settings.ws_checkpoint_mode).strip().lower() == "sync" else "async"
    return WorkspaceService(_resolve, protected=settings.ws_protected_branches_raw, autocheckpoint=settings.ws_autocheckpoint, checkpoint_mode=mode, wip_files=settings.ws_wip_files)


def run_ws(fn: Callable[[WorkspaceService], dict[str, Any]]) -> dict[str, Any]:
    """Run a workspace operation and convert failures into structured, model-readable errors."""
    try:
        return fn(workspace_service())
    except WorkspaceError as exc:
        return exc.as_dict()
    except HTTPException as exc:  # client resolution/ambiguity
        detail = exc.detail if isinstance(exc.detail, (dict, str)) else str(exc.detail)
        return {"status": "error", "error_code": "client_resolution", "error": detail if isinstance(detail, str) else detail.get("message", "client could not be resolved"), "detail": detail if isinstance(detail, dict) else None, "hint": "pass client_name exactly as listed by client_check"}
    except GitHubError as exc:
        return {"status": "error", "error_code": f"{exc.provider}_{exc.status}", "error": exc.message, "retryable": exc.retryable, "path": exc.path, **getattr(exc, "extra", {}),
                **({"needs_user": True, "fallback": {"tool": "workflow_odoo_code_change", "why": "credentials are rejected for the stateless lane; ask the user to fix the token, or use the checkout lane if it has working access"}} if exc.status in (401, 403) and not exc.retryable else {}),
                "hint": getattr(exc, "hint", None) or ("retry the same call" if exc.retryable else "fix token/permissions/branch, then retry")}
