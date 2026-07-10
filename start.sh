#!/bin/sh
set -eu

mkdir -p /tmp/keys /workspace/repos /tmp/odoo_agent_state /tmp/odoo_agent_screenshots /root/.ssh
mkdir -p /home/odoo/src/user /home/odoo/src/enterprise /home/odoo/src/odoo
chmod 700 /root/.ssh

write_b64_key() {
  var_name="$1"
  out_path="$2"
  value="$(printenv "$var_name" || true)"
  if [ -n "$value" ]; then
    printf '%s' "$value" | base64 -d > "$out_path"
    chmod 600 "$out_path"
  fi
}

write_b64_key ODOOSH_SSH_PRIVATE_KEY_B64 /tmp/keys/odoosh_key
write_b64_key GITHUB_SSH_PRIVATE_KEY_B64 /tmp/keys/github_key

if [ -f /tmp/keys/odoosh_key ]; then
  export ODOOSH_SSH_KEY_PATH="${ODOOSH_SSH_KEY_PATH:-/tmp/keys/odoosh_key}"
fi

if [ -f /tmp/keys/github_key ]; then
  export GIT_SSH_COMMAND="ssh -i /tmp/keys/github_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
fi

ssh-keyscan github.com >> /root/.ssh/known_hosts 2>/dev/null || true

start_chromium_cdp() {
  if [ -z "${ODOO_AGENT_CDP_URL:-}" ]; then
    export ODOO_AGENT_CDP_URL="http://127.0.0.1:9222"
  fi

  case "$ODOO_AGENT_CDP_URL" in
    http://127.0.0.1:9222|http://localhost:9222|http://0.0.0.0:9222)
      ;;
    *)
      echo "INFO: ODOO_AGENT_CDP_URL points to an external browser service: $ODOO_AGENT_CDP_URL"
      return 0
      ;;
  esac

  if curl -fsS "$ODOO_AGENT_CDP_URL/json/version" >/tmp/odoo_agent_cdp_version.json 2>/dev/null; then
    echo "INFO: Chromium CDP already reachable at $ODOO_AGENT_CDP_URL"
    cat /tmp/odoo_agent_cdp_version.json || true
    echo
    return 0
  fi

  CHROME_BIN="${CHROME_BIN:-}"
  if [ -z "$CHROME_BIN" ]; then
    for candidate in chromium chromium-browser google-chrome google-chrome-stable; do
      if command -v "$candidate" >/dev/null 2>&1; then
        CHROME_BIN="$candidate"
        break
      fi
    done
  fi

  if [ -z "$CHROME_BIN" ]; then
    echo "WARNING: No Chromium/Chrome binary found. Browser screenshot tools will be unavailable."
    echo "WARNING: Install chromium in Dockerfile or set CHROME_BIN to the browser executable."
    return 0
  fi

  # Clear stale Chromium processes/profile locks left by a previous container run.
  pkill -f "[c]hromium.*remote-debugging-port=9222" 2>/dev/null || true
  rm -rf /tmp/odoo_agent_chrome
  mkdir -p /tmp/odoo_agent_chrome

  echo "INFO: Starting Chromium CDP on http://127.0.0.1:9222 using $CHROME_BIN"
  "$CHROME_BIN" \
    --headless=new \
    --no-sandbox \
    --disable-setuid-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --disable-software-rasterizer \
    --disable-background-networking \
    --disable-sync \
    --disable-extensions \
    --no-first-run \
    --no-default-browser-check \
    --no-zygote \
    --remote-debugging-address=127.0.0.1 \
    --remote-debugging-port=9222 \
    --remote-allow-origins='*' \
    --user-data-dir=/tmp/odoo_agent_chrome \
    about:blank >/tmp/odoo_agent_chrome.log 2>&1 &
  CHROME_PID=$!

  # Chromium startup time varies on small/free containers. Wait for the real
  # DevTools endpoint instead of assuming it is ready after a fixed 2 seconds.
  CDP_READY=0
  attempt=1
  while [ "$attempt" -le 30 ]; do
    if curl -fsS "$ODOO_AGENT_CDP_URL/json/version" >/tmp/odoo_agent_cdp_version.json 2>/dev/null; then
      CDP_READY=1
      break
    fi
    if ! kill -0 "$CHROME_PID" 2>/dev/null; then
      echo "ERROR: Chromium exited before CDP became reachable."
      break
    fi
    sleep 1
    attempt=$((attempt + 1))
  done

  if [ "$CDP_READY" -eq 1 ]; then
    echo "INFO: Chromium CDP ready at $ODOO_AGENT_CDP_URL"
    cat /tmp/odoo_agent_cdp_version.json || true
    echo
  else
    echo "WARNING: Chromium was started but CDP is not reachable at $ODOO_AGENT_CDP_URL"
    echo "WARNING: Chromium PID: $CHROME_PID"
    tail -120 /tmp/odoo_agent_chrome.log 2>/dev/null || true
  fi
}

