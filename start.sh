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

# Verify SSH keys are valid
if [ -f /tmp/keys/odoosh_key ]; then
  if ssh-keygen -l -f /tmp/keys/odoosh_key >/dev/null 2>&1; then
    echo "INFO: Odoo.sh SSH key verified OK"
  else
    echo "WARNING: Odoo.sh SSH key is invalid or corrupted — odoosh tools will fail"
  fi
else
  echo "WARNING: ODOOSH_SSH_PRIVATE_KEY_B64 not set — odoosh SSH tools will fail"
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
