"""Strict Odoo pre-push verifier.

V14 verified Python/XML/manifest code against Odoo source.  V15 extends that
same guard to Odoo frontend assets: JS/OWL/POS/website/payment XML templates,
asset bundles, and import/template consistency.  The goal is not to replace a
browser test; it blocks the most common framework-pattern mistakes before push.
"""
from __future__ import annotations

import ast
import glob
import re
from pathlib import Path
from typing import Any

from . import core_sync_service
from ..config import settings
from .repo_index_service import RepoIndexService
from ..schemas import (
    SyntaxVerifyRequest,
    SyntaxVerifyResult,
    VerifyFinding,
    VerifySeverity,
)

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------
_FIELD_NAME_RE     = re.compile(r'^\s{4,}(\w+)\s*=\s*fields\.', re.MULTILINE)
_INHERIT_RE        = re.compile(r'_inherit\s*=\s*([^\n#]+)')
_MODEL_LITERAL_RE  = re.compile(r'["\']([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_.]+)["\']')
_DEPENDS_PY_RE     = re.compile(r'depends\(\[([^\]]+)\]\)')
_COMODEL_RE        = re.compile(r'(?:comodel_name\s*=\s*["\']([^"\']+)["\']|fields\.(?:Many2one|One2many|Many2many)\s*\(\s*["\']([^"\']+)["\'])')
_RELATED_RE        = re.compile(r'related\s*=\s*["\']([^"\']+)["\']')
_COMPUTE_RE        = re.compile(r'compute\s*=\s*["\']([^"\']+)["\']')
_XPATH_EXPR_RE     = re.compile(r'expr=["\']([^"\']+)["\']')
_FIELD_NAME_XML_RE = re.compile(r'<field\s+name=["\'](\w+)["\']')
_INHERIT_ID_RE     = re.compile(r'inherit_id\s+ref=["\']([^"\']+)["\']')
_MANIFEST_DEPS_RE  = re.compile(r"['\"]depends['\"]\s*:\s*\[([^\]]+)\]", re.DOTALL)
_MODULE_NAME_RE    = re.compile(r"['\"]([A-Za-z0-9_]+)['\"]")

_JS_IMPORT_RE      = re.compile(r'(?:from\s+["\']([^"\']+)["\']|import\s*\(\s*["\']([^"\']+)["\']\s*\))')
_JS_TEMPLATE_RE    = re.compile(r'(?:static\s+template\s*=|\.template\s*=)\s*["\']([^"\']+)["\']')
_POS_CONFIG_LOAD_FIELDS_RE = re.compile(r'load_fields\s*\(\s*["\']pos\.config["\']', re.IGNORECASE)
_T_NAME_RE         = re.compile(r't-name=["\']([^"\']+)["\']')
_T_INHERIT_RE      = re.compile(r't-inherit=["\']([^"\']+)["\']')
_T_CALL_RE         = re.compile(r't-call=["\']([^"\']+)["\']')
_T_XPATH_RE        = re.compile(r'<xpath\b[^>]*expr=["\']([^"\']+)["\']', re.DOTALL)
_CLASS_IN_EXPR_RE  = re.compile(r'contains\(@class,\s*["\']([^"\']+)["\']\)|hasclass\(["\']([^"\']+)["\']\)')

# Search roots for installed-source reads: enterprise, community, then user fallback.
_SRC_ROOTS = [
    Path("/home/odoo/src/enterprise"),
    Path("/home/odoo/src/odoo"),
    Path("/home/odoo/src/user"),
]

_ALIAS_MODULES = {
    "@web/": "web",
    "@point_of_sale/": "point_of_sale",
    "@pos/": "point_of_sale",
    "@website/": "website",
    "@website_sale/": "website_sale",
    "@website_payment/": "website_payment",
    "@payment/": "payment",
    "@mail/": "mail",
    "@portal/": "portal",
    "@spreadsheet/": "spreadsheet",
    "@hr/": "hr",
    "@account/": "account",
    "@sale/": "sale",
    "@stock/": "stock",
}

_EXTERNAL_IMPORTS = {"@odoo/owl", "luxon"}


def _read_file(path: str, max_bytes: int = 524288) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read(max_bytes)
    except OSError:
        return ""


def _module_dir(root: Path, module_name: str) -> Path | None:
    """Resolve direct and community addons layouts."""
    for candidate in (root / module_name, root / "addons" / module_name, root / "odoo" / "addons" / module_name):
        if candidate.is_dir():
            return candidate
    return None


def _iter_module_dirs(module_name: str) -> list[Path]:
    dirs: list[Path] = []
    for root in _SRC_ROOTS:
        found = _module_dir(root, module_name)
        if not found:
            core_sync_service.ensure_module_synced(str(root), module_name)
            found = _module_dir(root, module_name)
        if found:
            dirs.append(found)
    return dirs


