# Fast + Correct Agent Optimizations

This build keeps strict Odoo correctness checks while reducing avoidable latency.

## New workflow tools

- `workflow.smart_tool_router` — chooses the fastest safe workflow for the task.
- `workflow.repo_index` — cached lightweight index of modules, manifests, assets, JS/XML templates, and source files.
- `workflow.frontend_pattern_preflight` — now uses a persistent cache for repeated OWL/POS/website/payment pattern discovery.

## Validation behavior

`workflow.verify_against_core` remains strict, but now supports impact-aware changed-file validation:

- JS changed → include related XML templates and manifest from the same module.
- XML changed → include related JS components and manifest from the same module.
- Manifest changed → validate manifest/assets directly.

It does not scan the full repo unless the caller passes a large explicit file list.

## Qwen safer URL mode

Instead of putting the permanent `ODOO_AGENT_API_TOKEN` in Qwen app URL, call:

```bash
curl -X POST https://YOUR-SERVICE.onrender.com/qwen/token \
  -H "Authorization: Bearer YOUR_ODOO_AGENT_API_TOKEN"
```

Use the returned `qwen_mcp_url` in Qwen app:

```json
{
  "mcpServers": {
    "odoo": {
      "type": "streamable-http",
      "url": "https://YOUR-SERVICE.onrender.com/qwen/mcp/SHORT_TOKEN",
      "headers": {},
      "sse_read_timeout": 300
    }
  }
}
```

## Recommended env

```env
ODOO_AGENT_PATTERN_CACHE_ENABLED=true
ODOO_AGENT_PATTERN_CACHE_SECONDS=3600
ODOO_AGENT_PATTERN_CACHE_DIR=/workspace/odoo_agent_cache
ODOO_AGENT_REPO_INDEX_ENABLED=true
ODOO_AGENT_REPO_INDEX_CACHE_SECONDS=3600
ODOO_AGENT_VERIFY_IMPACT_AWARE_DEFAULT=true
ODOO_AGENT_QWEN_SHORT_TOKEN_TTL_SECONDS=3600
```

For Render, mount a persistent disk at `/workspace` so OAuth state, repo index, pattern cache, and Qwen short tokens survive restarts.
