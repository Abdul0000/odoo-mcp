from __future__ import annotations

import ast
import csv
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..schemas import Finding, Severity

COMMON_ODOO_FIELDS = {
    "id", "display_name", "create_uid", "create_date", "write_uid", "write_date", "__last_update",
    "message_ids", "message_follower_ids", "message_partner_ids", "activity_ids", "activity_state",
    "activity_type_id", "activity_user_id", "activity_date_deadline", "activity_exception_decoration",
    "active", "sequence", "company_id",
}
FIELD_TYPES_WITH_COMODEL = {"Many2one", "One2many", "Many2many"}
INHERIT_DEPENDS = {
    "account.": "account",
    "sale.": "sale",
    "purchase.": "purchase",
    "stock.": "stock",
    "hr.": "hr",
    "product.": "product",
    "payment.": "payment",
    "website.": "website",
    "calendar.": "calendar",
}

# Lightweight transitive dependency hints used only to reduce noisy static
# warnings.  Odoo validates real dependencies during module install/upgrade;
# these hints stop style-only tasks from being polluted by warnings such as
# ``website_sale`` not directly depending on ``sale``.
TRANSITIVE_DEPENDS_HINTS = {
    "website_sale": {"website", "sale", "product"},
    "sale_management": {"sale", "product"},
    "stock_account": {"stock", "account", "product"},
    "mrp": {"stock", "product"},
    "website_payment": {"website", "payment"},
}


@dataclass
class ParsedModel:
    technical_name: str
    inherited_model: str | None = None
    fields: set[str] = field(default_factory=set)
    relations: dict[str, str] = field(default_factory=dict)
    transient: bool = False
    file: Path | None = None


