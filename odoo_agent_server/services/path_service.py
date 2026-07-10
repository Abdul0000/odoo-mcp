from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, status

from ..config import settings


def resolve_allowed_path(path: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    allowed_roots = settings.allowed_roots
    if not allowed_roots:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No allowed roots configured",
        )
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Path is outside allowed roots: {resolved}",
        )
    return resolved


def _contains_odoo_core_marker(path: Path) -> bool:
    """Detect Odoo core/enterprise roots even inside isolated test sandboxes.

    The normal production paths are absolute immutable roots, but tests and local
    mirrors often mount them below a temporary prefix. Matching the exact path
    segment sequence keeps the server-side boundary effective in both cases.
    """
    parts = tuple(part.lower() for part in path.parts)
    markers = (
        ("home", "odoo", "src", "odoo"),
        ("home", "odoo", "src", "enterprise"),
    )
    for marker in markers:
        width = len(marker)
        for index in range(0, max(len(parts) - width + 1, 0)):
            if parts[index:index + width] == marker:
                return True
    return False


def ensure_mutable_path(path: str | Path, *, action: str = "mutation") -> Path:
    """Return a resolved allowed path only when it is safe to mutate.

    Odoo community/core and enterprise source are read-only reference trees. This
    check is deliberately enforced in services (not just prompts) so every write,
    delete, patch, atomic transaction, and Git mutation shares the same boundary.
    """
    resolved = resolve_allowed_path(str(path))
    immutable_roots = settings.immutable_roots
    immutable = any(resolved == root or root in resolved.parents for root in immutable_roots)
    if immutable or _contains_odoo_core_marker(resolved):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{action} is forbidden inside immutable Odoo core/enterprise source: {resolved}",
        )
    return resolved


def ensure_mutable_project(project_path: str, *, action: str = "mutation") -> Path:
    return ensure_mutable_path(project_path, action=action)
