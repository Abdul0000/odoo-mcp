from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urljoin

from ..config import settings
from ..schemas import BrowserCheckResult

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class PlaywrightService:
    """Playwright connector for Odoo UI screenshots and console/network checks."""

    async def screenshot_odoo_page(
        self,
        base_url: str | None,
        login: str | None,
        password: str | None,
        start_path: str,
        screenshot_name: str,
        timeout_seconds: int = 45,
        full_page: bool = True,
    ) -> BrowserCheckResult:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # noqa: BLE001
            return BrowserCheckResult(status="failed", error=f"Playwright is not installed or browsers are missing: {exc}")

        url_base = (base_url or os.environ.get("ODOO_BASE_URL") or settings.odoo_base_url or "").rstrip("/")
        user = login or os.environ.get("ODOO_LOGIN") or settings.odoo_login
        secret = password or os.environ.get("ODOO_PASSWORD") or settings.odoo_password
        if not url_base:
            return BrowserCheckResult(status="failed", error="base_url or ODOO_BASE_URL is required")

        safe_name = _SAFE_NAME_RE.sub("_", screenshot_name or "odoo_page.png")
        if not safe_name.lower().endswith((".png", ".jpg", ".jpeg")):
            safe_name += ".png"
        output_dir = Path(settings.screenshot_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / safe_name

        console_errors: list[str] = []
        network_errors: list[str] = []
        final_url: str | None = None
        title: str | None = None

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                page.set_default_timeout(timeout_seconds * 1000)

                page.on("console", lambda msg: console_errors.append(f"{msg.type}: {msg.text}") if msg.type in {"error", "warning"} else None)
                page.on("pageerror", lambda err: console_errors.append(f"pageerror: {err}"))
                page.on("response", lambda resp: network_errors.append(f"{resp.status} {resp.url}") if resp.status >= 400 else None)

                await page.goto(urljoin(url_base + "/", "web/login"), wait_until="domcontentloaded")
                if user and secret:
                    # Odoo login page fields are stable across recent versions.
                    await page.fill('input[name="login"]', user)
                    await page.fill('input[name="password"]', secret)
                    await page.click('button[type="submit"]')
                    await page.wait_for_load_state("networkidle")
                target_url = urljoin(url_base + "/", start_path.lstrip("/"))
                await page.goto(target_url, wait_until="networkidle")
                final_url = page.url
                title = await page.title()
                page_markers = await self._detect_odoo_breakage(page)
                console_errors.extend(page_markers)
                await page.screenshot(path=str(output_path), full_page=full_page)
                await browser.close()
            return BrowserCheckResult(
                status="failed" if page_markers else "ok",
                url=final_url,
                title=title,
                screenshot_path=str(output_path),
                console_errors=console_errors[-50:],
                network_errors=network_errors[-50:],
                error="; ".join(page_markers) if page_markers else None,
            )
        except Exception as exc:  # noqa: BLE001
            return BrowserCheckResult(
                status="failed",
                url=final_url,
                title=title,
                screenshot_path=str(output_path) if output_path.exists() else None,
                console_errors=console_errors[-50:],
                network_errors=network_errors[-50:],
                error=str(exc),
            )

    async def _detect_odoo_breakage(self, page) -> list[str]:
        markers: list[str] = []
        selectors = [
            ".o_error_dialog",
            ".o_dialog_error",
            ".o_technical_modal",
            ".o_notification_manager .border-danger",
            ".o_notification_manager .text-danger",
        ]
        for selector in selectors:
            try:
                count = await page.locator(selector).count()
            except Exception:
                count = 0
            if count:
                markers.append(f"odoo_ui_error_selector:{selector}")

        try:
            body_text = (await page.locator("body").inner_text(timeout=2000))[:5000]
        except Exception:
            body_text = ""
        for token in ("Odoo Client Error", "RPC_ERROR", "UncaughtPromiseError", "Traceback", "Internal Server Error"):
            if token in body_text:
                markers.append(f"odoo_ui_error_text:{token}")
        return markers

    async def screenshot_login_page(self, base_url: str, output_path: Path) -> Path:
        from playwright.async_api import async_playwright

        output_path.parent.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            await page.goto(base_url.rstrip("/") + "/web/login", wait_until="networkidle")
            await page.screenshot(path=str(output_path), full_page=True)
            await browser.close()
        return output_path
