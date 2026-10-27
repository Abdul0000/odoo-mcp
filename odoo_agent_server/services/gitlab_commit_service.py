"""Stateless GitLab access via the REST API (gitlab.com and self-managed).

Drop-in sibling of ``GitHubRepo``: the workspace engine only sees the duck-typed interface
(head / tree / blob_text / commit_to_branch / ref_exists / ...), so ws_* tools behave identically
on a GitLab client. No clone, no git binary, no local disk.

GitLab has no Git Data API (you cannot create a dangling tree/commit and move a ref afterwards).
Instead one ``POST /repository/commits`` creates the commit *and* advances the branch, so the
engine exposes ``commit_to_branch`` and keeps a compare-and-set guard around it:

* the branch tip is re-read right before the commit and must equal the expected parent, else
  ``NonFastForward`` (the caller then replays its edits on the newer head, exactly as on GitHub);
* the created commit's parent is checked afterwards, so a push that raced inside that few-ms
  window is reported (``GitLabRaceLanded``) instead of being silently accepted.

"tree sha" in this engine is simply the commit sha: GitLab addresses trees by ref, and a commit
sha is an immutable ref, so ``tree(head["tree"])`` lists exactly that commit's files.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from .github_commit_service import (
    _POOL,
    FileChange,
    GitHubError,
    GitHubRepo,
    NonFastForward,
    Transport,
    _decode,
    git_blob_sha,
)

DEFAULT_HOST = "gitlab.com"
_LOCAL = threading.local()


class GitLabError(GitHubError):
    """Subclasses GitHubError so every existing ``except GitHubError`` handler also covers GitLab."""

    provider = "gitlab"


class GitLabRaceLanded(GitLabError):
    """Our commit landed, but on top of a newer branch tip than the one we validated."""

    def __init__(self, commit: str, expected_parent: str, actual_parent: str, path: str = "") -> None:
        super().__init__(409, f"commit {commit[:12]} landed on {actual_parent[:12]} instead of {expected_parent[:12]}: the branch moved during the push; run ws_open fresh=true and re-check the diff", path=path)
        self.commit = commit
        self.expected_parent = expected_parent
        self.actual_parent = actual_parent
        self.hint = "the commit already exists on the branch (open urls.commit / compare with ws_open fresh=true); do NOT deploy until you checked nobody else's change to the same files was overwritten"
        self.extra = {"landed_commit": commit, "expected_parent": expected_parent, "actual_parent": actual_parent}


# --------------------------------------------------------------------------- URL handling
_SCP_RE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?P<path>[^\s]+)$")


def parse_gitlab_repo(url: str, *, api_url: str | None = None) -> tuple[str, str]:
    """Return ``(api_base, project_path)`` for an https/ssh/scp-style GitLab remote.

    ``project_path`` keeps subgroups (``group/sub/project``). Credentials in the URL are dropped.
    ``api_base`` is ``https://<host>/api/v4`` unless ``api_url`` overrides it (self-managed under a sub-path).
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("empty repository URL")
    host, path, scheme = "", "", "https"
    if "://" in raw:
        parts = urllib.parse.urlsplit(raw)
        host = parts.hostname or ""
        scheme = parts.scheme if parts.scheme in ("http", "https") else "https"
        path = parts.path
        if parts.port and parts.scheme in ("http", "https"):
            host = f"{host}:{parts.port}"
    else:
        m = _SCP_RE.match(raw)
        if not m:
            raise ValueError("not a GitLab repository URL")
        host, path = m.group("host"), m.group("path")
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or path.count("/") < 1:
        raise ValueError("GitLab repository URL needs namespace/project (e.g. https://gitlab.com/group/project.git)")
    base = (api_url or f"{scheme}://{host}/api/v4").rstrip("/")
    return base, path


# --------------------------------------------------------------------------- transport
def _ssl_context() -> ssl.SSLContext | None:
    bundle = os.getenv("GITLAB_CA_BUNDLE", "").strip()
    if os.getenv("GITLAB_SSL_VERIFY", "true").strip().lower() in {"0", "false", "no"}:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context(cafile=bundle) if bundle else None


