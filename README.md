# Odoo ChatGPT Direct Connector — Full Backend Plan

This package implements the backend-only connector from the final plan:

```text
ChatGPT = AI brain
        ↓
Custom GPT Actions / lightweight MCP JSON-RPC bridge
        ↓
FastAPI Odoo Agent Server = tools only
        ↓
GitHub + Odoo.sh + Odoo RPC + Playwright
```

No Codex, no CLI agent, no backend AI model, and no frontend UI are required. ChatGPT writes and reasons about code; this server performs the allowed operations.

## Implemented components

### Core API

- `GET /health`
- `GET /openapi.json`
- `GET /docs`
- `GET /tools/list`
- `POST /tools/call`
- `POST /mcp`

### Self verification and workflow

- `POST /qa/self-run`
- `GET /workflow/capability-check`
- `POST /workflow/full-verify`

### Repository tools

- `POST /repo/list`
- `POST /repo/read`
- `POST /repo/write`
- `POST /repo/delete`
- `POST /repo/apply-patch`
- `POST /repo/clean-pyc`

### Git tools

- `POST /git/status`
- `POST /git/current-branch`
- `POST /git/diff`
- `POST /git/commit`
- `POST /git/push`

`/git/push` defaults to the current branch and pushes `HEAD:<current_branch>` to avoid wrong-branch pushes. Protected branches such as `main`, `master`, `production`, `prod`, and `live` always require a real single-use `git.push` approval, even if global approval mode is disabled.

### Odoo.sh tools

- `POST /odoosh/health`
- `POST /odoosh/git-status`
- `POST /odoosh/log`
- `POST /odoosh/grep-errors`
- `POST /odoosh/module-update`

Odoo.sh deployment is normally triggered by GitHub push. The Odoo.sh tools verify and troubleshoot the build through SSH/logs.

### Odoo RPC tools

- `POST /odoo/health`
- `POST /odoo/module-status`
- `POST /odoo/module-install`
- `POST /odoo/module-update`
- `POST /odoo/search-read`

### Browser / Playwright tools

- `POST /browser/screenshot`

Captures an Odoo page screenshot and returns console/network errors.

### Safety and observability

- `POST /approvals/request`
- `POST /approvals/approve`
- `POST /approvals/reject`
- `GET /approvals`
- `GET /jobs`
- `GET /jobs/{job_id}`
- `GET /jobs/audit/events`

Dangerous actions can require approval. Protected branch pushes always require approval regardless of the global setting:

```text
repo.write
repo.delete
repo.apply_patch
repo.clean_pyc
git.commit
git.push
odoo.module_install
odoo.module_update
odoosh.module_update
```

Enable approval gating with:

```env
ODOO_AGENT_REQUIRE_APPROVAL=true
```

## Install locally

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env       # Windows: Copy-Item .env.example .env
uvicorn odoo_agent_server.main:app --reload --port 8080
```

Open API docs:

```text
http://localhost:8080/docs
```

## Docker

```bash
cp .env.example .env
mkdir -p repos
docker compose up --build
```

Mount your Odoo repo under `./repos` or change the volume and `ODOO_AGENT_ALLOWED_ROOTS`.

## Playwright setup

For screenshots:

```bash
playwright install chromium
```

On Linux VPS:

```bash
playwright install --with-deps chromium
```

## Connect to ChatGPT

### Option A: Custom GPT Actions

Deploy this server with HTTPS and import:

```text
https://your-domain.com/openapi.json
```

Use Bearer token auth with `ODOO_AGENT_API_TOKEN`.

### Option B: Lightweight MCP JSON-RPC bridge

Use:

```text
POST /mcp
```

Supported JSON-RPC methods:

```text
initialize
tools/list
tools/call
notifications/initialized
```

Example:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "qa.self_run",
    "arguments": {
      "project_path": "/workspace/repos/my-odoo-repo",
      "module_name": "my_custom_module",
      "run_pytest": true,
      "include_git_status": true
    }
  }
}
```

### Option C: REST tool-call bridge

Use:

```text
GET  /tools/list
POST /tools/call
```

Example:

```json
{
  "name": "qa.self_run",
  "arguments": {
    "project_path": "/workspace/repos/my-odoo-repo",
    "module_name": "my_custom_module",
    "run_pytest": true,
    "include_git_status": true
  }
}
```

This package includes both OpenAPI/Actions and a lightweight MCP JSON-RPC bridge. If your ChatGPT workspace requires a specific official remote MCP transport profile, the same service layer can be adapted without changing the Odoo/Git/Odoo.sh tools.

## Example full verify

```bash
curl -X POST http://localhost:8080/workflow/full-verify \
  -H "Authorization: Bearer change-me-to-a-long-random-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "project_path": "/workspace/repos/my-odoo-repo",
    "module_name": "my_custom_module",
    "run_pytest": true,
    "include_git_status": true,
    "include_git_diff": true,
    "include_odoosh_errors": true,
    "include_odoo_module_status": true,
    "take_browser_screenshot": true,
    "browser_start_path": "/web"
  }'
```

## Example approval flow

When `ODOO_AGENT_REQUIRE_APPROVAL=true`, a dangerous action without `approval_id` returns HTTP 409 with an approval ID.

Approve it:

```bash
curl -X POST http://localhost:8080/approvals/approve \
  -H "Authorization: Bearer change-me-to-a-long-random-secret" \
  -H "Content-Type: application/json" \
  -d '{"approval_id":"<id>","reason":"Reviewed diff"}'
```

Then repeat the original action with:

