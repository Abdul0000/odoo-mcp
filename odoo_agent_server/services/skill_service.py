from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, status


_SKILLS = {
    "odoo-accounting-development": ("01-SKILL-accounting.md", "Accounting, finance, invoices, journals, payments, taxes, reconciliation, and reporting."),
    "odoo-manufacturing-development": ("02-SKILL-manufacturing.md", "MRP, BoMs, manufacturing/work orders, quality, subcontracting, planning, and costing."),
    "odoo-inventory-development": ("03-SKILL-inventory.md", "Inventory, warehouses, locations, pickings, moves, reservations, tracking, routes, and valuation."),
    "odoo-sales-development": ("04-SKILL-sales.md", "Sales orders, quotations, pricing, discounts, delivery, invoicing, subscriptions, portal, and credit controls."),
    "odoo-purchase-development": ("05-SKILL-purchase.md", "Purchase orders, vendors, agreements, approvals, replenishment, receipts, billing, landed costs, and procurement analytics."),
    "odoo-hr-payroll-development": ("06-SKILL-hr-payroll.md", "HR, contracts, payroll, payslips, attendance, leave, timesheets, and statutory deductions."),
    "odoo-crm-pipeline-development": ("07-SKILL-crm-pipeline.md", "CRM leads/opportunities, stages, teams, scoring, activities, forecasting, automation, and pipeline reporting."),
    "odoo-helpdesk-development": ("08-SKILL-helpdesk.md", "Helpdesk tickets, teams, SLAs, escalation, communication, portal, knowledge, automation, and analytics."),
    "odoo-sql-query-development": ("09-SKILL-sql-queries.md", "Odoo ORM and SQL queries, domains, joins, read_group, analytics, reporting, and query performance."),
    "odoo-all-modules-reference": ("10-ALL-MODULES-REFERENCE.md", "Odoo module selection, dependencies, key models, integration planning, and implementation roadmap."),
    "odoo-qweb-reports-development": ("11-SKILL-qweb-reports-development.md", "General QWeb PDF/HTML reports, templates, controllers, styling, pagination, data fetching, performance, and debugging."),
    "odoo-accounting-reports": ("12-SKILL-accounting-reports.md", "Accounting QWeb reports including invoices, AR aging, P&L, balance sheet, tax reports, statements, and Pakistan compliance reporting."),
    "odoo-pos-development": ("odoo-pos-development-skill.md", "POS sessions, orders, payments, OWL UI, receipts, hardware, restaurant, loyalty, and reporting."),
    "odoo-website-development": ("odoo-website-development.md", "Website themes, QWeb, snippets, eCommerce, controllers, multi-site, SEO, and performance."),
    "odoo-custom-module-development": ("13-SKILL-custom-module.md", "Standalone custom modules with their own models/tables, manifest, security, views, and menus — not inherited/mixed into native Odoo models."),
    "odoo-task-planning": ("14-SKILL-planning.md", "Pre-implementation planning: scope classification (surgical vs new feature vs new module), file inventory checklist, impact/dependency check, and risk flags before writing code."),
}

_ALIASES = {
    "accounting": "odoo-accounting-development",
    "manufacturing": "odoo-manufacturing-development",
    "mrp": "odoo-manufacturing-development",
    "inventory": "odoo-inventory-development",
    "stock": "odoo-inventory-development",
    "sales": "odoo-sales-development",
    "purchase": "odoo-purchase-development",
    "procurement": "odoo-purchase-development",
    "hr-payroll": "odoo-hr-payroll-development",
    "payroll": "odoo-hr-payroll-development",
    "crm": "odoo-crm-pipeline-development",
    "helpdesk": "odoo-helpdesk-development",
    "sql": "odoo-sql-query-development",
    "sql-queries": "odoo-sql-query-development",
    "query": "odoo-sql-query-development",
    "queries": "odoo-sql-query-development",
    "orm": "odoo-sql-query-development",
    "database": "odoo-sql-query-development",
    "analytics": "odoo-sql-query-development",
    "report": "odoo-qweb-reports-development",
    "reports": "odoo-qweb-reports-development",
    "reporting": "odoo-sql-query-development",
    "pos": "odoo-pos-development",
    "website": "odoo-website-development",
    "modules": "odoo-all-modules-reference",
    "all-modules": "odoo-all-modules-reference",
    "module-reference": "odoo-all-modules-reference",
    "qweb": "odoo-qweb-reports-development",
    "qweb-report": "odoo-qweb-reports-development",
    "qweb-reports": "odoo-qweb-reports-development",
    "qweb-reporting": "odoo-qweb-reports-development",
    "pdf-report": "odoo-qweb-reports-development",
    "pdf-reports": "odoo-qweb-reports-development",
    "report-template": "odoo-qweb-reports-development",
    "accounting-report": "odoo-accounting-reports",
    "accounting-reports": "odoo-accounting-reports",
    "financial-report": "odoo-accounting-reports",
    "financial-reports": "odoo-accounting-reports",
    "invoice-report": "odoo-accounting-reports",
    "ar-aging": "odoo-accounting-reports",
    "profit-loss": "odoo-accounting-reports",
    "balance-sheet": "odoo-accounting-reports",
    "tax-report": "odoo-accounting-reports",
    "custom-module": "odoo-custom-module-development",
    "custom module": "odoo-custom-module-development",
    "new-module": "odoo-custom-module-development",
    "standalone-module": "odoo-custom-module-development",
    "own-model": "odoo-custom-module-development",
    "scratch-module": "odoo-custom-module-development",
    "planning": "odoo-task-planning",
    "plan": "odoo-task-planning",
    "task-plan": "odoo-task-planning",
    "scope": "odoo-task-planning",
    "checklist": "odoo-task-planning",
}