def default_gitlab_transport(method: str, url: str, headers: dict[str, str], body: bytes | None):
    """HTTPS/HTTP with a per-thread keep-alive connection per host (any GitLab host, not one fixed domain)."""
    parts = urllib.parse.urlsplit(url)
    scheme, netloc = parts.scheme, parts.netloc
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    conns: dict[tuple[str, str], http.client.HTTPConnection] = getattr(_LOCAL, "conns", None) or {}
    _LOCAL.conns = conns
    key = (scheme, netloc)
    last_error = "unknown"
    for attempt in (0, 1):
        conn = conns.get(key)
        if conn is None:
            if scheme == "http":
                conn = http.client.HTTPConnection(netloc, timeout=25)
            else:
                conn = http.client.HTTPSConnection(netloc, timeout=25, context=_ssl_context())
            conns[key] = conn
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            if resp_headers.get("connection", "").lower() == "close":
                conn.close()
                conns.pop(key, None)
            return resp.status, resp_headers, _decode(raw)
        except (http.client.HTTPException, OSError) as exc:  # stale keep-alive socket, reset, timeout
            last_error = str(exc)
            try:
                conn.close()
            finally:
                conns.pop(key, None)
            if attempt == 0 and not isinstance(exc, TimeoutError):
                continue
    return 599, {}, {"message": f"network error: {last_error}"}


