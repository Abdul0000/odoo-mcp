from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status

from .config import settings
from .services.oauth_service import OAuthStore


def external_base_url(request: Request) -> str:
    configured = settings.public_base_url or settings.oauth_issuer
    if configured:
        return configured.rstrip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")


def oauth_resource_metadata_url(request: Request) -> str:
    return f"{external_base_url(request)}/.well-known/oauth-protected-resource"


def require_token(request: Request, authorization: str | None = Header(default=None)) -> None:
    """Accept either the legacy connector bearer token or OAuth access tokens.

    The legacy token keeps REST/OpenAPI Actions working. OAuth access tokens are
    required by ChatGPT custom MCP apps/connectors.
    """
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()

    if token and token == settings.api_token:
        return
    if token and OAuthStore().validate_access_token(token):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid bearer token",
        headers={
            "WWW-Authenticate": f'Bearer resource_metadata="{oauth_resource_metadata_url(request)}"',
        },
    )


AuthDependency = Depends(require_token)