```json
{"approval_id":"<id>"}
```

## Production rules

1. Keep `ODOO_AGENT_ALLOWED_ROOTS` strict.
2. Use staging/development Odoo.sh first.
3. Keep `ODOO_AGENT_REQUIRE_APPROVAL=true` when connected to ChatGPT.
4. Store GitHub/Odoo.sh SSH keys only on this backend server.
5. Do not expose Odoo passwords, SSH keys, API keys, or cookies to ChatGPT.
6. Push only current branch unless a branch is explicitly provided.
7. Keep protected branches in `ODOO_AGENT_PROTECTED_BRANCHES`.
8. Review `GET /jobs/audit/events` regularly.

## Self-verification included in this build

The package includes tests for:

- health/root/OpenAPI readiness
- tool registry
- capability check
- Odoo module static QA
- repo read/write/path escape protection
- `.pyc` cleanup
- git status/diff
- Odoo.sh config error handling
- Odoo RPC missing config handling
- approval blocking and approval consumption
- `/tools/call` bridge
- `/workflow/full-verify`

Expected result after packaging:

```text
18 passed
```


## Hard verification additions

The latest verification pass adds regression coverage for:

```text
invalid auth rejection
symlink/path escape blocking
patch check-only non-mutation
unsafe patch path rejection
single-use approvals
protected branch push approval enforcement
runtime tool approval descriptors
/mcp initialize + tools/list + tools/call
failed full-verify job/audit persistence
```

Protected branches such as `main`, `master`, `production`, `prod`, and `live` always require a real approved `git.push` approval record, even when global approval gating is disabled.

## Full MCP conversion

All operational REST APIs are also exposed as MCP-callable tools through `POST /mcp` using JSON-RPC `tools/list` and `tools/call`.

Current MCP tool count: `33`.

```text
connector.health
workflow.capability_check
workflow.full_verify
qa.self_run
repo.list
repo.read
repo.write
repo.delete
repo.apply_patch
repo.clean_pyc
git.status
git.current_branch
git.diff
git.commit
git.push
odoosh.health
odoosh.git_status
odoosh.log
odoosh.grep_errors
odoosh.module_update
odoo.health
odoo.module_status
odoo.module_install
odoo.module_update
odoo.search_read
browser.screenshot
approvals.list
approvals.request
approvals.approve
approvals.reject
jobs.list
jobs.get
audit.list
```

`/tools/list` and MCP `tools/list` now expose the same operational tool surface. `/mcp` also returns proper per-tool input schemas generated from the Pydantic request models instead of a generic object schema.

The following REST endpoints are intentionally not exposed as MCP tools because they are protocol/documentation endpoints, not operational tools:

```text
/docs
/redoc
/openapi.json
/tools/list
/tools/call
/mcp
```

Use MCP `tools/list` instead of `/tools/list`, and MCP `tools/call` instead of `/tools/call` when connecting ChatGPT through MCP.

## ChatGPT Custom MCP App OAuth upgrade

This package now supports the OAuth 2.1 / PKCE flow required by ChatGPT custom MCP Apps, in addition to the legacy Bearer token used by Custom GPT Actions.

### New OAuth/MCP endpoints

```text
/.well-known/oauth-protected-resource
/.well-known/oauth-protected-resource/mcp
/.well-known/oauth-authorization-server
/.well-known/openid-configuration
/oauth/register
/oauth/authorize
/oauth/token
/mcp
```

### Recommended ChatGPT MCP setup

1. Keep the connector running behind HTTPS, for example with Cloudflare Tunnel:

```powershell
cloudflared tunnel --url http://localhost:8080
```

2. Set the public base URL in `.env` so discovery metadata is stable:

```env
ODOO_AGENT_PUBLIC_BASE_URL=https://your-current-tunnel.trycloudflare.com
```

3. Keep OAuth manual approval enabled for public tunnels:

```env
ODOO_AGENT_OAUTH_AUTO_APPROVE=false
```

4. Restart the FastAPI server after changing `.env`.

5. In ChatGPT custom MCP app setup, use:

```text
MCP URL: https://your-current-tunnel.trycloudflare.com/mcp
Authentication: OAuth
Registration method: Dynamic Client Registration
```

The connector advertises `/oauth/register`, `/oauth/authorize`, and `/oauth/token`. During authorization, the browser will show a small consent form. Paste your `ODOO_AGENT_API_TOKEN` into that form to approve ChatGPT. The connector then issues short-lived OAuth access tokens and refresh tokens for ChatGPT.

### Static OAuth client fallback

If ChatGPT asks for a user-defined OAuth client, use:

```env
ODOO_AGENT_OAUTH_STATIC_CLIENT_ID=odoo-agent-chatgpt
ODOO_AGENT_OAUTH_STATIC_CLIENT_SECRET=
```

Then enter this client ID in ChatGPT. Leave client secret empty unless you explicitly set `ODOO_AGENT_OAUTH_STATIC_CLIENT_SECRET`.

### Security notes

- The old `Authorization: Bearer ODOO_AGENT_API_TOKEN` still works for REST/OpenAPI Actions.
- OAuth access tokens also work for `/mcp`, `/tools/list`, and protected APIs.
- `WWW-Authenticate` now advertises protected-resource metadata when an unauthenticated MCP request is made.
- Keep `ODOO_AGENT_REQUIRE_APPROVAL=true`; write tools like `git.push`, `repo.apply_patch`, and module updates remain approval-gated.
- Rotate `ODOO_AGENT_API_TOKEN` if it was pasted anywhere public.
