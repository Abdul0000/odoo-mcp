# Render.com Free Tier Deployment Guide

## What This Connector Does

ChatGPT → MCP tools → This connector (Render) → GitHub SSH → Your repo → Odoo.sh auto-deploy

ChatGPT can: read files, patch code, commit, push to GitHub, wait for Odoo.sh deploy, upgrade module.

---

## Step 1 — Prepare SSH Keys

You need 2 SSH key pairs:

### GitHub SSH Key (for git push)
```bash
ssh-keygen -t ed25519 -C "odoo-mcp-agent" -f ~/.ssh/github_mcp_key -N ""
# Add ~/.ssh/github_mcp_key.pub to GitHub → Settings → SSH Keys
# Encode private key for Render:
base64 -w 0 ~/.ssh/github_mcp_key
# Copy this output — you will paste it as GITHUB_SSH_PRIVATE_KEY_B64
```

### Odoo.sh SSH Key (for module update via SSH)
```bash
ssh-keygen -t ed25519 -C "odoo-mcp-odoosh" -f ~/.ssh/odoosh_mcp_key -N ""
# Add ~/.ssh/odoosh_mcp_key.pub to Odoo.sh → Settings → SSH Keys
# Encode:
base64 -w 0 ~/.ssh/odoosh_mcp_key
# Copy output — paste as ODOOSH_SSH_PRIVATE_KEY_B64
```

---

## Step 2 — Create Render Service

1. Go to https://render.com → New → Web Service
2. Connect your GitHub repo (push this codebase there first)
3. Settings:
   - **Runtime:** Docker
   - **Branch:** main
   - **Instance Type:** Free (512MB RAM is enough)
   - **Port:** 8080

---

## Step 3 — Set Environment Variables on Render

Go to your service → Environment → Add the following:

### Required — Auth
```
ODOO_AGENT_API_TOKEN        = your-long-random-secret-here
```
Generate one: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`

### Required — SSH Keys (base64 encoded, from Step 1)
```
GITHUB_SSH_PRIVATE_KEY_B64  = <paste base64 output from step 1>
ODOOSH_SSH_PRIVATE_KEY_B64  = <paste base64 output from step 1>
```

### Required — Your Repo
```
TARGET_GIT_REPO             = git@github.com:YourUser/your-odoo-repo.git
TARGET_GIT_BRANCH           = your-branch-name
TARGET_PROJECT_PATH         = /workspace/repos/your-repo-folder
WORKSPACE_ROOT              = /workspace/repos
ODOO_AGENT_ALLOWED_ROOTS    = /workspace/repos,/tmp
```

### Required — Odoo.sh SSH
```
ODOOSH_SSH_HOST             = your-build.odoo.com
ODOOSH_SSH_USER             = odoo
ODOOSH_SSH_PORT             = 22
ODOOSH_DB_NAME              = your-database-name
ODOOSH_ODOO_PATH            = /home/odoo/src/odoo
ODOOSH_USER_ADDONS_PATH     = /home/odoo/src/user
```

### Required — Odoo XML-RPC (for module status check)
```
ODOO_BASE_URL               = https://your-instance.odoo.com
ODOO_DB_NAME                = your-database-name
ODOO_LOGIN                  = admin@example.com
ODOO_PASSWORD               = your-odoo-password
```

### Required — OAuth for ChatGPT MCP
```
ODOO_AGENT_PUBLIC_BASE_URL  = https://your-service-name.onrender.com
ODOO_AGENT_OAUTH_TOKEN_SECRET = another-long-random-secret
```
Generate: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`

### Optional
```
GIT_AUTHOR_NAME             = Odoo MCP Agent
GIT_AUTHOR_EMAIL            = odoo-mcp@yourcompany.com
ODOO_AGENT_OAUTH_AUTO_APPROVE = false
ODOOSH_WAIT_TIMEOUT_SECONDS = 600
ODOOSH_WAIT_INTERVAL_SECONDS = 10
```

---

## Step 4 — IMPORTANT: Render Free Tier Disk Warning

