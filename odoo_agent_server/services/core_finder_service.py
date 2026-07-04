"""V14+ — Self-Learning Agent: Core File Discovery + Adaptive Scope Advisor.

Mandatory dual source read:
  Every coding task MUST read the custom module from the active checkout first,
  then read only the relevant installed Odoo/Odoo.sh source files. The scope
  advisor decides how deeply to read based on what is found (L0 → L4 adaptive
  levels), but it does not duplicate the current custom module from
  /home/odoo/src/user.

Search priority for installed-source reads:
  1. /home/odoo/src/enterprise (priority 1)
  2. /home/odoo/src/odoo       (priority 2)
  3. /home/odoo/src/user       (priority 3 — fallback for other related custom addons)

Adaptive scope levels:
  L0 (base)     — Custom module already read from checkout; no extra source read
  L1 (inherit)  — _inherit chain detected → also read parent model
  L2 (mixin)    — Abstract model / mixin detected → read all mixins (mail.thread etc.)
  L3 (computed) — ir.model / computed fields → full module models/ dir scan
  L4 (cross)    — Cross-module dependency → related module core also read
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Sequence

from . import core_sync_service
from .client_resolver_service import ClientResolverService
from ..schemas import (
    CoreFileMatch,
    FindCoreFileResult,
    FindSimilarViewResult,
    SimilarViewMatch,
    ScopeAdviceRequest,
    ScopeAdviceResult,
    ScopeLevel,
    ScopeReadInstruction,
)

# ---------------------------------------------------------------------------
# Default search roots ordered by priority
# ---------------------------------------------------------------------------
_DEFAULT_ROOTS: list[tuple[int, str]] = [
    (1, "/home/odoo/src/enterprise"),
    (2, "/home/odoo/src/odoo"),
    (3, "/home/odoo/src/user"),
]

# ---------------------------------------------------------------------------
# file_type -> sub-directory hints (best-effort speedup; fallback to full scan)
# ---------------------------------------------------------------------------
_FILETYPE_SUBDIRS: dict[str, list[str]] = {
    "model":      ["models"],
    "view":       ["views"],
    "wizard":     ["wizard", "wizards"],
    "controller": ["controllers"],
    "report":     ["report", "reports"],
    "data":       ["data", "demo"],
    "security":   ["security"],
    "static":     ["static"],
    "manifest":   ["."],
    "any":        [],
}

# ---------------------------------------------------------------------------
# view_type -> XML attribute / filename patterns
# ---------------------------------------------------------------------------
_VIEWTYPE_PATTERNS: dict[str, list[re.Pattern]] = {
    "form":     [re.compile(r'type=["\']form["\']'), re.compile(r'_form')],
    "tree":     [re.compile(r'type=["\']tree["\']'), re.compile(r'_tree')],
    "list":     [re.compile(r'type=["\']list["\']'), re.compile(r'_list'), re.compile(r'_tree')],
    "kanban":   [re.compile(r'type=["\']kanban["\']'), re.compile(r'_kanban')],
    "search":   [re.compile(r'type=["\']search["\']'), re.compile(r'_search')],
    "pivot":    [re.compile(r'type=["\']pivot["\']'), re.compile(r'_pivot')],
    "graph":    [re.compile(r'type=["\']graph["\']'), re.compile(r'_graph')],
    "calendar": [re.compile(r'type=["\']calendar["\']'), re.compile(r'_calendar')],
    "activity": [re.compile(r'type=["\']activity["\']'), re.compile(r'_activity')],
    "any":      [],
}

# ---------------------------------------------------------------------------
# Known Odoo mixins / abstract models to probe at L2
# ---------------------------------------------------------------------------
_KNOWN_MIXINS: list[tuple[str, str, str]] = [
    ("mail",    "model", "thread"),
    ("mail",    "model", "activity"),
    ("mail",    "model", "alias"),
    ("portal",  "model", "mixin"),
    ("rating",  "model", "mixin"),
    ("website", "model", "published"),
]

_MIXIN_DETECT_RE = re.compile(
    r'_inherit\s*=\s*[\[\(]?["\'](?:mail\.thread|mail\.activity|portal\.mixin|'
    r'rating\.mixin|website\.published|base\.setup|mail\.alias)["\']',
    re.IGNORECASE,
)
_ABSTRACT_DETECT_RE = re.compile(r'_abstract\s*=\s*True|class\s+\w+.*AbstractModel')
_INHERIT_DETECT_RE  = re.compile(r'_inherit\s*=\s*["\']([^"\']+)["\']')
_COMPUTED_DETECT_RE = re.compile(r'fields\.(Many2many|One2many|Many2one)\s*\(|compute\s*=')
_IRMODEL_DETECT_RE  = re.compile(r'ir\.model|env\[.+\]\.fields_get\(')
_CROSSMOD_DETECT_RE = re.compile(r'comodel_name\s*=\s*["\'](\w+)\.')


def _model_to_filename_hints(model: str) -> list[str]:
    """Convert 'sale.order' to filename hints like 'sale_order', 'order'."""
    parts = model.replace(".", "_")
    last = model.rsplit(".", 1)[-1]
    return [parts, last]


class CoreFinderService:
    """Find Odoo core source files across the /home/odoo/src tree.

    Now includes scope_advice() for adaptive mandatory dual-read planning.
    """

    # ------------------------------------------------------------------
    # Public API -- find_core_file
    # ------------------------------------------------------------------

    def find_core_file(
        self,
        module_name: str,
        file_type: str,
        keyword: str,
        search_roots: Sequence[str] | None = None,
    ) -> FindCoreFileResult:
        """Locate files matching module+type+keyword across priority roots."""
        roots = self._resolve_roots(search_roots)
        subdirs = _FILETYPE_SUBDIRS.get(file_type, [])
        kw_lower = keyword.lower()
        all_matches: list[CoreFileMatch] = []

        for priority, root in roots:
            root_path = Path(root)
            if not root_path.exists():
                continue
            module_dir = self._find_module_dir(root_path, module_name)
            if not module_dir:
                core_sync_service.ensure_module_synced(root, module_name)
                module_dir = self._find_module_dir(root_path, module_name)
            if not module_dir:
                continue

            candidates = self._walk_module(module_dir, subdirs)
            for abs_path in candidates:
                name_lower = abs_path.name.lower()
                if kw_lower not in name_lower:
                    continue
                if file_type == "manifest" and abs_path.name != "__manifest__.py":
                    continue
                if file_type != "manifest" and not self._extension_matches(abs_path, file_type):
                    continue
                try:
                    size = abs_path.stat().st_size
                except OSError:
                    size = 0
                all_matches.append(CoreFileMatch(
                    path=str(abs_path),
                    relative_path=str(abs_path.relative_to(root_path)),
                    root=root,
                    priority=priority,
                    size=size,
                ))

        all_matches.sort(key=lambda m: (m.priority, len(m.path)))
        best = all_matches[0].path if all_matches else None

        return FindCoreFileResult(
            module_name=module_name,
            file_type=file_type,
            keyword=keyword,
            matches=all_matches,
            best_match=best,
            searched_roots=[r for _, r in roots],
            total_found=len(all_matches),
        )

    # ------------------------------------------------------------------
    # Public API -- find_similar_view
    # ------------------------------------------------------------------

    def find_similar_view(
        self,
        model: str,
        view_type: str,
        search_roots: Sequence[str] | None = None,
    ) -> FindSimilarViewResult:
        """Locate view XML files for a model+view_type across priority roots."""
        roots = self._resolve_roots(search_roots)
        model_hints = _model_to_filename_hints(model)
        module_candidate = model.split(".")[0]
        type_patterns = _VIEWTYPE_PATTERNS.get(view_type, [])
        all_matches: list[SimilarViewMatch] = []

        for priority, root in roots:
            root_path = Path(root)
            if not root_path.exists():
                continue
            module_dir = self._find_module_dir(root_path, module_candidate)
            if not module_dir:
                core_sync_service.ensure_module_synced(root, module_candidate)
                module_dir = self._find_module_dir(root_path, module_candidate)
            search_dirs: list[Path] = []
            if module_dir:
                search_dirs.append(module_dir)
            else:
                search_dirs.append(root_path)

            for search_dir in search_dirs:
                for current, dirs, files in os.walk(search_dir):
                    dirs[:] = [
                        d for d in dirs
                        if not d.startswith(".")
                        and d not in {"__pycache__", ".git", "node_modules", "static", "tests", "test"}
                    ]
                    for fname in files:
                        if not fname.endswith(".xml"):
                            continue
                        fname_lower = fname.lower()
                        if not any(h in fname_lower for h in model_hints):
                            continue
                        abs_path = Path(current) / fname
                        if view_type != "any":
                            matched_pattern = any(
                                p.search(fname_lower) for p in type_patterns
                            )
                            if not matched_pattern:
                                matched_pattern = self._quick_content_check(abs_path, type_patterns)
                            if not matched_pattern:
                                continue
                        try:
                            size = abs_path.stat().st_size
                        except OSError:
                            size = 0
                        all_matches.append(SimilarViewMatch(
                            path=str(abs_path),
                            relative_path=str(abs_path.relative_to(root_path)),
                            root=root,
                            priority=priority,
                            model_hint=model,
                            size=size,
                        ))

        seen: set[str] = set()
        unique: list[SimilarViewMatch] = []
        for m in sorted(all_matches, key=lambda m: (m.priority, len(m.path))):
            if m.path not in seen:
                seen.add(m.path)
                unique.append(m)

        best = unique[0].path if unique else None
        return FindSimilarViewResult(
            model=model,
            view_type=view_type,
            matches=unique,
            best_match=best,
            searched_roots=[r for _, r in roots],
            total_found=len(unique),
        )

    # ------------------------------------------------------------------
    # Public API -- scope_advice  (NEW -- Pillar 2: Adaptive Scope)
    # ------------------------------------------------------------------

    def scope_advice(
        self,
        custom_module_name: str,
        custom_file_paths: list[str],
        task_description: str,
        search_roots: Sequence[str] | None = None,
    ) -> ScopeAdviceResult:
        """Analyse custom module files and return adaptive mandatory read instructions.

        The agent MUST call this after repo.read on custom files and BEFORE
        writing any code. It returns an ordered list of ScopeReadInstruction
        objects (read_type + path) specifying everything that must be read.

        Levels (adaptive -- each level adds more reads):
          L0 -- always: custom module manifest (baseline)
          L1 -- _inherit detected: read parent core model(s)
          L2 -- mixin/abstract detected: read all related mixins
          L3 -- computed/relational fields or ir.model: scan full models/ dir
          L4 -- cross-module comodel: read that module's manifest + model
        """
        roots = self._resolve_roots(search_roots)
        instructions: list[ScopeReadInstruction] = []
        reached_level = ScopeLevel.L0
        cross_modules: set[str] = set()

        # Scan provided custom Python files for patterns
        inherit_models: list[str] = []
        has_mixin = False
        has_computed_or_relational = False
        has_irmodel = False

        for fpath in custom_file_paths:
            if not fpath.endswith(".py"):
                continue
            content = self._safe_read(fpath)
            if not content:
                continue

            # L1: detect _inherit
            for m in _INHERIT_DETECT_RE.finditer(content):
                model_name = m.group(1)
                if model_name and "." in model_name:
                    inherit_models.append(model_name)

            # L2: detect mixin / abstract
            if _MIXIN_DETECT_RE.search(content) or _ABSTRACT_DETECT_RE.search(content):
                has_mixin = True

            # L3: detect computed / relational fields or ir.model usage
            if _COMPUTED_DETECT_RE.search(content):
                has_computed_or_relational = True
            if _IRMODEL_DETECT_RE.search(content):
                has_irmodel = True

            # L4: detect cross-module comodel references
            for cm in _CROSSMOD_DETECT_RE.finditer(content):
                mod = cm.group(1)
                if mod and mod != custom_module_name:
                    cross_modules.add(mod)

        # L1: add parent model reads
        if inherit_models:
            reached_level = ScopeLevel.L1
            for model in sorted(set(inherit_models)):
                core_module = model.split(".")[0]
                keyword = model.split(".")[-1] if "." in model else model
                result = self.find_core_file(core_module, "model", keyword, search_roots)
                if result.best_match:
                    instructions.append(ScopeReadInstruction(
                        level=ScopeLevel.L1,
                        reason=f"_inherit '{model}' detected -- MUST read parent core model before writing fields",
                        read_type="core_model",
                        path=result.best_match,
                        mandatory=True,
                    ))
                else:
                    instructions.append(ScopeReadInstruction(
                        level=ScopeLevel.L1,
                        reason=f"_inherit '{model}' detected but not found in src roots -- check manually",
                        read_type="core_model_missing",
                        path=f"NOT_FOUND: /home/odoo/src/odoo/addons/{core_module}/models/{keyword}.py",
                        mandatory=False,
                    ))

        # L2: add mixin reads
        if has_mixin:
            reached_level = ScopeLevel.L2
            for mixin_module, mixin_type, mixin_kw in _KNOWN_MIXINS:
                result = self.find_core_file(mixin_module, mixin_type, mixin_kw, search_roots)
                if result.best_match:
                    instructions.append(ScopeReadInstruction(
                        level=ScopeLevel.L2,
                        reason=f"Mixin/abstract detected -- read {mixin_module}.{mixin_kw} source for correct method signatures",
                        read_type="core_mixin",
                        path=result.best_match,
                        mandatory=True,
                    ))

        # L3: full models/ dir scan hint
        if has_computed_or_relational or has_irmodel:
            reached_level = ScopeLevel.L3
            target_module = inherit_models[0].split(".")[0] if inherit_models else custom_module_name
            for _, root in roots:
                root_path = Path(root)
                module_dir = self._find_module_dir(root_path, target_module)
                if not module_dir:
                    core_sync_service.ensure_module_synced(root, target_module)
                    module_dir = self._find_module_dir(root_path, target_module)
                models_dir = module_dir / "models" if module_dir else root_path / target_module / "models"
                if models_dir.exists():
                    instructions.append(ScopeReadInstruction(
                        level=ScopeLevel.L3,
                        reason=(
                            "Computed/relational fields or ir.model detected -- "
                            "scan full models/ dir to find all field definitions and dependencies"
                        ),
                        read_type="core_models_dir_scan",
                        path=str(models_dir),
                        mandatory=True,
                    ))
                    break

        # L4: cross-module dependencies
        if cross_modules:
            reached_level = ScopeLevel.L4
            for dep_module in sorted(cross_modules):
                result = self.find_core_file(dep_module, "manifest", dep_module, search_roots)
                if result.best_match:
                    instructions.append(ScopeReadInstruction(
                        level=ScopeLevel.L4,
                        reason=f"Cross-module comodel ref to '{dep_module}' -- verify exact module technical name",
                        read_type="cross_module_manifest",
                        path=result.best_match,
                        mandatory=True,
                    ))

        return ScopeAdviceResult(
            custom_module=custom_module_name,
            reached_level=reached_level,
            instructions=instructions,
            mandatory_count=sum(1 for i in instructions if i.mandatory),
            total_count=len(instructions),
            warning=(
                "MANDATORY DUAL READ: Execute ALL mandatory instructions before writing code. "
                "Push is BLOCKED until workflow.verify_against_core succeeds."
            ),
        )

    # ------------------------------------------------------------------
    # Public API -- list_module_files (gap-fix)
    # ------------------------------------------------------------------

    def list_module_files(
        self,
        module_name: str,
        subdir: str = "",
        search_roots: Sequence[str] | None = None,
    ) -> "ListModuleFilesResult":
        """List all files in a module directory (or subdir) across priority roots.

        Use this when you don't know the exact filename/keyword yet and need
        to browse what exists before calling find_core_file with a keyword.
        """
        from ..schemas import ListModuleFilesResult, ModuleFileEntry

        roots = self._resolve_roots(search_roots)
        for priority, root in roots:
            root_path = Path(root)
            if not root_path.exists():
                continue
            module_dir = self._find_module_dir(root_path, module_name)
            if not module_dir:
                core_sync_service.ensure_module_synced(root, module_name)
                module_dir = self._find_module_dir(root_path, module_name)
            if not module_dir:
                continue

            search_path = module_dir / subdir if subdir else module_dir
            if not search_path.exists():
                continue

            entries: list[ModuleFileEntry] = []
            if search_path.is_file():
                entries.append(ModuleFileEntry(
                    path=str(search_path),
                    relative_path=str(search_path.relative_to(module_dir)),
                    size=search_path.stat().st_size,
                    ext=search_path.suffix,
                ))
            else:
                for current, dirs, files in os.walk(search_path):
                    dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"__pycache__", ".git"}]
                    for fname in sorted(files):
                        fpath = Path(current) / fname
                        try:
                            entries.append(ModuleFileEntry(
                                path=str(fpath),
                                relative_path=str(fpath.relative_to(module_dir)),
                                size=fpath.stat().st_size,
                                ext=fpath.suffix,
                            ))
                        except OSError:
                            continue

            entries.sort(key=lambda e: e.relative_path)
            return ListModuleFilesResult(
                module_name=module_name,
                subdir=subdir or "(root)",
                found=True,
                module_path=str(module_dir),
                root=root,
                priority=priority,
                files=entries[:100],
                total_found=len(entries),
            )

        # Not found in any root
        return ListModuleFilesResult(
            module_name=module_name,
            subdir=subdir or "(root)",
            found=False,
            files=[],
            total_found=0,
            message=(
                f"Module '{module_name}' not found in any search root "
                f"(checked: {', '.join(r for _, r in roots)}). "
                f"Check module name spelling or pass search_roots explicitly."
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_roots(search_roots: Sequence[str] | None) -> list[tuple[int, str]]:
        if search_roots:
            return [(i + 1, r) for i, r in enumerate(search_roots)]
        client = ClientResolverService().resolve_client(require_complete=False)
        if client:
            return [(i + 1, root) for i, root in enumerate(ClientResolverService().search_roots(client))]
        return _DEFAULT_ROOTS

    @staticmethod
    def _walk_module(module_dir: Path, subdirs: list[str]) -> list[Path]:
        results: list[Path] = []
        if subdirs:
            for sub in subdirs:
                sub_path = module_dir / sub if sub != "." else module_dir
                if sub_path.exists():
                    if sub_path.is_file():
                        results.append(sub_path)
                    else:
                        for current, dirs, files in os.walk(sub_path):
                            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"__pycache__", ".git"}]
                            for fname in files:
                                results.append(Path(current) / fname)
            if not results:
                subdirs = []
        if not subdirs:
            for current, dirs, files in os.walk(module_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"__pycache__", ".git"}]
                for fname in files:
                    results.append(Path(current) / fname)
        return results

    @staticmethod
    def _extension_matches(path: Path, file_type: str) -> bool:
        ext = path.suffix.lower()
        if file_type in {"model", "wizard", "controller"}:
            return ext == ".py"
        if file_type in {"view", "report", "data"}:
            return ext == ".xml"
        if file_type == "security":
            return ext in {".csv", ".xml"}
        if file_type == "static":
            return ext in {".js", ".css", ".scss", ".xml", ".html", ".png", ".svg"}
        return True

    @staticmethod
    def _quick_content_check(path: Path, patterns: list[re.Pattern], peek_bytes: int = 4096) -> bool:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                chunk = fh.read(peek_bytes)
            return any(p.search(chunk) for p in patterns)
        except OSError:
            return False

    @staticmethod
    def _safe_read(path: str, max_bytes: int = 65536) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                return fh.read(max_bytes)
        except OSError:
            return ""
