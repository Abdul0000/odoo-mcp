from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the direct ChatGPT Odoo agent connector."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_token: str = Field(default="change-me", alias="ODOO_AGENT_API_TOKEN")
    allowed_roots_raw: str = Field(default="/workspace/repos,/tmp", alias="ODOO_AGENT_ALLOWED_ROOTS")
    state_dir: str = Field(default="/tmp/odoo_agent_state", alias="ODOO_AGENT_STATE_DIR")
    screenshot_dir: str = Field(default="/tmp/odoo_agent_screenshots", alias="ODOO_AGENT_SCREENSHOT_DIR")

    # Approval safety. Keep false for local development/tests; enable in production.
    require_approval: bool = Field(default=False, alias="ODOO_AGENT_REQUIRE_APPROVAL")
    approval_required_actions_raw: str = Field(
        default="repo.write,repo.delete,repo.apply_patch,repo.clean_pyc,git.commit,git.push,odoo.module_install,odoo.module_update,odoosh.module_update,workflow.safe_deploy_module",
        alias="ODOO_AGENT_APPROVAL_REQUIRED_ACTIONS",
    )
    protected_branches_raw: str = Field(default="main,master,production,prod,live", alias="ODOO_AGENT_PROTECTED_BRANCHES")

    # Git deployment guardrails. These are optional but strongly recommended in production.
    target_git_repo: str | None = Field(default=None, alias="TARGET_GIT_REPO")
    target_git_branch: str | None = Field(default=None, alias="TARGET_GIT_BRANCH")
    git_author_name: str = Field(default="Odoo MCP Agent", alias="GIT_AUTHOR_NAME")
    git_author_email: str = Field(default="odoo-mcp@example.local", alias="GIT_AUTHOR_EMAIL")

    # OAuth 2.1 / MCP App authentication. Required for ChatGPT custom MCP apps.
    public_base_url: str | None = Field(default=None, alias="ODOO_AGENT_PUBLIC_BASE_URL")
    oauth_issuer: str | None = Field(default=None, alias="ODOO_AGENT_OAUTH_ISSUER")
    oauth_default_scope: str = Field(default="odoo.agent", alias="ODOO_AGENT_OAUTH_DEFAULT_SCOPE")
    oauth_auto_approve: bool = Field(default=False, alias="ODOO_AGENT_OAUTH_AUTO_APPROVE")
    oauth_static_client_id: str = Field(default="odoo-agent-chatgpt", alias="ODOO_AGENT_OAUTH_STATIC_CLIENT_ID")
    oauth_static_client_secret: str | None = Field(default=None, alias="ODOO_AGENT_OAUTH_STATIC_CLIENT_SECRET")
    oauth_token_secret: str | None = Field(default=None, alias="ODOO_AGENT_OAUTH_TOKEN_SECRET")
    oauth_allowed_redirect_uris_raw: str = Field(default="", alias="ODOO_AGENT_OAUTH_ALLOWED_REDIRECT_URIS")
    oauth_authorization_code_ttl_seconds: int = Field(default=600, alias="ODOO_AGENT_OAUTH_AUTH_CODE_TTL_SECONDS")
    oauth_access_token_ttl_seconds: int = Field(default=3600, alias="ODOO_AGENT_OAUTH_ACCESS_TOKEN_TTL_SECONDS")
    oauth_refresh_token_ttl_seconds: int = Field(default=2592000, alias="ODOO_AGENT_OAUTH_REFRESH_TOKEN_TTL_SECONDS")

    # Odoo RPC defaults.
    odoo_base_url: str | None = Field(default=None, alias="ODOO_BASE_URL")
    odoo_db_name: str | None = Field(default=None, alias="ODOO_DB_NAME")
    odoo_login: str | None = Field(default=None, alias="ODOO_LOGIN")
    odoo_password: str | None = Field(default=None, alias="ODOO_PASSWORD")

    # Odoo.sh SSH connector settings. Use a development/staging build host.
    odoosh_ssh_host: str | None = Field(default=None, alias="ODOOSH_SSH_HOST")
    odoosh_ssh_user: str = Field(default="odoo", alias="ODOOSH_SSH_USER")
    odoosh_ssh_port: int = Field(default=22, alias="ODOOSH_SSH_PORT")
    odoosh_ssh_key_path: str | None = Field(default=None, alias="ODOOSH_SSH_KEY_PATH")
    odoosh_db_name: str | None = Field(default=None, alias="ODOOSH_DB_NAME")
    odoosh_odoo_path: str = Field(default="/home/odoo/src/odoo", alias="ODOOSH_ODOO_PATH")
    odoosh_user_addons_path: str = Field(default="/home/odoo/src/user", alias="ODOOSH_USER_ADDONS_PATH")
    odoosh_wait_timeout_seconds: int = Field(default=600, alias="ODOOSH_WAIT_TIMEOUT_SECONDS")
    odoosh_wait_interval_seconds: int = Field(default=10, alias="ODOOSH_WAIT_INTERVAL_SECONDS")

    @property
    def allowed_roots(self) -> List[Path]:
        roots: list[Path] = []
        for raw in self.allowed_roots_raw.split(","):
            raw = raw.strip()
            if raw:
                roots.append(Path(raw).expanduser().resolve())
        return roots

    @property
    def approval_required_actions(self) -> set[str]:
        return {item.strip() for item in self.approval_required_actions_raw.split(",") if item.strip()}

    @property
    def protected_branches(self) -> set[str]:
        return {item.strip() for item in self.protected_branches_raw.split(",") if item.strip()}


    @property
    def oauth_allowed_redirect_uris(self) -> list[str]:
        return [item.strip() for item in self.oauth_allowed_redirect_uris_raw.split(",") if item.strip()]

    @property
    def state_path(self) -> Path:
        path = Path(self.state_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
