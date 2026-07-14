from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')


def test_stateless_oauth_refresh_rejects_state_directory_loss_to_prevent_replay(tmp_path, monkeypatch):
    first_state = tmp_path / 'state1'
    second_state = tmp_path / 'state2'
    monkeypatch.setattr(settings, 'state_dir', str(first_state))
    monkeypatch.setattr(settings, 'oauth_auto_approve', True)
    monkeypatch.setattr(settings, 'oauth_token_secret', 'stable-test-secret')

    client = TestClient(app, base_url='https://agent.example.com')
    verifier = 'redeploy-verifier-abcdefghijklmnopqrstuvwxyz1234567890'
    authorize = client.get(
        '/oauth/authorize',
        params={
            'response_type': 'code',
            'client_id': settings.oauth_static_client_id,
            'redirect_uri': 'https://chatgpt.com/connector_platform_oauth_redirect',
            'state': 'redeploy-state',
            'scope': 'odoo.agent',
            'code_challenge': _challenge(verifier),
                'code_challenge_method': 'S256',
                'resource': 'https://agent.example.com/mcp',
        },
        follow_redirects=False,
    )
    assert authorize.status_code == 302
    code = parse_qs(urlparse(authorize.headers['location']).query)['code'][0]
    token = client.post(
        '/oauth/token',
        data={
            'grant_type': 'authorization_code',
            'client_id': settings.oauth_static_client_id,
            'redirect_uri': 'https://chatgpt.com/connector_platform_oauth_redirect',
            'code': code,
            'code_verifier': verifier,
            'resource': 'https://agent.example.com/mcp',
        },
    )
    assert token.status_code == 200
    refresh_token = token.json()['refresh_token']

    # Losing durable state must not make an already-rotated signed token
    # reusable. A restart with the same state directory remains supported.
    monkeypatch.setattr(settings, 'state_dir', str(second_state))
    refreshed = client.post(
        '/oauth/token',
        data={
            'grant_type': 'refresh_token',
            'client_id': settings.oauth_static_client_id,
            'refresh_token': refresh_token,
            'resource': 'https://agent.example.com/mcp',
        },
    )
    assert refreshed.status_code == 400
    assert 'durable OAuth state is required' in refreshed.json()['detail']
