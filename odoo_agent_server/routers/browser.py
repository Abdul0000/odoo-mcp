from __future__ import annotations

from fastapi import APIRouter

from ..auth import AuthDependency
from ..schemas import BrowserCheckRequest, BrowserCheckResult
from ..services.playwright_service import PlaywrightService

router = APIRouter(prefix="/browser", tags=["browser"], dependencies=[AuthDependency])


@router.post("/screenshot", response_model=BrowserCheckResult)
async def browser_screenshot(payload: BrowserCheckRequest) -> BrowserCheckResult:
    return await PlaywrightService().screenshot_odoo_page(
        base_url=payload.base_url,
        login=payload.login,
        password=payload.password,
        start_path=payload.start_path,
        screenshot_name=payload.screenshot_name,
        timeout_seconds=payload.timeout_seconds,
        full_page=payload.full_page,
    )
