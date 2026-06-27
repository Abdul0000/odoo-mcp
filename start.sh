#!/bin/sh
set -eu

mkdir -p /tmp/keys /workspace/repos /tmp/odoo_agent_state /tmp/odoo_agent_screenshots /root/.ssh
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
    if [ -n "$(git status --porcelain)" ]; then
      echo "Repo has local changes; not fetching/resetting target branch."
    else
      if [ -n "$TARGET_GIT_BRANCH" ]; then
        git fetch origin "$TARGET_GIT_BRANCH"
        git checkout "$TARGET_GIT_BRANCH"
        git reset --hard "origin/$TARGET_GIT_BRANCH"
      else
        git fetch origin
      fi
    fi
  fi

  git -C "$TARGET_PROJECT_PATH" config user.name "$GIT_AUTHOR_NAME"
  git -C "$TARGET_PROJECT_PATH" config user.email "$GIT_AUTHOR_EMAIL"
fi

exec uvicorn odoo_agent_server.main:app --host 0.0.0.0 --port "${PORT:-8080}"
