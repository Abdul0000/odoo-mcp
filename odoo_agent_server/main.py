from __future__ import annotations

from fastapi import FastAPI

from .routers.approvals import router as approvals_router
from .routers.browser import router as browser_router
from .routers.git import router as git_router
from .routers.jobs import router as jobs_router
from .routers.mcp import router as mcp_router
from .routers.odoo import router as odoo_router
from .routers.odoosh import router as odoosh_router
from .routers.oauth import router as oauth_router
from .routers.qa import router as qa_router
from .routers.repo import router as repo_router
from .routers.tools import router as tools_router
from .routers.workflow import router as workflow_router

app = FastAPI(
    title="Odoo ChatGPT Direct Connector",
    version="1.3.0",
    description=(
        "Backend-only connector for direct ChatGPT Odoo development automation: "
        "repo patching, self QA, Git, Odoo.sh, Odoo RPC, Playwright screenshots, "
        "approvals, jobs/audit, full MCP conversion, OAuth 2.1/PKCE MCP auth, and tool-call bridge."
    ),
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "odoo-chatgpt-direct-connector"}


@app.get("/")
def root() -> dict[str, object]:
    return {
        "service": "Odoo ChatGPT Direct Connector",
        "ui": "not_required",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "tools": "/tools/list",
        "mcp": "/mcp",
        "workflow": ["/workflow/capability-check", "/workflow/full-verify", "/workflow/safe-deploy-module"],
    }


app.include_router(oauth_router)
app.include_router(qa_router)
app.include_router(repo_router)
app.include_router(git_router)
app.include_router(odoosh_router)
app.include_router(odoo_router)
app.include_router(browser_router)
app.include_router(approvals_router)
app.include_router(jobs_router)
app.include_router(tools_router)
app.include_router(mcp_router)
app.include_router(workflow_router)
