from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class CodeAwareUiPlanner:
    """Derive a conservative browser scenario from custom Odoo frontend code.

    This planner does not execute arbitrary code and does not guess destructive
    actions. It extracts templates, CSS classes, click handlers, visible labels,
    and state hints, then maps them to the deterministic browser action library.
    """

    _FRONTEND_SUFFIXES = {".xml", ".js", ".scss", ".css", ".py"}

    def plan(
        self,
        *,
        project_path: str | None,
        module_name: str | None,
        feature_goal: str | None,
        required_selectors: list[str],
        profile: str,
    ) -> dict[str, Any]:
        if not project_path or not module_name:
            return {"status": "skipped", "reason": "project_path_and_module_name_required"}
        root = Path(project_path).expanduser().resolve()
        module = (root / module_name).resolve()
        if root not in module.parents or not module.is_dir():
            return {"status": "failed", "reason": "module_not_found_or_outside_project", "module_path": str(module)}

        files = [p for p in module.rglob("*") if p.is_file() and p.suffix.lower() in self._FRONTEND_SUFFIXES]
        files = files[:200]
        combined = ""
        evidence_files: list[str] = []
        for file_path in files:
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if text.strip():
                combined += f"\n/* FILE:{file_path.relative_to(root)} */\n{text[:120000]}"
                evidence_files.append(str(file_path.relative_to(root)))

        classes = sorted(set(re.findall(r'class=["\'][^"\']*?\b([A-Za-z_][\w-]*(?:-[\w-]+)+)', combined)))
        selectors = [f".{name}" for name in classes[:80]]
        handlers = sorted(set(re.findall(r't-on-click=["\']([^"\']+)', combined)))
        template_names = sorted(set(re.findall(r't-name=["\']([^"\']+)', combined)))
        labels = sorted(set(x.strip() for x in re.findall(r'>\s*([A-Za-z][A-Za-z0-9 /&_-]{2,40})\s*<', combined) if x.strip()))
        lower = " ".join([feature_goal or "", " ".join(required_selectors), combined]).lower()

        scenario = "none"
        product_count = 0
        inferred_actions: list[dict[str, Any]] = []
        assertions: list[dict[str, Any]] = []
        confidence = 0.45

        if any(token in lower for token in ["trolley", "cart", "orderline", "order line"]):
            scenario, product_count, confidence = "trolley_with_products", 2, 0.94
            inferred_actions.append({"action": "click_visible_product", "count": 2, "reason": "feature depends on POS order lines"})
        elif "payment" in lower:
            scenario, product_count, confidence = "payment_screen", 1, 0.88
            inferred_actions += [{"action": "click_visible_product", "count": 1}, {"action": "open_payment"}]
        elif any(token in lower for token in ["customer", "partner", "client popup"]):
            scenario, confidence = "customer_popup", 0.86
            inferred_actions.append({"action": "open_customer"})
        elif "receipt" in lower:
            scenario, product_count, confidence = "receipt_screen", 1, 0.82
            inferred_actions += [{"action": "click_visible_product", "count": 1}, {"action": "open_payment"}]
        elif profile.startswith("pos_") and any(token in lower for token in ["product", "order", "pos"]):
            scenario, product_count, confidence = "product_screen_with_order", 1, 0.70
            inferred_actions.append({"action": "click_visible_product", "count": 1})

        for selector in required_selectors:
            assertions.append({"selector": selector, "min_count": 1, "source": "request"})
        if not assertions:
            goal_tokens = [t for t in re.findall(r"[a-z0-9_-]+", (feature_goal or "").lower()) if len(t) > 3]
            ranked = [sel for sel in selectors if any(token in sel.lower() for token in goal_tokens)]
            for selector in ranked[:5]:
                assertions.append({"selector": selector, "min_count": 1, "source": "code"})

        return {
            "status": "planned",
            "module": module_name,
            "module_path": str(module),
            "feature_goal": feature_goal,
            "scenario": scenario,
            "product_count": product_count,
            "confidence": confidence,
            "actions": inferred_actions,
            "assertions": assertions,
            "evidence": {
                "files": evidence_files[:40],
                "templates": template_names[:30],
                "handlers": handlers[:30],
                "selectors": selectors[:50],
                "labels": labels[:40],
            },
            "safety": "deterministic_actions_only",
        }