class OdooModuleValidator:
    """Static checks that catch common module errors before Odoo.sh build time."""

    def validate(self, module_path: Path, task_description: str = "") -> list[Finding]:
        findings: list[Finding] = []
        findings.extend(self._validate_basic_structure(module_path))
        manifest, manifest_findings = self._read_manifest(module_path)
        findings.extend(manifest_findings)
        findings.extend(self._validate_manifest_files(module_path, manifest))
        findings.extend(self._validate_import_closure(module_path))
        findings.extend(self._validate_manifest_registration(module_path, manifest))
        findings.extend(self._validate_python_files(module_path))
        findings.extend(self._validate_xml_files(module_path))
        findings.extend(self._validate_security_csv(module_path))
        findings.extend(self._detect_pyc(module_path))
        parsed_models = self._parse_custom_models(module_path)
        findings.extend(self._validate_missing_access(module_path, parsed_models))
        findings.extend(self._validate_manifest_dependency_hints(module_path, manifest, parsed_models))
        findings.extend(self._validate_xml_references_and_order(module_path, manifest, parsed_models))
        findings.extend(self._validate_custom_view_fields(module_path, parsed_models))
        findings.extend(self._validate_requested_features(module_path, manifest, parsed_models, task_description))
        return findings

    def _validate_basic_structure(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        if not module_path.exists():
            return [Finding(severity=Severity.error, code="MODULE_NOT_FOUND", message=f"Module path does not exist: {module_path}", hint="Check project_path and module_name.")]
        if not module_path.is_dir():
            return [Finding(severity=Severity.error, code="MODULE_NOT_DIRECTORY", message=f"Module path is not a directory: {module_path}")]
        manifest = module_path / "__manifest__.py"
        if not manifest.exists():
            findings.append(Finding(severity=Severity.error, code="MISSING_MANIFEST", message="Missing __manifest__.py", file=str(manifest)))
        init_file = module_path / "__init__.py"
        if not init_file.exists():
            findings.append(Finding(severity=Severity.error, code="MISSING_INIT", message="Missing mandatory __init__.py", file=str(init_file), hint="Create __init__.py before commit/push."))
        return findings

    def _read_manifest(self, module_path: Path) -> tuple[dict, list[Finding]]:
        manifest_path = module_path / "__manifest__.py"
        if not manifest_path.exists():
            return {}, []
        try:
            manifest = ast.literal_eval(manifest_path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            return {}, [Finding(severity=Severity.error, code="MANIFEST_SYNTAX_ERROR", message=str(exc), file=str(manifest_path), line=exc.lineno)]
        except Exception as exc:  # noqa: BLE001
            return {}, [Finding(severity=Severity.error, code="MANIFEST_PARSE_ERROR", message=f"Could not parse manifest as a Python literal dict: {exc}", file=str(manifest_path))]
        if not isinstance(manifest, dict):
            return {}, [Finding(severity=Severity.error, code="MANIFEST_NOT_DICT", message="Manifest must be a Python dictionary.", file=str(manifest_path))]
        findings: list[Finding] = []
        for required_key in ("name", "version", "depends"):
            if required_key not in manifest:
                findings.append(Finding(severity=Severity.error, code="MANIFEST_MISSING_KEY", message=f"Manifest is missing mandatory key: {required_key}", file=str(manifest_path)))
        depends = manifest.get("depends", [])
        if not isinstance(depends, list) or not all(isinstance(dep, str) for dep in depends):
            findings.append(Finding(severity=Severity.error, code="MANIFEST_BAD_DEPENDS", message="Manifest 'depends' must be a list of module technical names.", file=str(manifest_path)))
        return manifest, findings

    def _validate_manifest_files(self, module_path: Path, manifest: dict) -> list[Finding]:
        findings: list[Finding] = []
        manifest_path = module_path / "__manifest__.py"
        for key in ("data", "demo", "qweb"):
            entries = manifest.get(key, []) or []
            if not isinstance(entries, list):
                findings.append(Finding(severity=Severity.error, code="MANIFEST_BAD_FILE_LIST", message=f"Manifest '{key}' must be a list of file paths.", file=str(manifest_path)))
                continue
            for rel_path in entries:
                if not isinstance(rel_path, str):
                    findings.append(Finding(severity=Severity.error, code="MANIFEST_BAD_FILE_ENTRY", message=f"Manifest '{key}' contains a non-string file entry.", file=str(manifest_path)))
                    continue
                target = module_path / rel_path
                if not target.exists():
                    findings.append(Finding(severity=Severity.error, code="MANIFEST_FILE_MISSING", message=f"Manifest references missing file: {rel_path}", file=str(manifest_path), hint="Create the file or remove it from the manifest."))
        return findings

    @staticmethod
    def _relative_imports(init_path: Path) -> set[str]:
        try:
            tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
        except Exception:
            return set()
        imported: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level < 1:
                continue
            if node.module:
                imported.add(node.module.split(".", 1)[0])
            else:
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        return imported

    def _validate_import_closure(self, module_path: Path) -> list[Finding]:
        """Require every functional Python package/file to be reachable from root."""
        findings: list[Finding] = []
        excluded = {"tests", "test", "migrations", "static", "__pycache__"}
        directories = [module_path]
        directories.extend(
            path for path in module_path.rglob("*")
            if path.is_dir() and not any(part in excluded or part.startswith(".") for part in path.relative_to(module_path).parts)
        )
        for directory in directories:
            py_modules = sorted(path.stem for path in directory.glob("*.py") if path.name not in {"__init__.py", "__manifest__.py", "__openerp__.py"})
            child_packages = sorted(
                child.name for child in directory.iterdir()
                if child.is_dir() and child.name not in excluded and not child.name.startswith(".")
                and any(path.is_file() and path.suffix == ".py" for path in child.rglob("*.py"))
            )
            expected = set(py_modules + child_packages)
            if not expected:
                continue
            init_path = directory / "__init__.py"
            if not init_path.is_file():
                findings.append(Finding(
                    severity=Severity.error,
                    code="PYTHON_PACKAGE_INIT_MISSING",
                    message=f"Python package is missing __init__.py: {directory.relative_to(module_path)}",
                    file=str(init_path),
                    hint=f"Create __init__.py and import: {', '.join(sorted(expected))}.",
                ))
                continue
            missing = expected - self._relative_imports(init_path)
            if missing:
                findings.append(Finding(
                    severity=Severity.error,
                    code="PYTHON_IMPORT_CLOSURE_MISSING",
                    message=f"{init_path.relative_to(module_path)} does not import: {', '.join(sorted(missing))}",
                    file=str(init_path),
                    hint="Add relative imports before commit/push.",
                ))
        return findings

    def _validate_manifest_registration(self, module_path: Path, manifest: dict) -> list[Finding]:
        """Require deployable XML/CSV files to be registered in manifest data."""
        findings: list[Finding] = []
        entries = [item.replace("\\", "/") for item in (manifest.get("data") or []) if isinstance(item, str)]
        entry_set = set(entries)
        structural_dirs = {"security", "views", "data", "report", "reports", "wizard", "wizards"}
        for path in module_path.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".xml", ".csv"}:
                continue
            rel = path.relative_to(module_path).as_posix()
            if not path.relative_to(module_path).parts or path.relative_to(module_path).parts[0] not in structural_dirs:
                continue
            if rel not in entry_set:
                findings.append(Finding(
                    severity=Severity.error,
                    code="MANIFEST_FILE_UNREGISTERED",
                    message=f"Module file is not registered in manifest data: {rel}",
                    file=str(module_path / "__manifest__.py"),
                    hint=f"Add '{rel}' to manifest data in dependency/load order.",
                ))
        first_non_security = next((index for index, item in enumerate(entries) if not item.startswith("security/")), len(entries))
        late_security = [item for index, item in enumerate(entries) if item.startswith("security/") and index > first_non_security]
        if late_security:
            findings.append(Finding(
                severity=Severity.error,
                code="MANIFEST_SECURITY_ORDER",
                message=f"Security files must load before views/data: {', '.join(late_security)}",
                file=str(module_path / "__manifest__.py"),
            ))
        return findings

    def _validate_requested_features(
        self,
        module_path: Path,
        manifest: dict,
        parsed_models: dict[str, ParsedModel],
        task_description: str,
    ) -> list[Finding]:
        """Block a 'complete module' response that silently omits requested layers."""
        text = str(task_description or "").lower()
        complete = bool(re.search(r"\b(?:complete|full)\s+(?:odoo\s+)?(?:module|addon)\b|\bmvp\b", text))
        if not complete:
            return []
        findings: list[Finding] = []
        entries = [item for item in (manifest.get("data") or []) if isinstance(item, str)]
        checks = {
            "models": (bool(parsed_models), "models with imported Python files"),
            "security": (any(item.startswith("security/") for item in entries), "security XML/ACL files registered in manifest"),
            "views": (any(item.startswith(("views/", "wizard/", "wizards/")) for item in entries), "views registered in manifest"),
            "menu": (any("menuitem" in path.read_text(encoding="utf-8", errors="ignore") for path in module_path.rglob("*.xml")), "menu XML"),
            "report": (any(item.startswith(("report/", "reports/")) for item in entries), "report files registered in manifest"),
            "dashboard": (any("dashboard" in item.lower() for item in entries) or any("dashboard" in path.name.lower() for path in module_path.rglob("*")), "dashboard implementation"),
            "demo": (bool(manifest.get("demo")), "demo files registered in manifest demo"),
            "automation": (any("ir.cron" in path.read_text(encoding="utf-8", errors="ignore") for path in module_path.rglob("*.xml")), "automation/ir.cron data"),
        }
        for keyword, (present, requirement) in checks.items():
            if keyword in text and not present:
                findings.append(Finding(
                    severity=Severity.error,
                    code="REQUESTED_MODULE_LAYER_MISSING",
                    message=f"Complete-module request includes '{keyword}' but the module lacks {requirement}.",
                    file=str(module_path),
                    hint="Complete the requested layer before commit/push.",
                ))
        functional_files = [path for path in module_path.rglob("*") if path.is_file() and path.name not in {"__init__.py", "__manifest__.py"}]
        if not functional_files:
            findings.append(Finding(
                severity=Severity.error,
                code="COMPLETE_MODULE_EMPTY",
                message="Complete-module request produced only scaffold files and no functional implementation.",
                file=str(module_path),
            ))
        return findings

    # Fix 8: Odoo-specific wrong-import patterns that pass Python syntax but fail at runtime.
    # Keyed by the bad import string → (correct_alternative, severity).
    _ODOO_WRONG_IMPORTS: dict[str, tuple[str, Severity]] = {
        # Wrong attribute access paths inside odoo package
        "from odoo.models import BaseModel": ("from odoo import models", Severity.error),
        "from odoo.models import Model": ("from odoo import models", Severity.error),
        "from odoo.fields import Field": ("from odoo import fields  # then use fields.Char etc.", Severity.error),
        "from odoo.api import Environment": ("from odoo import api  # env is self.env", Severity.warning),
        "from odoo.exceptions import UserError": (None, Severity.info),  # this one IS correct, skip
        # Common wrong form: importing Command from wrong place
        "from odoo import Command": ("from odoo.fields import Command", Severity.error),
        # Relative import of odoo internals
        "import odoo.models as models": ("from odoo import models", Severity.warning),
        "import odoo.fields as fields": ("from odoo import fields", Severity.warning),
        # Deprecated / removed in 17+
        "from odoo.addons.base.models.ir_ui_view import": (
            "Avoid importing ir.ui.view internals; use self.env.ref() instead.", Severity.warning
        ),
    }

    # Correct patterns — must NOT be flagged
    _ODOO_CORRECT_IMPORTS: set[str] = {
        "from odoo import models, fields, api",
        "from odoo import models",
        "from odoo import fields",
        "from odoo import api",
        "from odoo import _",
        "from odoo import tools",
        "from odoo import SUPERUSER_ID",
        "from odoo.fields import Command",
        "from odoo.exceptions import UserError",
        "from odoo.exceptions import ValidationError",
        "from odoo.exceptions import AccessError",
        "from odoo.tools import",
        "from odoo.tools.translate import",
        "from odoo.http import",
        "from odoo.addons.",
    }

    def _validate_python_files(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        for py_file in self._iter_files(module_path, "*.py"):
            if "__pycache__" in py_file.parts:
                continue
            try:
                source = py_file.read_text(encoding="utf-8")
                ast.parse(source, filename=str(py_file))
            except SyntaxError as exc:
                findings.append(Finding(severity=Severity.error, code="PYTHON_SYNTAX_ERROR", message=str(exc), file=str(py_file), line=exc.lineno))
                continue
            except UnicodeDecodeError as exc:
                findings.append(Finding(severity=Severity.error, code="PYTHON_ENCODING_ERROR", message=str(exc), file=str(py_file)))
                continue
            # Fix 8: validate Odoo-specific import patterns line-by-line
            findings.extend(self._validate_odoo_imports(py_file, source))
        return findings

    def _validate_odoo_imports(self, py_file: Path, source: str) -> list[Finding]:
        """Check for wrong Odoo import paths that pass Python syntax but blow up at runtime."""
        findings: list[Finding] = []
        for lineno, raw_line in enumerate(source.splitlines(), start=1):
            line = raw_line.strip()
            if not line.startswith(("import ", "from ")):
                continue
            # Skip lines that are known-correct
            if any(line.startswith(pat) for pat in self._ODOO_CORRECT_IMPORTS):
                continue
            # Match against known-wrong patterns
            for wrong_pat, (fix_hint, severity) in self._ODOO_WRONG_IMPORTS.items():
                if wrong_pat == "from odoo.exceptions import UserError":
                    continue  # marked as info/skip above
                if line.startswith(wrong_pat) or wrong_pat in line:
                    hint = f"Use instead: {fix_hint}" if fix_hint else None
                    findings.append(Finding(
                        severity=severity,
                        code="ODOO_WRONG_IMPORT",
                        message=f"Wrong Odoo import pattern: '{line}' — this will fail at module load.",
                        file=str(py_file),
                        line=lineno,
                        hint=hint,
                    ))
        return findings

    def _validate_xml_files(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        for xml_file in self._iter_files(module_path, "*.xml"):
            try:
                ET.parse(xml_file)
            except ET.ParseError as exc:
                line, _col = exc.position
                findings.append(Finding(severity=Severity.error, code="XML_PARSE_ERROR", message=str(exc), file=str(xml_file), line=line))
            except UnicodeDecodeError as exc:
                findings.append(Finding(severity=Severity.error, code="XML_ENCODING_ERROR", message=str(exc), file=str(xml_file)))
        return findings

    def _validate_security_csv(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        expected_header = ["id", "name", "model_id:id", "group_id:id", "perm_read", "perm_write", "perm_create", "perm_unlink"]
        for csv_file in self._iter_files(module_path, "*.csv"):
            if csv_file.name != "ir.model.access.csv":
                continue
            try:
                with csv_file.open(newline="", encoding="utf-8") as handle:
                    reader = csv.DictReader(handle)
                    if reader.fieldnames != expected_header:
                        findings.append(Finding(severity=Severity.error, code="ACCESS_CSV_BAD_HEADER", message="ir.model.access.csv header is not the standard Odoo access header.", file=str(csv_file), hint=",".join(expected_header)))
                    for row_index, row in enumerate(reader, start=2):
                        for perm in ("perm_read", "perm_write", "perm_create", "perm_unlink"):
                            if row.get(perm) not in {"0", "1"}:
                                findings.append(Finding(severity=Severity.error, code="ACCESS_CSV_BAD_PERMISSION", message=f"{perm} must be 0 or 1.", file=str(csv_file), line=row_index))
            except UnicodeDecodeError as exc:
                findings.append(Finding(severity=Severity.error, code="ACCESS_CSV_ENCODING_ERROR", message=str(exc), file=str(csv_file)))
        return findings

    def _detect_pyc(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        for path in module_path.rglob("*"):
            if path.name == "__pycache__" and path.is_dir():
                findings.append(Finding(severity=Severity.warning, code="PYCACHE_FOUND", message="__pycache__ directory should not be committed.", file=str(path), hint="Delete __pycache__ before commit/push."))
            elif path.suffix == ".pyc":
                findings.append(Finding(severity=Severity.warning, code="PYC_FOUND", message=".pyc file should not be committed.", file=str(path), hint="Delete .pyc files before commit/push."))
        return findings

    def _validate_missing_access(self, module_path: Path, parsed_models: dict[str, ParsedModel]) -> list[Finding]:
        access_model_refs: set[str] = set()
        for csv_file in self._iter_files(module_path, "ir.model.access.csv"):
            with csv_file.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    model_ref = row.get("model_id:id") or ""
                    if model_ref.startswith("model_"):
                        access_model_refs.add(model_ref)
        findings: list[Finding] = []
        for model_name, model in sorted(parsed_models.items()):
            if model.inherited_model or model.transient:
                continue
            expected_ref = f"model_{model_name.replace('.', '_')}"
            if expected_ref not in access_model_refs:
                findings.append(Finding(severity=Severity.error, code="ACCESS_ROW_MISSING_STATIC", message=f"No access row found for custom model {model_name}.", file=str(model.file) if model.file else None, hint=f"Add model_{model_name.replace('.', '_')} to security/ir.model.access.csv. New persistent models cannot be pushed without explicit access control."))
        return findings

    def _validate_manifest_dependency_hints(self, module_path: Path, manifest: dict, parsed_models: dict[str, ParsedModel]) -> list[Finding]:
        depends = set(manifest.get("depends") or []) if isinstance(manifest.get("depends") or [], list) else set()
        effective_depends = set(depends)
        for dep in list(depends):
            effective_depends.update(TRANSITIVE_DEPENDS_HINTS.get(dep, set()))
        findings: list[Finding] = []
        inherited = {model.inherited_model for model in parsed_models.values() if model.inherited_model}
        for inherited_model in inherited:
            for prefix, dep in INHERIT_DEPENDS.items():
                if inherited_model and inherited_model.startswith(prefix) and dep not in effective_depends:
                    findings.append(Finding(severity=Severity.warning, code="MANIFEST_DEPENDENCY_MISSING_STATIC", message=f"Model inherits {inherited_model} but manifest does not depend on {dep}.", file=str(module_path / "__manifest__.py"), hint=f"Add '{dep}' to manifest depends if this inheritance is active."))
        return findings

    def _validate_xml_references_and_order(self, module_path: Path, manifest: dict, parsed_models: dict[str, ParsedModel]) -> list[Finding]:
        findings: list[Finding] = []
        entries = [item for item in (manifest.get("data") or []) if isinstance(item, str)]
        defined: set[str] = set()
        all_ids: set[str] = set()
        parsed_files: list[tuple[str, ET.Element]] = []
        for rel in entries:
            path = module_path / rel
            if path.exists() and path.suffix == ".xml":
                try:
                    root = ET.parse(path).getroot()
                except ET.ParseError:
                    continue
                parsed_files.append((rel, root))
                for node in root.iter():
                    xml_id = node.attrib.get("id")
                    if xml_id:
                        all_ids.add(xml_id)
        for rel, root in parsed_files:
            for node in root:
                xml_id = node.attrib.get("id")
                if node.tag == "menuitem":
                    for attr, code in (("action", "MENU_ACTION_DEFINED_LATE"), ("parent", "MENU_PARENT_DEFINED_LATE")):
                        ref = node.attrib.get(attr)
                        if ref and "." not in ref and ref not in defined:
                            severity = Severity.error if ref not in all_ids else Severity.warning
                            findings.append(Finding(severity=severity, code=code, message=f"Menu {node.attrib.get('id') or node.attrib.get('name')} references {attr}={ref} before it is defined in manifest load order.", file=str(module_path / rel), hint="Define actions before menus, or move the action XML file before menu XML in manifest data."))
                if node.tag == "record" and node.attrib.get("model") == "ir.actions.act_window":
                    action_id = node.attrib.get("id") or "<no id>"
                    res_model = self._get_record_field_text(node, "res_model")
                    if res_model and res_model not in parsed_models and res_model.split(".", 1)[0] == module_path.name.split("_", 1)[0]:
                        findings.append(Finding(severity=Severity.warning, code="ACTION_RESMODEL_NOT_FOUND_STATIC", message=f"Action {action_id} uses model {res_model}, not found in parsed custom models.", file=str(module_path / rel)))
                if xml_id:
                    defined.add(xml_id)
        return findings

    def _validate_custom_view_fields(self, module_path: Path, parsed_models: dict[str, ParsedModel]) -> list[Finding]:
        if not parsed_models:
            return []
        findings: list[Finding] = []
        for xml_file in self._iter_files(module_path, "*.xml"):
            try:
                root = ET.parse(xml_file).getroot()
            except ET.ParseError:
                continue
            for record in root.iter("record"):
                if record.attrib.get("model") != "ir.ui.view":
                    continue
                view_model = self._get_record_field_text(record, "model")
                if not view_model or view_model not in parsed_models:
                    continue
                arch_field = self._get_record_field_node(record, "arch")
                if arch_field is None:
                    continue
                findings.extend(self._walk_view_fields(xml_file, arch_field, view_model, parsed_models))
        return findings

    def _walk_view_fields(self, xml_file: Path, node: ET.Element, current_model: str, parsed_models: dict[str, ParsedModel]) -> list[Finding]:
        findings: list[Finding] = []
        model = parsed_models.get(current_model)
        model_fields = (model.fields if model else set()) | COMMON_ODOO_FIELDS
        for child in list(node):
            next_model = current_model
            if child.tag == "field":
                field_name = child.attrib.get("name")
                if field_name and field_name not in {"model", "arch", "inherit_id", "name"}:
                    if field_name not in model_fields:
                        findings.append(Finding(severity=Severity.warning, code="VIEW_FIELD_NOT_FOUND_STATIC", message=f"Field '{field_name}' is used in a view for custom model '{current_model}' but was not found in parsed Python fields.", file=str(xml_file), hint="Static check only. Confirm the field exists through inheritance, or add it to the model."))
                    elif model and field_name in model.relations:
                        next_model = model.relations[field_name]
            findings.extend(self._walk_view_fields(xml_file, child, next_model, parsed_models))
        return findings

    def _parse_custom_models(self, module_path: Path) -> dict[str, ParsedModel]:
        models: dict[str, ParsedModel] = {}
        pending_inherits: list[ParsedModel] = []
        for py_file in self._iter_files(module_path, "*.py"):
            if "__pycache__" in py_file.parts:
                continue
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            except Exception:
                continue
            for cls in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
                base_names = {self._name_from_ast(base) for base in cls.bases}
                transient = any(name.endswith("TransientModel") for name in base_names if name)
                attrs: dict[str, ast.AST] = {}
                for stmt in cls.body:
                    if isinstance(stmt, ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, ast.Name):
                                attrs[target.id] = stmt.value
                model_name = self._literal(attrs.get("_name"))
                inherited = self._inherit_value(attrs.get("_inherit"))
                if model_name:
                    parsed = ParsedModel(technical_name=model_name, inherited_model=None, transient=transient, file=py_file)
                    self._extract_fields(attrs, parsed)
                    models[model_name] = parsed
                elif inherited:
                    parsed = ParsedModel(technical_name=inherited, inherited_model=inherited, transient=transient, file=py_file)
                    self._extract_fields(attrs, parsed)
                    pending_inherits.append(parsed)
        for inherited in pending_inherits:
            if inherited.technical_name in models:
                models[inherited.technical_name].fields.update(inherited.fields)
                models[inherited.technical_name].relations.update(inherited.relations)
            else:
                models[inherited.technical_name] = inherited
        return models

    def _extract_fields(self, attrs: dict[str, ast.AST], parsed: ParsedModel) -> None:
        for name, value in attrs.items():
            if name.startswith("_"):
                continue
            if isinstance(value, ast.Call):
                func_name = self._name_from_ast(value.func)
                if func_name.startswith("fields."):
                    parsed.fields.add(name)
                    field_type = func_name.split(".")[-1]
                    if field_type in FIELD_TYPES_WITH_COMODEL and value.args:
                        comodel = self._literal(value.args[0])
                        if isinstance(comodel, str):
                            parsed.relations[name] = comodel

    @staticmethod
    def _literal(node: ast.AST | None):
        if node is None:
            return None
        try:
            return ast.literal_eval(node)
        except Exception:
            return None

    @staticmethod
    def _inherit_value(node: ast.AST | None) -> str | None:
        value = OdooModuleValidator._literal(node)
        if isinstance(value, str):
            return value
        if isinstance(value, list) and value and isinstance(value[0], str):
            return value[0]
        return None

    @staticmethod
    def _name_from_ast(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = OdooModuleValidator._name_from_ast(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return ""

    @staticmethod
    def _get_record_field_text(record: ET.Element, field_name: str) -> str | None:
        node = OdooModuleValidator._get_record_field_node(record, field_name)
        if node is None or node.text is None:
            return None
        return node.text.strip()

    @staticmethod
    def _get_record_field_node(record: ET.Element, field_name: str) -> ET.Element | None:
        for node in record.findall("field"):
            if node.attrib.get("name") == field_name:
                return node
        return None

    @staticmethod
    def _iter_files(module_path: Path, pattern: str) -> Iterable[Path]:
        return sorted(path for path in module_path.rglob(pattern) if path.is_file() and ".git" not in path.parts)