def _core_source_available() -> bool:
    """True if at least one of the Odoo core/enterprise/user source roots is
    actually reachable on this machine — either it already exists locally,
    or on-demand sync (core_sync_service, rsync over SSH to Odoo.sh) is
    configured and able to reach the build host.

    This exists because /home/odoo/src/* is a HARDCODED path. On a Render
    deployment with no Odoo.sh SSH configured (ODOOSH_SSH_HOST unset, or
    CORE_SYNC_ENABLED=false), none of that tree exists, ensure_module_synced()
    silently returns False every time, and every downstream lookup returns
    "not found" — indistinguishable from a genuinely-missing module/import.
    Without this check, comodel_module_exists and js_import_resolves would
    report CRITICAL/push-blocking findings for perfectly valid Odoo code,
    purely because core source isn't reachable, not because the code is wrong.
    """
    if any(root.exists() and any(root.iterdir()) for root in _SRC_ROOTS if root.exists()):
        return True
    return bool(settings.core_sync_enabled and settings.odoosh_ssh_host)


def _find_core_model_content(model_name: str) -> str:
    """Return concatenated content of core model files for a given model."""
    module = model_name.split(".")[0]
    contents: list[str] = []
    for module_dir in _iter_module_dirs(module):
        models_dir = module_dir / "models"
        if not models_dir.exists():
            continue
        for py_file in models_dir.rglob("*.py"):
            content = _read_file(str(py_file))
            if f"'{model_name}'" in content or f'"{model_name}"' in content:
                contents.append(content)
    return "\n".join(contents)


def _extract_core_field_names(core_content: str) -> set[str]:
    """Extract field names defined in core model source."""
    return {m.group(1) for m in _FIELD_NAME_RE.finditer(core_content)}


def _extract_model_literals(raw: str) -> list[str]:
    """Extract Odoo model strings from scalar or list/tuple assignments."""
    return [m.group(1) for m in _MODEL_LITERAL_RE.finditer(raw)]


def _find_core_view_content(model_name: str) -> str:
    """Return concatenated content of core view XML files for a model."""
    if not model_name:
        return ""
    module = model_name.split(".")[0]
    hints = [model_name.replace(".", "_"), model_name.split(".")[-1]]
    contents: list[str] = []
    for module_dir in _iter_module_dirs(module):
        views_dir = module_dir / "views"
        if not views_dir.exists():
            continue
        for xml_file in views_dir.rglob("*.xml"):
            fname_lower = xml_file.name.lower()
            if any(h in fname_lower for h in hints):
                contents.append(_read_file(str(xml_file)))
    return "\n".join(contents)


def _module_exists_in_src(module_name: str) -> bool:
    """Check whether a module directory exists anywhere in src roots."""
    return bool(_iter_module_dirs(module_name))


def _extract_all_template_names(xml_contents: dict[str, str]) -> set[str]:
    names: set[str] = set()
    for content in xml_contents.values():
        names.update(m.group(1) for m in _T_NAME_RE.finditer(content))
    return names


def _find_template_content(template_name: str, local_xml_contents: dict[str, str] | None = None) -> str:
    """Find QWeb template source in written XML files or Odoo source roots."""
    if local_xml_contents:
        for content in local_xml_contents.values():
            if f't-name="{template_name}"' in content or f"t-name='{template_name}'" in content:
                return content
    # Try module prefix first for speed: web.SomeTemplate -> module web.
    module_hint = template_name.split(".", 1)[0] if "." in template_name else None
    module_dirs = _iter_module_dirs(module_hint) if module_hint else []
    search_dirs = module_dirs or [root for root in _SRC_ROOTS if root.exists()]
    for search_dir in search_dirs:
        for xml_file in search_dir.rglob("*.xml"):
            content = _read_file(str(xml_file), max_bytes=1048576)
            if f't-name="{template_name}"' in content or f"t-name='{template_name}'" in content:
                return content
    return ""


def _resolve_odoo_alias(import_path: str) -> list[Path]:
    """Map @web/foo/bar imports to possible source files."""
    for alias, module in _ALIAS_MODULES.items():
        if not import_path.startswith(alias):
            continue
        tail = import_path[len(alias):]
        candidates: list[Path] = []
        for module_dir in _iter_module_dirs(module):
            base = module_dir / "static" / "src" / tail
            candidates.extend([base, base.with_suffix(".js"), base.with_suffix(".xml"), base / "index.js"])
        return candidates
    return []


def _known_odoo_alias(import_path: str) -> bool:
    return any(import_path.startswith(alias) for alias in _ALIAS_MODULES)


