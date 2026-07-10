# Self Verification Report

Package: `odoo_chatgpt_direct_connector_full_mcp_oauth.zip`

## Result

```text
39 passed
```

## Verified capabilities

- Existing REST/OpenAPI bearer-token auth still works.
- Existing MCP JSON-RPC bridge still exposes the complete operational tool surface.
- OAuth 2.1 / PKCE discovery metadata is public.
- Protected-resource metadata is available for MCP clients.
- Unauthenticated `/mcp` calls return `WWW-Authenticate` with `resource_metadata`.
- Dynamic Client Registration works through `/oauth/register`.
- Authorization-code flow with PKCE S256 works.
- Manual consent page requires `ODOO_AGENT_API_TOKEN` when auto-approval is disabled.
- `/oauth/token` exchanges authorization codes for access/refresh tokens.
- Refresh-token grant issues a new access token.
- OAuth access tokens authenticate MCP `tools/call` requests.
- Legacy API bearer token still authenticates REST `/tools/list`.
- Existing hard safety tests still pass: path safety, approvals, protected branches, and tool parity.

## Not verified in this environment

- Live ChatGPT custom MCP registration screen, because it requires the user's ChatGPT account/workspace UI.
- Real Odoo.sh build/deploy execution, because live credentials and a running tunnel are user-local.

## Expected ChatGPT setup

```text
MCP URL: https://your-public-url/mcp
Authentication: OAuth
Registration method: Dynamic Client Registration
```

If using a temporary Cloudflare quick tunnel, set:

```env
ODOO_AGENT_PUBLIC_BASE_URL=https://your-current-tunnel.trycloudflare.com
```

Then restart the connector.

## Added safe-deploy checks

Run:

```bash
pytest -q
```

The suite now includes guard tests for Git identity setup, target branch validation, `.git` write blocking, Odoo.sh update log classification, and approval failure handling.

## 2026-06-27 OAuth redeploy/session hardening

Additional fix after live ChatGPT MCP testing showed intermittent `We couldn't connect your account` after Koyeb redeploys:

- OAuth access/refresh tokens are now stateless HMAC-signed (`sat_`/`srt_`) using `ODOO_AGENT_OAUTH_TOKEN_SECRET` or `ODOO_AGENT_API_TOKEN`.
- Refresh tokens survive container restarts and empty `/tmp` state directories.
- Existing file-backed OAuth state remains for authorization codes and backward compatibility.
- Standalone `odoosh.module_update` accepts `expected_sha` and waits for Odoo.sh to deploy it before upgrading.
- `workflow.safe_deploy_module` now records a visible `git_diff` step before QA/commit.

Verification:

```text
45 passed
```

## v10 server-side wait / polling verification

Implemented and verified that background module deploy/install/update and `jobs.get` support server-side wait mode. This prevents repeated ChatGPT-side polling loops from causing message stream failures.

Verification:

```text
63 passed
python compile passed
```

New behavior:

- `jobs.get` waits by default up to `wait_timeout_seconds`.
- `odoo.module_deploy`, `odoo.module_install`, and `odoo.module_update` wait by default after starting their background job.
- Direct SSH module tools also wait by default after starting the background job.
- If still running, responses return `finished=false`, `job_id`, and the next `jobs.get` call to use.
