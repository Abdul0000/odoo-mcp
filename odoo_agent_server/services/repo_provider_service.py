"""Pick the Git hosting engine (GitHub or GitLab) for a client repo URL.

Framework-free on purpose (no FastAPI/settings imports) so it can be unit-tested and reused.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

from .github_commit_service import GitHubRepo, parse_repo
from .gitlab_commit_service import DEFAULT_HOST, GitLabRepo, parse_gitlab_repo

PROVIDERS = ("github", "gitlab")
_SCP_HOST = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s]+):")


def repo_host(url: str) -> str:
    raw = (url or "").strip()
    if "://" in raw:
        return (urllib.parse.urlsplit(raw).hostname or "").lower()
    m = _SCP_HOST.match(raw)
    return m.group("host").lower() if m else ""


def parse_hosts(raw: str | None) -> frozenset[str]:
    return frozenset(h.strip().lower() for h in (raw or "").split(",") if h.strip())


def detect_provider(url: str, explicit: str | None = None, gitlab_hosts: frozenset[str] = frozenset()) -> str:
    """``explicit`` ("github"/"gitlab" from the client's ``provider`` key) always wins; otherwise infer from the host."""
    choice = (explicit or "").strip().lower()
    if choice:
        if choice not in PROVIDERS:
            raise ValueError(f"unknown provider '{explicit}' (use 'github' or 'gitlab')")
        return choice
    host = repo_host(url)
    if host in ("github.com", "www.github.com"):
        return "github"
    if host == DEFAULT_HOST or host in gitlab_hosts or host.startswith("gitlab.") or ".gitlab." in host:
        return "gitlab"
    raise ValueError(f"cannot tell which Git host '{host or url}' is: set \"provider\": \"gitlab\" (or \"github\") on this client in ODOO_CLIENTS, or add the host to GITLAB_HOSTS")


def build_repo(
    url: str,
    token: str,
    *,
    provider: str | None = None,
    api_url: str | None = None,
    gitlab_hosts: frozenset[str] = frozenset(),
    github_token: str | None = None,
    **engine_kwargs: Any,
) -> GitHubRepo | GitLabRepo:
    """Build the engine for ``url``. ``github_token`` only matters for GitLab clients (odoo/odoo core lookups)."""
    kind = detect_provider(url, provider, gitlab_hosts)
    if kind == "gitlab":
        base, project = parse_gitlab_repo(url, api_url=api_url)
        return GitLabRepo(base, project, token, core_token=github_token, **engine_kwargs)
    owner, repo = parse_repo(url)
    return GitHubRepo(owner, repo, token, **engine_kwargs)
