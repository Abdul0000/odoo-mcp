"""Glue between the framework-free workspace engine and the connector's client config."""
from __future__ import annotations

import hashlib
from typing import Any, Callable

from fastapi import HTTPException

from ..config import settings
from .client_resolver_service import ClientResolverService
from .github_commit_service import GitHubError, GitHubRepo, parse_repo
from .workspace_service import ClientTarget, WorkspaceError, WorkspaceService


def _resolve(name: str | None) -> ClientTarget:
    client = ClientResolverService().resolve_client(name or None, require_complete=True)
    if client is None:
        raise WorkspaceError("client_missing", "no client configured in ODOO_CLIENTS")
    token = client.token or settings.github_token
    if not token:
        raise WorkspaceError("github_token_missing", f"client '{client.name}' has no GitHub token (set 'token' in ODOO_CLIENTS or GITHUB_TOKEN with contents:write)")
    try:
        owner, repo = parse_repo(client.repo)
    except ValueError as exc:
        raise WorkspaceError("repo_not_github", str(exc), repo=client.public.get("repo")) from exc
    return ClientTarget(client.name, _repo(owner, repo, token), client.branch)


_REPOS: dict[tuple[str, str, str], GitHubRepo] = {}


def _repo(owner: str, repo: str, token: str) -> GitHubRepo:
    """One GitHubRepo per repo+token so its known-blob cache survives across tool calls."""
    key = (owner, repo, hashlib.sha256(token.encode()).hexdigest()[:16])
    found = _REPOS.get(key)
    if found is None:
        if len(_REPOS) > 32:
            _REPOS.clear()
        found = _REPOS[key] = GitHubRepo(owner, repo, token)
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
        return {"status": "error", "error_code": f"github_{exc.status}", "error": exc.message, "retryable": exc.retryable, "path": exc.path,
                "hint": "retry the same call" if exc.retryable else "fix token/permissions/branch, then retry"}
