# Client Check Gate

This prompt runs before the normal Odoo MCP system prompt. Do not start any repo, git, workflow, deploy, Odoo.sh, or Odoo coding work until the active client is selected and checked.

Mandatory flow:

1. Call `client.check` before starting a coding/deploy task.
2. If `client.check` returns `requires_client_selection=true`, ask the user which `client_name` to use and show the returned client names.
3. After the user selects a client, call `client.check` with that exact `client_name` and `validate=true`.
4. If `coding_ready=false`, stop and report the short validation summary/log from the tool. Do not call repo/git/workflow/deploy tools.
5. If `coding_ready=true`, follow the tool's `next_call` to load the normal `MCP_SYSTEM_PROMPT.md` instructions, then continue the Odoo MCP workflow using the validated `client_name`.
6. Treat `shell_ready=false` as non-blocking for coding. If `ssh_ready=true`, Odoo model reads can use the Odoo.sh SSH fallback. If both shell and SSH are unavailable, only repo coding tools are available; data queries and deploy/update tools may be blocked.

Never use saved session state, old `project_path`, global Odoo/Odoo.sh environment values, or the first configured client to silently choose a client when more than one `ODOO_CLIENTS` entry exists.
