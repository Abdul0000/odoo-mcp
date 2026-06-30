"""V14+ — Pre-Push Syntax Verifier: cross-check written files against Odoo core.

Pillar 3 of the Mandatory Dual Read system:
  Before every git.push the agent MUST call workflow.verify_against_core.
  This service reads the written custom files, compares them against core
  source files already on disk, and blocks push if mismatches are found.

What is verified:
  Python model files (.py):
    - _inherit model names exist in core
    - Field names used in depends=[], comodel_name=, related= match core fields
    - No hallucinated field names (fields not present in core model)

  XML view files (.xml):
    - xpath expr= attributes reference real node IDs found in core view
    - field name= attributes reference real field names from core model
    - inherit_id ref= references a real view ID in core

  Manifest files (__manifest__.py):
    - module names in depends=[] exist in /home/odoo/src/{user,enterprise,odoo}

Push is BLOCKED if any CRITICAL mismatches are found.
Max 3 fix cycles are tracked; after that the service reports and halts.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import core_sync_service
from ..schemas import (
    SyntaxVerifyRequest,
    SyntaxVerifyResult,
    VerifyFinding,
    VerifySeverity,
)

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------
_FIELD_NAME_RE    = re.compile(r'^\s{4,}(\w+)\s*=\s*fields\.', re.MULTILINE)
_INHERIT_RE       = re.compile(r'_inherit\s*=\s*["\']([^"\']+)["\']')
_DEPENDS_PY_RE    = re.compile(r'depends\(\[([^\]]+)\]\)')
_COMODEL_RE       = re.compile(r'comodel_name\s*=\s*["\']([^"\']+)["\']')
_RELATED_RE       = re.compile(r'related\s*=\s*["\']([^"\']+)["\']')
_COMPUTE_RE       = re.compile(r'compute\s*=\s*["\']([^"\']+)["\']')
_XPATH_EXPR_RE    = re.compile(r'expr=["\']([^"\']+)["\']')
_FIELD_NAME_XML_RE= re.compile(r'<field\s+name=["\'](\w+)["\']')
_INHERIT_ID_RE    = re.compile(r'inherit_id\s+ref=["\']([^"\']+)["\']')
_MANIFEST_DEPS_RE = re.compile(r"'depends'\s*:\s*\[([^\]]+)\]", re.DOTALL)
_MODULE_NAME_RE   = re.compile(r"'(\w+)'")

# Search roots (same as CoreFinderService)
_SRC_ROOTS = [
    Path("/home/odoo/src/user"),
    Path("/home/odoo/src/enterprise"),
    Path("/home/odoo/src/odoo"),
]


def _read_file(path: str, max_bytes: int = 524288) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read(max_bytes)
    except OSError:
        return ""


def _find_core_model_content(model_name: str) -> str:
    """Return concatenated content of core model files for a given model."""
    module = model_name.split(".")[0]
    keyword = model_name.replace(".", "_")
    contents: list[str] = []
    for root in _SRC_ROOTS:
        models_dir = root / module / "models"
        if not models_dir.exists():
            core_sync_service.ensure_module_synced(str(root), module)
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


def _find_core_view_content(model_name: str) -> str:
    """Return concatenated content of core view XML files for a model."""
    module = model_name.split(".")[0]
    hints = [model_name.replace(".", "_"), model_name.split(".")[-1]]
    contents: list[str] = []
    for root in _SRC_ROOTS:
        views_dir = root / module / "views"
        if not views_dir.exists():
            core_sync_service.ensure_module_synced(str(root), module)
        if not views_dir.exists():
            continue
        for xml_file in views_dir.rglob("*.xml"):
            fname_lower = xml_file.name.lower()
            if any(h in fname_lower for h in hints):
                contents.append(_read_file(str(xml_file)))
    return "\n".join(contents)


def _module_exists_in_src(module_name: str) -> bool:
    """Check whether a module directory exists anywhere in src roots."""
    for root in _SRC_ROOTS:
        if (root / module_name).is_dir():
            return True
        if core_sync_service.ensure_module_synced(str(root), module_name) and (root / module_name).is_dir():
            return True
    return False


class SyntaxVerifierService:
    """Pre-push cross-checker: verify written files against Odoo core source.

    Usage (workflow.verify_against_core):
      Pass the list of absolute paths of files the agent wrote.
      Returns SyntaxVerifyResult with findings and push_blocked flag.

    Push is BLOCKED when any finding has severity=CRITICAL.
    """

    def verify(self, request: SyntaxVerifyRequest) -> SyntaxVerifyResult:
        findings: list[VerifyFinding] = []
        files_checked: list[str] = []

        for file_path in request.written_files:
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

            if path.name == "__manifest__.py":
                findings.extend(self._verify_manifest(file_path, content))
            elif path.suffix == ".py":
                findings.extend(self._verify_python_model(file_path, content))
            elif path.suffix == ".xml":
                findings.extend(self._verify_xml_view(file_path, content))

        push_blocked = any(f.severity == VerifySeverity.CRITICAL for f in findings)
        critical_count = sum(1 for f in findings if f.severity == VerifySeverity.CRITICAL)
        warning_count  = sum(1 for f in findings if f.severity == VerifySeverity.WARNING)
        info_count     = sum(1 for f in findings if f.severity == VerifySeverity.INFO)

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

    # ------------------------------------------------------------------
    # Manifest verification
    # ------------------------------------------------------------------

    def _verify_manifest(self, file_path: str, content: str) -> list[VerifyFinding]:
        findings: list[VerifyFinding] = []
        m = _MANIFEST_DEPS_RE.search(content)
        if not m:
            return findings
        deps_block = m.group(1)
        dep_names = [n.group(1) for n in _MODULE_NAME_RE.finditer(deps_block)]
        for dep in dep_names:
            if dep in {"base", "web", "mail", "sale", "purchase", "account", "stock", "hr"}:
                # common well-known modules — always pass without disk check
                continue
            if not _module_exists_in_src(dep):
                findings.append(VerifyFinding(
                    file=file_path,
                    severity=VerifySeverity.CRITICAL,
                    check="manifest_depends",
                    message=f"depends[] contains '{dep}' but module directory not found in any /home/odoo/src/* root.",
                    fix_hint=(
                        f"Run: workflow.find_core_file('{dep}', 'manifest', '{dep}') to verify the exact module name. "
                        "Then correct the depends[] entry."
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
        return findings

    # ------------------------------------------------------------------
    # Python model verification
    # ------------------------------------------------------------------

    def _verify_python_model(self, file_path: str, content: str) -> list[VerifyFinding]:
        findings: list[VerifyFinding] = []

        # Find all _inherit models
        inherit_models = [m.group(1) for m in _INHERIT_RE.finditer(content)]

        for model_name in inherit_models:
            core_content = _find_core_model_content(model_name)
            if not core_content:
                findings.append(VerifyFinding(
                    file=file_path,
                    severity=VerifySeverity.WARNING,
                    check="inherit_model_found",
                    message=f"_inherit '{model_name}' — core model source not found in /home/odoo/src/*. Cannot cross-check fields.",
                    fix_hint=f"Run workflow.find_core_file('{model_name.split('.')[0]}', 'model', '{model_name.split('.')[-1]}') manually.",
                ))
                continue

            core_fields = _extract_core_field_names(core_content)

            # Check depends=[] field chains
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
                                f"Re-read core model with repo.read and use only real field names."
                            ),
                        ))

            # Check comodel_name references
            for cm_m in _COMODEL_RE.finditer(content):
                comodel = cm_m.group(1)
                if "." in comodel:
                    comodel_module = comodel.split(".")[0]
                    if not _module_exists_in_src(comodel_module):
                        findings.append(VerifyFinding(
                            file=file_path,
                            severity=VerifySeverity.CRITICAL,
                            check="comodel_module_exists",
                            message=f"comodel_name='{comodel}' — module '{comodel_module}' not found in src roots.",
                            fix_hint=f"Run workflow.find_core_file('{comodel_module}', 'model', '{comodel.split('.')[-1]}') to verify.",
                        ))

            # Check related= field chains
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

            # Check compute= method exists in file itself
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
    # XML view verification
    # ------------------------------------------------------------------

    def _verify_xml_view(self, file_path: str, content: str) -> list[VerifyFinding]:
        findings: list[VerifyFinding] = []

        # Find inherit_id ref to get the base model context
        inherit_refs = [m.group(1) for m in _INHERIT_ID_RE.finditer(content)]

        # Get model name from the first inherit ref (e.g. 'sale.order.form.view' -> 'sale.order')
        model_name: str | None = None
        for ref in inherit_refs:
            parts = ref.split(".")
            if len(parts) >= 2:
                # Heuristic: first two parts are likely module.model_ish
                candidate = f"{parts[0]}.{parts[1]}"
                core_content_test = _find_core_view_content(candidate)
                if core_content_test:
                    model_name = candidate
                    break

        core_view_content = _find_core_view_content(model_name) if model_name else ""

        # Check xpath expr= references
        for xpath_m in _XPATH_EXPR_RE.finditer(content):
            expr = xpath_m.group(1)
            if not core_view_content:
                findings.append(VerifyFinding(
                    file=file_path,
                    severity=VerifySeverity.WARNING,
                    check="xpath_core_view_found",
                    message=f"xpath expr='{expr}' — cannot cross-check: core view not found for model '{model_name}'.",
                    fix_hint=f"Run workflow.find_similar_view('{model_name}', 'form') and repo.read the result before using this xpath.",
                ))
                continue

            # Extract node identifier from xpath (last @name or last element name)
            name_in_expr = re.search(r'@name\s*=\s*["\']([^"\']+)["\']', expr)
            if name_in_expr:
                node_name = name_in_expr.group(1)
                if node_name not in core_view_content:
                    findings.append(VerifyFinding(
                        file=file_path,
                        severity=VerifySeverity.CRITICAL,
                        check="xpath_node_exists",
                        message=f"xpath expr='{expr}' — node with name='{node_name}' not found in core view source.",
                        fix_hint=(
                            f"Re-read the core view with workflow.find_similar_view + repo.read "
                            f"to find the correct node name. The node '{node_name}' does not exist."
                        ),
                    ))
                else:
                    findings.append(VerifyFinding(
                        file=file_path,
                        severity=VerifySeverity.INFO,
                        check="xpath_node_exists",
                        message=f"xpath node name='{node_name}' — confirmed in core view.",
                        fix_hint="",
                    ))

        # Check <field name="..."> references against core model fields
        if model_name and core_view_content:
            field_names_in_xml = {m.group(1) for m in _FIELD_NAME_XML_RE.finditer(content)}
            core_model_content = _find_core_model_content(model_name)
            if core_model_content:
                core_fields = _extract_core_field_names(core_model_content)
                for field_name in field_names_in_xml:
                    if field_name not in core_fields:
                        # Could be a custom field — warn but not block (it might be defined in the same module)
                        findings.append(VerifyFinding(
                            file=file_path,
                            severity=VerifySeverity.WARNING,
                            check="xml_field_in_core",
                            message=(
                                f"<field name='{field_name}'> not found in core model '{model_name}'. "
                                "If this is a custom field added by your module, this is expected."
                            ),
                            fix_hint=(
                                f"Verify '{field_name}' is defined in your custom module's model file. "
                                "If it should be a core field, check the spelling."
                            ),
                        ))

        return findings
