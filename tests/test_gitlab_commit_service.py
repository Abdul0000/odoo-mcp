import unittest
import urllib.parse

from odoo_agent_server.services.github_commit_service import FileChange, GitHubRepo, NonFastForward
from odoo_agent_server.services.gitlab_commit_service import (
    GitLabError,
    GitLabRaceLanded,
    GitLabRepo,
    parse_gitlab_repo,
)
from odoo_agent_server.services.repo_provider_service import build_repo, detect_provider, parse_hosts
from odoo_agent_server.services.workspace_service import ClientTarget, WorkspaceRegistry, WorkspaceService
from tests.support.fake_gitlab import PROJECT, FakeGitLab
from tests.test_workspace_service import BASE

API = "https://gitlab.com/api/v4"


def make(files=None, **fake_kwargs):
    gl = FakeGitLab(dict(files if files is not None else BASE), branch="staging", **fake_kwargs)
    repo = GitLabRepo(API, PROJECT, "tok", transport=gl, sleep=lambda s: None)
    return gl, repo


class ParseUrls(unittest.TestCase):
    def test_https_with_subgroups_and_git_suffix(self):
        self.assertEqual(parse_gitlab_repo("https://gitlab.com/group/sub/proj.git"), (API, "group/sub/proj"))

    def test_credentials_are_dropped(self):
        self.assertEqual(parse_gitlab_repo("https://oauth2:glpat-secret@gitlab.com/g/p.git")[1], "g/p")

    def test_ssh_forms(self):
        self.assertEqual(parse_gitlab_repo("git@gitlab.com:g/p.git"), (API, "g/p"))
        self.assertEqual(parse_gitlab_repo("ssh://git@git.acme.io/g/sub/p.git"), ("https://git.acme.io/api/v4", "g/sub/p"))

    def test_self_managed_port_and_api_override(self):
        self.assertEqual(parse_gitlab_repo("https://git.acme.io:8443/g/p")[0], "https://git.acme.io:8443/api/v4")
        self.assertEqual(parse_gitlab_repo("https://git.acme.io/g/p", api_url="https://git.acme.io/gitlab/api/v4/")[0], "https://git.acme.io/gitlab/api/v4")

    def test_rejects_urls_without_a_project(self):
        for bad in ("", "https://gitlab.com/onlyone", "nonsense"):
            with self.assertRaises(ValueError):
                parse_gitlab_repo(bad)


class ProviderDetection(unittest.TestCase):
    def test_hosts(self):
        self.assertEqual(detect_provider("https://github.com/o/r.git"), "github")
        self.assertEqual(detect_provider("git@github.com:o/r.git"), "github")
        self.assertEqual(detect_provider("https://gitlab.com/g/p.git"), "gitlab")
        self.assertEqual(detect_provider("https://gitlab.acme.io/g/p.git"), "gitlab")

    def test_custom_host_needs_a_hint(self):
        with self.assertRaises(ValueError) as ctx:
            detect_provider("https://git.acme.io/g/p.git")
        self.assertIn("provider", str(ctx.exception))
        self.assertEqual(detect_provider("https://git.acme.io/g/p.git", "gitlab"), "gitlab")
        self.assertEqual(detect_provider("https://git.acme.io/g/p.git", gitlab_hosts=parse_hosts("Git.Acme.io, other.io")), "gitlab")

    def test_explicit_provider_wins_and_is_validated(self):
        self.assertEqual(detect_provider("https://gitlab.com/g/p.git", "github"), "github")
        with self.assertRaises(ValueError):
            detect_provider("https://gitlab.com/g/p.git", "bitbucket")

    def test_build_repo_returns_the_right_engine(self):
        gl = build_repo("https://gitlab.com/g/s/p.git", "t", github_token="ghp")
        self.assertIsInstance(gl, GitLabRepo)
        self.assertEqual((gl.project, gl.core_token), ("g/s/p", "ghp"))
        gh = build_repo("https://github.com/o/r.git", "t")
        self.assertIsInstance(gh, GitHubRepo)
        self.assertEqual((gh.owner, gh.repo), ("o", "r"))