start_chromium_cdp

# Verify SSH keys are valid
if [ -f /tmp/keys/odoosh_key ]; then
  if ssh-keygen -l -f /tmp/keys/odoosh_key >/dev/null 2>&1; then
    echo "INFO: Odoo.sh SSH key verified OK"
  else
    echo "WARNING: Odoo.sh SSH key is invalid or corrupted — odoosh tools will fail"
  fi
else
  if python3 - <<'PY'
import json, os, sys
raw = os.environ.get("ODOO_CLIENTS", "[]")
try:
    data = json.loads(raw.strip().strip("'"))
except Exception:
    data = []
found = any(isinstance(c, dict) and (c.get("odoosh_ssh_private_key_b64") or c.get("odoosh_ssh_key_path")) for c in data if isinstance(c, dict))
sys.exit(0 if found else 1)
PY
  then
    echo "INFO: Per-client Odoo.sh SSH key configured in ODOO_CLIENTS"
  else
    echo "WARNING: No global or per-client Odoo.sh SSH key configured — odoosh SSH tools will fail"
  fi
fi

if [ -n "${TARGET_GIT_REPO:-}" ] && echo "$TARGET_GIT_REPO" | grep -q "^git@"; then
  if [ -f /tmp/keys/github_key ]; then
    echo "INFO: GitHub SSH key present for SSH repo"
  else
    echo "WARNING: TARGET_GIT_REPO uses SSH but GITHUB_SSH_PRIVATE_KEY_B64 not set"
  fi
fi

TARGET_PROJECT_PATH="${TARGET_PROJECT_PATH:-/workspace/repos/test_18}"
TARGET_GIT_BRANCH="${TARGET_GIT_BRANCH:-}"
GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-Odoo MCP Agent}"
GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-odoo-mcp@example.local}"

if [ -n "${TARGET_GIT_REPO:-}" ]; then
  mkdir -p "$(dirname "$TARGET_PROJECT_PATH")"
  if [ ! -d "$TARGET_PROJECT_PATH/.git" ]; then
    if [ -n "$TARGET_GIT_BRANCH" ]; then
      git clone --branch "$TARGET_GIT_BRANCH" "$TARGET_GIT_REPO" "$TARGET_PROJECT_PATH"
    else
      git clone "$TARGET_GIT_REPO" "$TARGET_PROJECT_PATH"
    fi
  else
    cd "$TARGET_PROJECT_PATH"
    LOCAL_CHANGES="$(git status --porcelain)"
    if [ -n "$LOCAL_CHANGES" ]; then
      echo "INFO: Repo has local changes — skipping fetch to preserve agent work-in-progress"
      echo "$LOCAL_CHANGES"
    else
      if [ -n "$TARGET_GIT_BRANCH" ]; then
        git fetch origin "$TARGET_GIT_BRANCH" || echo "WARNING: git fetch failed — using existing local state"
        git checkout "$TARGET_GIT_BRANCH" || true
        # Use --ff-only instead of --hard-reset to never destroy agent commits
        git pull --ff-only origin "$TARGET_GIT_BRANCH" || echo "INFO: ff-only pull skipped (diverged or up-to-date)"
      else
        git fetch origin || echo "WARNING: git fetch failed"
      fi
    fi
  fi

  git -C "$TARGET_PROJECT_PATH" config user.name "$GIT_AUTHOR_NAME"
  git -C "$TARGET_PROJECT_PATH" config user.email "$GIT_AUTHOR_EMAIL"
fi

exec uvicorn odoo_agent_server.main:app --host 0.0.0.0 --port "${PORT:-8080}"