def _enc(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _message(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("message", "error", "error_description"):
            val = data.get(key)
            if val:
                return val if isinstance(val, str) else json.dumps(val)[:300]
        return ""
    return str(data or "")


# --------------------------------------------------------------------------- the repo
@dataclass
class GitLabRepo:
    api_base: str            # e.g. https://gitlab.com/api/v4
    project: str             # "group/sub/project" (numeric id also works)
    token: str
    transport: Transport = default_gitlab_transport
    sleep: Callable[[float], None] = time.sleep
    max_attempts: int = 5
    core_token: str | None = None   # GitHub token used for the odoo/odoo core mirror (GitLab tokens do not work there)
    label: str = "GitLab"
    provider: str = "gitlab"
    calls: int = field(default=0, init=False)
    known_blobs: set = field(default_factory=set, init=False, repr=False)
    _default_branch: str | None = field(default=None, init=False, repr=False)
    _trees: dict = field(default_factory=dict, init=False, repr=False)  # ref -> {path: entry}, tiny LRU

    # kept for symbol_service/log parity with GitHubRepo
    @property
    def owner(self) -> str:
        return self.project.rsplit("/", 1)[0] if "/" in self.project else ""

    @property
    def repo(self) -> str:
        return self.project.rsplit("/", 1)[-1]

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.api_base).netloc

    # ------------------------------------------------------------ low level
    def _headers(self, with_body: bool) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json", "User-Agent": "odoo-mcp-agent"}
        if with_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self.api_base}/projects/{_enc(self.project)}{path}"

    def request_full(self, method: str, path: str, payload: Any = None, *, ok: tuple[int, ...] = (200, 201), retry_server_errors: bool = True) -> tuple[Any, dict[str, str]]:
        url = self._url(path)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = self._headers(body is not None)
        last: GitLabError | None = None
        for attempt in range(self.max_attempts):
            self.calls += 1
            status, resp_headers, data = self.transport(method, url, headers, body)
            if status in ok:
                return data, resp_headers
            message = _message(data)
            retry_after = resp_headers.get("retry-after") or resp_headers.get("ratelimit-reset")
            limited = status == 429 or (status == 403 and ("rate limit" in message.lower() or "retry later" in message.lower()))
            ambiguous = status >= 500 or status == 599  # the server may have applied the request before failing
            if ambiguous and not retry_server_errors:
                raise GitLabError(status, message, retryable=True, path=path)
            if ambiguous or limited:
                delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() and float(retry_after) < 120 else min(0.5 * (2 ** attempt), 8.0)
                last = GitLabError(status, message, retryable=True, path=path)
                self.sleep(min(delay, 30.0))
                continue
            hint = ""
            if status == 401:
                hint = " (token invalid or expired)"
            elif status == 403:
                hint = " (token needs api scope and Developer+ role; protected branches need Maintainer)"
            elif status == 404:
                hint = " (project/branch not found, or token cannot see this private project)"
            raise GitLabError(status, message + hint, path=path)
        assert last is not None
        raise last

    def request(self, method: str, path: str, payload: Any = None, *, ok: tuple[int, ...] = (200, 201), retry_server_errors: bool = True) -> Any:
        return self.request_full(method, path, payload, ok=ok, retry_server_errors=retry_server_errors)[0]

    def web_url(self, kind: str = "", ref: str = "") -> str:
        base = f"{self.api_base.split('/api/')[0]}/{self.project}"
        return f"{base}/-/{kind}/{ref}" if kind else base

    def default_branch(self) -> str | None:
        if self._default_branch is None:
            try:
                self._default_branch = (self.request("GET", "") or {}).get("default_branch") or ""
            except GitHubError:
                self._default_branch = ""
        return self._default_branch or None

    def core_repo(self) -> GitHubRepo:
        """odoo/odoo core source lives on GitHub regardless of where the client repo is hosted."""
        return GitHubRepo("odoo", "odoo", self.core_token or "", sleep=self.sleep)

    # ------------------------------------------------------------ refs & trees
    def head(self, branch: str) -> dict[str, str]:
        commit = self.request("GET", f"/repository/commits/{_enc(branch)}")
        sha = commit["id"]
        return {"commit": sha, "tree": sha, "message": commit.get("message", "")}

    def _branch_tip(self, branch: str) -> str | None:
        try:
            data = self.request("GET", f"/repository/branches/{_enc(branch)}")
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        return ((data or {}).get("commit") or {}).get("id")

    def ref_exists(self, branch: str) -> bool:
        return self._branch_tip(branch) is not None

    def _list_tree(self, ref: str, *, recursive: bool, path: str = "") -> list[dict[str, Any]]:
        query = {"ref": ref, "per_page": "100", "recursive": "true" if recursive else "false"}
        if path:
            query["path"] = path
        base = f"/repository/tree?{urllib.parse.urlencode(query)}"
        first, hdrs = self.request_full("GET", base)
        rows: list[dict[str, Any]] = list(first or [])
        total = hdrs.get("x-total-pages", "")
        if total.isdigit() and int(total) > 1:  # known page count: fetch the rest concurrently
            futures = [_POOL.submit(self.request, "GET", f"{base}&page={n}") for n in range(2, int(total) + 1)]
            for fut in futures:
                rows.extend(fut.result() or [])
            return rows
        while True:  # unknown count (large trees omit it): follow keyset Link or X-Next-Page
            link = hdrs.get("link", "")
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            nxt = m.group(1) if m else None
            if nxt is None and hdrs.get("x-next-page", "").isdigit():
                nxt = f"{base}&page={hdrs['x-next-page']}"
            if nxt is None:
                return rows
            page, hdrs = self.request_full("GET", nxt)
            rows.extend(page or [])

    def tree(self, tree_sha: str) -> dict[str, dict[str, str]]:
        """{path: {sha, mode, size}} for every blob at ``tree_sha`` (a commit sha). ``size`` is "0": GitLab's tree listing omits it."""
        cached = self._trees.get(tree_sha)
        if cached is not None:
            return {p: dict(v) for p, v in cached.items()}
        out: dict[str, dict[str, str]] = {}
        for item in self._list_tree(tree_sha, recursive=True):
            if item.get("type") == "blob":
                out[item["path"]] = {"sha": item["id"], "mode": item.get("mode") or "100644", "size": "0"}
        self.known_blobs.update(v["sha"] for v in out.values())
        self._remember_tree(tree_sha, out)
        return {p: dict(v) for p, v in out.items()}

    def _remember_tree(self, ref: str, files: dict[str, dict[str, str]]) -> None:
        if len(self._trees) >= 4:
            self._trees.pop(next(iter(self._trees)))
        self._trees[ref] = {p: dict(v) for p, v in files.items()}

    def tree_at(self, tree_sha: str, path: str) -> dict[str, dict[str, str]] | None:
        rows = self._list_tree(tree_sha, recursive=False, path=path.strip("/"))
        if not rows and path.strip("/"):
            return None
        return {r["name"]: {"sha": r["id"], "type": "tree" if r["type"] == "tree" else "blob", "size": "0"} for r in rows}

    def root_blob(self, tree_sha: str, name: str) -> str | None:
        for item in self._list_tree(tree_sha, recursive=False):
            if item["name"] == name and item["type"] == "blob":
                return item["id"]
        return None

    def blob_text(self, sha: str) -> str:
        data = self.request("GET", f"/repository/blobs/{sha}")
        raw = base64.b64decode(data["content"]) if data.get("encoding") == "base64" else data["content"].encode()
        return raw.decode("utf-8", "replace")

    def blobs_text(self, shas: dict[str, str], workers: int = 8) -> dict[str, str]:
        if not shas:
            return {}
        if len(shas) == 1:
            path, sha = next(iter(shas.items()))
            return {path: self.blob_text(sha)}
        futures = {path: _POOL.submit(self.blob_text, sha) for path, sha in shas.items()}
        return {path: fut.result() for path, fut in futures.items()}

    def recent_commit_messages(self, branch: str, count: int = 8) -> list[dict[str, str]]:
        rows = self.request("GET", f"/repository/commits?ref_name={_enc(branch)}&per_page={count}")
        return [{"sha": r["id"], "message": r.get("message") or r.get("title", "")} for r in rows or []]

    # ------------------------------------------------------------ writing
    def _plan(self, base_files: dict[str, dict[str, str]], parent_files: dict[str, dict[str, str]], changes: list[FileChange]) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
        """Actions that turn ``parent`` into ``base_tree + changes`` (GitHub's tree semantics), plus the resulting file map."""
        desired = {p: dict(v) for p, v in base_files.items()}
        contents: dict[str, str] = {}
        for ch in changes:
            if ch.content is None:
                desired.pop(ch.path, None)
            else:
                desired[ch.path] = {"sha": git_blob_sha(ch.content), "mode": ch.mode, "size": "0"}
                contents[ch.path] = ch.content
        actions: list[dict[str, Any]] = []
        for path, entry in desired.items():
            current = parent_files.get(path)
            if current is not None and current["sha"] == entry["sha"]:
                continue  # already identical on the parent (also avoids empty-update errors)
            text = contents[path] if path in contents else self.blob_text(entry["sha"])  # reverting a path to its base content
            actions.append({"action": "update" if current is not None else "create", "file_path": path, "content": base64.b64encode(text.encode("utf-8")).decode("ascii"), "encoding": "base64"})
        for path in parent_files:
            if path not in desired:
                actions.append({"action": "delete", "file_path": path})
        return actions, desired

    def _landed(self, branch: str, parent: str, message: str) -> dict[str, Any] | None:
        """After an ambiguous failure: is the tip our own commit (same message, parented on ``parent``)?"""
        try:
            tip = self.request("GET", f"/repository/commits/{_enc(branch)}")
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        same = (tip.get("message") or "").strip() == message.strip()
        return tip if same and (tip.get("parent_ids") or [None])[0] == parent else None

    def commit_to_branch(self, branch: str, *, parent: str, base_tree: str, changes: list[FileChange], message: str, create: bool = False) -> dict[str, str]:
        """Commit ``base_tree + changes`` on top of ``parent`` and move ``branch`` to it (never force).

        Same contract as ``GitHubRepo.commit_to_branch``: raises ``NonFastForward`` when ``branch`` is no longer at ``parent``.
        The commit POST is the one non-idempotent call, so a 5xx/timeout is never blindly replayed: we first check
        whether our commit already landed.
        """
        tip = self._branch_tip(branch)
        if tip is not None and tip != parent:
            raise NonFastForward(409, f"branch '{branch}' moved ({tip[:12]} != {parent[:12]})", path=branch)
        base_ref = base_tree or parent
        stale_retried = False
        for attempt in range(4):
            base_files = self.tree(base_ref)
            parent_files = base_files if base_ref == parent else self.tree(parent)
            actions, desired = self._plan(base_files, parent_files, changes)
            if not actions:
                return {"commit": parent, "tree": parent}
            body: dict[str, Any] = {"branch": branch, "commit_message": message, "actions": actions}
            if tip is None:
                body["start_sha"] = parent  # new branch cut from the expected parent
            try:
                commit = self.request("POST", "/repository/commits", body, retry_server_errors=False)
            except GitLabError as exc:
                if exc.status >= 500:  # includes 599 network errors: outcome unknown
                    landed = self._landed(branch, parent, message)
                    if landed is None:
                        now = self._branch_tip(branch)
                        if now is not None and now != parent:
                            raise NonFastForward(409, f"branch '{branch}' moved while a commit was in doubt", path=branch) from exc
                        self.sleep(min(0.5 * (2 ** attempt), 4.0))
                        continue
                    commit = landed
                elif exc.status in (400, 409, 422):
                    now = self._branch_tip(branch)
                    if now is not None and now != parent:
                        raise NonFastForward(409, f"branch '{branch}' moved during commit ({exc.message[:120]})", path=branch) from exc
                    text = exc.message.lower()
                    if not stale_retried and ("already exists" in text or "doesn't exist" in text or "does not exist" in text):
                        stale_retried = True
                        self._trees.pop(parent, None)  # our view of the parent was stale: rebuild the plan once
                        self._trees.pop(base_ref, None)
                        continue
                    raise
                else:
                    raise
            sha = commit["id"]
            actual_parent = (commit.get("parent_ids") or [parent])[0]
            if tip is not None and actual_parent != parent:
                raise GitLabRaceLanded(sha, parent, actual_parent, path=branch)
            self._remember_tree(sha, desired)
            return {"commit": sha, "tree": sha, "confirmed": True}  # the API created the commit on the branch and returned its id
        raise GitLabError(503, "commit could not be confirmed after several attempts", retryable=True, path=branch)

    def move_ref(self, branch: str, sha: str, *, create: bool = False) -> None:
        """Only branch *creation* at an existing commit is expressible on GitLab (commits advance branches themselves)."""
        if create:
            self.request("POST", "/repository/branches", {"branch": branch, "ref": sha})
            return
        if self._branch_tip(branch) == sha:
            return
        raise GitLabError(422, "GitLab cannot move an existing branch to an arbitrary commit; use commit_to_branch", path=branch)

    def delete_branch(self, branch: str) -> None:
        self.request("DELETE", f"/repository/branches/{_enc(branch)}", ok=(200, 204, 404))