class ReadPath(unittest.TestCase):
    def test_head_tree_blob_and_encoding(self):
        gl, repo = make()
        head = repo.head("staging")
        self.assertEqual(head["tree"], head["commit"])
        tree = repo.tree(head["tree"])
        self.assertIn("my_mod/models/sale.py", tree)
        self.assertEqual(set(tree["my_mod/models/sale.py"]), {"sha", "mode", "size"})
        self.assertIn("note_x", repo.blob_text(tree["my_mod/models/sale.py"]["sha"]))
        urls = [u for _, u in gl.calls]
        self.assertTrue(all("/projects/" + urllib.parse.quote(PROJECT, safe="") + "/" in u for u in urls), urls)
        self.assertEqual(gl.headers_seen[0]["Authorization"], "Bearer tok")

    def test_branch_names_with_slashes_are_encoded(self):
        gl, repo = make()
        gl.refs["mcp/wip/x"] = gl.refs["staging"]
        self.assertTrue(repo.ref_exists("mcp/wip/x"))
        self.assertEqual(repo.head("mcp/wip/x")["commit"], gl.refs["staging"])
        self.assertTrue(any("mcp%2Fwip%2Fx" in u for _, u in gl.calls))
        self.assertFalse(repo.ref_exists("nope"))

    def test_tree_is_cached_and_callers_cannot_corrupt_it(self):
        gl, repo = make()
        sha = repo.head("staging")["tree"]
        first = repo.tree(sha)
        first.pop("my_mod/models/sale.py")
        n = len(gl.calls)
        again = repo.tree(sha)
        self.assertIn("my_mod/models/sale.py", again)
        self.assertEqual(len(gl.calls), n)

    def test_pagination_variants_all_return_every_file(self):
        for mode in ("total", "link", "next"):
            gl, repo = make(page_size=3, pagination=mode)
            self.assertEqual(sorted(repo.tree(repo.head("staging")["tree"])), sorted(BASE), mode)

    def test_root_blob_tree_at_and_recent_commits(self):
        gl, repo = make()
        sha = repo.head("staging")["tree"]
        self.assertIsNone(repo.root_blob(sha, "missing.json"))
        listing = repo.tree_at(sha, "my_mod/models")
        self.assertEqual(sorted(listing), ["__init__.py", "sale.py"])
        self.assertIsNone(repo.tree_at(sha, "nope/nothing"))
        gl.push_external("staging", {"x.txt": "1"}, message="second\n\nMCP-Op: abc")
        rows = repo.recent_commit_messages("staging", 5)
        self.assertEqual([r["message"].splitlines()[0] for r in rows], ["second", "initial"])

    def test_urls_and_default_branch(self):
        gl, repo = make()
        self.assertEqual(repo.web_url("commit", "abc"), f"https://gitlab.com/{PROJECT}/-/commit/abc")
        self.assertEqual(repo.web_url("tree", "staging"), f"https://gitlab.com/{PROJECT}/-/tree/staging")
        self.assertEqual(repo.default_branch(), "main")

    def test_rate_limit_is_retried_and_errors_are_typed(self):
        gl, repo = make()
        gl.fail_next = [(429, {"retry-after": "1"}, {"message": "Retry later"})]
        self.assertTrue(repo.head("staging")["commit"])
        gl.fail_next = [(401, {}, {"message": "401 Unauthorized"})]
        with self.assertRaises(GitLabError) as ctx:
            repo.head("staging")
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.provider, "gitlab")
        gl.fail_next = [(404, {}, {"message": "404 Branch Not Found"})]
        with self.assertRaises(GitLabError) as ctx:
            repo.head("staging")
        self.assertIn("private", str(ctx.exception))

    def test_core_repo_uses_github_not_the_gitlab_token(self):
        repo = GitLabRepo(API, PROJECT, "gitlab-token", core_token=None)
        core = repo.core_repo()
        self.assertIsInstance(core, GitHubRepo)
        self.assertEqual((core.owner, core.repo, core.token), ("odoo", "odoo", ""))
        seen = {}

        def transport(method, url, headers, body):
            seen.update(headers)
            return 200, {}, {"default_branch": "18.0"}

        GitHubRepo("odoo", "odoo", "", transport=transport).default_branch()
        self.assertNotIn("Authorization", seen)  # anonymous reads of public odoo/odoo, never a bogus empty bearer