def _asset_path_exists(module_root: Path, asset: str) -> bool:
    """Return True if a manifest asset string resolves to at least one file."""
    if not isinstance(asset, str):
        return True
    if asset.startswith(("http://", "https://", "//")):
        return True
    # Odoo assets commonly start with module_name/static/src/...
    parent = module_root.parent
    raw_candidates = [parent / asset, module_root / asset]
    if any(candidate.exists() for candidate in raw_candidates):
        return True
    if "*" in asset:
        return bool(glob.glob(str(parent / asset)) or glob.glob(str(module_root / asset)))
    return False


def _flatten_asset_entries(entries: Any) -> list[str]:
    assets: list[str] = []
    if isinstance(entries, str):
        return [entries]
    if not isinstance(entries, (list, tuple)):
        return assets
    for item in entries:
        if isinstance(item, str):
            assets.append(item)
        elif isinstance(item, (list, tuple)):
            # Examples: ('include', 'web._assets_helpers'), ('replace', old, new)
            assets.extend(value for value in item if isinstance(value, str) and "/" in value)
    return assets


class SyntaxVerifierService:
    """Pre-push cross-checker for Odoo Python/XML/manifest/JS asset files."""

    def verify(self, request: SyntaxVerifyRequest) -> SyntaxVerifyResult:
        findings: list[VerifyFinding] = []
        files_checked: list[str] = []
        xml_contents: dict[str, str] = {}
        js_contents: dict[str, str] = {}

        if not _core_source_available():
            findings.append(VerifyFinding(
                file=request.written_files[0] if request.written_files else "(no files)",
                severity=VerifySeverity.INFO,
                check="core_source_unavailable",
                message=(
                    "Odoo core/enterprise source is not reachable from this environment (no local "
                    "/home/odoo/src/* tree, and Odoo.sh SSH sync is disabled or unconfigured). "
                    "comodel_module_exists, js_import_resolves, and manifest_depends checks below have been "
                    "downgraded to WARNING instead of CRITICAL because they cannot be reliably verified — "
                    "they are NOT proof the code is correct, just that it couldn't be checked."
                ),
                fix_hint="Set ODOOSH_SSH_HOST and ODOOSH_SSH_KEY_PATH env vars to enable on-demand core sync via core_sync_service.",
            ))

        written_files = self._impact_aware_files(request)

        # First pass: read files and collect XML/JS context for template matching.
        file_contents: dict[str, str] = {}
        for file_path in written_files:
            path = Path(file_path)
            if not path.exists():
                findings.append(VerifyFinding(
                    file=file_path,
                    severity=VerifySeverity.WARNING,
                    check="file_exists",
                    message=f"File not found on disk: {file_path} — was it written correctly?",
                    fix_hint="Re-run repo.write or repo.write_many_atomic for this file.",
                ))
                continue
            files_checked.append(file_path)
            content = _read_file(file_path)
            file_contents[file_path] = content
            if path.suffix == ".xml":
                xml_contents[file_path] = content
            elif path.suffix == ".js":
                js_contents[file_path] = content

        template_names = _extract_all_template_names(xml_contents)
        findings.extend(self._verify_duplicate_template_names(xml_contents))

        for file_path, content in file_contents.items():
            path = Path(file_path)
            if path.name == "__manifest__.py":
                findings.extend(self._verify_manifest(file_path, content))
            elif path.suffix == ".py":
                findings.extend(self._verify_python_model(file_path, content))
            elif path.suffix == ".xml":
                findings.extend(self._verify_xml_view(file_path, content))
                findings.extend(self._verify_qweb_template(file_path, content, xml_contents))
            elif path.suffix == ".js":
                findings.extend(self._verify_js_syntax_real(file_path, content))
                findings.extend(self._verify_js_owl(file_path, content, template_names))
            elif path.suffix == ".scss":
                findings.extend(self._verify_scss_asset(file_path, content))

        push_blocked = any(f.severity == VerifySeverity.CRITICAL for f in findings)
        critical_count = sum(1 for f in findings if f.severity == VerifySeverity.CRITICAL)
        warning_count = sum(1 for f in findings if f.severity == VerifySeverity.WARNING)
        info_count = sum(1 for f in findings if f.severity == VerifySeverity.INFO)

        fix_cycle = request.fix_cycle or 0
        if push_blocked and fix_cycle >= 3:
            status_msg = (
                f"BLOCKED after {fix_cycle} fix cycles. "
                "Report exact findings to user and stop retrying."
            )
        elif push_blocked:
            status_msg = (
                f"PUSH BLOCKED — {critical_count} critical issue(s) found. "
                f"Fix cycle {fix_cycle + 1}/3. Fix all CRITICAL issues and re-run verify_against_core."
            )
        else:
            status_msg = (
                f"PUSH ALLOWED — {len(files_checked)} file(s) verified. "
                f"{warning_count} warning(s), {info_count} info note(s)."
            )

        return SyntaxVerifyResult(
            push_blocked=push_blocked,
            status=status_msg,
            files_checked=files_checked,
            findings=findings,
            critical_count=critical_count,
            warning_count=warning_count,
            info_count=info_count,
            fix_cycle=fix_cycle,
        )

    def _impact_aware_files(self, request: SyntaxVerifyRequest) -> list[str]:
        enabled = settings.verify_impact_aware_default if request.impact_aware is None else bool(request.impact_aware)
        if not enabled or not request.project_path or request.max_related_files <= 0:
            return list(request.written_files)
        try:
            return RepoIndexService().related_files(
                request.written_files,
                project_path=request.project_path,
                max_related=request.max_related_files,
            )
        except Exception:
            # Verification must never fail just because the optimiser could not
            # locate related files. Fall back to exact changed files.
            return list(request.written_files)

    # ------------------------------------------------------------------
    # Manifest verification
    # ------------------------------------------------------------------

    def _verify_manifest(self, file_path: str, content: str) -> list[VerifyFinding]:
        findings: list[VerifyFinding] = []
        m = _MANIFEST_DEPS_RE.search(content)
        if m:
            deps_block = m.group(1)
            dep_names = [n.group(1) for n in _MODULE_NAME_RE.finditer(deps_block)]
            for dep in dep_names:
                if dep in {"base", "web", "mail", "sale", "purchase", "account", "stock", "hr"}:
                    continue
                if not _module_exists_in_src(dep):
                    source_available = _core_source_available()
                    findings.append(VerifyFinding(
                        file=file_path,
                        severity=VerifySeverity.CRITICAL if source_available else VerifySeverity.WARNING,
                        check="manifest_depends",
                        message=(
                            f"depends[] contains '{dep}' but module directory not found in any /home/odoo/src/* root."
                            if source_available else
                            f"depends[] contains '{dep}' — could not verify: Odoo core/enterprise source is not reachable in this environment."
                        ),
                        fix_hint=(
                            f"Run workflow.find_core_file('{dep}', 'manifest', '{dep}') to verify the exact module name. "
                            "Then correct the depends[] entry."
                            if source_available else
                            "Set ODOOSH_SSH_HOST/ODOOSH_SSH_KEY_PATH to enable core sync, or verify this dependency manually before pushing."
                        ),
                    ))
                else:
                    findings.append(VerifyFinding(
                        file=file_path,
                        severity=VerifySeverity.INFO,
                        check="manifest_depends",
                        message=f"depends[] '{dep}' — verified exists in src roots.",
                        fix_hint="",
                    ))
        findings.extend(self._verify_manifest_assets(file_path, content))
        return findings

    def _verify_manifest_assets(self, file_path: str, content: str) -> list[VerifyFinding]:
        findings: list[VerifyFinding] = []
        try:
            manifest = ast.literal_eval(content)
        except Exception:
            return findings
        if not isinstance(manifest, dict):
            return findings
        assets = manifest.get("assets") or {}
        if not isinstance(assets, dict):
            return findings
        module_root = Path(file_path).parent
        for bundle, entries in assets.items():
            for asset in _flatten_asset_entries(entries):
                if not _asset_path_exists(module_root, asset):
                    findings.append(VerifyFinding(
                        file=file_path,
                        severity=VerifySeverity.CRITICAL,
                        check="manifest_asset_exists",
                        message=f"Asset '{asset}' in bundle '{bundle}' does not resolve to a file/glob from this module.",
                        fix_hint="Fix the asset path or remove it from __manifest__.py before pushing.",
                    ))
                lower_asset = asset.lower()
                if ("point_of_sale" in lower_asset or "/pos" in lower_asset or "pos_" in lower_asset) and bundle != "point_of_sale._assets_pos":
                    findings.append(VerifyFinding(
                        file=file_path,
                        severity=VerifySeverity.CRITICAL,
                        check="manifest_pos_asset_bundle",
                        message=f"POS-looking asset '{asset}' is in '{bundle}', not point_of_sale._assets_pos.",
                        fix_hint="Move POS JS/XML/SCSS assets to point_of_sale._assets_pos unless real Odoo source proves another bundle.",
                    ))
                if ("website" in lower_asset or "snippet" in lower_asset or "editor" in lower_asset) and bundle == "web.assets_backend":
                    findings.append(VerifyFinding(
                        file=file_path,
                        severity=VerifySeverity.WARNING,
                        check="manifest_website_asset_bundle",
                        message=f"Website/editor-looking asset '{asset}' is in backend bundle '{bundle}'.",
                        fix_hint="Verify from website source whether this belongs in web.assets_frontend or website.assets_editor.",
                    ))
        return findings

    # ------------------------------------------------------------------
    # Python model verification
    # ------------------------------------------------------------------

    def _verify_python_model(self, file_path: str, content: str) -> list[VerifyFinding]:
        findings: list[VerifyFinding] = []
        inherit_models: list[str] = []
        for m in _INHERIT_RE.finditer(content):
            inherit_models.extend(_extract_model_literals(m.group(1)))

        for model_name in inherit_models:
            core_content = _find_core_model_content(model_name)
            core_fields: set[str] = set()
            if not core_content:
                findings.append(VerifyFinding(
                    file=file_path,
                    severity=VerifySeverity.WARNING,
                    check="inherit_model_found",
                    message=f"_inherit '{model_name}' — core model source not found in /home/odoo/src/*. Cannot cross-check fields.",
                    fix_hint=f"Run workflow.find_core_file('{model_name.split('.')[0]}', 'model', '{model_name.split('.')[-1]}') manually.",
                ))
            else:
                core_fields = _extract_core_field_names(core_content)
                for dep_m in _DEPENDS_PY_RE.finditer(content):
                    raw = dep_m.group(1)
                    field_refs = [s.strip().strip("'\"") for s in raw.split(",")]
                    for ref in field_refs:
                        first_field = ref.split(".")[0] if "." in ref else ref
                        if first_field and first_field not in core_fields:
                            findings.append(VerifyFinding(
                                file=file_path,
                                severity=VerifySeverity.CRITICAL,
                                check="depends_field_exists",
                                message=f"depends(['{ref}']) — field '{first_field}' not found in core model '{model_name}'.",
                                fix_hint=(
                                    f"Core fields available: {sorted(list(core_fields))[:20]}... "
                                    "Re-read core model with repo.read and use only real field names."
                                ),
                            ))

            for cm_m in _COMODEL_RE.finditer(content):
                comodel = cm_m.group(1) or cm_m.group(2) or ""
                if "." in comodel:
                    comodel_module = comodel.split(".")[0]
                    if not _module_exists_in_src(comodel_module):
                        source_available = _core_source_available()
                        findings.append(VerifyFinding(
                            file=file_path,
                            severity=VerifySeverity.CRITICAL if source_available else VerifySeverity.WARNING,
                            check="comodel_module_exists",
                            message=(
                                f"comodel_name='{comodel}' — module '{comodel_module}' not found in src roots."
                                if source_available else
                                f"comodel_name='{comodel}' — could not verify: Odoo core/enterprise source is not reachable in this environment (no local /home/odoo/src/* tree and no Odoo.sh SSH sync configured)."
                            ),
                            fix_hint=(
                                f"Run workflow.find_core_file('{comodel_module}', 'model', '{comodel.split('.')[-1]}') to verify."
                                if source_available else
                                "Set ODOOSH_SSH_HOST/ODOOSH_SSH_KEY_PATH to enable core sync, or verify this comodel manually before pushing."
                            ),
                        ))

            for rel_m in _RELATED_RE.finditer(content):
                chain = rel_m.group(1)
                first_field = chain.split(".")[0]
                if first_field and core_fields and first_field not in core_fields:
                    findings.append(VerifyFinding(
                        file=file_path,
                        severity=VerifySeverity.CRITICAL,
                        check="related_field_exists",
                        message=f"related='{chain}' — first field '{first_field}' not found in core model '{model_name}'.",
                        fix_hint=f"Valid starting fields in '{model_name}': {sorted(list(core_fields))[:15]}",
                    ))

            compute_methods_used = {m.group(1) for m in _COMPUTE_RE.finditer(content)}
            for method_name in compute_methods_used:
                if method_name and f"def {method_name}" not in content:
                    findings.append(VerifyFinding(
                        file=file_path,
                        severity=VerifySeverity.CRITICAL,
                        check="compute_method_defined",
                        message=f"compute='{method_name}' referenced but 'def {method_name}' not found in this file.",
                        fix_hint=f"Add the missing compute method 'def {method_name}(self):' to the model class.",
                    ))

        if not inherit_models:
            findings.append(VerifyFinding(
                file=file_path,
                severity=VerifySeverity.INFO,
                check="inherit_detected",
                message="No _inherit detected — new model, skipping core field cross-check.",
                fix_hint="",
            ))
        return findings

    # ------------------------------------------------------------------
    # XML / QWeb verification
    # ------------------------------------------------------------------

    def _verify_xml_view(self, file_path: str, content: str) -> list[VerifyFinding]:
        findings: list[VerifyFinding] = []
        inherit_refs = [m.group(1) for m in _INHERIT_ID_RE.finditer(content)]

        model_name: str | None = None
        for ref in inherit_refs:
            parts = ref.split(".")
            if len(parts) >= 2:
                candidate = f"{parts[0]}.{parts[1]}"
                core_content_test = _find_core_view_content(candidate)
                if core_content_test:
                    model_name = candidate
                    break

        core_view_content = _find_core_view_content(model_name) if model_name else ""
        for xpath_m in _XPATH_EXPR_RE.finditer(content):
            expr = xpath_m.group(1)
            # QWeb template xpaths are verified by _verify_qweb_template.
            if "t-inherit" in content and not inherit_refs:
                continue
            if not core_view_content:
                findings.append(VerifyFinding(
                    file=file_path,
                    severity=VerifySeverity.WARNING,
                    check="xpath_core_view_found",
                    message=f"xpath expr='{expr}' — cannot cross-check: core view not found for model '{model_name}'.",
                    fix_hint=f"Run workflow.find_similar_view('{model_name}', 'form') and repo.read the result before using this xpath.",
                ))
                continue

            name_in_expr = re.search(r'@name\s*=\s*["\']([^"\']+)["\']', expr)
            if name_in_expr:
                node_name = name_in_expr.group(1)
                if node_name not in core_view_content:
                    findings.append(VerifyFinding(
                        file=file_path,
                        severity=VerifySeverity.CRITICAL,
                        check="xpath_node_exists",
                        message=f"xpath expr='{expr}' — node with name='{node_name}' not found in core view source.",
                        fix_hint="Re-read the core view with workflow.find_similar_view + repo.read to find the correct node name.",
                    ))
                else:
                    findings.append(VerifyFinding(
                        file=file_path,
                        severity=VerifySeverity.INFO,
                        check="xpath_node_exists",
                        message=f"xpath node name='{node_name}' — confirmed in core view.",
                        fix_hint="",
                    ))

        if model_name and core_view_content:
            field_names_in_xml = {m.group(1) for m in _FIELD_NAME_XML_RE.finditer(content)}
            core_model_content = _find_core_model_content(model_name)
            if core_model_content:
                core_fields = _extract_core_field_names(core_model_content)
                for field_name in field_names_in_xml:
                    if field_name not in core_fields:
                        findings.append(VerifyFinding(
                            file=file_path,
                            severity=VerifySeverity.WARNING,
                            check="xml_field_in_core",
                            message=(
                                f"<field name='{field_name}'> not found in core model '{model_name}'. "
                                "If this is a custom field added by your module, this is expected."
                            ),
                            fix_hint=f"Verify '{field_name}' is defined in your custom module's model file.",
                        ))
        return findings

    def _verify_qweb_template(self, file_path: str, content: str, xml_contents: dict[str, str]) -> list[VerifyFinding]:
        findings: list[VerifyFinding] = []
        template_names = [m.group(1) for m in _T_NAME_RE.finditer(content)]
        inherit_names = [m.group(1) for m in _T_INHERIT_RE.finditer(content)]
        call_names = [m.group(1) for m in _T_CALL_RE.finditer(content)]

        if (template_names or inherit_names) and 'owl="1"' not in content and "owl='1'" not in content:
            findings.append(VerifyFinding(
                file=file_path,
                severity=VerifySeverity.WARNING,
                check="qweb_owl_marker",
                message="QWeb/OWL template has t-name or t-inherit but no owl='1' marker.",
                fix_hint="Verify from installed Odoo source whether this template should use owl='1' for this version/bundle.",
            ))

        for template in inherit_names + call_names:
            source = _find_template_content(template, xml_contents)
            if not source:
                findings.append(VerifyFinding(
                    file=file_path,
                    severity=VerifySeverity.CRITICAL,
                    check="qweb_template_source_exists",
                    message=f"Template reference '{template}' not found in written XML or Odoo source roots.",
                    fix_hint="Run workflow.frontend_pattern_preflight and repo.read the source template before using t-inherit/t-call.",
                ))
                continue
            findings.append(VerifyFinding(
                file=file_path,
                severity=VerifySeverity.INFO,
                check="qweb_template_source_exists",
                message=f"Template reference '{template}' — source found.",
                fix_hint="",
            ))
            for expr in _T_XPATH_RE.findall(content):
                name_in_expr = re.search(r'@name\s*=\s*["\']([^"\']+)["\']', expr)
                if name_in_expr and name_in_expr.group(1) not in source:
                    findings.append(VerifyFinding(
                        file=file_path,
                        severity=VerifySeverity.CRITICAL,
                        check="qweb_xpath_name_exists",
                        message=f"QWeb xpath expr='{expr}' targets name='{name_in_expr.group(1)}' not found in inherited template '{template}'.",
                        fix_hint="Re-read inherited template and target a confirmed node.",
                    ))
                for class_match in _CLASS_IN_EXPR_RE.findall(expr):
                    class_name = class_match[0] or class_match[1]
                    if class_name and class_name not in source:
                        findings.append(VerifyFinding(
                            file=file_path,
                            severity=VerifySeverity.WARNING,
                            check="qweb_xpath_class_exists",
                            message=f"QWeb xpath expr='{expr}' targets class '{class_name}' not found in inherited template '{template}'.",
                            fix_hint="Verify the class from source before using this xpath.",
                        ))
        return findings

    @staticmethod
    def _verify_duplicate_template_names(xml_contents: dict[str, str]) -> list[VerifyFinding]:
        findings: list[VerifyFinding] = []
        seen: dict[str, str] = {}
        for file_path, content in xml_contents.items():
            for name in _T_NAME_RE.findall(content):
                if name in seen:
                    findings.append(VerifyFinding(
                        file=file_path,
                        severity=VerifySeverity.CRITICAL,
                        check="qweb_duplicate_t_name",
                        message=f"Duplicate t-name '{name}' also appears in {seen[name]}.",
                        fix_hint="Use a unique template name per XML asset.",
                    ))
                else:
                    seen[name] = file_path
        return findings

    # ------------------------------------------------------------------
    # JS/OWL/SCSS verification
    # ------------------------------------------------------------------

    def _verify_js_owl(self, file_path: str, content: str, template_names: set[str]) -> list[VerifyFinding]:
        findings: list[VerifyFinding] = []
        modern_signals = ("from \"@" in content or "from '@" in content or "import " in content or "registry.category" in content or "Component" in content)

        if _POS_CONFIG_LOAD_FIELDS_RE.search(content):
            findings.append(VerifyFinding(
                file=file_path,
                severity=VerifySeverity.CRITICAL,
                check="pos_config_no_load_fields",
                message="POS code must not use load_fields('pos.config', ...).",
                fix_hint=(
                    "Read the installed point_of_sale source first and extend the correct loader/backend pattern. "
                    "For pos.config data, use the Odoo version's real loader params/model-loading mechanism instead of legacy load_fields."
                ),
            ))

        if modern_signals and "/** @odoo-module **/" not in content:
            findings.append(VerifyFinding(
                file=file_path,
                severity=VerifySeverity.CRITICAL,
                check="js_odoo_module_header",
                message="Modern Odoo JS/OWL file is missing /** @odoo-module **/ header.",
                fix_hint="Add the Odoo module header at the top, matching installed Odoo source style.",
            ))
        if "odoo.define(" in content and modern_signals:
            findings.append(VerifyFinding(
                file=file_path,
                severity=VerifySeverity.CRITICAL,
                check="js_no_mixed_legacy_modern",
                message="File mixes legacy odoo.define/require with modern ES module/OWL syntax.",
                fix_hint="Use one pattern only. Read the installed Odoo source and match its style for this component.",
            ))

        for import_match in _JS_IMPORT_RE.finditer(content):
            import_path = import_match.group(1) or import_match.group(2) or ""
            if not import_path or import_path in _EXTERNAL_IMPORTS or import_path.startswith((".", "/")):
                continue
            if import_path.startswith("@"):
                candidates = _resolve_odoo_alias(import_path)
                if not candidates:
                    if _known_odoo_alias(import_path) and not _core_source_available():
                        findings.append(VerifyFinding(
                            file=file_path,
                            severity=VerifySeverity.WARNING,
                            check="js_import_resolves",
                            message=f"Import '{import_path}' could not be verified: Odoo core source is not reachable in this environment (no local /home/odoo/src/* tree and no Odoo.sh SSH sync configured).",
                            fix_hint="Set ODOOSH_SSH_HOST/ODOOSH_SSH_KEY_PATH to enable core sync, or verify this import manually before pushing.",
                        ))
                    else:
                        findings.append(VerifyFinding(
                            file=file_path,
                            severity=VerifySeverity.WARNING,
                            check="js_import_alias_known",
                            message=f"Import alias '{import_path}' is not in the verifier's known Odoo aliases.",
                            fix_hint="Read real Odoo source and verify this alias exists before pushing.",
                        ))
                elif not any(candidate.exists() for candidate in candidates):
                    source_available = _core_source_available()
                    findings.append(VerifyFinding(
                        file=file_path,
                        severity=VerifySeverity.CRITICAL if source_available else VerifySeverity.WARNING,
                        check="js_import_resolves",
                        message=(
                            f"Import '{import_path}' did not resolve to any source file in /home/odoo/src roots."
                            if source_available else
                            f"Import '{import_path}' could not be verified: Odoo core source is not reachable in this environment (no local /home/odoo/src/* tree and no Odoo.sh SSH sync configured)."
                        ),
                        fix_hint=(
                            "Correct the import path after reading the installed Odoo source file that exports it."
                            if source_available else
                            "Set ODOOSH_SSH_HOST/ODOOSH_SSH_KEY_PATH to enable core sync, or verify this import manually before pushing."
                        ),
                    ))
                else:
                    findings.append(VerifyFinding(
                        file=file_path,
                        severity=VerifySeverity.INFO,
                        check="js_import_resolves",
                        message=f"Import '{import_path}' resolved in source roots.",
                        fix_hint="",
                    ))

        used_templates = {m.group(1) for m in _JS_TEMPLATE_RE.finditer(content)}
        for template in used_templates:
            if template not in template_names and not _find_template_content(template):
                findings.append(VerifyFinding(
                    file=file_path,
                    severity=VerifySeverity.WARNING,
                    check="js_template_exists",
                    message=f"Component template '{template}' not found in changed XML files or Odoo source roots.",
                    fix_hint="Add/read the matching XML template and ensure t-name equals Component.template/static template.",
                ))

        if "useService(" in content and "setup(" not in content:
            findings.append(VerifyFinding(
                file=file_path,
                severity=VerifySeverity.WARNING,
                check="js_use_service_setup",
                message="useService() appears but no setup() method/function was detected in this file.",
                fix_hint="Confirm hook usage from source; hooks normally run inside setup().",
            ))
        if "useState(" in content and "setup(" not in content:
            findings.append(VerifyFinding(
                file=file_path,
                severity=VerifySeverity.WARNING,
                check="js_use_state_setup",
                message="useState() appears but no setup() method/function was detected in this file.",
                fix_hint="Confirm hook usage from source; hooks normally run inside setup().",
            ))

        lower_path = file_path.lower()
        if ("point_of_sale" in lower_path or "/pos" in lower_path) and "@point_of_sale/" not in content and "odoo.define(" not in content:
            findings.append(VerifyFinding(
                file=file_path,
                severity=VerifySeverity.WARNING,
                check="js_pos_pattern_source",
                message="POS JS file does not import from @point_of_sale and is not legacy odoo.define.",
                fix_hint="Verify this exact POS pattern from point_of_sale/static/src before pushing.",
            ))
        return findings

    @staticmethod
    def _verify_js_syntax_real(file_path: str, content: str) -> list[VerifyFinding]:
        """Actually parse the JS file with Node instead of regex heuristics.

        Regex checks (js_odoo_module_header, js_no_mixed_legacy_modern, etc.)
        can never catch a plain broken-syntax file (unmatched brace, stray
        token, bad destructuring) because they only look for known substrings.
        This runs `node --check` against the real file content, which is a
        full parse and will fail on any genuine syntax error, with an exact
        line number. Odoo OWL/JS files are ES modules, so the temp file is
        written with a .mjs extension — Node infers module parsing from the
        extension without needing --input-type (which only works on stdin).

        If Node is not installed in this environment, this degrades to a
        single INFO finding rather than crashing verify_against_core.
        """
        import shutil
        import subprocess
        import tempfile

        findings: list[VerifyFinding] = []
        node_path = shutil.which("node")
        if not node_path:
            findings.append(VerifyFinding(
                file=file_path,
                severity=VerifySeverity.INFO,
                check="js_real_syntax_check_skipped",
                message="Node.js not found in this environment; falling back to regex-only JS checks.",
                fix_hint="Install Node.js in the container so `node --check` can catch real JS syntax errors.",
            ))
            return findings

        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs", delete=False, encoding="utf-8") as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            completed = subprocess.run(
                [node_path, "--check", tmp_path],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if completed.returncode != 0:
                stderr = completed.stderr or ""
                line_no = None
                line_match = re.search(re.escape(tmp_path) + r":(\d+)", stderr)
                if line_match:
                    line_no = int(line_match.group(1))
                error_line = next(
                    (ln for ln in stderr.splitlines() if "Error" in ln or "SyntaxError" in ln),
                    stderr.strip().splitlines()[-1] if stderr.strip() else "Unknown syntax error",
                )
                findings.append(VerifyFinding(
                    file=file_path,
                    severity=VerifySeverity.CRITICAL,
                    check="js_real_syntax_error",
                    message=f"Node failed to parse this JS file: {error_line.strip()}",
                    line=line_no,
                    fix_hint="This is a genuine JS syntax error (not a style/pattern warning) — fix the exact line before anything else.",
                ))
        except subprocess.TimeoutExpired:
            findings.append(VerifyFinding(
                file=file_path,
                severity=VerifySeverity.WARNING,
                check="js_real_syntax_check_timeout",
                message="Node syntax check timed out after 10s.",
                fix_hint="File may be unusually large or Node startup is slow in this environment; re-run if this persists.",
            ))
        except Exception as exc:  # noqa: BLE001 - this check must never crash verify_against_core
            findings.append(VerifyFinding(
                file=file_path,
                severity=VerifySeverity.INFO,
                check="js_real_syntax_check_error",
                message=f"Could not run real JS syntax check: {exc}",
            ))
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass
        return findings

    @staticmethod
    def _verify_scss_asset(file_path: str, content: str) -> list[VerifyFinding]:
        findings: list[VerifyFinding] = []
        if "var(--" in content and "min(" in content:
            findings.append(VerifyFinding(
                file=file_path,
                severity=VerifySeverity.WARNING,
                check="scss_css_min_with_vars",
                message="SCSS contains min() with CSS variables; Odoo sass compilation may treat min as a Sass function.",
                fix_hint="Use calc(), clamp(), or plain CSS values; avoid Sass min/max with var() in Odoo assets.",
            ))
        return findings
