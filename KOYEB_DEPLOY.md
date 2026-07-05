# Koyeb deployment notes

Use Dockerfile builder. Set exposed port to `8080` and public route `/`.

Set `ODOO_AGENT_PUBLIC_BASE_URL=https://{{ KOYEB_PUBLIC_DOMAIN }}` in Koyeb environment variables.

For SSH private keys, add base64 values:
- `ODOOSH_SSH_PRIVATE_KEY_B64`
- `GITHUB_SSH_PRIVATE_KEY_B64`

The startup script writes those keys to `/tmp/keys`, clones `TARGET_GIT_REPO` to `TARGET_PROJECT_PATH`, and starts uvicorn on `${PORT:-8080}`.

## Safe deployment variables

Set these in Koyeb when using Git/Odoo.sh deployment tools:

```env
TARGET_GIT_REPO=git@github.com:Abdul0000/test_18.git
TARGET_GIT_BRANCH=test_stagging_18
TARGET_PROJECT_PATH=/workspace/repos/test_18
GIT_AUTHOR_NAME=Odoo MCP Agent
GIT_AUTHOR_EMAIL=odoo-mcp@example.local
ODOOSH_ODOO_PATH=/home/odoo/src/odoo
ODOOSH_USER_ADDONS_PATH=/home/odoo/src/user
ODOOSH_WAIT_TIMEOUT_SECONDS=600
ODOOSH_WAIT_INTERVAL_SECONDS=10
```

Use `workflow.safe_deploy_module` for normal changes. It checks Odoo.sh once after push; if the pushed SHA is not deployed yet it returns a pollable `retry_later` state with `next_call=workflow.resume_job` instead of blocking the request.

## Important OAuth persistence setting

Set a stable secret so ChatGPT OAuth sessions survive Koyeb redeploys:

```env
ODOO_AGENT_OAUTH_TOKEN_SECRET=<long-random-stable-secret>
```

If omitted, the connector falls back to `ODOO_AGENT_API_TOKEN`. Keep that value stable across redeploys too. After deploying this build, reconnect the ChatGPT MCP once so it receives the new stateless refresh token format.
