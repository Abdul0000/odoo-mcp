# Dual ChatGPT + Qwen MCP Setup

This build exposes one MCP endpoint that works with both:

- ChatGPT custom MCP connector using OAuth / PKCE / Dynamic Client Registration.
- Qwen or Qwen-Agent using a direct connector token.

## Endpoint

```text
https://YOUR_RENDER_DOMAIN/mcp
```

For this deployment:

```text
https://odoo-mcp-1-jh8m.onrender.com/mcp
```

## Required Render environment variables

```env
ODOO_AGENT_PUBLIC_BASE_URL=https://odoo-mcp-1-jh8m.onrender.com
ODOO_AGENT_API_TOKEN=replace_with_a_long_random_secret
ODOO_AGENT_OAUTH_TOKEN_SECRET=replace_with_another_long_random_secret
ODOO_AGENT_OAUTH_STATIC_CLIENT_ID=odoo-agent-chatgpt
ODOO_AGENT_OAUTH_STATIC_CLIENT_SECRET=
ODOO_AGENT_OAUTH_AUTO_APPROVE=false
ODOO_AGENT_OAUTH_ACCEPT_UNKNOWN_DCR_CLIENTS=true
ODOO_AGENT_QUERY_TOKEN_AUTH_ENABLED=true
```

For stable OAuth sessions across redeploys, prefer a persistent path:

```env
ODOO_AGENT_STATE_DIR=/workspace/odoo_agent_state
```

Then mount a Render persistent disk at `/workspace`.

## ChatGPT setup

Use OAuth authentication.

Recommended:

```text
MCP URL: https://odoo-mcp-1-jh8m.onrender.com/mcp
Authentication: OAuth
Dynamic Client Registration: enabled
```

If ChatGPT asks for a static client:

```text
Client ID: odoo-agent-chatgpt
Client Secret: leave empty
```

When the authorization page opens, paste `ODOO_AGENT_API_TOKEN`.

If you see this error:

```json
{"detail":"Unknown OAuth client_id"}
```

Enable:

```env
ODOO_AGENT_OAUTH_ACCEPT_UNKNOWN_DCR_CLIENTS=true
```

Then redeploy and reconnect ChatGPT.

## Qwen setup

Use Streamable HTTP, not SSE.

Preferred Qwen MCP JSON:

```json
{
  "mcpServers": {
    "odoo": {
      "type": "streamable-http",
      "url": "https://odoo-mcp-1-jh8m.onrender.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_ODOO_AGENT_API_TOKEN"
      },
      "sse_read_timeout": 300
    }
  }
}
```

If the Qwen app has separate fields:

```text
Name: odoo
Transport: streamable-http / HTTP
URL: https://odoo-mcp-1-jh8m.onrender.com/mcp
Header name: Authorization
Header value: Bearer YOUR_ODOO_AGENT_API_TOKEN
```

If Qwen cannot send headers, enable query-token fallback:

```env
ODOO_AGENT_QUERY_TOKEN_AUTH_ENABLED=true
```

Then use this URL in Qwen:

```text
https://odoo-mcp-1-jh8m.onrender.com/mcp?access_token=YOUR_ODOO_AGENT_API_TOKEN
```

Only use query-token fallback over HTTPS and only for your private connector URL.

## Quick tests

### Direct token / Qwen-style test

PowerShell:

```powershell
$TOKEN="paste_ODOO_AGENT_API_TOKEN_here"
$BODY='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
Invoke-RestMethod `
  -Uri "https://odoo-mcp-1-jh8m.onrender.com/mcp" `
  -Method Post `
  -ContentType "application/json" `
  -Headers @{Authorization="Bearer $TOKEN"} `
  -Body $BODY
```

Query-token fallback test:

```powershell
$TOKEN="paste_ODOO_AGENT_API_TOKEN_here"
$BODY='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
Invoke-RestMethod `
  -Uri "https://odoo-mcp-1-jh8m.onrender.com/mcp?access_token=$TOKEN" `
  -Method Post `
  -ContentType "application/json" `
  -Body $BODY
```
