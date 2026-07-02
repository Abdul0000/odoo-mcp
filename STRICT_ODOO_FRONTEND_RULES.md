# Strict Odoo Frontend / OWL / JS / XML Rules

This connector includes a blocking preflight tool for Odoo frontend work:

```text
workflow.frontend_pattern_preflight
```

Use it before writing or modifying any of these files or features:

- POS screens, store patches, popups, receipt/payment/product screens
- OWL components, backend web client actions, dashboards, controllers, renderers
- Website snippets, website editor options, website forms, frontend widgets
- Payment provider frontend, checkout, portal/website payment logic
- QWeb/OWL XML templates, t-inherit, xpath, t-name/t-call
- SCSS/asset bundle changes

## Required workflow

```text
CUSTOM SOURCE → FRONTEND PREFLIGHT → CORE/ENTERPRISE SOURCE READS → IMPLEMENT → VERIFY → BROWSER CHECK
```

1. Read the custom module manifest/views/static files related to the task.
2. Call `workflow.frontend_pattern_preflight`.
3. Read every mandatory source path returned by the tool.
4. Confirm exact imports, registry category, services, hooks, patch syntax, template names, XPath targets, and asset bundle from source.
5. Write code only after source patterns are confirmed.
6. Run `workflow.verify_against_core` with all changed `.py`, `.xml`, `.js`, `.scss`, and `__manifest__.py` files.
7. For UI tasks, deploy with exact `verify_ui_paths` or run `workflow.browser_menu_crawl`.

## What verify_against_core now blocks

- Missing `/** @odoo-module **/` in modern JS/OWL files
- Mixing legacy `odoo.define/require` with ES module imports
- Bad Odoo import aliases such as `@web/...` or `@point_of_sale/...`
- Missing or duplicate QWeb `t-name`
- JS `Component.template` not found in XML/source templates
- `t-inherit` or `t-call` source template missing
- QWeb xpath targets not found in inherited template
- Manifest asset path not found on disk
- POS-looking assets outside `point_of_sale._assets_pos`
- Website/editor-looking assets placed in backend bundle without verification
- SCSS `min()/max()` with CSS variables that can break Odoo sass compilation

## Area-specific read patterns

### POS
Read:

```text
point_of_sale/static/src/app
point_of_sale/static/src/app/screens
point_of_sale/static/src/app/store
point_of_sale/static/src/app/components
point_of_sale/views
web/static/src/core
```

Expected bundle: `point_of_sale._assets_pos`.

### Website
Read:

```text
website/static/src
website/views
website_form/static/src
website_sale/static/src
web/static/src/core
```

Expected bundles: `web.assets_frontend`, `website.assets_editor`, or `web.assets_frontend_lazy`, depending on source pattern.

### Payment
Read:

```text
payment/static/src
payment/views
website_payment/static/src
website_payment/views
website_sale/static/src
web/static/src/core
```

### Backend web/OWL
Read:

```text
web/static/src/core
web/static/src/views
web/static/src/webclient
web/views
```

## Tool example

```json
{
  "task_description": "Fix POS PaymentScreen OWL popup",
  "target_area": "auto",
  "target_module_name": "my_custom_pos_module",
  "custom_file_paths": [
    "/workspace/repos/test_18/my_custom_pos_module/static/src/js/payment_screen.js",
    "/workspace/repos/test_18/my_custom_pos_module/static/src/xml/payment_screen.xml"
  ],
  "planned_file_paths": []
}
```