class WritePath(unittest.TestCase):
    def test_create_update_delete_in_one_commit(self):
        gl, repo = make()
        head = repo.head("staging")
        out = repo.commit_to_branch("staging", parent=head["commit"], base_tree=head["tree"], message="m", changes=[
            FileChange("my_mod/models/sale.py", "x = 1\n"),
            FileChange("my_mod/models/new.py", "y = 2\n"),
            FileChange("other/__init__.py", None),
            FileChange("never/existed.py", None),
        ])
        files = gl.head_files("staging")
        self.assertEqual(files["my_mod/models/sale.py"], "x = 1\n")
        self.assertEqual(files["my_mod/models/new.py"], "y = 2\n")
        self.assertNotIn("other/__init__.py", files)
        self.assertEqual(gl.refs["staging"], out["commit"])
        self.assertEqual(gl.commits[out["commit"]]["parents"], [head["commit"]])
        self.assertEqual(out["tree"], out["commit"])

    def test_unicode_and_empty_content_round_trip(self):
        gl, repo = make()
        head = repo.head("staging")
        repo.commit_to_branch("staging", parent=head["commit"], base_tree=head["tree"], message="m", changes=[FileChange("my_mod/i18n/ur.po", "msgstr \"السلام\"\n"), FileChange("my_mod/empty.py", "")])
        files = gl.head_files("staging")
        self.assertEqual(files["my_mod/i18n/ur.po"], "msgstr \"السلام\"\n")
        self.assertEqual(files["my_mod/empty.py"], "")

    def test_new_branch_is_cut_from_the_expected_parent(self):
        gl, repo = make()
        head = repo.head("staging")
        gl.push_external("staging", {"later.txt": "1"})  # staging moves on, the new branch must still start at `parent`
        out = repo.commit_to_branch("mcp/wip/x", parent=head["commit"], base_tree=head["tree"], message="wip", changes=[FileChange("a.txt", "a")], create=True)
        self.assertEqual(gl.commits[out["commit"]]["parents"], [head["commit"]])
        self.assertNotIn("later.txt", gl.head_files("mcp/wip/x"))

    def test_branch_moved_before_commit_is_non_fast_forward_and_writes_nothing(self):
        gl, repo = make()
        head = repo.head("staging")
        gl.push_external("staging", {"other/__init__.py": "# up\n"})
        n = len(gl.commits)
        with self.assertRaises(NonFastForward):
            repo.commit_to_branch("staging", parent=head["commit"], base_tree=head["tree"], message="m", changes=[FileChange("a.txt", "a")])
        self.assertEqual(len(gl.commits), n)

    def test_commit_error_while_branch_moved_is_non_fast_forward(self):
        gl, repo = make()
        head = repo.head("staging")
        gl.before_commit = lambda g: g.push_external("staging", {"a.txt": "theirs"})  # same path: GitLab rejects our "create"
        with self.assertRaises(NonFastForward):
            repo.commit_to_branch("staging", parent=head["commit"], base_tree=head["tree"], message="m", changes=[FileChange("a.txt", "mine")])
        self.assertEqual(gl.head_files("staging")["a.txt"], "theirs")

    def test_commit_that_raced_onto_a_newer_tip_is_reported_not_swallowed(self):
        gl, repo = make()
        head = repo.head("staging")
        gl.before_commit = lambda g: g.push_external("staging", {"other/__init__.py": "# up\n"})  # lands between tip check and commit
        with self.assertRaises(GitLabRaceLanded) as ctx:
            repo.commit_to_branch("staging", parent=head["commit"], base_tree=head["tree"], message="m", changes=[FileChange("my_mod/new.py", "n")])
        self.assertEqual(ctx.exception.expected_parent, head["commit"])
        self.assertIn("ws_open", str(ctx.exception))

    def test_5xx_before_the_commit_applied_is_retried_after_checking_nothing_landed(self):
        gl, repo = make()
        head = repo.head("staging")
        gl.fail_commit = [(502, {}, {"message": "Bad Gateway"})]
        out = repo.commit_to_branch("staging", parent=head["commit"], base_tree=head["tree"], message="m", changes=[FileChange("a.txt", "a")])
        self.assertEqual(gl.refs["staging"], out["commit"])
        self.assertEqual(sum(1 for c in gl.commits.values() if c["message"] == "m"), 1)
        posts = sum(1 for m, u in gl.calls if m == "POST" and u.endswith("/repository/commits"))
        self.assertEqual(posts, 2)  # the failed attempt plus exactly one retry, and only one commit exists

    def test_response_lost_after_apply_returns_the_landed_commit(self):
        gl, repo = make()
        head = repo.head("staging")
        gl.lose_response_once = True
        out = repo.commit_to_branch("staging", parent=head["commit"], base_tree=head["tree"], message="same message", changes=[FileChange("a.txt", "a")])
        self.assertEqual(out["commit"], gl.refs["staging"])
        self.assertEqual(sum(1 for c in gl.commits.values() if c["message"] == "same message"), 1)
        self.assertEqual(sum(1 for m, u in gl.calls if m == "POST" and u.endswith("/repository/commits")), 1)  # never replayed

    def test_response_lost_and_someone_else_pushed_is_non_fast_forward(self):
        gl, repo = make()
        head = repo.head("staging")
        gl.fail_commit = [(599, {}, {"message": "network error"})]
        gl.before_tip_check = None
        real_tip = repo._branch_tip
        state = {"n": 0}

        def tip_then_someone_pushes(branch):
            tip = real_tip(branch)
            state["n"] += 1
            if state["n"] == 1:  # first call is the pre-commit tip check; the push lands while the commit is in doubt
                gl.push_external("staging", {"zzz.txt": "1"})
            return tip

        repo._branch_tip = tip_then_someone_pushes
        with self.assertRaises(NonFastForward):
            repo.commit_to_branch("staging", parent=head["commit"], base_tree=head["tree"], message="m", changes=[FileChange("a.txt", "a")])

    def test_stale_tree_view_is_rebuilt_once(self):
        gl, repo = make()
        head = repo.head("staging")
        repo.tree(head["tree"])  # warm the cache, then make it lie: pretend a.txt does not exist yet
        gl.commits[head["commit"]]["files"]["a.txt"] = ("100644", gl._blob("old"))
        out = repo.commit_to_branch("staging", parent=head["commit"], base_tree=head["tree"], message="m", changes=[FileChange("a.txt", "new")])
        self.assertEqual(gl.head_files("staging")["a.txt"], "new")
        self.assertTrue(out["commit"])

    def test_wip_commit_mirrors_base_plus_changes_not_accumulated_edits(self):
        gl, repo = make()
        base = repo.head("staging")
        one = repo.commit_to_branch("mcp/wip/x", parent=base["commit"], base_tree=base["tree"], message="1", changes=[FileChange("my_mod/a.py", "a"), FileChange("my_mod/b.py", "b")], create=True)
        two = repo.commit_to_branch("mcp/wip/x", parent=one["commit"], base_tree=base["tree"], message="2", changes=[FileChange("my_mod/b.py", "b2")])
        files = gl.head_files("mcp/wip/x")
        self.assertNotIn("my_mod/a.py", files)  # reverted file disappears, same as GitHub's base_tree semantics
        self.assertEqual(files["my_mod/b.py"], "b2")
        reverted = repo.commit_to_branch("mcp/wip/x", parent=two["commit"], base_tree=base["tree"], message="3", changes=[FileChange("my_mod/models/sale.py", "z")])
        self.assertEqual(gl.head_files("mcp/wip/x")["my_mod/models/sale.py"], "z")
        self.assertNotIn("my_mod/b.py", gl.head_files("mcp/wip/x"))
        self.assertTrue(reverted["commit"])

    def test_delete_branch_and_move_ref_contract(self):
        gl, repo = make()
        sha = gl.refs["staging"]
        repo.move_ref("copy", sha, create=True)
        self.assertEqual(gl.refs["copy"], sha)
        repo.move_ref("copy", sha)  # already there: no-op
        gl.push_external("copy", {"q": "1"})
        with self.assertRaises(GitLabError):
            repo.move_ref("copy", sha)  # arbitrary ref moves are not expressible on GitLab
        repo.delete_branch("copy")
        repo.delete_branch("copy")  # idempotent
        self.assertNotIn("copy", gl.refs)