**Render free tier has NO persistent disk.** This means:
- On every redeploy/restart, `/workspace/repos` is wiped
- The connector re-clones your repo on startup via `start.sh` automatically
- **But:** if Render sleeps your service (free tier sleeps after 15 min inactivity) and wakes it up, the repo will be gone

**Solutions (pick one):**

### Option A — Use repo.prepare before every ChatGPT session (FREE)
Tell ChatGPT: "Always call repo.prepare first before any coding task."
`start.sh` clones on boot, `repo.prepare` re-clones/pulls if needed.
This is the zero-cost approach and works fine.

### Option B — Render Persistent Disk ($0.25/GB/month)
In Render → your service → Disks → Add Disk:
- Mount path: `/workspace`
- Size: 1GB ($0.25/month)

### Option C — Move to Railway.app (free persistent storage)
Railway gives 1GB persistent storage on free tier.

---

## Step 5 — Connect ChatGPT MCP

1. In ChatGPT → Settings → Connected Apps → Add MCP
2. MCP URL: `https://your-service-name.onrender.com/mcp`
3. Authentication: OAuth
4. It will redirect you to your connector's consent page
5. Enter your `ODOO_AGENT_API_TOKEN` to authorize
6. Done — ChatGPT now has all tools

---

## Step 6 — Test the Connection

In ChatGPT, type:
```
Call workflow.capability_check tool and tell me what's configured
```

Expected: shows git, ssh, odoo_rpc_config status.

Then test the full pipeline:
```
Call workflow.coding_task_plan with task="test pipeline"
```

---

## ChatGPT Coding Workflow (How to Use)

Tell ChatGPT:
```
I want you to add a new field 'customer_tier' (selection field with 
values: bronze, silver, gold) to the kashees_loyalty_card module.
Use the MCP coding tools to do it end-to-end.
```

ChatGPT will automatically:
1. `repo.prepare` — pull latest code
2. `repo.list_custom_modules` — find your modules
3. `repo.read` — read the module files
4. `repo.apply_patch` — write the code changes
5. `git.diff` → `qa.self_run` — verify
6. `git.commit` → `git.push` — deploy to GitHub
7. `odoosh.check_commit` (polling loop) — wait for Odoo.sh
8. `odoosh.module_update` — upgrade module
9. `odoo.module_status` — confirm installed

---

## Troubleshooting

### "SSH auth failed" on git push
- Check `GITHUB_SSH_PRIVATE_KEY_B64` is correct (no newlines)
- Check GitHub SSH key is added to your GitHub account
- Check `TARGET_GIT_REPO` uses SSH format: `git@github.com:user/repo.git`

### "couldn't connect your account" in ChatGPT
- Set `ODOO_AGENT_OAUTH_TOKEN_SECRET` to a stable value
- This prevents session loss on Render redeploys

### Tool call loop / hang
- This is fixed in this build via `odoosh.check_commit` (non-blocking)
- Make sure ChatGPT uses `odoosh.check_commit` NOT `odoosh.wait_for_commit`
- Tell ChatGPT: "Use odoosh.check_commit in a polling loop, not wait_for_commit"

### Render service sleeping (free tier)
- Free tier sleeps after 15 min — first request after sleep takes ~30 sec
- This is normal — ChatGPT will retry
- Use Render's "Health Check" to keep it warm (costs nothing extra)
  - Health Check Path: `/health`

### "Module path not found" 
- Run `repo.prepare` first — it clones the repo
- Check `TARGET_PROJECT_PATH` matches `WORKSPACE_ROOT/your-repo-name`

---

## Render Free Tier Limits

| Resource | Limit | Impact |
|---|---|---|
| RAM | 512MB | Fine for connector + git |
| CPU | Shared | git push may be slow |
| Disk | Ephemeral | Repo wiped on restart |
| Sleep | 15min inactivity | First request slow |
| Build time | 400 min/month | Enough for normal use |
| Bandwidth | 100GB/month | More than enough |

