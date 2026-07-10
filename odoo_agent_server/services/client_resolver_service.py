from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, status

from ..config import (
    get_odoo_clients,
    make_authenticated_repo_url,
    normalise_client,
    safe_client_public,
)


@dataclass(frozen=True)
class ResolvedClient:
    name: str
    repo: str
    branch: str
    path: str
    token: str | None = None
    odoo_url: str | None = None
    odoo_db: str | None = None
    odoo_login: str | None = None
    odoo_password: str | None = None

    @property
    def authenticated_repo(self) -> str:
        return make_authenticated_repo_url(self.repo, self.token)

    @property
    def public(self) -> dict:
        return safe_client_public(
            {
                "name": self.name,
                "repo": self.repo,
                "branch": self.branch,
                "path": self.path,
                "odoo_url": self.odoo_url,
                "odoo_db": self.odoo_db,
            }
        )


class ClientResolverService:
    """Resolve the active ODOO_CLIENTS entry without exposing secrets."""

    _required = ("repo", "branch", "path")

    def resolve_client(
        self,
        client_name: str | None = None,
        project_path: str | None = None,
        odoo_url: str | None = None,
        odoo_db: str | None = None,
        *,
        require_complete: bool = True,
    ) -> ResolvedClient | None:
        clients = [normalise_client(item) for item in get_odoo_clients()]
        if not clients:
            return None

        complete_clients = [item for item in clients if self._is_complete(item)]

        if client_name:
            matches = [item for item in clients if item.get("name", "").lower() == client_name.lower()]
            if not matches:
                self._raise_not_found(client_name, clients)
            return self._to_resolved(matches[0], require_complete=require_complete)

        selected = self._match_odoo_target(complete_clients, odoo_url, odoo_db)
        if selected:
            return selected

        selected = self._match_path(complete_clients, project_path)
        if selected:
            return selected

        if len(complete_clients) == 1:
            return self._to_resolved(complete_clients[0], require_complete=True)

        if len(complete_clients) > 1:
            self._raise_ambiguous(complete_clients)

        if require_complete and clients:
            self._raise_incomplete(clients)
        return None

    def apply_odoo_env(self, client: ResolvedClient | None) -> None:
        if not client:
            return
        if client.odoo_url:
            os.environ["ODOO_BASE_URL"] = client.odoo_url
        if client.odoo_db:
            os.environ["ODOO_DB_NAME"] = client.odoo_db
        if client.odoo_login:
            os.environ["ODOO_LOGIN"] = client.odoo_login
        if client.odoo_password:
            os.environ["ODOO_PASSWORD"] = client.odoo_password

    def search_roots(self, client: ResolvedClient | None) -> list[str]:
        roots: list[str] = [
            "/home/odoo/src/enterprise",
            "/home/odoo/src/odoo",
            "/home/odoo/src/user",
        ]
        seen: set[str] = set()
        result: list[str] = []
        for root in roots:
            if root and root not in seen:
                seen.add(root)
                result.append(root)
        return result

    def _match_odoo_target(
        self,
        clients: list[dict],
        odoo_url: str | None,
        odoo_db: str | None,
    ) -> ResolvedClient | None:
        url = (odoo_url or os.environ.get("ODOO_BASE_URL") or "").rstrip("/")
        db = odoo_db or os.environ.get("ODOO_DB_NAME") or ""
        if not url or not db:
            return None
        matches = [
            item for item in clients
            if item.get("odoo_url", "").rstrip("/") == url and item.get("odoo_db") == db
        ]
        return self._single_match(matches)

    def _match_path(self, clients: list[dict], project_path: str | None) -> ResolvedClient | None:
        if not project_path:
            return None
        target = self._normal_path(project_path)
        matches = [item for item in clients if self._normal_path(item.get("path")) == target]
        return self._single_match(matches)

    def _single_match(self, matches: list[dict]) -> ResolvedClient | None:
        if not matches:
            return None
        if len(matches) > 1:
            self._raise_ambiguous(matches)
        return self._to_resolved(matches[0], require_complete=True)

    def _to_resolved(self, client: dict, *, require_complete: bool) -> ResolvedClient | None:
        if require_complete and not self._is_complete(client):
            self._raise_incomplete([client])
        if not self._is_complete(client):
            return None
        return ResolvedClient(
            name=client.get("name") or "ODOO_CLIENTS[0]",
            repo=client["repo"],
            branch=client["branch"],
            path=client["path"],
            token=client.get("token") or None,
            odoo_url=client.get("odoo_url") or None,
            odoo_db=client.get("odoo_db") or None,
            odoo_login=client.get("odoo_login") or None,
            odoo_password=client.get("odoo_password") or None,
        )

    def _is_complete(self, client: dict) -> bool:
        return all(client.get(key) for key in self._required)

    @staticmethod
    def _normal_path(path: str | None) -> str:
        if not path:
            return ""
        return str(Path(path).expanduser().resolve())

    def _raise_not_found(self, name: str, clients: list[dict]) -> None:
        names = [item.get("name") for item in clients if item.get("name")]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Client '{name}' not found in ODOO_CLIENTS. Available clients: {names}",
        )

    def _raise_ambiguous(self, clients: list[dict]) -> None:
        names = [item.get("name") or "(unnamed client)" for item in clients]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Multiple ODOO_CLIENTS entries match. Choose one client_name from: {names}",
        )

    def _raise_incomplete(self, clients: list[dict]) -> None:
        safe = [safe_client_public(item) for item in clients]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "ODOO_CLIENTS is configured, but no selected entry contains all required "
                f"repo/branch/path values. Safe client metadata: {safe}"
            ),
        )
