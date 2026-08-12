from __future__ import annotations

import ast
import csv
import io
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..schemas import Severity
from .validator_service import OdooModuleValidator
from .coding_session_service import CodingSessionService


class ModuleAnalysisService:
    """One-pass, local-only Odoo addon evidence collector."""

    _cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
    _lock = threading.Lock()
    _ttl_seconds = 300
    _extensions = {".py", ".xml", ".csv", ".js", ".scss", ".css"}

    @staticmethod
    def _priority(path: Path) -> tuple[int, str]:
        rel = path.as_posix()
        name = path.name
        if name == "__manifest__.py":
            return (0, rel)
        if name == "__init__.py":
            return (1, rel)
        first = path.parts[0] if path.parts else ""
        order = {
            "models": 2, "controllers": 3, "controller": 3, "iot_handlers": 4,
            "handlers": 4, "wizard": 5, "wizards": 5, "security": 6,
            "views": 7, "report": 8, "reports": 8, "data": 9, "demo": 10,
            "tests": 11, "static": 12,
        }
        return (order.get(first, 20), rel)

    @staticmethod
    def _git_sha(project: Path) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(project), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else "untracked"
        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"

    @staticmethod
    def _preview(content: str, budget: int) -> tuple[str, bool]:
        if len(content) <= budget:
            return content, False
        half = max(1, budget // 2)
        omitted = len(content) - (half * 2)
        return content[:half] + f"\n...[{omitted} chars omitted]...\n" + content[-half:], True

    @staticmethod
    def _python_facts(path: str, content: str) -> dict[str, Any]:
        facts: dict[str, Any] = {"path": path, "models": [], "classes": [], "methods": [], "routes": []}
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return facts
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = [ast.unparse(base) for base in node.bases]
                row: dict[str, Any] = {"class": node.name, "bases": bases, "model": None, "inherit": None, "fields": []}
                for item in node.body:
                    if isinstance(item, (ast.Assign, ast.AnnAssign)):
                        target = item.targets[0] if isinstance(item, ast.Assign) else item.target
                        value = item.value
                        target_name = target.id if isinstance(target, ast.Name) else None
                        if target_name in {"_name", "_inherit"} and isinstance(value, ast.Constant):
                            row["model" if target_name == "_name" else "inherit"] = value.value
                        if target_name and isinstance(value, ast.Call) and "fields." in ast.unparse(value.func):
                            row["fields"].append(target_name)
                facts["classes"].append(row)
                if row["model"] or row["inherit"]:
                    facts["models"].append(row)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                facts["methods"].append(node.name)
                for decorator in node.decorator_list:
                    rendered = ast.unparse(decorator)
                    if rendered.startswith(("http.route", "route")):
                        facts["routes"].append({"method": node.name, "decorator": rendered})
        return facts

    @staticmethod
    def _xml_facts(path: str, content: str) -> dict[str, Any]:
        facts: dict[str, Any] = {"path": path, "records": [], "menus": [], "templates": []}
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return facts
        for node in root.iter():
            if node.tag == "record":
                facts["records"].append({"id": node.attrib.get("id"), "model": node.attrib.get("model")})
            elif node.tag == "menuitem":
                facts["menus"].append({"id": node.attrib.get("id"), "name": node.attrib.get("name"), "action": node.attrib.get("action")})
            elif node.tag in {"template", "t"} and (node.attrib.get("id") or node.attrib.get("t-name")):
                facts["templates"].append(node.attrib.get("id") or node.attrib.get("t-name"))
        return facts

    def analyze(
        self,
        *,
        project: Path,
        module_path: Path,
        module_name: str,
        analysis_goal: str,
        max_files: int,
        max_total_chars: int,
        validator_findings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        all_files = [
            path for path in module_path.rglob("*")
            if path.is_file()
            and path.suffix.lower() in self._extensions
            and not any(part in {"__pycache__", ".pytest_cache", "node_modules"} for part in path.relative_to(module_path).parts)
            and "static/lib" not in path.relative_to(module_path).as_posix()
        ]
        all_files.sort(key=lambda item: self._priority(item.relative_to(module_path)))
        content_fingerprint = CodingSessionService._module_fingerprint_from_files(module_path, all_files)
        sha = self._git_sha(project)
        cache_key = (str(module_path.resolve()), sha, content_fingerprint, max_files, max_total_chars)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > now:
                result = deepcopy(cached[1])
                result["cache_hit"] = True
                result["analysis_goal"] = analysis_goal
                return result

        selected = all_files[:max_files]
        evidence: list[dict[str, Any]] = []
        python_facts: list[dict[str, Any]] = []
        xml_facts: list[dict[str, Any]] = []
        csv_facts: list[dict[str, Any]] = []
        remaining = max_total_chars
        for index, path in enumerate(selected):
            if remaining <= 0:
                break
            rel = path.relative_to(module_path).as_posix()
            content = path.read_text(encoding="utf-8", errors="replace")
            files_left = max(1, len(selected) - index)
            budget = min(4000, max(300, remaining // files_left))
            preview, truncated = self._preview(content, budget)
            evidence.append({"path": rel, "size": len(content), "truncated": truncated, "content": preview})
            remaining -= len(preview)
            if path.suffix.lower() == ".py":
                python_facts.append(self._python_facts(rel, content))
            elif path.suffix.lower() == ".xml":
                xml_facts.append(self._xml_facts(rel, content))
            elif path.suffix.lower() == ".csv":
                try:
                    rows = list(csv.DictReader(io.StringIO(content)))
                except csv.Error:
                    rows = []
                csv_facts.append({"path": rel, "row_count": len(rows), "columns": list(rows[0]) if rows else []})

        manifest: dict[str, Any] = {}
        manifest_path = module_path / "__manifest__.py"
        if manifest_path.is_file():
            try:
                parsed = ast.literal_eval(manifest_path.read_text(encoding="utf-8"))
                manifest = parsed if isinstance(parsed, dict) else {}
            except (SyntaxError, ValueError):
                manifest = {}
        findings = (
            validator_findings
            if validator_findings is not None
            else [
                item.model_dump(mode="json")
                for item in OdooModuleValidator().validate(module_path)
                if item.severity in {Severity.error, Severity.warning}
            ]
        )
        result = {
            "status": "analyzed",
            "module_name": module_name,
            "module_path": str(module_path),
            "git_sha": sha,
            "cache_hit": False,
            "analysis_goal": analysis_goal,
            "manifest": {
                key: manifest.get(key) for key in ("name", "version", "summary", "depends", "data", "demo", "assets", "application")
                if key in manifest
            },
            "inventory": {
                "total_relevant_files": len(all_files),
                "returned_files": len(evidence),
                "omitted_files": [path.relative_to(module_path).as_posix() for path in all_files[len(evidence):]],
            },
            "architecture": {"python": python_facts, "xml": xml_facts, "csv": csv_facts},
            "validator_findings": findings,
            "source_evidence": evidence,
            "complete_for_analysis": len(evidence) == len(all_files),
            "success_allowed": True,
            "completion_required": False,
            "inspection_instructions": (
                "Answer the user's architecture, use-case, and gap question directly from this evidence. "
                "Do not call code-change, core-source, deployment, QA, history, or another inspection tool."
            ),
            "next_call": None,
        }
        with self._lock:
            if len(self._cache) > 32:
                self._cache.clear()
            self._cache[cache_key] = (now + self._ttl_seconds, deepcopy(result))
        return result
