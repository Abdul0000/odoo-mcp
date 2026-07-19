from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, status


_SKILLS = {
    "odoo-accounting-development": ("01-SKILL-accounting.md", "Accounting, finance, invoices, journals, payments, taxes, reconciliation, and reporting."),
    "odoo-manufacturing-development": ("02-SKILL-manufacturing.md", "MRP, BoMs, manufacturing/work orders, quality, subcontracting, planning, and costing."),
    "odoo-inventory-development": ("03-SKILL-inventory.md", "Inventory, warehouses, locations, pickings, moves, reservations, tracking, routes, and valuation."),
    "odoo-sales-development": ("04-SKILL-sales.md", "Sales orders, quotations, pricing, discounts, delivery, invoicing, subscriptions, portal, and credit controls."),
    "odoo-hr-payroll-development": ("06-SKILL-hr-payroll.md", "HR, contracts, payroll, payslips, attendance, leave, timesheets, and statutory deductions."),
    "odoo-crm-pipeline-development": ("07-SKILL-crm-pipeline.md", "CRM leads/opportunities, stages, teams, scoring, activities, forecasting, automation, and pipeline reporting."),
    "odoo-helpdesk-development": ("08-SKILL-helpdesk.md", "Helpdesk tickets, teams, SLAs, escalation, communication, portal, knowledge, automation, and analytics."),
    "odoo-pos-development": ("odoo-pos-development-skill.md", "POS sessions, orders, payments, OWL UI, receipts, hardware, restaurant, loyalty, and reporting."),
    "odoo-website-development": ("odoo-website-development.md", "Website themes, QWeb, snippets, eCommerce, controllers, multi-site, SEO, and performance."),
}

_ALIASES = {
    "accounting": "odoo-accounting-development",
    "manufacturing": "odoo-manufacturing-development",
    "mrp": "odoo-manufacturing-development",
    "inventory": "odoo-inventory-development",
    "stock": "odoo-inventory-development",
    "sales": "odoo-sales-development",
    "hr-payroll": "odoo-hr-payroll-development",
    "payroll": "odoo-hr-payroll-development",
    "crm": "odoo-crm-pipeline-development",
    "helpdesk": "odoo-helpdesk-development",
    "pos": "odoo-pos-development",
    "website": "odoo-website-development",
}


def _skills_root() -> Path:
    return Path(__file__).resolve().parents[2] / "skills"


class SkillService:
    def list(self) -> dict:
        return {
            "skills": [
                {"name": name, "description": description}
                for name, (_, description) in _SKILLS.items()
            ],
            "count": len(_SKILLS),
            "loading": "on_demand",
        }

    def read(self, name: str) -> dict:
        requested = str(name or "").strip().lower()
        canonical = _ALIASES.get(requested, requested)
        entry = _SKILLS.get(canonical)
        if not entry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown skill '{name}'. Call skills.list for available names.",
            )
        filename, description = entry
        path = (_skills_root() / filename).resolve()
        root = _skills_root().resolve()
        if root not in path.parents or not path.is_file():
            raise HTTPException(status_code=500, detail=f"Skill resource missing: {canonical}")
        return {
            "name": canonical,
            "description": description,
            "content": path.read_text(encoding="utf-8"),
            "source": f"skills/{filename}",
            "exact": True,
        }
