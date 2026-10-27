"""ws_commit reports evidence (what changed, what is confirmed, what is NOT yet true) on GitHub and GitLab."""
import unittest

from odoo_agent_server.services.github_commit_service import GitHubRepo
from odoo_agent_server.services.gitlab_commit_service import GitLabRepo
from odoo_agent_server.services.workspace_service import ClientTarget, WorkspaceError, WorkspaceRegistry, WorkspaceService
from tests.support.fake_github import FakeGitHub
from tests.support.fake_gitlab import PROJECT, FakeGitLab
from tests.test_workspace_service import BASE

EDIT = [{"path": "models/sale.py", "search": "note_x = fields.Char()", "replace": "note_x = fields.Char()\n    note_y = fields.Integer()"}]


def service(kind):
    if kind == "github":
        fake = FakeGitHub(dict(BASE), branch="staging")
        repo = GitHubRepo("o", "r", "tok", transport=fake, sleep=lambda s: None)
    else:
        fake = FakeGitLab(dict(BASE), branch="staging")
        repo = GitLabRepo("https://gitlab.com/api/v4", PROJECT, "tok", transport=fake, sleep=lambda s: None)
    return fake, WorkspaceService(lambda name: ClientTarget("Acme", repo, "staging"), protected="", registry=WorkspaceRegistry())


class Evidence(unittest.TestCase):
    def check(self, kind):
        fake, svc = service(kind)
        ws_id = svc.open("Acme", "my_mod")["ws_id"]
        svc.edit(ws_id, EDIT)
        svc.edit(ws_id, [{"path": "models/extra.py", "content": "x = 1\n"}])
        res = svc.commit(ws_id, "add fields")
        self.assertEqual(res["status"], "pushed", res)
        ev = res["evidence"]
        self.assertEqual((ev["implemented"], ev["validated"], ev["committed"], ev["pushed"]), (True, "lint_passed", True, True))
        self.assertTrue(ev["remote_confirmed"], f"{kind}: the server response should prove the push")
        self.assertEqual((ev["deployed"], ev["verified_live"]), (False, False))
        self.assertIn("next_call", ev["pending"])
        ch = res["changes"]
        kinds = {f["path"]: f for f in ch["files"]}
        self.assertEqual(kinds["my_mod/models/sale.py"]["change"], "modified")
        self.assertEqual((kinds["my_mod/models/sale.py"]["added"], kinds["my_mod/models/sale.py"]["removed"]), (1, 0))
        self.assertEqual(kinds["my_mod/models/extra.py"]["change"], "created")
        self.assertEqual(ch["file_count"], len(ch["files"]))
        self.assertGreaterEqual(ch["added"], 2)

    def test_github(self):
        self.check("github")

    def test_gitlab(self):
        self.check("gitlab")

    def test_deleted_files_are_labelled(self):
        for kind in ("github", "gitlab"):
            fake, svc = service(kind)
            ws_id = svc.open("Acme", "my_mod")["ws_id"]
            svc.edit(ws_id, [{"path": "views/sale_views.xml", "op": "delete"}])
            out = svc.commit(ws_id, "drop view")
            self.assertEqual(out["status"], "pushed", out)
            changes = {f["path"]: f["change"] for f in out["changes"]["files"]}
            self.assertEqual(changes["my_mod/views/sale_views.xml"], "deleted", kind)
            self.assertEqual(changes["my_mod/__manifest__.py"], "modified", kind)  # auto-unwired

    def test_unconfirmed_push_is_reported_honestly(self):
        fake, svc = service("github")
        real = fake.__call__

        def no_echo(method, url, headers, body):
            status, hdrs, data = real(method, url, headers, body)
            return (status, hdrs, {}) if method == "PATCH" else (status, hdrs, data)

        ws_id = svc.open("Acme", "my_mod")["ws_id"]
        svc.edit(ws_id, EDIT)
        ws = svc.registry.get(ws_id)
        ws.repo.transport = no_echo
        res = svc.commit(ws_id, "m")
        self.assertEqual(res["status"], "pushed", res)
        self.assertFalse(res["evidence"]["remote_confirmed"])
        self.assertTrue(res["evidence"]["pushed"])


class Fallback(unittest.TestCase):
    def test_unreachable_repo_errors_name_the_alternate_lane(self):
        for code in ("github_token_missing", "gitlab_token_missing", "repo_provider_unknown", "repo_url_invalid"):
            out = WorkspaceError(code, "x").as_dict()
            self.assertEqual(out["fallback"]["tool"], "workflow_odoo_code_change", code)

    def test_other_errors_have_no_fallback(self):
        self.assertNotIn("fallback", WorkspaceError("module_not_found", "x").as_dict())


if __name__ == "__main__":
    unittest.main()
