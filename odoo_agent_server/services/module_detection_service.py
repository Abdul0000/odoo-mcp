from __future__ import annotations

from pathlib import Path
from typing import Iterable


def _safe_relative(project: Path, raw_path: str) -> Path | None:
    value = (raw_path or "").strip().replace("\\", "/").strip("\"'")
    if not value or value.startswith(".git/") or value == ".git":
        return None
    # Git status/diff output may include rename notation. Keep the destination.
    if " -> " in value:
        value = value.rsplit(" -> ", 1)[-1].strip()
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(project)
        except Exception:
            return None
    parts = [part for part in path.parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    return Path(*parts)


def _nearest_module_dir(project: Path, rel_path: Path) -> Path | None:
    candidates: list[Path] = []
    path = project / rel_path
    # If the changed path itself is a module directory, check it first. For file
    # paths, start at the parent directory. This also handles __manifest__.py.
    if (path / "__manifest__.py").is_file():
        candidates.append(path)
    start = path if path.is_dir() else path.parent
    candidates.extend([start, *start.parents])
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except Exception:
            continue
        if candidate == project.parent:
            break
        if candidate == project or project not in candidate.parents:
            continue
        if (candidate / "__manifest__.py").is_file():
            return candidate
    return None


def detect_affected_modules(project_path: str, changed_paths: Iterable[str]) -> list[str]:
    """Return Odoo module technical names touched by changed git paths.

    A module is any directory below ``project_path`` containing ``__manifest__.py``.
    The returned value is the directory name (the Odoo technical module name),
    sorted and deduplicated. Paths outside the project, .git paths, unsafe paths,
    and files that are not inside a module are ignored.
    """
    project = Path(project_path).expanduser().resolve()
    modules: set[str] = set()
    for raw_path in changed_paths:
        rel_path = _safe_relative(project, str(raw_path))
        if rel_path is None:
            continue
        module_dir = _nearest_module_dir(project, rel_path)
        if module_dir is not None:
            modules.add(module_dir.name)
    return sorted(modules)
