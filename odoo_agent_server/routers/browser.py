from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..auth import AuthDependency
from ..config import settings
from ..schemas import BrowserCheckRequest, BrowserCheckResult, BrowserLoginRequest, PosFlowCheckRequest, PosFlowCheckResult
from ..services.browser_cdp_service import CdpBrowserService

router = APIRouter(prefix="/browser", tags=["browser"], dependencies=[AuthDependency])


def _require_browser_tools_enabled() -> None:
    if not settings.browser_tools_enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error_type": "browser_tools_disabled",
                "message": (
                    "Browser tools are disabled for the small-instance profile. "
                    "Enable ODOO_AGENT_BROWSER_TOOLS_ENABLED=true and configure an external ODOO_AGENT_CDP_URL."
                ),
            },
        )


@router.post("/login", response_model=BrowserCheckResult)
async def browser_login(payload: BrowserLoginRequest) -> BrowserCheckResult:
    _require_browser_tools_enabled()
    return await CdpBrowserService().login_odoo(payload)


@router.post("/screenshot", response_model=BrowserCheckResult)
async def browser_screenshot(payload: BrowserCheckRequest) -> BrowserCheckResult:
    _require_browser_tools_enabled()
    return await CdpBrowserService().screenshot_odoo_page(
        base_url=payload.base_url,
        login=payload.login,
        password=payload.password,
        start_path=payload.start_path,
        screenshot_name=payload.screenshot_name,
        timeout_seconds=payload.timeout_seconds,
        full_page=payload.full_page,
        client_name=payload.client_name,
        reuse_session=payload.reuse_session,
        login_strategy=payload.login_strategy,
        wait_selector=payload.wait_selector,
        actions=payload.actions,
        diagnostics=payload.diagnostics,
        debug_contrast=payload.debug_contrast,
        debug_contrast_screenshot_name=payload.debug_contrast_screenshot_name,
        restore_theme_after_verify=payload.restore_theme_after_verify,
        cleanup_screenshot_dir=payload.cleanup_screenshot_dir,
        cleanup_screenshot_retention_seconds=payload.cleanup_screenshot_retention_seconds,
        delete_screenshots_after_verify=payload.delete_screenshots_after_verify,
        ui_profile=payload.ui_profile,
        viewport_width=payload.viewport_width,
        viewport_height=payload.viewport_height,
        device_scale_factor=payload.device_scale_factor,
        required_selectors=payload.required_selectors,
        forbidden_selectors=payload.forbidden_selectors,
        required_text=payload.required_text,
        forbidden_text=payload.forbidden_text,
        min_elements=payload.min_elements,
        wait_until=payload.wait_until,
        require_all_actions_pass=payload.require_all_actions_pass,
        screenshot_mode=payload.screenshot_mode,
        screenshot_format=payload.screenshot_format,
        screenshot_quality=payload.screenshot_quality,
        include_screenshot_base64=payload.include_screenshot_base64,
        screenshot_base64_max_bytes=payload.screenshot_base64_max_bytes,
        reference_image_path=payload.reference_image_path,
        visual_compare_mode=payload.visual_compare_mode,
        max_visual_diff_ratio=payload.max_visual_diff_ratio,
        human_pos_navigation=payload.human_pos_navigation,
        pos_config_id=payload.pos_config_id,
        pos_config_name=payload.pos_config_name,
        scenario=payload.scenario,
        scenario_goal=payload.scenario_goal,
        scenario_product_count=payload.scenario_product_count,
        cleanup_after_verify=payload.cleanup_after_verify,
        require_functional_proof=payload.require_functional_proof,
        code_aware=payload.code_aware,
        project_path=payload.project_path,
        module_name=payload.module_name,
        feature_goal=payload.feature_goal,
    )




@router.get("/screenshots/{filename}")
async def browser_screenshot_file(filename: str):
    """Serve a generated browser screenshot through the authenticated MCP API.

    This makes screenshots available on Render Free without SSH/shell access.
    Only simple basenames inside ODOO_AGENT_SCREENSHOT_DIR are allowed.
    """
    safe = Path(filename).name
    if not safe or safe != filename or not safe.lower().endswith((".png", ".jpg", ".jpeg")):
        raise HTTPException(status_code=400, detail="invalid screenshot filename")
    root = Path(settings.screenshot_dir).expanduser().resolve()
    path = (root / safe).resolve()
    if root not in [path, *path.parents] or not path.is_file():
        raise HTTPException(status_code=404, detail="screenshot not found")
    return FileResponse(path)

@router.post("/pos-flow", response_model=PosFlowCheckResult)
async def browser_pos_flow(payload: PosFlowCheckRequest) -> PosFlowCheckResult:
    """Run a real end-to-end POS interaction (login -> open POS -> click
    product -> payment -> validate) in one persistent session, with a
    console/network capture at every step. Use this instead of
    /browser/screenshot whenever the change under test touches POS OWL code —
    a single cold-load screenshot never exercises the click/payment surface
    where POS component bugs actually throw.
    """
    _require_browser_tools_enabled()
    return await CdpBrowserService().run_pos_flow(payload)
