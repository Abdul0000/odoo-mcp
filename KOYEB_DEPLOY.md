# Koyeb deployment notes

Use Dockerfile builder. Set exposed port to `8080` and public route `/`.

Set `ODOO_AGENT_PUBLIC_BASE_URL=https://{{ KOYEB_PUBLIC_DOMAIN }}` in Koyeb environment variables.

For SSH private keys, add base64 values:
- `ODOOSH_SSH_PRIVATE_KEY_B64`
- `GITHUB_SSH_PRIVATE_KEY_B64`

The startup script writes those keys to `/tmp/keys`, clones `TARGET_GIT_REPO` to `TARGET_PROJECT_PATH`, and starts uvicorn on `${PORT:-8080}`.
