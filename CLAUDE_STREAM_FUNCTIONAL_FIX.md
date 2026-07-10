# Claude stream + functional query fix

## Problem
Claude can show `Error in message stream` while it is still `Looking for available tools` if the remote MCP server returns a large tool list or complex coding schemas.

## Fix
This build keeps the normal `/mcp` endpoint for ChatGPT and keeps `/qwen/mcp/...` for Qwen, but adds a Claude-safe functional MCP profile:

```text
https://YOUR-SERVICE/claude/mcp
```

The Claude profile exposes a small read-only tool list:

- `connector_health`
- `odoo_query`
- `odoo_search_read`
- `odoo_health`
- `odoo_module_status`

`odoo_query` accepts natural language such as:

```text
List users
List draft invoices
Show unpaid invoices
List customers
List employees
List products
```

It maps the query to the correct Odoo model/domain/fields and runs live XML-RPC `search_read`.

## URLs

ChatGPT:

```text
https://YOUR-SERVICE/mcp
```

Qwen:

```text
https://YOUR-SERVICE/qwen/mcp/YOUR_SHORT_OR_API_TOKEN
```

Claude:

```text
https://YOUR-SERVICE/claude/mcp
```

If Claude is already pointed at `/mcp`, this build also tries to auto-detect Claude from request headers and returns the slim Claude profile automatically.
