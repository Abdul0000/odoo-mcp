# Odoo MCP connection gate

On connection or after idle time, call `workflow.resume_context` first.

- If it returns `resume_mode=fast_local`, continue the returned task/job without rerunning session bootstrap or external health checks.
- If it returns `client_selection_required`, call `client.check` without a client name, ask the user to choose from the safe names, then call `client.check(client_name=..., validate=true)`.
- If capability refresh is required, validate the selected client once. Patching requires `coding_ready`; deployment requires `deploy_ready`.
- Never select one of multiple clients from stale state or silently reuse an unrelated path.

After the client is resolved, follow the compact `MCP_SYSTEM_PROMPT.md` hybrid flow. Do not reload the full instructions on every tool call.
