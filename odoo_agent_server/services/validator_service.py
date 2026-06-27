from __future__ import annotations

import ast
import csv
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..schemas import Finding, Severity

_FIELD_ASSIGNMENT_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*fields\.[A-Za-z_][A-Za-z0-9_]*\(")
_MODEL_NAME_RE = re.compile(r"^\s*_name\s*=\s*['\"]([^'\"]+)['\"]")
_MODEL_INHERIT_RE = re.compile(r"^\s*_inherit\s*=\s*['\"]([^'\"]+)['\"]")

# Common fields every Odoo model may expose. This is intentionally conservative.
COMMON_ODOO_FIELDS = {
    "id",
    "display_name",
    "create_uid",
    "create_date",
    "write_uid",
    "write_date",
    "__last_update",
    "message_ids",
    "message_follower_ids",
    "message_partner_ids",
    "activity_ids",
    "activity_state",
    "activity_type_id",
    "activity_user_id",
    "activity_date_deadline",
}


@dataclass
class ParsedModel:
    technical_name: str
    inherited_model: str | None = None
    fields: set[str] = field(default_factory=set)
    file: Path | None = None


class OdooModuleValidator:
    """Static checks that catch common module errors before Odoo.sh build time."""

    def validate(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        findings.extend(self._validate_basic_structure(module_path))
        findings.extend(self._validate_manifest(module_path))
        findings.extend(self._validate_python_files(module_path))
        findings.extend(self._validate_xml_files(module_path))
        findings.extend(self._validate_security_csv(module_path))
        findings.extend(self._detect_pyc(module_path))
        findings.extend(self._validate_custom_view_fields(module_path))
        return findings

    def _validate_basic_structure(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        if not module_path.exists():
            return [
                Finding(
                    severity=Severity.error,
                    code="MODULE_NOT_FOUND",
                    message=f"Module path does not exist: {module_path}",
                    hint="Check project_path and module_name.",
                )
            ]
        if not module_path.is_dir():
            return [
                Finding(
                    severity=Severity.error,
                    code="MODULE_NOT_DIRECTORY",
                    message=f"Module path is not a directory: {module_path}",
                )
            ]
        manifest = module_path / "__manifest__.py"
        if not manifest.exists():
            findings.append(
                Finding(
                    severity=Severity.error,
                    code="MISSING_MANIFEST",
                    message="Missing __manifest__.py",
                    file=str(manifest),
                )
            )
        init_file = module_path / "__init__.py"
        if not init_file.exists():
            findings.append(
                Finding(
                    severity=Severity.warning,
                    code="MISSING_INIT",
                    message="Missing __init__.py",
                    file=str(init_file),
                    hint="Most Odoo modules should have __init__.py even if it is empty.",
                )
            )
        return findings

    def _validate_manifest(self, module_path: Path) -> list[Finding]:
        manifest_path = module_path / "__manifest__.py"
        if not manifest_path.exists():
            return []
        try:
            manifest_text = manifest_path.read_text(encoding="utf-8")
            manifest = ast.literal_eval(manifest_text)
        except SyntaxError as exc:
            return [
                Finding(
                    severity=Severity.error,
                    code="MANIFEST_SYNTAX_ERROR",
                    message=str(exc),
                    file=str(manifest_path),
                    line=exc.lineno,
                )
            ]
        except Exception as exc:  # noqa: BLE001 - user-facing diagnostic
            return [
                Finding(
                    severity=Severity.error,
                    code="MANIFEST_PARSE_ERROR",
                    message=f"Could not parse manifest as a Python literal dict: {exc}",
                    file=str(manifest_path),
                )
            ]
        if not isinstance(manifest, dict):
            return [
                Finding(
                    severity=Severity.error,
                    code="MANIFEST_NOT_DICT",
                    message="Manifest must be a Python dictionary.",
                    file=str(manifest_path),
                )
            ]

        findings: list[Finding] = []
        for required_key in ("name", "version", "depends"):
            if required_key not in manifest:
                findings.append(
                    Finding(
                        severity=Severity.warning if required_key == "version" else Severity.error,
                        code="MANIFEST_MISSING_KEY",
                        message=f"Manifest is missing key: {required_key}",
                        file=str(manifest_path),
                    )
                )
        depends = manifest.get("depends", [])
        if not isinstance(depends, list) or not all(isinstance(dep, str) for dep in depends):
            findings.append(
                Finding(
                    severity=Severity.error,
                    code="MANIFEST_BAD_DEPENDS",
                    message="Manifest 'depends' must be a list of module technical names.",
                    file=str(manifest_path),
                )
            )
        for key in ("data", "demo", "qweb"):
            entries = manifest.get(key, []) or []
            if not isinstance(entries, list):
                findings.append(
                    Finding(
                        severity=Severity.error,
                        code="MANIFEST_BAD_FILE_LIST",
                        message=f"Manifest '{key}' must be a list of file paths.",
                        file=str(manifest_path),
                    )
                )
                continue
            for rel_path in entries:
                if not isinstance(rel_path, str):
                    findings.append(
                        Finding(
                            severity=Severity.error,
                            code="MANIFEST_BAD_FILE_ENTRY",
                            message=f"Manifest '{key}' contains a non-string file entry.",
                            file=str(manifest_path),
                        )
                    )
                    continue
                target = module_path / rel_path
                if not target.exists():
                    findings.append(
                        Finding(
                            severity=Severity.error,
                            code="MANIFEST_FILE_MISSING",
                            message=f"Manifest references missing file: {rel_path}",
                            file=str(manifest_path),
                            hint="Create the file or remove it from the manifest.",
                        )
                    )
        return findings

    def _validate_python_files(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        for py_file in self._iter_files(module_path, "*.py"):
            if "__pycache__" in py_file.parts:
                continue
            try:
                source = py_file.read_text(encoding="utf-8")
                ast.parse(source, filename=str(py_file))
            except SyntaxError as exc:
                findings.append(
                    Finding(
                        severity=Severity.error,
                        code="PYTHON_SYNTAX_ERROR",
                        message=str(exc),
                        file=str(py_file),
                        line=exc.lineno,
                    )
                )
            except UnicodeDecodeError as exc:
                findings.append(
                    Finding(
                        severity=Severity.error,
                        code="PYTHON_ENCODING_ERROR",
                        message=str(exc),
                        file=str(py_file),
                    )
                )
        return findings

    def _validate_xml_files(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        for xml_file in self._iter_files(module_path, "*.xml"):
            try:
                ET.parse(xml_file)
            except ET.ParseError as exc:
                line, _col = exc.position
                findings.append(
                    Finding(
                        severity=Severity.error,
                        code="XML_PARSE_ERROR",
                        message=str(exc),
                        file=str(xml_file),
                        line=line,
                    )
                )
            except UnicodeDecodeError as exc:
                findings.append(
                    Finding(
                        severity=Severity.error,
                        code="XML_ENCODING_ERROR",
                        message=str(exc),
                        file=str(xml_file),
                    )
                )
        return findings

    def _validate_security_csv(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        expected_header = [
            "id",
            "name",
            "model_id:id",
            "group_id:id",
            "perm_read",
            "perm_write",
            "perm_create",
            "perm_unlink",
        ]
        for csv_file in self._iter_files(module_path, "*.csv"):
            if csv_file.name != "ir.model.access.csv":
                continue
            try:
                with csv_file.open(newline="", encoding="utf-8") as handle:
                    reader = csv.DictReader(handle)
                    if reader.fieldnames != expected_header:
                        findings.append(
                            Finding(
                                severity=Severity.error,
                                code="ACCESS_CSV_BAD_HEADER",
                                message="ir.model.access.csv header is not the standard Odoo access header.",
                                file=str(csv_file),
                                hint=",".join(expected_header),
                            )
                        )
                    for row_index, row in enumerate(reader, start=2):
                        for perm in ("perm_read", "perm_write", "perm_create", "perm_unlink"):
                            if row.get(perm) not in {"0", "1"}:
                                findings.append(
                                    Finding(
                                        severity=Severity.error,
                                        code="ACCESS_CSV_BAD_PERMISSION",
                                        message=f"{perm} must be 0 or 1.",
                                        file=str(csv_file),
                                        line=row_index,
                                    )
                                )
            except UnicodeDecodeError as exc:
                findings.append(
                    Finding(
                        severity=Severity.error,
                        code="ACCESS_CSV_ENCODING_ERROR",
                        message=str(exc),
                        file=str(csv_file),
                    )
                )
        return findings

    def _detect_pyc(self, module_path: Path) -> list[Finding]:
        findings: list[Finding] = []
        for path in module_path.rglob("*"):
            if path.name == "__pycache__" and path.is_dir():
                findings.append(
                    Finding(
                        severity=Severity.warning,
                        code="PYCACHE_FOUND",
                        message="__pycache__ directory should not be committed.",
                        file=str(path),
                        hint="Delete __pycache__ before commit/push.",
                    )
                )
            elif path.suffix == ".pyc":
                findings.append(
                    Finding(
                        severity=Severity.warning,
                        code="PYC_FOUND",
                        message=".pyc file should not be committed.",
                        file=str(path),
                        hint="Delete .pyc files before commit/push.",
                    )
                )
        return findings

    def _validate_custom_view_fields(self, module_path: Path) -> list[Finding]:
        parsed_models = self._parse_custom_models(module_path)
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
                model_fields = parsed_models[view_model].fields | COMMON_ODOO_FIELDS
                for field_node in arch_field.iter("field"):
                    field_name = field_node.attrib.get("name")
                    if not field_name or field_name in {"model", "arch", "inherit_id", "name"}:
                        continue
                    if field_name not in model_fields:
                        findings.append(
                            Finding(
                                severity=Severity.warning,
                                code="VIEW_FIELD_NOT_FOUND_STATIC",
                                message=(
                                    f"Field '{field_name}' is used in a view for custom model "
                                    f"'{view_model}' but was not found in parsed Python fields."
                                ),
                                file=str(xml_file),
                                hint=(
                                    "Static check only. Confirm the field exists through inheritance, "
                                    "or add it to the model."
                                ),
                            )
                        )
        return findings

    def _parse_custom_models(self, module_path: Path) -> dict[str, ParsedModel]:
        models: dict[str, ParsedModel] = {}
        for py_file in self._iter_files(module_path, "*.py"):
            if "__pycache__" in py_file.parts:
                continue
            try:
                lines = py_file.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            current_model: ParsedModel | None = None
            for line in lines:
                model_match = _MODEL_NAME_RE.match(line)
                inherit_match = _MODEL_INHERIT_RE.match(line)
                field_match = _FIELD_ASSIGNMENT_RE.match(line)
                if model_match:
                    technical_name = model_match.group(1)
                    current_model = ParsedModel(technical_name=technical_name, file=py_file)
                    models[technical_name] = current_model
                elif inherit_match and current_model is None:
                    technical_name = inherit_match.group(1)
                    # Inherited standard models are not complete from static source, so we do not
                    # validate their views strictly unless the module also declares _name.
                    current_model = ParsedModel(
                        technical_name=technical_name,
                        inherited_model=technical_name,
                        file=py_file,
                    )
                elif field_match and current_model is not None:
                    current_model.fields.add(field_match.group(1))
        return models

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
        return sorted(
            path for path in module_path.rglob(pattern) if path.is_file() and ".git" not in path.parts
        )