class WorkspaceOnGitLab(unittest.TestCase):
    """The same ws_* flows the GitHub tests cover, driven through the GitLab engine."""

    def svc(self, **kw):
        gl, repo = make()
        reg = WorkspaceRegistry()
        svc = WorkspaceService(lambda name: ClientTarget("Acme", repo, "staging"), protected="", registry=reg, **kw)
        return gl, repo, svc, reg

    def edit_ok(self, svc, ws_id):
        return svc.edit(ws_id, [{"path": "models/sale.py", "search": "note_x = fields.Char()", "replace": "note_x = fields.Char()\n    note_y = fields.Integer()"}])

    def test_open_edit_commit(self):
        gl, repo, svc, reg = self.svc()
        out = svc.open("Acme", "my_mod")
        self.assertEqual(out["module"], "my_mod")
        ws_id = out["ws_id"]
        self.edit_ok(svc, ws_id)
        res = svc.commit(ws_id, "add note_y")
        self.assertEqual(res["status"], "pushed", res)
        self.assertIn("note_y", gl.head_files("staging")["my_mod/models/sale.py"])
        self.assertIn("MCP-Op:", gl.commits[res["commit"]]["message"])
        self.assertTrue(res["urls"]["commit"].endswith("/-/commit/" + res["commit"]))
        self.assertIn("in GitLab use the branch dropdown", res["where"])
        self.assertIn("Pushed to GitLab", res["deploy_note"])
        self.assertEqual(svc.commit(ws_id, "again")["status"], "no_changes")

    def test_wip_branch_shows_real_files_and_survives_restart(self):
        gl, repo, svc, reg = self.svc()
        ws_id = svc.open("Acme", "my_mod")["ws_id"]
        res = svc.edit(ws_id, [{"path": "models/thing.py", "content": "x = 1\n"}, {"path": "models/sale.py", "search": "Char()", "replace": "Text()"}])
        self.assertTrue(res["checkpoint"]["saved"], res["checkpoint"])
        wip = gl.head_files("mcp/wip/acme-my-mod")
        self.assertEqual(wip["my_mod/models/thing.py"], "x = 1\n")
        self.assertNotIn("my_mod/models/thing.py", gl.head_files("staging"))
        self.assertIn("gitlab.com/group/sub/proj/-/tree/mcp/wip/acme-my-mod", res["checkpoint"]["url"])
        reg.clear()  # Render spun down
        self.assertIn("Text()", svc.read(ws_id, ["models/sale.py"])["files"][0]["text"])
        self.assertEqual(svc.commit(ws_id, "finish")["status"], "pushed")
        self.assertNotIn("mcp/wip/acme-my-mod", gl.refs)

    def test_second_edit_updates_existing_wip_branch(self):
        gl, repo, svc, reg = self.svc()
        ws_id = svc.open("Acme", "my_mod")["ws_id"]
        svc.edit(ws_id, [{"path": "models/thing.py", "content": "x = 1\n"}])
        r2 = svc.edit(ws_id, [{"path": "models/thing.py", "content": "x = 2\n"}])
        self.assertTrue(r2["checkpoint"]["saved"], r2["checkpoint"])
        self.assertEqual(gl.head_files("mcp/wip/acme-my-mod")["my_mod/models/thing.py"], "x = 2\n")

    def test_upstream_moved_unrelated_file_rebases(self):
        gl, repo, svc, reg = self.svc()
        ws_id = svc.open("Acme", "my_mod")["ws_id"]
        self.edit_ok(svc, ws_id)
        gl.push_external("staging", {"other/__init__.py": "# touched\n"})
        res = svc.commit(ws_id, "m")
        self.assertEqual(res["status"], "pushed", res)
        self.assertTrue(res["rebased_onto_newer_head"])
        files = gl.head_files("staging")
        self.assertIn("note_y", files["my_mod/models/sale.py"])
        self.assertEqual(files["other/__init__.py"], "# touched\n")

    def test_race_inside_the_commit_window_is_surfaced_with_the_landed_commit(self):
        gl, repo, svc, reg = self.svc()
        ws_id = svc.open("Acme", "my_mod")["ws_id"]
        self.edit_ok(svc, ws_id)
        gl.before_commit = lambda g: g.push_external("staging", {"other/__init__.py": "# up\n"})
        with self.assertRaises(GitLabRaceLanded) as ctx:
            svc.commit(ws_id, "m")
        self.assertIn(ctx.exception.commit, gl.commits)
        self.assertEqual(ctx.exception.extra["landed_commit"], ctx.exception.commit)
        self.assertTrue(str(ctx.exception).startswith("GitLab 409"))

    def test_lost_response_after_the_commit_landed_is_not_duplicated(self):
        gl, repo, svc, reg = self.svc()
        ws_id = svc.open("Acme", "my_mod")["ws_id"]
        self.edit_ok(svc, ws_id)
        before = len(gl.commits)
        gl.lose_response_once = True
        res = svc.commit(ws_id, "m")
        self.assertEqual(res["status"], "pushed", res)
        self.assertEqual(res["commit"], gl.refs["staging"])
        self.assertGreaterEqual(len(gl.commits) - before, 1)
        landed = [c for c in gl.commits.values() if "MCP-Op:" in c["message"]]
        self.assertEqual(len(landed), 1)

    def test_commit_to_a_new_branch_cuts_it_from_the_base(self):
        gl, repo, svc, reg = self.svc()
        ws_id = svc.open("Acme", "my_mod")["ws_id"]
        self.edit_ok(svc, ws_id)
        res = svc.commit(ws_id, "feature work", branch="feature/note-y")
        self.assertEqual(res["status"], "pushed", res)
        self.assertEqual(res["branch"], "feature/note-y")
        self.assertIn("note_y", gl.head_files("feature/note-y")["my_mod/models/sale.py"])
        self.assertNotIn("note_y", gl.head_files("staging")["my_mod/models/sale.py"])  # client branch untouched
        self.assertTrue(res["urls"]["branch"].endswith("/-/tree/feature/note-y"))
        again = svc.open("Acme", "my_mod", branch="feature/note-y")
        self.assertEqual(again["module"], "my_mod")

    def test_status_names_the_provider_in_links(self):
        gl, repo, svc, reg = self.svc()
        ws_id = svc.open("Acme", "my_mod")["ws_id"]
        self.edit_ok(svc, ws_id)
        status = svc.status(ws_id)
        self.assertIn("gitlab.com", str(status))


if __name__ == "__main__":
    unittest.main()
