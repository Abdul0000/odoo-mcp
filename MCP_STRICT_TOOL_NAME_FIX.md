# MCP Strict Tool Name Compatibility Fix

Some MCP frontends validate tool names with this regex:

```text
^[a-zA-Z0-9_-]{1,64}$
```

The connector's internal tool names use dotted namespaces such as:

```text
repo.write_many_smart
workflow.frontend_pattern_preflight
odoo.search_read
```

Those dotted names are convenient internally, but strict MCP clients reject them.

## Fix

The MCP `/mcp` and `/qwen/mcp/...` `tools/list` responses now expose safe names:

```text
repo_write_many_smart
workflow_frontend_pattern_preflight
odoo_search_read
```

The dispatcher maps those names back to the internal dotted tool names before running the tool.

## Backward compatibility

Existing clients that still call dotted names continue to work:

```text
repo.write_many_smart
```

New strict MCP clients should use the safe names returned by `tools/list`.