_ROUTING_TERMS = {
    "odoo-accounting-reports": ("accounting report", "financial report", "invoice report", "ar aging", "aged receivable", "profit loss", "balance sheet", "tax report", "partner statement"),
    "odoo-qweb-reports-development": ("qweb", "pdf report", "print report", "report template", "paperformat", "wkhtmltopdf", "ir.actions.report"),
    "odoo-sql-query-development": ("sql", "query", "search_read", "read_group", "domain", "group by", "aggregate", "record count", "analytics"),
    "odoo-pos-development": ("point of sale", "pos ", "pos_", "receipt", "payment screen", "restaurant pos"),
    "odoo-website-development": ("website", "web page", "landing page", "snippet", "ecommerce", "portal page", "seo"),
    "odoo-manufacturing-development": ("manufacturing", "mrp", "bill of materials", "bom", "work order"),
    "odoo-inventory-development": ("inventory", "warehouse", "stock move", "picking", "location", "reservation"),
    "odoo-sales-development": ("sale order", "sales order", "quotation", "pricelist", "subscription"),
    "odoo-purchase-development": ("purchase order", "vendor", "procurement", "replenishment", "request for quotation", "rfq"),
    "odoo-hr-payroll-development": ("payroll", "payslip", "employee", "attendance", "leave", "timesheet", "contract"),
    "odoo-crm-pipeline-development": ("crm", "lead", "opportunity", "pipeline", "sales team"),
    "odoo-helpdesk-development": ("helpdesk", "ticket", "sla", "support team", "escalation"),
    "odoo-accounting-development": ("invoice", "journal", "payment", "tax", "reconciliation", "fiscal position", "account.move"),
    "odoo-all-modules-reference": ("which module", "module dependency", "depends", "module selection", "implementation roadmap"),
    "odoo-custom-module-development": ("custom module", "new module", "standalone module", "own model", "own table", "scratch module", "_name =", "not inherit"),
    "odoo-task-planning": ("plan", "planning", "scope this", "before implementing", "file checklist", "what files are needed", "break down the task"),
}


def _skills_root() -> Path:
    return Path(__file__).resolve().parents[2] / "skills"


class SkillService:
    def list(self, task_description: str | None = None) -> dict:
        result = {
            "skills": [
                {"name": name, "description": description}
                for name, (_, description) in _SKILLS.items()
            ],
            "count": len(_SKILLS),
            "loading": "on_demand",
        }
        if str(task_description or "").strip():
            result["recommendation"] = self.recommend(task_description or "")
        return result

    @staticmethod
    def recommend(task_description: str) -> dict | None:
        text = f" {str(task_description or '').lower()} "
        ranked: list[tuple[int, int, str, list[str]]] = []
        for priority, (name, terms) in enumerate(_ROUTING_TERMS.items()):
            matched = [term for term in terms if term in text]
            if matched:
                score = sum(3 if " " in term or "." in term or "_" in term else 1 for term in matched)
                ranked.append((score, -priority, name, matched))
        if not ranked:
            return None
        score, _, name, matched = max(ranked)
        return {"name": name, "matched_terms": matched[:5], "confidence": "high" if score >= 3 else "medium"}

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
