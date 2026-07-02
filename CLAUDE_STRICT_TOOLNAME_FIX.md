# Claude Strict Tool Name Fix

Some Claude MCP/front-end builds validate remote tool names with:

```text
^[a-zA-Z0-9_-]{1,64}$
```

Odoo MCP internal tool names use dotted namespaces such as `odoo.search_read`,
`workflow.smart_tool_router`, and `repo.write_many_smart`. Dots are rejected by
those clients.

## Fix

The connector now exposes strict external names everywhere a client may list MCP
or REST tools:

- `odoo.search_read` -> `odoo_search_read`
- `workflow.smart_tool_router` -> `workflow_smart_tool_router`
- `repo.write_many_smart` -> `repo_write_many_smart`

Calls using either format are accepted. The server maps the external safe name
back to the internal dotted route before dispatching.

## Functional query priority

The tool list is sorted so functional/live Odoo tools appear first:

1. `connector_health`
2. `odoo_search_read`
3. `odoo_health`
4. `odoo_module_status`
5. `workflow_smart_tool_router`

This makes simple prompts such as "list draft invoices" use `odoo_search_read`
directly instead of entering the coding workflow.
