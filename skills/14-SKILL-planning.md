# IDENTITY
Pre-implementation planning pass. Run this **before** writing any code — it
decides scope, the exact file set, and the risks to check. Not a coding
skill; once planned, switch to the matching domain skill (or
`odoo-custom-module-development` for a standalone module) to implement.

When scope includes views/XML/QWeb, add the `odoo-view-development` evidence
gate: exact version and parent, owning dependencies, inheritance priority,
XPath match count, security visibility, `noupdate`, Studio risk, and final
compiled-view proof.

---

## 1. SCOPE CLASSIFICATION (decide first, changes everything downstream)
```text
SURGICAL      — one bug, one field, one method, one view tweak.
                -> touch only the exact file(s)/lines. No rewrites.

NEW FEATURE   — adds fields/logic to an EXISTING model/module.
                -> model change + matching view + matching access rule,
                   as one complete set.

NEW MODULE    — standalone models that don't exist yet.
                -> full skeleton: model(s) + ir.model.access.csv + views
                   + actions + menu + (security groups/rules if multi-user)
                   + data (sequence, defaults) — all in one complete pass.
```
Never treat a "new module" request as a series of surgical edits arriving
later (model now, security "later", views "later"). Plan the full file set
up front, then write it as one complete set — see CHANGE SCOPE in the
system prompt.

## 2. FILE INVENTORY CHECKLIST (tick every box that applies before writing)
- [ ] Model file(s) (`models/*.py`)
- [ ] `security/ir.model.access.csv` — every new model needs a row or it's unusable
- [ ] `security/*_security.xml` — only if groups or record rules are needed
- [ ] View(s) (`views/*.xml`) — form/list/search as the feature needs
- [ ] Action + menu — a model with no action/menu is unreachable in the UI
- [ ] Data file — sequence, default categories, demo data if relevant
- [ ] `__manifest__.py` `data` list updated with every new XML/CSV file
- [ ] `__init__.py` (module and `models/`) updated to import new files

## 3. IMPACT / DEPENDENCY CHECK
```python
# What already depends on the model(s) you're about to touch?
model = self.env['ir.model'].search([('model', '=', 'sale.order')])
model.field_id.filtered(lambda f: f.state == 'manual')  # custom fields already there
# What modules currently inherit/extend it?
self.env['ir.model.data'].search([('model', '=', 'ir.ui.view'), ('name', 'like', 'sale_order')])
```
- Editing a **native** model/view: check who else inherits it — a field
  rename or view xpath change can break other installed modules.
- Editing a **custom** model: check `_inherits`/`_inherit` users and any
  report/automation referencing it before renaming or removing a field.

## 4. RISK FLAGS (call out explicitly in the plan, don't silently skip)
- **Migration**: existing DB + new `required=True` field → needs `default=`
  or a post-init hook, or install will fail on data present.
- **Multi-company**: does the model need `company_id` + a record rule, or
  will data leak across companies?
- **Performance**: any loop over `search()`/`write()` per record instead of
  batched ORM calls or `read_group`/raw SQL for reporting-scale data.
- **Security**: every new model needs explicit `ir.model.access.csv`; every
  user-facing action needs a group check if not meant for all internal users.
- **Native source touch**: never edit core/enterprise files directly —
  inherit instead.

## 5. VERIFICATION PLAN (decide before writing, not after)
- Which existing tests/flows could this affect (list them)?
- What manual check confirms success (e.g. "create record, confirm state
  transitions draft→confirmed→done", "run report, check totals")?
- Is a module upgrade required (new fields/views) vs just a restart?

## 6. WHEN TO STOP AND ASK
Stop and ask the user before planning further if:
- The task could be either a surgical fix or a new feature and the two
  imply meaningfully different scope (e.g. "add a discount field" — one
  field vs a full approval workflow).
- Multiple existing modules could reasonably own this — confirm placement
  before creating a new module that duplicates existing functionality
  (check `odoo-all-modules-reference` first).

---

## CRITICAL RULES
- Classify scope (§1) before touching any file — it determines whether you
  do a pinpoint edit or a full complete-file-set write.
- A "new module" plan is incomplete until every box in §2 is accounted for;
  do not start writing code with an unfinished checklist.
- Use `workflow.coding_task_plan` for the server-owned automated plan when
  available; this skill is the human-readable judgment layer around it —
  domain placement, risk flags, and completeness the automated tool doesn't
  reason about.
- Implement via `workflow.odoo_code_change` (server-owned read/write/verify/deploy)
  once planning is done. Never call hidden repo/git/odoosh tools directly.
