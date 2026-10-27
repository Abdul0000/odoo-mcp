"""Opt-in smoke test against a REAL GitLab project (gitlab.com or self-managed).

The offline suite proves the engine against a faithful fake; this proves it against your server, once.
It only touches a throw-away branch named mcp/smoke/<timestamp> and deletes it at the end.

    GITLAB_SMOKE_REPO=https://gitlab.com/group/project.git \
    GITLAB_SMOKE_TOKEN=glpat-... \
    GITLAB_SMOKE_BRANCH=staging \
    python -m tests.live_gitlab_smoke

Optional: GITLAB_SMOKE_PROVIDER=gitlab, GITLAB_SMOKE_API_URL=https://git.example.com/api/v4
"""
from __future__ import annotations

import os
import sys
import time

from odoo_agent_server.services.github_commit_service import FileChange, NonFastForward
from odoo_agent_server.services.repo_provider_service import build_repo


def main() -> int:
    url, token, branch = os.getenv("GITLAB_SMOKE_REPO"), os.getenv("GITLAB_SMOKE_TOKEN"), os.getenv("GITLAB_SMOKE_BRANCH")
    if not (url and token and branch):
        print("set GITLAB_SMOKE_REPO, GITLAB_SMOKE_TOKEN and GITLAB_SMOKE_BRANCH")
        return 2
    repo = build_repo(url, token, provider=os.getenv("GITLAB_SMOKE_PROVIDER") or "gitlab", api_url=os.getenv("GITLAB_SMOKE_API_URL") or None)
    tmp = f"mcp/smoke/{int(time.time())}"
    ok = True

    def check(label: str, cond: bool, extra: str = "") -> None:
        nonlocal ok
        ok = ok and cond
        print(("PASS " if cond else "FAIL ") + label + (f"  {extra}" if extra else ""))

    try:
        head = repo.head(branch)
        check("read head of base branch", bool(head["commit"]), head["commit"][:12])
        tree = repo.tree(head["tree"])
        check("list tree", len(tree) > 0, f"{len(tree)} files")
        any_blob = next(iter(tree.values()))["sha"]
        check("read a blob", isinstance(repo.blob_text(any_blob), str))
        first = repo.commit_to_branch(tmp, parent=head["commit"], base_tree=head["tree"], changes=[FileChange("mcp_smoke/a.txt", "one\n")], message="mcp smoke 1", create=True)
        check("create branch + commit", repo.ref_exists(tmp), first["commit"][:12])
        second = repo.commit_to_branch(tmp, parent=first["commit"], base_tree=head["tree"], changes=[FileChange("mcp_smoke/a.txt", "two\n"), FileChange("mcp_smoke/b.txt", "b\n")], message="mcp smoke 2")
        check("update + create in one commit", repo.blob_text(repo.tree(second["tree"])["mcp_smoke/a.txt"]["sha"]) == "two\n")
        third = repo.commit_to_branch(tmp, parent=second["commit"], base_tree=head["tree"], changes=[FileChange("mcp_smoke/b.txt", "b2\n")], message="mcp smoke 3")
        files3 = repo.tree(third["tree"])
        check("WIP semantics: file dropped from changes disappears", "mcp_smoke/a.txt" not in files3 and "mcp_smoke/b.txt" in files3)
        try:
            repo.commit_to_branch(tmp, parent=first["commit"], base_tree=head["tree"], changes=[FileChange("mcp_smoke/c.txt", "c\n")], message="stale parent")
            check("stale parent is rejected", False)
        except NonFastForward:
            check("stale parent is rejected (NonFastForward)", True)
        msgs = repo.recent_commit_messages(tmp, 3)
        check("commit messages round-trip", msgs[0]["message"].strip() == "mcp smoke 3", msgs[0]["message"][:40])
        print("web url:", repo.web_url("tree", tmp))
    finally:
        try:
            repo.delete_branch(tmp)
            print("cleaned up", tmp)
        except Exception as exc:  # noqa: BLE001
            print("could not delete", tmp, exc)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
