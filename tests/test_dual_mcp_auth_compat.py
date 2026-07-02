from types import SimpleNamespace

from odoo_agent_server import auth
from odoo_agent_server.config import settings


class DummyQueryParams(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def test_request_token_accepts_bearer():
    request = SimpleNamespace(query_params=DummyQueryParams())
    assert auth._request_token(request, "Bearer abc", None) == "abc"


def test_request_token_accepts_x_api_key():
    request = SimpleNamespace(query_params=DummyQueryParams())
    assert auth._request_token(request, None, "abc") == "abc"


def test_request_token_accepts_raw_authorization_token():
    request = SimpleNamespace(query_params=DummyQueryParams())
    assert auth._request_token(request, "abc", None) == "abc"


def test_request_token_query_fallback(monkeypatch):
    monkeypatch.setattr(settings, "query_token_auth_enabled", True)
    request = SimpleNamespace(query_params=DummyQueryParams(access_token="abc"))
    assert auth._request_token(request, None, None) == "abc"
