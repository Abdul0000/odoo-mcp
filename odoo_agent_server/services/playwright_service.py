from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urljoin

from ..config import settings
from ..schemas import BrowserCheckResult, PosFlowCheckRequest, PosFlowCheckResult, PosFlowStepResult

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

    async def run_pos_flow(self, request: PosFlowCheckRequest) -> PosFlowCheckResult:
        """Drive a real POS session end-to-end in one persistent browser context.

        This exists because a single cold-load screenshot of /pos/ui never
        exercises the actual failure surface: clicking a product, opening the
        payment screen, and validating an order are where POS OWL component
        bugs (bad state mutation, broken service injection, template mismatch)
        actually throw. Steps run in sequence on the SAME page/context so POS
        session state carries over, and each step captures its own screenshot
        plus console/network errors so a failure at step 3 doesn't lose the
        diagnostic trail from steps 1-2.
        """
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # noqa: BLE001
            return PosFlowCheckResult(status="failed", error=f"Playwright is not installed or browsers are missing: {exc}")

        url_base = (request.base_url or os.environ.get("ODOO_BASE_URL") or settings.odoo_base_url or "").rstrip("/")
        user = request.login or os.environ.get("ODOO_LOGIN") or settings.odoo_login
        secret = request.password or os.environ.get("ODOO_PASSWORD") or settings.odoo_password
        if not url_base:
            return PosFlowCheckResult(status="failed", error="base_url or ODOO_BASE_URL is required")

        output_dir = Path(settings.screenshot_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        steps: list[PosFlowStepResult] = []
        all_console: list[str] = []
        all_network: list[str] = []
        overall_failed = False

        # Console/network listeners are attached once per page and accumulate
        # for the whole session; each step snapshots the delta since its start.
        session_console: list[str] = []
        session_network: list[str] = []

        async def screenshot_step(page, step_name: str) -> str:
            safe_name = _SAFE_NAME_RE.sub("_", f"{request.screenshot_prefix}_{step_name}.png")
            out_path = output_dir / safe_name
            try:
                await page.screenshot(path=str(out_path), full_page=True)
                return str(out_path)
            except Exception:
                return ""

        def snapshot_and_clear(buf: list[str]) -> list[str]:
            items = list(buf)
            buf.clear()
            return items

        def record_step(name: str, status: str, screenshot_path: str = "", error: str | None = None) -> None:
            nonlocal overall_failed
            console_delta = snapshot_and_clear(session_console)
            network_delta = snapshot_and_clear(session_network)
            all_console.extend(console_delta)
            all_network.extend(network_delta)
            if status == "failed":
                overall_failed = True
            steps.append(PosFlowStepResult(
                step=name,
                status=status,
                screenshot_path=screenshot_path or None,
                console_errors=console_delta[-30:],
                network_errors=network_delta[-30:],
                error=error,
            ))

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                page.set_default_timeout(request.timeout_seconds * 1000)
                page.on("console", lambda msg: session_console.append(f"{msg.type}: {msg.text}") if msg.type in {"error", "warning"} else None)
                page.on("pageerror", lambda err: session_console.append(f"pageerror: {err}"))
                page.on("response", lambda resp: session_network.append(f"{resp.status} {resp.url}") if resp.status >= 400 else None)

                # Step 1: login
                try:
                    await page.goto(urljoin(url_base + "/", "web/login"), wait_until="domcontentloaded")
                    if user and secret:
                        await page.fill('input[name="login"]', user)
                        await page.fill('input[name="password"]', secret)
                        await page.click('button[type="submit"]')
                        await page.wait_for_load_state("domcontentloaded")
                    shot = await screenshot_step(page, "01_login")
                    record_step("login", "ok", shot)
                except Exception as exc:  # noqa: BLE001
                    shot = await screenshot_step(page, "01_login_failed")
                    record_step("login", "failed", shot, error=str(exc))
                    await browser.close()
                    return PosFlowCheckResult(status="failed", steps=steps, console_errors=all_console, network_errors=all_network, error=f"Login failed: {exc}")

                # Step 2: open POS UI
                try:
                    await page.goto(urljoin(url_base + "/", "odoo/point-of-sale"), wait_until="domcontentloaded")
                    # Some versions route POS config selection through a backend action list first.
                    if request.pos_config_name:
                        config_link = page.get_by_text(request.pos_config_name, exact=False).first
                        if await config_link.count():
                            await config_link.click()
                    await page.wait_for_load_state("domcontentloaded")
                    shot = await screenshot_step(page, "02_pos_backend")
                    record_step("open_pos_backend", "ok", shot)
                except Exception as exc:  # noqa: BLE001
                    shot = await screenshot_step(page, "02_pos_backend_failed")
                    record_step("open_pos_backend", "failed", shot, error=str(exc))

                # Step 3: enter POS session (the actual OWL frontend, /pos/ui or /pos/web)
                try:
                    opened = False
                    for candidate_selector in ('button:has-text("New Session")', 'button:has-text("Resume")', 'button:has-text("Continue Selling")'):
                        loc = page.locator(candidate_selector).first
                        if await loc.count():
                            await loc.click()
                            opened = True
                            break
                    await page.wait_for_load_state("networkidle", timeout=request.timeout_seconds * 1000)
                    shot = await screenshot_step(page, "03_pos_session_opened")
                    record_step("open_pos_session", "ok" if opened or "pos/ui" in page.url or "pos/web" in page.url else "skipped", shot)
                except Exception as exc:  # noqa: BLE001
                    shot = await screenshot_step(page, "03_pos_session_failed")
                    record_step("open_pos_session", "failed", shot, error=str(exc))

                # Step 4: click a product tile
                try:
                    product_locator = (
                        page.get_by_text(request.product_name, exact=False).first
                        if request.product_name
                        else page.locator(".product-card, .o_pos_product, article.product").first
                    )
                    if await product_locator.count():
                        await product_locator.click()
                        shot = await screenshot_step(page, "04_product_clicked")
                        record_step("click_product", "ok", shot)
                    else:
                        shot = await screenshot_step(page, "04_product_not_found")
                        record_step("click_product", "failed", shot, error="No product tile found on screen — check product_name or POS catalog visibility.")
                except Exception as exc:  # noqa: BLE001
                    shot = await screenshot_step(page, "04_product_click_failed")
                    record_step("click_product", "failed", shot, error=str(exc))

                # Step 5: go to payment screen
                try:
                    pay_button = page.locator('button:has-text("Payment"), .o_pos_payment_button, button.pay-order-button').first
                    if await pay_button.count():
                        await pay_button.click()
                        await page.wait_for_load_state("domcontentloaded")
                        shot = await screenshot_step(page, "05_payment_screen")
                        record_step("open_payment_screen", "ok", shot)
                    else:
                        shot = await screenshot_step(page, "05_payment_button_not_found")
                        record_step("open_payment_screen", "failed", shot, error="No Payment button found — order may not have a line, or button label differs in this version.")
                except Exception as exc:  # noqa: BLE001
                    shot = await screenshot_step(page, "05_payment_screen_failed")
                    record_step("open_payment_screen", "failed", shot, error=str(exc))

                # Step 6: select a payment method and validate
                try:
                    method_locator = (
                        page.get_by_text(request.payment_method_name, exact=False).first
                        if request.payment_method_name
                        else page.locator(".paymentmethod, .payment-method-button").first
                    )
                    if await method_locator.count():
                        await method_locator.click()
                    validate_button = page.locator('button:has-text("Validate")').first
                    if await validate_button.count():
                        await validate_button.click()
                        await page.wait_for_load_state("domcontentloaded")
                    shot = await screenshot_step(page, "06_receipt_screen")
                    record_step("validate_order", "ok" if await validate_button.count() else "skipped", shot)
                except Exception as exc:  # noqa: BLE001
                    shot = await screenshot_step(page, "06_validate_failed")
                    record_step("validate_order", "failed", shot, error=str(exc))

                # Final page-level breakage check (dialogs/tracebacks left on screen)
                markers = await self._detect_odoo_breakage(page)
                if markers:
                    all_console.extend(markers)
                    overall_failed = True

                await browser.close()

            return PosFlowCheckResult(
                status="failed" if overall_failed else "ok",
                steps=steps,
                console_errors=sorted(set(all_console), key=all_console.index)[-100:],
                network_errors=sorted(set(all_network), key=all_network.index)[-100:],
            )
        except Exception as exc:  # noqa: BLE001
            return PosFlowCheckResult(status="failed", steps=steps, console_errors=all_console, network_errors=all_network, error=str(exc))

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
        for token in ("Odoo Client Error", "shell_ERROR", "UncaughtPromiseError", "Traceback", "Internal Server Error"):
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
