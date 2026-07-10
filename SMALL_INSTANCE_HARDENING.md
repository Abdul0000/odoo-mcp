# Small-Instance Hardening

This build targets a single Render free web service (approximately 0.1 CPU and 512 MB RAM).

## Resource limits

| Setting | Default | Purpose |
|---|---:|---|
| `ODOO_AGENT_BACKGROUND_WORKERS` | `1` | One long-running job owner |
| `ODOO_AGENT_BACKGROUND_QUEUE_LIMIT` | `1` | One bounded waiting slot |
| `ODOO_AGENT_MAX_ACTIVE_DEPLOYS` | `1` | Prevent overlapping Odoo.sh deploys |
| `ODOO_AGENT_MCP_DISPATCH_CONCURRENCY` | `2` | Keep sync file/Git/SSH work off the event loop without oversubscribing CPU |
| `ODOO_AGENT_FRONTEND_PREFLIGHT_MAX_READS` | `20` | Bound source-read fan-out |
| `ODOO_AGENT_UPGRADE_MAX_ATTEMPTS` | `2` | Avoid expensive fake retry loops |
| `ODOO_AGENT_BROWSER_TOOLS_ENABLED` | `false` | No Chromium process or screenshot gate |

## Correctness guarantees

- SQLite WAL transactions preserve concurrent jobs and steps.
- A persisted lease gives exactly one safe-deploy owner across processes.
- Queue saturation fails quickly with explicit backpressure.
- Multi-file writes validate first and roll back every touched file on failure.
- Odoo Community and Enterprise source trees are immutable.
- Delete operations use the same read/scope gate as writes.
- A destructive migration cannot deploy without a confirmed recovery reference.
- Interrupted jobs are reconciled to a resumable warning after restart.
- Permanent upgrade errors stop and request a real code fix; only transient failures retry.
- Frontend routing uses tokens/paths rather than accidental `pos` substrings.
- Generated SVG is XML-safe and must prove CSS-bundle/template attachment.

## Render operational rule

Do not increase worker or deployment concurrency on the 512 MB plan. `429`, `deploy_capacity_full`, and `background_capacity_full` are protective responses. Poll the active job and retry after it finishes.

The Render free filesystem is ephemeral. SQLite protects runtime/restart correctness inside the current container, but durable job history across fresh redeploys requires a persistent disk on a paid plan.
