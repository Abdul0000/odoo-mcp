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
