"""workspace_factory picks GitHub or GitLab per client and reports provider-specific errors.

fastapi / pydantic-settings / the client resolver are replaced by tiny stubs *only while the module is imported*,
so this runs in a bare interpreter and never leaks the stubs into other tests.
"""
import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from odoo_agent_server.services.github_commit_service import GitHubRepo
from odoo_agent_server.services.gitlab_commit_service import GitLabError, GitLabRaceLanded, GitLabRepo
from odoo_agent_server.services.workspace_service import WorkspaceError

SETTINGS = SimpleNamespace(github_token="ghp-env", gitlab_token="glpat-env", gitlab_hosts_raw="git.acme.io", ws_checkpoint_mode="sync", ws_protected_branches_raw="main", ws_autocheckpoint=True, ws_wip_files=True)
CLIENTS: dict[str, SimpleNamespace] = {}


class _HTTPException(Exception):
    def __init__(self, status_code=400, detail=""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


class _Resolver:
    def resolve_client(self, name, require_complete=False):
        return CLIENTS.get(name)


def client(name, repo, token=None, provider=None, api_url=None):
    CLIENTS[name] = SimpleNamespace(name=name, repo=repo, branch="staging", token=token, provider=provider, api_url=api_url, public={"repo": repo})
    return name


def load_factory():
    stubs = {
        "fastapi": types.SimpleNamespace(HTTPException=_HTTPException),
        "odoo_agent_server.config": types.SimpleNamespace(settings=SETTINGS),
        "odoo_agent_server.services.client_resolver_service": types.SimpleNamespace(ClientResolverService=_Resolver),
    }
    sys.modules.pop("odoo_agent_server.services.workspace_factory", None)
    with mock.patch.dict(sys.modules, stubs):
        module = importlib.import_module("odoo_agent_server.services.workspace_factory")
    return module


class Factory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = load_factory()

    def setUp(self):
        CLIENTS.clear()
        self.f._REPOS.clear()

    def test_gitlab_client_gets_the_gitlab_engine(self):
        target = self.f._resolve(client("B", "https://gitlab.com/group/sub/proj.git", token="glpat-own"))
        self.assertIsInstance(target.repo, GitLabRepo)
        self.assertEqual((target.repo.project, target.repo.token, target.repo.core_token), ("group/sub/proj", "glpat-own", "ghp-env"))
        self.assertEqual(target.branch, "staging")

    def test_github_client_is_unchanged(self):
        target = self.f._resolve(client("A", "https://github.com/o/r.git"))
        self.assertIsInstance(target.repo, GitHubRepo)
        self.assertEqual((target.repo.owner, target.repo.repo, target.repo.token), ("o", "r", "ghp-env"))

    def test_token_fallback_is_per_provider(self):
        self.assertEqual(self.f._resolve(client("B", "https://gitlab.com/g/p.git")).repo.token, "glpat-env")
        self.assertEqual(self.f._resolve(client("A", "https://github.com/o/r.git")).repo.token, "ghp-env")

    def test_missing_token_error_names_the_provider(self):
        with mock.patch.object(SETTINGS, "gitlab_token", None):
            with self.assertRaises(WorkspaceError) as ctx:
                self.f._resolve(client("B", "https://gitlab.com/g/p.git"))
        self.assertEqual(ctx.exception.as_dict()["error_code"], "gitlab_token_missing")
        self.assertIn("GITLAB_TOKEN", ctx.exception.as_dict()["error"])

    def test_self_managed_host_via_env_or_client_provider(self):
        self.assertIsInstance(self.f._resolve(client("C", "https://git.acme.io/g/p.git", token="t")).repo, GitLabRepo)  # GITLAB_HOSTS
        target = self.f._resolve(client("D", "https://code.other.io/g/p.git", token="t", provider="gitlab", api_url="https://code.other.io/gl/api/v4"))
        self.assertEqual(target.repo.api_base, "https://code.other.io/gl/api/v4")

    def test_unknown_host_without_hint_is_an_actionable_error(self):
        with self.assertRaises(WorkspaceError) as ctx:
            self.f._resolve(client("E", "https://code.other.io/g/p.git", token="t"))
        self.assertEqual(ctx.exception.as_dict()["error_code"], "repo_provider_unknown")
        self.assertIn("provider", ctx.exception.as_dict()["error"])

    def test_bad_url_is_reported_not_crashed(self):
        with self.assertRaises(WorkspaceError) as ctx:
            self.f._resolve(client("F", "https://gitlab.com/onlyone", token="t"))
        self.assertEqual(ctx.exception.as_dict()["error_code"], "repo_url_invalid")

    def test_engine_is_cached_per_repo_and_token(self):
        a = self.f._resolve(client("B", "https://gitlab.com/g/p.git", token="t1")).repo
        b = self.f._resolve(client("B2", "https://oauth2:t1@gitlab.com/g/p.git", token="t1")).repo  # credentials in the URL do not split the cache
        c = self.f._resolve(client("B3", "https://gitlab.com/g/p.git", token="t2")).repo
        self.assertIs(a, b)
        self.assertIsNot(a, c)


class RunWs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = load_factory()

    def raising(self, exc):
        def fn(_svc):
            raise exc
        with mock.patch.object(self.f, "workspace_service", lambda: object()):
            return self.f.run_ws(fn)

    def test_gitlab_error_code_uses_the_provider(self):
        out = self.raising(GitLabError(403, "denied", path="/x"))
        self.assertEqual((out["status"], out["error_code"], out["retryable"]), ("error", "gitlab_403", False))

    def test_github_error_code_is_unchanged(self):
        from odoo_agent_server.services.github_commit_service import GitHubError

        self.assertEqual(self.raising(GitHubError(404, "nf"))["error_code"], "github_404")

    def test_rejected_credentials_ask_the_user_and_name_the_fallback(self):
        for exc in (GitLabError(401, "bad token"), GitLabError(403, "denied")):
            out = self.raising(exc)
            self.assertTrue(out["needs_user"])
            self.assertEqual(out["fallback"]["tool"], "workflow_odoo_code_change")
        self.assertNotIn("needs_user", self.raising(GitLabError(404, "nf")))

    def test_landed_race_returns_the_commit_and_a_specific_hint(self):
        out = self.raising(GitLabRaceLanded("c" * 40, "a" * 40, "b" * 40, path="staging"))
        self.assertEqual(out["error_code"], "gitlab_409")
        self.assertEqual(out["landed_commit"], "c" * 40)
        self.assertIn("do NOT deploy", out["hint"])


if __name__ == "__main__":
    unittest.main()
