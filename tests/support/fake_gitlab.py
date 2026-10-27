"""In-memory GitLab REST API (the subset GitLabRepo uses) for offline tests.

Mirrors the behaviours that matter: one POST /repository/commits creates the commit and advances the branch
(on the *current* tip, like GitLab does when no start_sha applies), create/update/delete validation errors,
paginated trees, URL-encoded project ids and branch names.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse

from tests.support.fake_github import blob_sha

PROJECT = "group/sub/proj"


class FakeGitLab:
    def __init__(self, files: dict[str, str], branch: str = "staging", *, page_size: int = 100, pagination: str = "total"):
        self.blobs: dict[str, str] = {}
        self.commits: dict[str, dict] = {}
        self.refs: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.headers_seen: list[dict[str, str]] = []
        self.fail_next: list[tuple[int, dict, dict]] = []
        self.default_branch = "main"
        self.fail_commit: list[tuple[int, dict, dict]] = []  # injected before a commit is applied
        self.lose_response_once = False  # apply the commit, then answer with a network error
        self.before_commit = None  # hook(fake) fired once, after the tip check and before the commit is applied
        self.page_size = page_size
        self.pagination = pagination  # "total" (x-total-pages) | "link" (keyset Link header) | "next" (x-next-page)
        flat = {p: ("100644", self._blob(t)) for p, t in files.items()}
        self.refs[branch] = self._commit(flat, [], "initial")

    # ---- object helpers
    def _blob(self, text: str) -> str:
        sha = blob_sha(text)
        self.blobs[sha] = text
        return sha

    def _commit(self, flat, parents: list[str], message: str) -> str:
        sha = hashlib.sha1(json.dumps([sorted(flat.items()), parents, message, len(self.commits)]).encode()).hexdigest()
        self.commits[sha] = {"files": dict(flat), "parents": parents, "message": message}
        return sha

    def head_files(self, branch: str) -> dict[str, str]:
        return {p: self.blobs[s] for p, (m, s) in self.commits[self.refs[branch]]["files"].items()}

    def push_external(self, branch: str, changes: dict[str, str | None], message="upstream change") -> str:
        parent = self.refs[branch]
        flat = dict(self.commits[parent]["files"])
        for p, t in changes.items():
            if t is None:
                flat.pop(p, None)
            else:
                flat[p] = ("100644", self._blob(t))
        self.refs[branch] = self._commit(flat, [parent], message)
        return self.refs[branch]

    def _resolve(self, ref: str) -> str | None:
        return self.refs.get(ref) or (ref if ref in self.commits else None)

    def _entries(self, ref: str, recursive: bool, path: str) -> list[dict]:
        flat = self.commits[self._resolve(ref)]["files"]
        prefix = f"{path.strip('/')}/" if path.strip("/") else ""
        out: list[dict] = []
        dirs: set[str] = set()
        for p, (mode, sha) in sorted(flat.items()):
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            if recursive or "/" not in rest:
                out.append({"id": sha, "name": rest.rsplit("/", 1)[-1], "type": "blob", "path": p, "mode": mode})
            else:
                dirs.add(rest.split("/", 1)[0])
        for d in sorted(dirs):
            out.append({"id": hashlib.sha1(f"{ref}:{prefix}{d}".encode()).hexdigest(), "name": d, "type": "tree", "path": f"{prefix}{d}", "mode": "040000"})
        return out

    # ---- transport
    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        self.headers_seen.append(dict(headers))
        if self.fail_next:
            return self.fail_next.pop(0)
        parts = urllib.parse.urlsplit(url)
        marker = "/projects/" + urllib.parse.quote(PROJECT, safe="")
        if not parts.path.startswith("/api/v4" + marker):
            return 404, {}, {"message": f"404 Project Not Found ({parts.path})"}
        path = urllib.parse.unquote(parts.path[len("/api/v4" + marker):]) or ""
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        payload = json.loads(body) if body else None
        if method == "GET" and path == "":
            return 200, {}, {"default_branch": self.default_branch}
        m = re.fullmatch(r"/repository/commits/(.+)", path)
        if method == "GET" and m:
            sha = self._resolve(m.group(1))
            if sha is None:
                return 404, {}, {"message": "404 Commit Not Found"}
            c = self.commits[sha]
            return 200, {}, {"id": sha, "message": c["message"], "parent_ids": c["parents"]}
        m = re.fullmatch(r"/repository/branches/(.+)", path)
        if method == "GET" and m:
            tip = self.refs.get(m.group(1))
            return (200, {}, {"name": m.group(1), "commit": {"id": tip}}) if tip else (404, {}, {"message": "404 Branch Not Found"})
        if method == "DELETE" and m:
            self.refs.pop(m.group(1), None)
            return 204, {}, None
        if method == "POST" and path == "/repository/branches":
            if payload["branch"] in self.refs:
                return 400, {}, {"message": "Branch already exists"}
            self.refs[payload["branch"]] = self._resolve(payload["ref"])
            return 201, {}, {"name": payload["branch"]}
        if method == "GET" and path == "/repository/tree":
            if self._resolve(query["ref"]) is None:
                return 404, {}, {"message": "404 Tree Not Found"}
            rows = self._entries(query["ref"], query.get("recursive") == "true", query.get("path", ""))
            return self._page(rows, query, parts)
        m = re.fullmatch(r"/repository/blobs/([0-9a-f]+)", path)
        if method == "GET" and m:
            if m.group(1) not in self.blobs:
                return 404, {}, {"message": "404 Blob Not Found"}
            text = self.blobs[m.group(1)]
            return 200, {}, {"sha": m.group(1), "size": len(text.encode()), "encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        if method == "GET" and path == "/repository/commits":
            out, cur = [], self.refs.get(query["ref_name"])
            while cur and len(out) < int(query.get("per_page", 20)):
                out.append({"id": cur, "message": self.commits[cur]["message"], "title": self.commits[cur]["message"].splitlines()[0]})
                cur = (self.commits[cur]["parents"] or [None])[0]
            return 200, {}, out
        if method == "POST" and path == "/repository/commits":
            if self.fail_commit:
                return self.fail_commit.pop(0)
            out = self._create_commit(payload)
            if self.lose_response_once and out[0] == 201:
                self.lose_response_once = False
                return 599, {}, {"message": "network error: connection reset"}
            return out
        return 404, {}, {"message": f"unhandled {method} {path}"}

    def _page(self, rows, query, parts):
        size = min(int(query.get("per_page", 100)), self.page_size)
        page = int(query.get("page", 1))
        chunk = rows[(page - 1) * size: page * size]
        total = max(1, -(-len(rows) // size))
        hdrs: dict[str, str] = {}
        if self.pagination == "total":
            hdrs["x-total-pages"] = str(total)
        elif page < total:
            if self.pagination == "link":
                nxt = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode({**query, "page": page + 1}), ""))
                hdrs["link"] = f'<{nxt}>; rel="next"'
            else:
                hdrs["x-next-page"] = str(page + 1)
        return 200, hdrs, chunk

    def _create_commit(self, payload):
        branch = payload["branch"]
        if self.before_commit:
            hook, self.before_commit = self.before_commit, None
            hook(self)
        if branch in self.refs:
            parent = self.refs[branch]  # GitLab commits on the current tip of an existing branch
        else:
            parent = self._resolve(payload.get("start_sha", ""))
            if parent is None:
                return 400, {}, {"message": "start_sha or start_branch required to create a branch"}
        flat = dict(self.commits[parent]["files"])
        for a in payload["actions"]:
            fp, act = a["file_path"], a["action"]
            if act == "create" and fp in flat:
                return 400, {}, {"message": f"A file with this name already exists: {fp}"}
            if act in ("update", "delete") and fp not in flat:
                return 400, {}, {"message": f"A file with this name doesn't exist: {fp}"}
            if act == "delete":
                flat.pop(fp)
            else:
                text = base64.b64decode(a["content"]).decode() if a.get("encoding") == "base64" else a["content"]
                flat[fp] = ("100644", self._blob(text))
        sha = self._commit(flat, [parent], payload["commit_message"])
        self.refs[branch] = sha
        return 201, {}, {"id": sha, "parent_ids": [parent], "message": payload["commit_message"]}
