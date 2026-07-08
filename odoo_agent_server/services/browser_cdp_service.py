from __future__ import annotations

import asyncio
import base64
import json
import io
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse
from urllib.request import Request, urlopen

from ..config import settings
from ..schemas import BrowserAction, BrowserCheckResult, BrowserLoginRequest, PosFlowCheckRequest, PosFlowCheckResult, PosFlowStepResult, UiVerifySpec
from .client_resolver_service import ClientResolverService
from .code_aware_ui_planner import CodeAwareUiPlanner


class CdpBrowserService:
    """Lightweight browser diagnostics through Chrome DevTools Protocol.

    The connector no longer bundles Chromium. Set ODOO_AGENT_CDP_URL to an
    external browser's remote-debugging endpoint, for example http://host:9222.
    """

    async def screenshot_odoo_page(
        self,
        base_url: str | None,
        login: str | None,
        password: str | None,
        start_path: str,
        screenshot_name: str,
        timeout_seconds: int = 45,
        full_page: bool = True,
        *,
        client_name: str | None = None,
        reuse_session: bool = True,
        login_strategy: str = "reuse_first",
        wait_selector: str | None = None,
        actions: list[BrowserAction] | list[dict[str, Any]] | None = None,
        diagnostics: list[str] | None = None,
        debug_contrast: bool = False,
        debug_contrast_screenshot_name: str | None = None,
        restore_theme_after_verify: bool = True,
        cleanup_screenshot_dir: bool = True,
        cleanup_screenshot_retention_seconds: int | None = None,
        delete_screenshots_after_verify: bool = False,
        ui_profile: str = "auto",
        viewport_width: int | None = None,
        viewport_height: int | None = None,
        device_scale_factor: float = 1.0,
        required_selectors: list[str] | None = None,
        forbidden_selectors: list[str] | None = None,
        required_text: list[str] | None = None,
        forbidden_text: list[str] | None = None,
        min_elements: dict[str, int] | None = None,
        wait_until: str | None = None,
        require_all_actions_pass: bool = True,
        screenshot_mode: str = "memory_only",
        screenshot_format: str = "jpeg",
        screenshot_quality: int = 50,
        include_screenshot_base64: bool = False,
        screenshot_base64_max_bytes: int = 1_500_000,
        reference_image_path: str | None = None,
        visual_compare_mode: str = "off",
        max_visual_diff_ratio: float | None = None,
        human_pos_navigation: bool | None = None,
        pos_config_id: int | None = None,
        pos_config_name: str | None = None,
        scenario: str = "auto",
        scenario_goal: str | None = None,
        scenario_product_count: int = 0,
        cleanup_after_verify: bool = False,
        require_functional_proof: bool = True,
        code_aware: bool = False,
        project_path: str | None = None,
        module_name: str | None = None,
        feature_goal: str | None = None,
    ) -> BrowserCheckResult:
        return await asyncio.to_thread(
            self._check_page_sync,
            base_url,
            login,
            password,
            start_path,
            screenshot_name,
            timeout_seconds,
            full_page,
            client_name,
            reuse_session,
            login_strategy,
            wait_selector,
            actions or [],
            diagnostics or [],
            debug_contrast,
            debug_contrast_screenshot_name,
            restore_theme_after_verify,
            cleanup_screenshot_dir,
            cleanup_screenshot_retention_seconds,
            delete_screenshots_after_verify,
            ui_profile,
            viewport_width,
            viewport_height,
            device_scale_factor,
            required_selectors or [],
            forbidden_selectors or [],
            required_text or [],
            forbidden_text or [],
            min_elements or {},
            wait_until,
            require_all_actions_pass,
            screenshot_mode,
            screenshot_format,
            screenshot_quality,
            include_screenshot_base64,
            screenshot_base64_max_bytes,
            reference_image_path,
            visual_compare_mode,
            max_visual_diff_ratio,
            human_pos_navigation,
            pos_config_id,
            pos_config_name,
            scenario,
            scenario_goal,
            scenario_product_count,
            cleanup_after_verify,
            require_functional_proof,
            code_aware,
            project_path,
            module_name,
            feature_goal,
        )

    async def run_pos_flow(self, request: PosFlowCheckRequest) -> PosFlowCheckResult:
        return await asyncio.to_thread(self._run_pos_flow_sync, request)

    async def login_odoo(self, request: BrowserLoginRequest) -> BrowserCheckResult:
        """Open Odoo login, submit credentials, and keep the CDP browser session.

        This is intentionally exposed as a separate MCP tool so visual agents can
        seed a logged-in Chrome profile once, then run screenshot/diagnostic tools
        with reuse_session=true without re-sending credentials on every call.
        """
        return await asyncio.to_thread(self._login_page_sync, request)

    def _check_page_sync(
        self,
        base_url: str | None,
        login: str | None,
        password: str | None,
        start_path: str,
        screenshot_name: str,
        timeout_seconds: int,
        full_page: bool,
        client_name: str | None = None,
        reuse_session: bool = True,
        login_strategy: str = "reuse_first",
        wait_selector: str | None = None,
        actions: list[BrowserAction] | list[dict[str, Any]] | None = None,
        diagnostics: list[str] | None = None,
        debug_contrast: bool = False,
        debug_contrast_screenshot_name: str | None = None,
        restore_theme_after_verify: bool = True,
        cleanup_screenshot_dir: bool = True,
        cleanup_screenshot_retention_seconds: int | None = None,
        delete_screenshots_after_verify: bool = False,
        ui_profile: str = "auto",
        viewport_width: int | None = None,
        viewport_height: int | None = None,
        device_scale_factor: float = 1.0,
        required_selectors: list[str] | None = None,
        forbidden_selectors: list[str] | None = None,
        required_text: list[str] | None = None,
        forbidden_text: list[str] | None = None,
        min_elements: dict[str, int] | None = None,
        wait_until: str | None = None,
        require_all_actions_pass: bool = True,
        screenshot_mode: str = "memory_only",
        screenshot_format: str = "jpeg",
        screenshot_quality: int = 50,
        include_screenshot_base64: bool = False,
        screenshot_base64_max_bytes: int = 1_500_000,
        reference_image_path: str | None = None,
        visual_compare_mode: str = "off",
        max_visual_diff_ratio: float | None = None,
        human_pos_navigation: bool | None = None,
        pos_config_id: int | None = None,
        pos_config_name: str | None = None,
        scenario: str = "auto",
        scenario_goal: str | None = None,
        scenario_product_count: int = 0,
        cleanup_after_verify: bool = False,
        require_functional_proof: bool = True,
        code_aware: bool = False,
        project_path: str | None = None,
        module_name: str | None = None,
        feature_goal: str | None = None,
    ) -> BrowserCheckResult:
        client = None
        if client_name:
            try:
                client = ClientResolverService().resolve_client(client_name=client_name, require_complete=False)
            except Exception:
                client = None
        resolved_project_path = project_path or (str(client.path) if client and getattr(client, "path", None) else None)
        url_base = (base_url or (client.odoo_url if client else None) or os.environ.get("ODOO_BASE_URL") or settings.odoo_base_url or "").rstrip("/")
        user = login or (client.odoo_login if client else None) or os.environ.get("ODOO_LOGIN") or settings.odoo_login
        secret = password or (client.odoo_password if client else None) or os.environ.get("ODOO_PASSWORD") or settings.odoo_password
        cleanup_info = self._cleanup_screenshot_dir(cleanup_screenshot_retention_seconds) if (cleanup_screenshot_dir and settings.screenshot_auto_clean) else {
            "enabled": False,
            "reason": "disabled",
            "dir": str(Path(settings.screenshot_dir).expanduser()),
        }
        if not url_base and not _is_absolute_http_url(start_path):
            return BrowserCheckResult(
                status="failed",
                error="base_url/client.odoo_url/ODOO_BASE_URL is required unless start_path is a full URL",
                screenshot_cleanup=cleanup_info,
            )

        page = _CdpPage.new(timeout_seconds)
        if page.error:
            return BrowserCheckResult(status="failed", error=page.error, screenshot_cleanup=cleanup_info)
        action_results: list[dict[str, Any]] = []
        diagnostics_result: dict[str, Any] = {}
        assertion_results: dict[str, Any] = {}
        visual_compare_result: dict[str, Any] = {}
        screenshot_analysis: dict[str, Any] = {}
        viewport_result: dict[str, Any] = {}
        debug_screenshot_path: str | None = None
        debug_contrast_applied = False
        theme_restored_after_verify = False
        session_reused = False
        session_expired = False
        try:
            page.enable()
            effective_profile = self._infer_ui_profile(ui_profile, start_path)
            spec_defaults = self._ui_profile_defaults(effective_profile)
            effective_required_selectors = self._merge_unique(spec_defaults.get("required_selectors", []), required_selectors or [])
            effective_forbidden_selectors = self._merge_unique(spec_defaults.get("forbidden_selectors", []), forbidden_selectors or [])
            effective_required_text = self._merge_unique(spec_defaults.get("required_text", []), required_text or [])
            effective_forbidden_text = self._merge_unique(spec_defaults.get("forbidden_text", []), forbidden_text or [])
            effective_min_elements = {**spec_defaults.get("min_elements", {}), **(min_elements or {})}
            effective_wait_until = wait_until or spec_defaults.get("wait_until")
            effective_diagnostics = self._merge_unique(diagnostics or [], spec_defaults.get("diagnostics", []))
            code_plan = None
            if code_aware:
                code_plan = CodeAwareUiPlanner().plan(
                    project_path=resolved_project_path,
                    module_name=module_name,
                    feature_goal=feature_goal or scenario_goal,
                    required_selectors=required_selectors or [],
                    profile=effective_profile,
                )
                action_results.append({
                    "index": len(action_results),
                    "type": "code_aware_plan",
                    "status": "ok" if code_plan.get("status") == "planned" else code_plan.get("status", "failed"),
                    "optional": False,
                    "value": code_plan,
                })
                if code_plan.get("status") == "planned":
                    if str(scenario or "auto").lower() == "auto" and code_plan.get("scenario"):
                        scenario = str(code_plan["scenario"])
                    if scenario_product_count <= 0:
                        scenario_product_count = int(code_plan.get("product_count") or 0)
                    if not scenario_goal:
                        scenario_goal = feature_goal
            scenario_spec = self._resolve_visual_scenario(
                scenario=scenario,
                goal=scenario_goal,
                profile=effective_profile,
                required_selectors=required_selectors or [],
                required_text=required_text or [],
                product_count=scenario_product_count,
            )
            if viewport_width or viewport_height:
                page.set_viewport(viewport_width or 1280, viewport_height or 720, device_scale_factor)
                page.drain(0.1)
            viewport_result = self._current_viewport(page)
            target_url = _build_target_url(url_base, start_path)
            strategy = (login_strategy or "reuse_first").lower().strip()
            if strategy not in {"reuse_only", "reuse_first", "login_first"}:
                strategy = "reuse_first"

            if strategy == "login_first":
                if not (user and secret):
                    result = BrowserCheckResult(
                        status="failed",
                        url=urljoin(url_base + "/", "web/login"),
                        title=None,
                        screenshot_path=None,
                        console_errors=page.console_errors[-50:],
                        console_all=page.console_all[-500:],
                        network_errors=page.network_errors[-50:],
                        debug_contrast_applied=False,
                        theme_restored_after_verify=False,
                        session_reused=False,
                        session_expired=True,
                        error="odoo_login_credentials_not_configured",
                    )
                    return self._finalize_screenshot_cleanup(result, cleanup_info, delete_screenshots_after_verify)
                self._login(page, url_base, user, secret, timeout_seconds, redirect_path=start_path)
                page.drain(1.0)
                if (page.best_effort_url() or "").rstrip("/") != target_url.rstrip("/"):
                    page.navigate(target_url, timeout_seconds)
                    page.drain(1.0)
                session_reused = False
                session_expired = False
            elif reuse_session or strategy in {"reuse_first", "reuse_only"}:
                page.navigate(target_url, timeout_seconds)
                page.drain(1.0)
                session_reused = not self._is_login_page(page)
                if not session_reused:
                    session_expired = True
                    if strategy == "reuse_only" or not (user and secret):
                        result = BrowserCheckResult(
                            status="failed",
                            url=page.best_effort_url(),
                            title=page.best_effort_title(),
                            screenshot_path=self._capture_screenshot(page, screenshot_name, full_page) if (screenshot_mode == "persist_latest" and screenshot_name) else None,
                            console_errors=page.console_errors[-50:],
                            console_all=page.console_all[-500:],
                            network_errors=page.network_errors[-50:],
                            debug_contrast_applied=False,
                            theme_restored_after_verify=False,
                            session_reused=False,
                            session_expired=True,
                            error="session_expired_login_once_required",
                        )
                        return self._finalize_screenshot_cleanup(result, cleanup_info, delete_screenshots_after_verify)
                    self._login(page, url_base, user, secret, timeout_seconds, redirect_path=start_path)
                    page.drain(1.0)
                    if (page.best_effort_url() or "").rstrip("/") != target_url.rstrip("/"):
                        page.navigate(target_url, timeout_seconds)
                        page.drain(1.0)
                    session_expired = False
            else:
                if not (user and secret):
                    result = BrowserCheckResult(
                        status="failed",
                        url=urljoin(url_base + "/", "web/login"),
                        title=None,
                        screenshot_path=None,
                        console_errors=page.console_errors[-50:],
                        console_all=page.console_all[-500:],
                        network_errors=page.network_errors[-50:],
                        debug_contrast_applied=False,
                        theme_restored_after_verify=False,
                        session_reused=False,
                        session_expired=True,
                        error="odoo_login_credentials_not_configured",
                    )
                    return self._finalize_screenshot_cleanup(result, cleanup_info, delete_screenshots_after_verify)
                self._login(page, url_base, user, secret, timeout_seconds, redirect_path=start_path)
                page.drain(1.0)
                if (page.best_effort_url() or "").rstrip("/") != target_url.rstrip("/"):
                    page.navigate(target_url, timeout_seconds)
                    page.drain(1.0)

            # A successful login attempt can still land back on /web/login when
            # credentials are rejected, the account lacks access, or the redirect
            # session cookie was not accepted. Report that as an auth failure
            # before selector waits so visual workflows do not hide the real cause
            # behind "Selector not found: .product-screen".
            if self._is_login_page(page):
                result = BrowserCheckResult(
                    status="failed",
                    url=page.best_effort_url(),
                    title=page.best_effort_title(),
                    screenshot_path=self._capture_screenshot(page, screenshot_name, full_page) if (screenshot_mode == "persist_latest" and screenshot_name) else None,
                    console_errors=page.console_errors[-50:],
                    console_all=page.console_all[-500:],
                    network_errors=page.network_errors[-50:],
                    debug_contrast_applied=False,
                    theme_restored_after_verify=False,
                    session_reused=session_reused,
                    session_expired=session_expired,
                    error="odoo_login_failed_or_still_on_login_page",
                )
                return self._finalize_screenshot_cleanup(result, cleanup_info, delete_screenshots_after_verify)

            should_open_pos = human_pos_navigation if human_pos_navigation is not None else effective_profile.startswith("pos_")
            if should_open_pos and effective_profile.startswith("pos_"):
                resolved_config_id = pos_config_id
                if resolved_config_id is None:
                    raw_config_id = (parse_qs(urlparse(target_url).query).get("config_id") or [None])[0]
                    if str(raw_config_id or "").isdigit():
                        resolved_config_id = int(str(raw_config_id))
                navigation = self._navigate_pos_as_human(
                    page,
                    url_base,
                    timeout_seconds,
                    pos_config_id=resolved_config_id,
                    pos_config_name=pos_config_name,
                )
                action_results.append({"index": 0, "type": "human_pos_navigation", "status": "ok", "optional": False, "value": navigation})

            # Reach only the base screen first. Feature selectors often depend on
            # setup interactions (for example a trolley is rendered only after
            # products are added), so final assertions must not block setup.
            base_required_selectors = list(spec_defaults.get("required_selectors", []))
            base_min_elements = dict(spec_defaults.get("min_elements", {}))
            base_wait_until = spec_defaults.get("wait_until")
            if wait_selector:
                page.wait_for_selector(wait_selector, timeout_seconds)
            self._wait_for_ui_ready(
                page,
                timeout_seconds,
                required_selectors=base_required_selectors,
                min_elements=base_min_elements,
                wait_until=base_wait_until,
            )
            if scenario_spec.get("name") != "none":
                scenario_result = self._run_visual_scenario(
                    page,
                    scenario_spec,
                    timeout_seconds,
                    require_functional_proof=require_functional_proof,
                )
                action_results.append({
                    "index": len(action_results) + 1,
                    "type": "scenario",
                    "status": scenario_result.get("status", "failed"),
                    "optional": False,
                    "value": scenario_result,
                })
            effective_actions = actions or []
            if screenshot_mode != "persist_latest":
                effective_actions = [
                    action for action in effective_actions
                    if str((action.model_dump() if hasattr(action, "model_dump") else action).get("type", "")).lower() != "screenshot"
                ]
            action_results.extend(self._run_actions(page, effective_actions, timeout_seconds, full_page))
            self._wait_for_ui_ready(
                page,
                timeout_seconds,
                required_selectors=effective_required_selectors,
                min_elements=effective_min_elements,
                wait_until=effective_wait_until,
            )
            if debug_contrast:
                self._apply_debug_contrast(page)
                debug_contrast_applied = True
                page.drain(0.2)
            action_markers = self._action_failure_markers(action_results) if require_all_actions_pass else []
            assertion_results = self._run_ui_assertions(
                page,
                profile=effective_profile,
                required_selectors=effective_required_selectors,
                forbidden_selectors=effective_forbidden_selectors,
                required_text=effective_required_text,
                forbidden_text=effective_forbidden_text,
                min_elements=effective_min_elements,
                wait_until=effective_wait_until,
            )
            assertion_markers = [f"ui_assertion:{item}" for item in assertion_results.get("failures", [])]
            markers = self._detect_odoo_breakage(page)
            diagnostics_result = self._run_visual_diagnostics(page, effective_diagnostics)
            diag_markers = [f"visual_diagnostic:{item}" for item in diagnostics_result.get("failures", [])]
            all_markers = markers + action_markers + assertion_markers + diag_markers
            page.console_errors.extend(all_markers)
            if debug_contrast and debug_contrast_screenshot_name and screenshot_mode == "persist_latest":
                debug_screenshot_path = self._capture_screenshot(page, debug_contrast_screenshot_name, full_page)
            if debug_contrast and restore_theme_after_verify:
                self._remove_debug_contrast(page)
                theme_restored_after_verify = True
                page.drain(0.2)
            screenshot_path = None
            screenshot_bytes = None
            if screenshot_mode == "persist_latest" and screenshot_name:
                screenshot_path = self._capture_screenshot(page, screenshot_name, full_page)
                visual_compare_result = self._compare_screenshot(screenshot_path, reference_image_path, visual_compare_mode, max_visual_diff_ratio)
            elif screenshot_mode == "memory_only":
                screenshot_bytes, screenshot_analysis = self._capture_screenshot_memory(
                    page, full_page=False, image_format=screenshot_format, quality=screenshot_quality
                )
                visual_compare_result = self._compare_screenshot_bytes(
                    screenshot_bytes, reference_image_path, visual_compare_mode, max_visual_diff_ratio
                )
            else:
                screenshot_analysis = {"captured": False, "reason": "screenshot_mode_disabled"}
            compare_markers = [f"visual_compare:{item}" for item in visual_compare_result.get("failures", [])]
            if compare_markers:
                all_markers.extend(compare_markers)
                page.console_errors.extend(compare_markers)
            if cleanup_after_verify and scenario_spec.get("temporary_order_created"):
                cleanup_result = self._cleanup_visual_scenario(page, scenario_spec, timeout_seconds)
                action_results.append({
                    "index": len(action_results) + 1,
                    "type": "scenario_cleanup",
                    "status": cleanup_result.get("status", "failed"),
                    "optional": True,
                    "value": cleanup_result,
                })
            title = page.eval_value("document.title")
            result = BrowserCheckResult(
                status="failed" if all_markers else "ok",
                url=page.eval_value("location.href"),
                title=str(title) if title is not None else None,
                screenshot_path=screenshot_path,
                screenshot_url=self._screenshot_url(screenshot_path),
                screenshot_base64=self._screenshot_base64(screenshot_path, screenshot_base64_max_bytes) if (include_screenshot_base64 and screenshot_mode == "persist_latest") else None,
                screenshot_mode=screenshot_mode,
                screenshot_persisted=bool(screenshot_path),
                screenshot_analysis=screenshot_analysis,
                viewport=viewport_result,
                assertion_results=assertion_results,
                visual_compare=visual_compare_result,
                console_errors=page.console_errors[-50:],
                console_all=page.console_all[-500:],
                network_errors=page.network_errors[-50:],
                action_results=action_results,
                diagnostics=diagnostics_result,
                debug_contrast_applied=debug_contrast_applied,
                debug_screenshot_path=debug_screenshot_path,
                debug_screenshot_url=self._screenshot_url(debug_screenshot_path),
                theme_restored_after_verify=theme_restored_after_verify,
                session_reused=session_reused,
                session_expired=False,
                error="; ".join(all_markers) if all_markers else None,
            )
            return self._finalize_screenshot_cleanup(result, cleanup_info, delete_screenshots_after_verify)
        except Exception as exc:  # noqa: BLE001
            failure_screenshot = self._capture_screenshot(page, screenshot_name, full_page) if (screenshot_mode == "persist_latest" and screenshot_name) else None
            if screenshot_mode == "memory_only":
                _failure_bytes, screenshot_analysis = self._capture_screenshot_memory(page, full_page=False, image_format=screenshot_format, quality=screenshot_quality)
                del _failure_bytes
            result = BrowserCheckResult(
                status="failed",
                url=page.best_effort_url(),
                title=page.best_effort_title(),
                screenshot_path=failure_screenshot,
                screenshot_url=self._screenshot_url(failure_screenshot),
                screenshot_base64=self._screenshot_base64(failure_screenshot, screenshot_base64_max_bytes) if (include_screenshot_base64 and screenshot_mode == "persist_latest") else None,
                screenshot_mode=screenshot_mode,
                screenshot_persisted=bool(failure_screenshot),
                screenshot_analysis=screenshot_analysis,
                console_errors=page.console_errors[-50:],
                console_all=page.console_all[-500:],
                network_errors=page.network_errors[-50:],
                viewport=viewport_result,
                assertion_results=assertion_results,
                visual_compare=visual_compare_result,
                action_results=action_results,
                diagnostics=diagnostics_result,
                debug_contrast_applied=debug_contrast_applied,
                debug_screenshot_path=debug_screenshot_path,
                debug_screenshot_url=self._screenshot_url(debug_screenshot_path),
                theme_restored_after_verify=theme_restored_after_verify,
                session_reused=session_reused,
                session_expired=session_expired,
                error=str(exc),
            )
            return self._finalize_screenshot_cleanup(result, cleanup_info, delete_screenshots_after_verify)
        finally:
            page.close()

    def _login_page_sync(self, request: BrowserLoginRequest) -> BrowserCheckResult:
        client = None
        if request.client_name:
            try:
                client = ClientResolverService().resolve_client(client_name=request.client_name, require_complete=False)
            except Exception:
                client = None
        url_base = (request.base_url or (client.odoo_url if client else None) or os.environ.get("ODOO_BASE_URL") or settings.odoo_base_url or "").rstrip("/")
        user = request.login or (client.odoo_login if client else None) or os.environ.get("ODOO_LOGIN") or settings.odoo_login
        secret = request.password or (client.odoo_password if client else None) or os.environ.get("ODOO_PASSWORD") or settings.odoo_password
        cleanup_info = self._cleanup_screenshot_dir(request.cleanup_screenshot_retention_seconds) if (request.cleanup_screenshot_dir and settings.screenshot_auto_clean) else {
            "enabled": False,
            "reason": "disabled",
            "dir": str(Path(settings.screenshot_dir).expanduser()),
        }
        if not url_base:
            return BrowserCheckResult(status="failed", error="base_url/client.odoo_url/ODOO_BASE_URL is required for browser.login", screenshot_cleanup=cleanup_info)

        page = _CdpPage.new(request.timeout_seconds)
        if page.error:
            return BrowserCheckResult(status="failed", error=page.error, screenshot_cleanup=cleanup_info)
        try:
            page.enable()
            if not user or not secret:
                result = BrowserCheckResult(
                    status="failed",
                    url=urljoin(url_base + "/", "web/login"),
                    title=None,
                    error="odoo_login_credentials_not_configured",
                )
                return self._finalize_screenshot_cleanup(result, cleanup_info, request.delete_screenshots_after_verify)
            self._login(page, url_base, user, secret, request.timeout_seconds, redirect_path=request.redirect_path)
            target_url = _build_target_url(url_base, request.redirect_path)
            if target_url and (page.best_effort_url() or "").rstrip("/") != target_url.rstrip("/"):
                page.navigate(target_url, request.timeout_seconds)
                page.drain(1.0)
            screenshot_path = self._capture_screenshot(page, request.screenshot_name, request.full_page) if request.screenshot_name else None
            still_login = self._is_login_page(page)
            result = BrowserCheckResult(
                status="failed" if still_login else "ok",
                url=page.best_effort_url(),
                title=page.best_effort_title(),
                screenshot_path=screenshot_path,
                console_errors=page.console_errors[-50:],
                console_all=page.console_all[-500:],
                network_errors=page.network_errors[-50:],
                session_reused=False,
                session_expired=still_login,
                error="odoo_login_failed_or_still_on_login_page" if still_login else None,
            )
            return self._finalize_screenshot_cleanup(result, cleanup_info, request.delete_screenshots_after_verify)
        except Exception as exc:  # noqa: BLE001
            result = BrowserCheckResult(
                status="failed",
                url=page.best_effort_url(),
                title=page.best_effort_title(),
                screenshot_path=self._capture_screenshot(page, request.screenshot_name, request.full_page) if request.screenshot_name else None,
                console_errors=page.console_errors[-50:],
                console_all=page.console_all[-500:],
                network_errors=page.network_errors[-50:],
                error=str(exc),
            )
            return self._finalize_screenshot_cleanup(result, cleanup_info, request.delete_screenshots_after_verify)
        finally:
            page.close()

    def _resolve_pos_config(
        self,
        page: "_CdpPage",
        *,
        pos_config_id: int | None,
        pos_config_name: str | None,
    ) -> dict[str, Any]:
        """Resolve the intended register through the authenticated web session.

        This deliberately uses the browser's JSON-RPC session instead of a
        second transport/login. It mirrors what the dashboard knows and keeps
        multi-company record rules intact.
        """
        domain: list[Any] = []
        if pos_config_id:
            domain = [["id", "=", int(pos_config_id)]]
        elif pos_config_name:
            domain = [["name", "ilike", str(pos_config_name)]]
        expression = """
        (async () => {
          const response = await fetch('/web/dataset/call_kw/pos.config/search_read', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              jsonrpc: '2.0', method: 'call', id: Date.now(),
              params: {
                model: 'pos.config', method: 'search_read', args: [%s],
                kwargs: {fields: ['id', 'name', 'current_session_id'], limit: 50, order: 'id'}
              }
            })
          });
          const payload = await response.json();
          return {status: response.status, result: payload.result || [], error: payload.error || null};
        })()
        """ % json.dumps(domain)
        payload = page.eval_value(expression, await_promise=True)
        records = payload.get("result", []) if isinstance(payload, dict) else []
        if not records:
            target = f"id={pos_config_id}" if pos_config_id else f"name={pos_config_name!r}" if pos_config_name else "first available register"
            raise RuntimeError(f"pos_config_not_found:{target}")
        if pos_config_name and not pos_config_id:
            exact = next((item for item in records if str(item.get("name", "")).casefold() == str(pos_config_name).casefold()), None)
            return exact or records[0]
        return records[0]

    def _click_pos_card_action(self, page: "_CdpPage", config_name: str, *, allow_fallback: bool) -> dict[str, Any]:
        """Click the start/resume action scoped to the named POS dashboard card."""
        expression = """
        (() => {
          const wanted = %s.trim().toLowerCase();
          const labels = ['continue selling', 'new session', 'open register', 'resume'];
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
          };
          const actionIn = (root) => Array.from(root.querySelectorAll('button, a, [role="button"]'))
            .find((el) => visible(el) && labels.some((label) => (el.innerText || el.textContent || '').trim().toLowerCase().includes(label)));
          const names = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,strong,span,div'))
            .filter((el) => visible(el) && (el.innerText || el.textContent || '').trim().toLowerCase() === wanted);
          for (const nameNode of names) {
            let root = nameNode;
            for (let depth = 0; root && depth < 9; depth += 1, root = root.parentElement) {
              const action = actionIn(root);
              if (action) {
                const label = (action.innerText || action.textContent || '').trim();
                action.scrollIntoView({block: 'center', inline: 'center'});
                action.click();
                return {clicked: true, scoped: true, label};
              }
            }
          }
          if (!%s) return {clicked: false, scoped: false, reason: 'named_dashboard_card_action_not_found'};
          const fallback = Array.from(document.querySelectorAll('button, a, [role="button"]'))
            .find((el) => visible(el) && labels.some((label) => (el.innerText || el.textContent || '').trim().toLowerCase().includes(label)));
          if (!fallback) return {clicked: false, scoped: false, reason: 'dashboard_action_not_found'};
          const label = (fallback.innerText || fallback.textContent || '').trim();
          fallback.scrollIntoView({block: 'center', inline: 'center'});
          fallback.click();
          return {clicked: true, scoped: false, label};
        })()
        """ % (json.dumps(config_name), "true" if allow_fallback else "false")
        value = page.eval_value(expression)
        return value if isinstance(value, dict) else {"clicked": False, "reason": "dashboard_action_evaluation_failed"}


    def _is_pos_product_screen(self, page: "_CdpPage") -> bool:
        return bool(
            page.eval_value(
                "!!document.querySelector('.pos .product-screen, .pos .product-screen-main, .pos .products-widget, .pos .pos-content .product-list')"
            )
        )

    def _handle_pos_blocking_state(self, page: "_CdpPage") -> str | None:
        """Handle only safe, known POS startup blockers."""
        value = page.eval_value(
            """
            (() => {
              const visible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const normalized = (el) => (el.innerText || el.textContent || '').trim().toLowerCase();
              const labels = [
                'open session', 'open register', 'continue selling', 'start session',
                'confirm', 'continue', 'resume', 'ok'
              ];
              const roots = Array.from(document.querySelectorAll(
                '.modal, .modal-dialog, .o_dialog, .popup, .opening-cash-control, .cashier-selection, .login-overlay, .login-screen'
              )).filter(visible);
              for (const root of roots) {
                const buttons = Array.from(root.querySelectorAll('button, a, [role="button"]')).filter(visible);
                const button = buttons.find((el) => labels.some((label) => normalized(el) === label || normalized(el).includes(label)));
                if (button) {
                  const label = (button.innerText || button.textContent || '').trim();
                  button.scrollIntoView({block: 'center', inline: 'center'});
                  button.click();
                  return label || 'POS dialog action';
                }
                const employee = root.querySelector('.employee, .login-button, [data-employee-id]');
                if (visible(employee)) {
                  const label = (employee.innerText || employee.textContent || 'employee').trim();
                  employee.click();
                  return `Employee: ${label}`;
                }
              }
              return null;
            })()
            """
        )
        return str(value) if value else None

    def _pos_runtime_state(self, page: "_CdpPage") -> dict[str, Any]:
        value = page.eval_value(
            """
            (() => {
              const visible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const dialogs = Array.from(document.querySelectorAll('.modal, .modal-dialog, .o_dialog, .popup'))
                .filter(visible)
                .map((el) => (el.innerText || el.textContent || '').trim().slice(0, 800));
              const bodyText = (document.body && document.body.innerText || '').trim();
              const fatal = !!document.querySelector('.o_error_dialog, .o_notification.border-danger, .error-dialog') ||
                /traceback|odoo client error|uncaught promise error/i.test(bodyText);
              return {
                url: location.href,
                title: document.title,
                readyState: document.readyState,
                htmlClass: document.documentElement.className || '',
                bodyClass: document.body ? document.body.className || '' : '',
                hasPosRoot: !!document.querySelector('.pos'),
                hasProductScreen: !!document.querySelector('.pos .product-screen, .pos .product-screen-main, .pos .products-widget, .pos .pos-content .product-list'),
                loaders: document.querySelectorAll('.loader, .pos-loader, .o_pos_loader, .o_loading_indicator').length,
                dialogs,
                text: bodyText.slice(0, 1600),
                fatal_error: fatal,
              };
            })()
            """
        )
        return value if isinstance(value, dict) else {"url": page.best_effort_url(), "state": value}

    def _navigate_pos_as_human(
        self,
        page: "_CdpPage",
        url_base: str,
        timeout_seconds: int,
        *,
        pos_config_id: int | None = None,
        pos_config_name: str | None = None,
    ) -> dict[str, Any]:
        """Enter a POS register through the same dashboard flow used by staff."""
        if self._is_pos_product_screen(page):
            return {"status": "already_open", "url": page.best_effort_url()}

        dashboard_url = urljoin(url_base + "/", "odoo/point-of-sale")
        page.navigate(dashboard_url, timeout_seconds)
        page.wait_for_condition("!!document.querySelector('.o_web_client, .o_action_manager')", min(timeout_seconds, 30))
        config = self._resolve_pos_config(page, pos_config_id=pos_config_id, pos_config_name=pos_config_name)
        config_name = str(config.get("name") or "").strip()
        if not config_name:
            raise RuntimeError("pos_config_name_missing_after_resolution")
        card_rendered = page.wait_for_condition(
            "(document.body && document.body.innerText || '').toLowerCase().includes(%s)" % json.dumps(config_name.lower()),
            min(timeout_seconds, 30),
        )
        if not card_rendered:
            raise RuntimeError(f"pos_config_card_not_rendered:{config_name}")
        clicked = self._click_pos_card_action(
            page,
            config_name,
            allow_fallback=not bool(pos_config_id or pos_config_name),
        )
        if not clicked.get("clicked"):
            raise RuntimeError(f"pos_dashboard_action_not_found:{config_name}:{clicked.get('reason')}")
        page.drain(1.0)

        deadline = time.monotonic() + timeout_seconds
        handled: list[str] = [str(clicked.get("label") or "dashboard action")]
        direct_fallback_used = False
        click_started = time.monotonic()
        while time.monotonic() < deadline:
            if self._is_pos_product_screen(page):
                return {
                    "status": "opened",
                    "config": {"id": config.get("id"), "name": config_name, "current_session_id": config.get("current_session_id")},
                    "handled": handled,
                    "url": page.best_effort_url(),
                    "direct_fallback_used": direct_fallback_used,
                }

            state = self._pos_runtime_state(page)
            if state.get("fatal_error"):
                raise RuntimeError(f"pos_runtime_error:{json.dumps(state, default=str)}")

            action = self._handle_pos_blocking_state(page)
            if action:
                handled.append(str(action))
                page.drain(1.0)
                continue

            # Odoo's dashboard action can open the register in a new browser tab.
            # A single-target CDP verifier cannot automatically follow that tab,
            # so after the real dashboard click has been attempted, continue in
            # the current tab using the exact resolved register URL.
            current_url = (page.best_effort_url() or "").lower()
            if (
                not direct_fallback_used
                and time.monotonic() - click_started >= 3.0
                and "/pos/ui" not in current_url
            ):
                direct_fallback_used = True
                fallback_url = urljoin(url_base + "/", f"pos/ui?config_id={int(config.get('id'))}")
                handled.append("Follow opened POS tab")
                page.navigate(fallback_url, min(timeout_seconds, 45))
                page.drain(1.0)
                continue
            page.drain(0.5)

        state = self._pos_runtime_state(page)
        state["handled"] = handled
        state["direct_fallback_used"] = direct_fallback_used
        raise TimeoutError(f"pos_product_screen_not_reached:{json.dumps(state, default=str)}")

    def _run_pos_flow_sync(self, request: PosFlowCheckRequest) -> PosFlowCheckResult:
        client = None
        if request.client_name:
            try:
                client = ClientResolverService().resolve_client(client_name=request.client_name, require_complete=False)
            except Exception:
                client = None
        url_base = (request.base_url or (client.odoo_url if client else None) or os.environ.get("ODOO_BASE_URL") or settings.odoo_base_url or "").rstrip("/")
        user = request.login or (client.odoo_login if client else None) or os.environ.get("ODOO_LOGIN") or settings.odoo_login
        secret = request.password or (client.odoo_password if client else None) or os.environ.get("ODOO_PASSWORD") or settings.odoo_password
        if not url_base:
            return PosFlowCheckResult(status="failed", error="base_url or ODOO_BASE_URL is required")

        page = _CdpPage.new(request.timeout_seconds)
        if page.error:
            return PosFlowCheckResult(status="failed", error=page.error)

        steps: list[PosFlowStepResult] = []
        all_console: list[str] = []
        all_console_all: list[str] = []
        all_network: list[str] = []
        overall_failed = False

        def record_step(name: str, status: str, error: str | None = None) -> None:
            nonlocal overall_failed
            screenshot_path = self._capture_screenshot(
                page,
                f"{request.screenshot_prefix}_{len(steps) + 1:02d}_{name}.png",
                full_page=True,
            )
            console_delta, console_all_delta, network_delta = page.snapshot_and_clear()
            all_console.extend(console_delta)
            all_console_all.extend(console_all_delta)
            all_network.extend(network_delta)
            if status == "failed":
                overall_failed = True
            steps.append(
                PosFlowStepResult(
                    step=name,
                    status=status,  # type: ignore[arg-type]
                    screenshot_path=screenshot_path,
                    console_errors=console_delta[-30:],
                    console_all=console_all_delta[-200:],
                    network_errors=network_delta[-30:],
                    error=error,
                )
            )

        try:
            page.enable()
            try:
                self._login(page, url_base, user, secret, request.timeout_seconds)
                record_step("login", "ok")
            except Exception as exc:  # noqa: BLE001
                record_step("login", "failed", str(exc))
                return PosFlowCheckResult(status="failed", steps=steps, console_errors=all_console, console_all=all_console_all[-1000:], network_errors=all_network, error=f"Login failed: {exc}")

            try:
                navigation = self._navigate_pos_as_human(
                    page,
                    url_base,
                    request.timeout_seconds,
                    pos_config_id=request.pos_config_id,
                    pos_config_name=request.pos_config_name,
                )
                record_step("open_pos_session", "ok")
            except Exception as exc:  # noqa: BLE001
                record_step("open_pos_session", "failed", str(exc))

            try:
                clicked = page.click_text(request.product_name) if request.product_name else page.click_selector(".product-list .product, article.product, .product-card, .o_pos_product")
                if clicked:
                    record_step("click_product", "ok")
                else:
                    record_step("click_product", "failed", "No product tile found on screen — check product_name or POS catalog visibility.")
            except Exception as exc:  # noqa: BLE001
                record_step("click_product", "failed", str(exc))

            try:
                clicked = page.click_first_text(["Payment"]) or page.click_selector(".o_pos_payment_button, button.pay-order-button")
                if clicked:
                    page.drain(0.5)
                    record_step("open_payment_screen", "ok")
                else:
                    record_step("open_payment_screen", "failed", "No Payment button found — order may not have a line, or button label differs in this version.")
            except Exception as exc:  # noqa: BLE001
                record_step("open_payment_screen", "failed", str(exc))

            if request.validate_order:
                try:
                    if request.payment_method_name:
                        page.click_text(request.payment_method_name)
                    else:
                        page.click_selector(".paymentmethod, .payment-method-button")
                    validate_clicked = page.click_first_text(["Validate"])
                    page.drain(0.5)
                    record_step("validate_order", "ok" if validate_clicked else "failed", None if validate_clicked else "Validate button not found")
                except Exception as exc:  # noqa: BLE001
                    record_step("validate_order", "failed", str(exc))
            else:
                record_step("validate_order", "skipped", "Non-destructive verification: validate_order is disabled.")

            markers = self._detect_odoo_breakage(page)
            if markers:
                overall_failed = True
                all_console.extend(markers)
                all_console_all.extend(markers)

            return PosFlowCheckResult(
                status="failed" if overall_failed else "ok",
                steps=steps,
                console_errors=sorted(set(all_console), key=all_console.index)[-100:],
                console_all=all_console_all[-1000:],
                network_errors=sorted(set(all_network), key=all_network.index)[-100:],
            )
        except Exception as exc:  # noqa: BLE001
            return PosFlowCheckResult(status="failed", steps=steps, console_errors=all_console, console_all=all_console_all[-1000:], network_errors=all_network, error=str(exc))
        finally:
            page.close()

    def _login(self, page: "_CdpPage", url_base: str, user: str | None, secret: str | None, timeout_seconds: int, redirect_path: str | None = None) -> None:
        if not user or not secret:
            raise RuntimeError("odoo_login_credentials_not_configured")
        login_url = urljoin(url_base + "/", "web/login")
        if redirect_path:
            path = redirect_path
            if _is_absolute_http_url(path):
                parsed = urlparse(path)
                path = parsed.path or "/web"
                if parsed.query:
                    path += "?" + parsed.query
            if not path.startswith("/"):
                path = "/" + path
            login_url = f"{login_url}?redirect={quote(path, safe='')}"

        page.navigate(login_url, timeout_seconds)
        page.wait_for_condition("document.readyState === 'complete'", min(10, timeout_seconds))
        if not self._is_login_page(page):
            return

        form_ready = page.wait_for_condition(
            "!!document.querySelector('input[name=\"login\"], input#login') && !!document.querySelector('input[name=\"password\"], input#password')",
            min(15, timeout_seconds),
        )
        if not form_ready:
            raise RuntimeError(f"odoo_login_form_not_found:{self._login_error_hint(page)}")

        attempts: list[dict[str, Any]] = []
        for name, attempt in (
            ("js_request_submit", self._login_attempt_js_request_submit),
            ("native_form_submit", self._login_attempt_native_form_submit),
            ("cdp_keyboard_enter", self._login_attempt_cdp_keyboard_enter),
        ):
            try:
                details = attempt(page, user, secret, timeout_seconds)
                if isinstance(details, dict):
                    details.setdefault("attempt", name)
                    attempts.append(details)
                else:
                    attempts.append({"attempt": name, "result": details})
            except Exception as exc:  # noqa: BLE001
                attempts.append({"attempt": name, "error": str(exc)})
            page.wait_for_condition("!location.pathname.endsWith('/web/login') || !!document.querySelector('.o_web_client')", min(15, timeout_seconds))
            page.drain(1.0)
            if not self._is_login_page(page):
                return

        hint = self._login_error_hint(page)
        raise RuntimeError(f"odoo_login_failed_or_still_on_login_page:{hint}; attempts={json.dumps(attempts, default=str)[:1200]}")

    def _login_attempt_js_request_submit(self, page: "_CdpPage", user: str, secret: str, timeout_seconds: int) -> dict[str, Any]:
        return page.eval_value(
            """
            (async () => {
              const setNativeValue = (el, value) => {
                const proto = Object.getPrototypeOf(el);
                const descriptor = Object.getOwnPropertyDescriptor(proto, 'value') || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                if (descriptor && descriptor.set) descriptor.set.call(el, value);
                else el.value = value;
                for (const type of ['focus', 'keydown', 'beforeinput', 'input', 'keyup', 'change']) {
                  el.dispatchEvent(new Event(type, {bubbles: true, cancelable: true}));
                }
              };
              const login = document.querySelector('input[name="login"], input#login');
              const password = document.querySelector('input[name="password"], input#password');
              const button = document.querySelector('button[type="submit"], input[type="submit"], .oe_login_buttons button, .btn-primary');
              const form = (button && button.form) || (password && password.form) || document.querySelector('form');
              if (!login || !password || !form) return {loginOk: !!login, passOk: !!password, formOk: !!form, buttonOk: !!button};
              login.scrollIntoView({block: 'center', inline: 'center'});
              setNativeValue(login, %s);
              setNativeValue(password, %s);
              await new Promise((resolve) => setTimeout(resolve, 150));
              if (form.requestSubmit) form.requestSubmit(button || undefined);
              else if (button) button.click();
              else form.submit();
              return {loginOk: true, passOk: true, formOk: true, buttonOk: !!button, submittedBy: form.requestSubmit ? 'requestSubmit' : (button ? 'button.click' : 'form.submit')};
            })()
            """
            % (json.dumps(user), json.dumps(secret)),
            await_promise=True,
        ) or {}

    def _login_attempt_native_form_submit(self, page: "_CdpPage", user: str, secret: str, timeout_seconds: int) -> dict[str, Any]:
        return page.eval_value(
            """
            (() => {
              const setValue = (selector, value) => {
                const el = document.querySelector(selector);
                if (!el) return false;
                el.focus();
                el.value = value;
                el.setAttribute('value', value);
                el.dispatchEvent(new InputEvent('input', {bubbles: true, cancelable: true, inputType: 'insertText', data: value}));
                el.dispatchEvent(new Event('change', {bubbles: true, cancelable: true}));
                return true;
              };
              const loginOk = setValue('input[name="login"], input#login', %s);
              const passOk = setValue('input[name="password"], input#password', %s);
              const form = document.querySelector('input[name="password"], input#password')?.form || document.querySelector('form');
              if (form) HTMLFormElement.prototype.submit.call(form);
              return {loginOk, passOk, formOk: !!form, submittedBy: 'HTMLFormElement.prototype.submit'};
            })()
            """
            % (json.dumps(user), json.dumps(secret)),
        ) or {}

    def _login_attempt_cdp_keyboard_enter(self, page: "_CdpPage", user: str, secret: str, timeout_seconds: int) -> dict[str, Any]:
        login_focused = page.focus_selector('input[name="login"], input#login')
        if login_focused:
            page.select_all()
            page.insert_text(user)
        password_focused = page.focus_selector('input[name="password"], input#password')
        if password_focused:
            page.select_all()
            page.insert_text(secret)
            page.press_key("Enter")
        return {"loginFocused": login_focused, "passwordFocused": password_focused, "submittedBy": "Input.insertText+Enter"}

    def _login_error_hint(self, page: "_CdpPage") -> str:
        payload = page.eval_value(
            """
            (() => {
              const selectors = ['.alert-danger', '.alert-warning', '.o_notification', '.o_notification_manager', '.oe_login_form', 'main', 'body'];
              const texts = [];
              for (const selector of selectors) {
                const el = document.querySelector(selector);
                const text = (el && el.innerText || '').trim();
                if (text) texts.push(`${selector}:${text.slice(0, 300)}`);
              }
              return {url: location.href, title: document.title, text: texts.join(' | ').slice(0, 900)};
            })()
            """
        )
        if not isinstance(payload, dict):
            return "login_page_no_diagnostics"
        text = str(payload.get("text") or "").replace("\n", " ").strip()
        for token in ["Wrong login/password", "Invalid", "Access Denied", "Too many", "csrf", "Database", "Session expired"]:
            if token.lower() in text.lower():
                return token
        return (text or str(payload.get("url") or "login_page_no_error_text"))[:900]

    def _cleanup_screenshot_dir(self, retention_seconds: int | None = None) -> dict[str, Any]:
        """Best-effort cleanup for generated browser screenshots.

        The current visual proof screenshots should usually remain available in
        the response, so this cleanup removes old files first and enforces a
        maximum file count.  Request-level delete_screenshots_after_verify can
        still remove the screenshots generated by the current request.
        """
        output_dir = Path(settings.screenshot_dir).expanduser().resolve()
        retention = settings.screenshot_retention_seconds if retention_seconds is None else retention_seconds
        max_files = max(0, int(settings.screenshot_max_files or 0))
        info: dict[str, Any] = {
            "enabled": True,
            "dir": str(output_dir),
            "retention_seconds": retention,
            "max_files": max_files,
            "deleted_old_paths": [],
            "deleted_old_count": 0,
            "errors": [],
        }
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            files = [
                path for path in output_dir.iterdir()
                if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg"}
            ]
            now = time.time()
            retained: list[Path] = []
            for path in files:
                try:
                    too_old = retention >= 0 and (now - path.stat().st_mtime) > retention
                    if too_old:
                        path.unlink(missing_ok=True)
                        info["deleted_old_paths"].append(str(path))
                    else:
                        retained.append(path)
                except Exception as exc:  # noqa: BLE001
                    info["errors"].append(f"{path}: {exc}")
                    retained.append(path)
            if max_files and len(retained) > max_files:
                retained.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0)
                for path in retained[: len(retained) - max_files]:
                    try:
                        path.unlink(missing_ok=True)
                        info["deleted_old_paths"].append(str(path))
                    except Exception as exc:  # noqa: BLE001
                        info["errors"].append(f"{path}: {exc}")
            info["deleted_old_count"] = len(info["deleted_old_paths"])
        except Exception as exc:  # noqa: BLE001
            info["errors"].append(str(exc))
        return info


    def _infer_ui_profile(self, profile: str | None, start_path: str) -> str:
        raw = (profile or "auto").strip() or "auto"
        if raw != "auto":
            return raw
        path = (start_path or "").lower()
        if "/pos/ui" in path or "/pos/web" in path:
            return "pos_product_screen"
        if "payment" in path and ("/my/invoices" in path or "/payment" in path):
            return "payment_provider"
        if "/shop" in path or "/website" in path:
            return "website_page"
        if "/web" in path or "/odoo" in path:
            if "view_type=list" in path:
                return "backend_list_view"
            return "backend_form_view"
        return "generic"

    def _ui_profile_defaults(self, profile: str) -> dict[str, Any]:
        common_diagnostics = ["overlap", "double_scrollbar", "clipping", "text_overflow", "hidden_scrollable"]
        profiles: dict[str, dict[str, Any]] = {
            "generic": {"diagnostics": common_diagnostics},
            "pos_product_screen": {
                "required_selectors": [
                    ".pos",
                    ".product-screen, .product-screen-main, .products-widget, .pos-content .product-list",
                ],
                "wait_until": "!document.querySelector('.loader, .pos-loader, .o_pos_loader, .o_loading_indicator')",
                "diagnostics": common_diagnostics,
            },
            "pos_payment_screen": {"required_selectors": [".pos", ".payment-screen"], "diagnostics": common_diagnostics},
            "pos_receipt_screen": {"required_selectors": [".pos", ".receipt-screen, .pos-receipt"], "diagnostics": common_diagnostics},
            "backend_form_view": {"required_selectors": [".o_web_client", ".o_action_manager"], "diagnostics": common_diagnostics},
            "backend_list_view": {"required_selectors": [".o_web_client", ".o_action_manager", ".o_list_renderer, .o_list_view"], "diagnostics": common_diagnostics},
            "website_page": {"required_selectors": ["body"], "forbidden_text": ["404", "500: Internal Server Error"], "diagnostics": common_diagnostics},
            "website_form": {"required_selectors": ["body", "form"], "diagnostics": common_diagnostics},
            "dashboard_owl": {"required_selectors": [".o_web_client, .o_action_manager, main, body"], "diagnostics": common_diagnostics},
            "payment_provider": {"required_selectors": ["body"], "forbidden_text": ["Traceback", "Odoo Client Error"], "diagnostics": common_diagnostics},
            "report_pdf_preview": {"required_selectors": ["body"], "diagnostics": common_diagnostics},
        }
        return profiles.get(profile, profiles["generic"])

    def _merge_unique(self, left: list[str], right: list[str]) -> list[str]:
        seen: set[str] = set()
        merged: list[str] = []
        for item in [*(left or []), *(right or [])]:
            value = str(item or "").strip()
            if value and value not in seen:
                seen.add(value)
                merged.append(value)
        return merged

    def _selector_count(self, page: "_CdpPage", selector: str) -> int:
        return int(page.eval_value("document.querySelectorAll(%s).length" % json.dumps(selector)) or 0)

    def _page_text_contains(self, page: "_CdpPage", text: str) -> bool:
        return bool(page.eval_value("(document.body && document.body.innerText || '').includes(%s)" % json.dumps(text)))

    def _wait_for_ui_ready(
        self,
        page: "_CdpPage",
        timeout_seconds: int,
        *,
        required_selectors: list[str],
        min_elements: dict[str, int],
        wait_until: str | None,
    ) -> None:
        selectors = [item for item in required_selectors if item]
        min_map = {str(k): int(v) for k, v in (min_elements or {}).items() if str(k).strip() and int(v) > 0}
        conditions: list[str] = []
        for selector in selectors:
            conditions.append(f"document.querySelector({json.dumps(selector)})")
        for selector, minimum in min_map.items():
            conditions.append(f"document.querySelectorAll({json.dumps(selector)}).length >= {int(minimum)}")
        if wait_until:
            conditions.append(f"Boolean({wait_until})")
        if not conditions:
            return
        expression = " && ".join(f"({item})" for item in conditions)
        if not page.wait_for_condition(expression, timeout_seconds):
            snapshot = self._run_ui_assertions(
                page,
                profile="ready_wait",
                required_selectors=selectors,
                forbidden_selectors=[],
                required_text=[],
                forbidden_text=[],
                min_elements=min_map,
                wait_until=wait_until,
            )
            raise TimeoutError(f"ui_not_ready_or_wrong_screen: {json.dumps(snapshot.get('failures', []), default=str)}")

    def _run_ui_assertions(
        self,
        page: "_CdpPage",
        *,
        profile: str,
        required_selectors: list[str],
        forbidden_selectors: list[str],
        required_text: list[str],
        forbidden_text: list[str],
        min_elements: dict[str, int],
        wait_until: str | None,
    ) -> dict[str, Any]:
        failures: list[str] = []
        required = []
        for selector in required_selectors or []:
            count = self._selector_count(page, selector)
            ok = count > 0
            if not ok:
                failures.append(f"required_selector_missing:{selector}")
            required.append({"selector": selector, "count": count, "ok": ok})
        forbidden = []
        for selector in forbidden_selectors or []:
            count = self._selector_count(page, selector)
            ok = count == 0
            if not ok:
                failures.append(f"forbidden_selector_visible:{selector}")
            forbidden.append({"selector": selector, "count": count, "ok": ok})
        mins = []
        for selector, minimum in (min_elements or {}).items():
            count = self._selector_count(page, selector)
            ok = count >= int(minimum)
            if not ok:
                failures.append(f"min_elements_not_met:{selector}:{count}<{int(minimum)}")
            mins.append({"selector": selector, "count": count, "minimum": int(minimum), "ok": ok})
        required_text_result = []
        for text in required_text or []:
            present = self._page_text_contains(page, text)
            if not present:
                failures.append(f"required_text_missing:{text}")
            required_text_result.append({"text": text, "present": present, "ok": present})
        forbidden_text_result = []
        for text in forbidden_text or []:
            present = self._page_text_contains(page, text)
            if present:
                failures.append(f"forbidden_text_visible:{text}")
            forbidden_text_result.append({"text": text, "present": present, "ok": not present})
        wait_ok = True
        if wait_until:
            wait_ok = bool(page.eval_value(f"Boolean({wait_until})"))
            if not wait_ok:
                failures.append("wait_until_false")
        return {
            "status": "failed" if failures else "ok",
            "profile": profile,
            "required_selectors": required,
            "forbidden_selectors": forbidden,
            "min_elements": mins,
            "required_text": required_text_result,
            "forbidden_text": forbidden_text_result,
            "wait_until": {"expression": wait_until, "ok": wait_ok} if wait_until else None,
            "failures": failures,
        }

    def _action_failure_markers(self, action_results: list[dict[str, Any]]) -> list[str]:
        markers: list[str] = []
        for item in action_results or []:
            if item.get("status") == "failed" and not item.get("optional"):
                markers.append(f"browser_action_failed:{item.get('index')}:{item.get('type')}:{item.get('error')}")
        return markers

    def _current_viewport(self, page: "_CdpPage") -> dict[str, Any]:
        value = page.eval_value("(() => ({width: innerWidth, height: innerHeight, devicePixelRatio}))()")
        return value if isinstance(value, dict) else {}

    def _screenshot_url(self, path: str | None) -> str | None:
        if not path:
            return None
        base = (settings.screenshot_public_base_url or settings.public_base_url or "").rstrip("/")
        if not base:
            return None
        return f"{base}/browser/screenshots/{quote(Path(path).name)}"

    def _compare_screenshot(
        self,
        screenshot_path: str | None,
        reference_image_path: str | None,
        mode: str,
        max_diff_ratio: float | None,
    ) -> dict[str, Any]:
        mode = (mode or "off").strip().lower()
        if mode == "off" or not reference_image_path:
            return {}
        result: dict[str, Any] = {"mode": mode, "reference_image_path": reference_image_path, "screenshot_path": screenshot_path, "failures": []}
        if not screenshot_path:
            result["failures"].append("screenshot_missing")
            return result
        try:
            from PIL import Image, ImageChops, ImageStat
            shot = Image.open(screenshot_path).convert("RGB")
            ref = Image.open(reference_image_path).convert("RGB")
            result["screenshot_size"] = list(shot.size)
            result["reference_size"] = list(ref.size)
            size_match = shot.size == ref.size
            result["size_match"] = size_match
            if not size_match:
                result["failures"].append(f"viewport_or_screenshot_size_mismatch:{shot.size[0]}x{shot.size[1]}!={ref.size[0]}x{ref.size[1]}")
            compare_shot = shot if size_match else shot.resize(ref.size)
            diff = ImageChops.difference(compare_shot, ref)
            stat = ImageStat.Stat(diff)
            mean_abs = sum(stat.mean) / (len(stat.mean) * 255.0)
            result["mean_abs_diff_ratio"] = round(float(mean_abs), 6)
            if max_diff_ratio is not None and mean_abs > max_diff_ratio:
                result["failures"].append(f"visual_diff_ratio_too_high:{mean_abs:.6f}>{max_diff_ratio:.6f}")
        except Exception as exc:  # noqa: BLE001
            result["failures"].append(f"visual_compare_error:{exc}")
        result["status"] = "failed" if result.get("failures") else "ok"
        return result

    def _screenshot_base64(self, path: str | None, max_bytes: int) -> str | None:
        if not path or max_bytes <= 0:
            return None
        try:
            file_path = Path(path)
            if not file_path.is_file() or file_path.stat().st_size > max_bytes:
                return None
            return base64.b64encode(file_path.read_bytes()).decode("ascii")
        except Exception:
            return None

    def _finalize_screenshot_cleanup(self, result: BrowserCheckResult, cleanup_info: dict[str, Any], delete_generated: bool) -> BrowserCheckResult:
        cleanup = dict(cleanup_info or {})
        cleanup.setdefault("deleted_generated_paths", [])

        # Keep only the newest final proof screenshot. Every successful capture
        # replaces the previous proof instead of accumulating image files.
        if (
            not delete_generated
            and settings.screenshot_keep_latest_only
            and result.screenshot_path
            and Path(result.screenshot_path).is_file()
        ):
            deleted_replaced = self._delete_all_screenshots_except(result.screenshot_path)
            cleanup["deleted_replaced_paths"] = deleted_replaced
            cleanup["deleted_replaced_count"] = len(deleted_replaced)
            if result.debug_screenshot_path in deleted_replaced:
                result.debug_screenshot_path = None
                result.debug_screenshot_url = None
            for action in result.action_results:
                if isinstance(action, dict) and action.get("screenshot_path") in deleted_replaced:
                    action["deleted_screenshot_path"] = action.pop("screenshot_path")

        if delete_generated:
            generated_paths: list[str] = []
            if result.screenshot_path:
                generated_paths.append(result.screenshot_path)
            if result.debug_screenshot_path:
                generated_paths.append(result.debug_screenshot_path)
            for action in result.action_results:
                path = action.get("screenshot_path") if isinstance(action, dict) else None
                if path:
                    generated_paths.append(path)
            deleted = self._delete_screenshot_paths(generated_paths)
            cleanup["deleted_generated_paths"] = deleted
            result.screenshots_deleted_after_verify = bool(deleted)
            if result.screenshot_path in deleted:
                result.screenshot_path = None
                result.screenshot_url = None
                result.screenshot_base64 = None
            if result.debug_screenshot_path in deleted:
                result.debug_screenshot_path = None
                result.debug_screenshot_url = None
            for action in result.action_results:
                if isinstance(action, dict) and action.get("screenshot_path") in deleted:
                    action["deleted_screenshot_path"] = action.pop("screenshot_path")
        result.screenshot_cleanup = cleanup
        return result


    def _delete_all_screenshots_except(self, keep_path: str) -> list[str]:
        output_dir = Path(settings.screenshot_dir).expanduser().resolve()
        try:
            keep = Path(keep_path).expanduser().resolve()
        except Exception:
            return []
        deleted: list[str] = []
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            for path in output_dir.iterdir():
                try:
                    resolved = path.resolve()
                    if (
                        resolved == keep
                        or not path.is_file()
                        or path.suffix.lower() not in {".png", ".jpg", ".jpeg"}
                    ):
                        continue
                    path.unlink(missing_ok=True)
                    deleted.append(str(resolved))
                except Exception:
                    continue
        except Exception:
            return deleted
        return deleted

    def _delete_screenshot_paths(self, paths: list[str]) -> list[str]:
        output_dir = Path(settings.screenshot_dir).expanduser().resolve()
        deleted: list[str] = []
        for raw in paths:
            try:
                path = Path(raw).expanduser().resolve()
                if output_dir not in [path, *path.parents]:
                    continue
                if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                    path.unlink(missing_ok=True)
                    deleted.append(str(path))
            except Exception:
                continue
        return deleted

    def _capture_screenshot_memory(
        self, page: "_CdpPage", *, full_page: bool = False, image_format: str = "jpeg", quality: int = 50
    ) -> tuple[bytes | None, dict[str, Any]]:
        """Capture one viewport image in RAM only. Never creates a Render file."""
        fmt = "png" if str(image_format).lower() == "png" else "jpeg"
        params: dict[str, Any] = {"format": fmt, "captureBeyondViewport": bool(full_page), "fromSurface": True}
        if fmt == "jpeg":
            params["quality"] = max(1, min(int(quality), 100))
        try:
            result = page.call("Page.captureScreenshot", params, timeout=20)
            encoded = result.get("data")
            if not encoded:
                return None, {"captured": False, "reason": "empty_cdp_screenshot"}
            raw = base64.b64decode(encoded)
            analysis: dict[str, Any] = {
                "captured": True, "persisted": False, "format": fmt, "bytes": len(raw), "viewport_only": not bool(full_page)
            }
            try:
                from PIL import Image, ImageStat
                with Image.open(io.BytesIO(raw)) as image:
                    rgb = image.convert("RGB")
                    stat = ImageStat.Stat(rgb.resize((64, 64)))
                    analysis.update({
                        "width": image.width, "height": image.height,
                        "mean_rgb": [round(value, 2) for value in stat.mean],
                    })
            except Exception as exc:  # image metadata is best-effort
                analysis["image_analysis_warning"] = str(exc)
            return raw, analysis
        except Exception as exc:  # noqa: BLE001
            page.console_errors.append(f"memory_screenshot_capture_failed:{exc}")
            return None, {"captured": False, "persisted": False, "reason": str(exc)}

    def _compare_screenshot_bytes(
        self, screenshot_bytes: bytes | None, reference_image_path: str | None, mode: str, max_ratio: float | None
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"mode": mode, "reference_image_path": reference_image_path, "screenshot_path": None, "memory_only": True, "failures": []}
        if mode == "off" or not reference_image_path:
            return result
        if not screenshot_bytes:
            result["failures"].append("memory_screenshot_missing")
            return result
        try:
            from PIL import Image, ImageChops, ImageStat
            shot = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
            ref = Image.open(reference_image_path).convert("RGB")
            ref = ref.resize(shot.size)
            diff = ImageChops.difference(shot, ref)
            stat = ImageStat.Stat(diff)
            ratio = sum(stat.mean) / (3 * 255)
            result.update({"diff_ratio": ratio, "shot_size": shot.size, "reference_size": ref.size})
            if max_ratio is not None and ratio > max_ratio:
                result["failures"].append(f"visual_diff_ratio_exceeded:{ratio:.6f}>{max_ratio:.6f}")
        except Exception as exc:  # noqa: BLE001
            result["failures"].append(f"memory_visual_compare_failed:{exc}")
        return result

    def _capture_screenshot(self, page: "_CdpPage", screenshot_name: str, full_page: bool) -> str | None:
        """Capture with bounded retries because headless Chromium can be busy
        immediately after a heavy POS mount. A screenshot timeout must not hide
        the actual navigation/readiness result.
        """
        safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (screenshot_name or "odoo_page.png"))
        if not safe_name.lower().endswith((".png", ".jpg", ".jpeg")):
            safe_name += ".png"
        output_dir = Path(settings.screenshot_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / safe_name

        attempts = [
            ({"format": "png", "captureBeyondViewport": bool(full_page), "fromSurface": True}, 15),
            ({"format": "png", "captureBeyondViewport": False, "fromSurface": True}, 20),
            ({"format": "png", "captureBeyondViewport": False}, 25),
        ]
        last_error: Exception | None = None
        for index, (params, timeout) in enumerate(attempts):
            try:
                if index:
                    page.drain(0.75)
                result = page.call("Page.captureScreenshot", params, timeout=timeout)
                data = result.get("data")
                if data:
                    output_path.write_bytes(base64.b64decode(data))
                    return str(output_path)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        if last_error:
            page.console_errors.append(f"screenshot_capture_failed_after_retries:{last_error}")
        return None


    def _is_login_page(self, page: "_CdpPage") -> bool:
        return bool(page.eval_value("location.pathname.endsWith('/web/login') || !!document.querySelector('input[name=\"login\"], input[name=\"password\"]')"))

    def _resolve_visual_scenario(
        self,
        *,
        scenario: str | None,
        goal: str | None,
        profile: str,
        required_selectors: list[str],
        required_text: list[str],
        product_count: int,
    ) -> dict[str, Any]:
        requested = str(scenario or "auto").strip().lower().replace("-", "_")
        haystack = " ".join([str(goal or ""), profile, *required_selectors, *required_text]).lower()
        aliases = {
            "trolley": "trolley_with_products",
            "cart": "trolley_with_products",
            "order": "product_screen_with_order",
            "payment": "payment_screen",
            "customer": "customer_popup",
            "receipt": "receipt_screen",
            "disabled": "none",
            "off": "none",
        }
        requested = aliases.get(requested, requested)
        if requested == "auto":
            if any(token in haystack for token in ["trolley", "cart", "orderline", "order line"]):
                requested = "trolley_with_products"
            elif "payment" in haystack:
                requested = "payment_screen"
            elif any(token in haystack for token in ["customer popup", "customer screen", "select customer"]):
                requested = "customer_popup"
            elif "receipt" in haystack:
                requested = "receipt_screen"
            elif product_count > 0:
                requested = "product_screen_with_order"
            else:
                requested = "none"
        defaults = {
            "none": {"name": "none", "product_count": 0},
            "product_screen_with_order": {"name": "product_screen_with_order", "product_count": max(product_count, 1)},
            "trolley_with_products": {"name": "trolley_with_products", "product_count": max(product_count, 2)},
            "payment_screen": {"name": "payment_screen", "product_count": max(product_count, 1)},
            "customer_popup": {"name": "customer_popup", "product_count": max(product_count, 0)},
            "receipt_screen": {"name": "receipt_screen", "product_count": max(product_count, 1)},
        }
        spec = defaults.get(requested)
        if spec is None:
            return {"name": "none", "product_count": 0, "resolution_warning": f"unknown_scenario:{requested}"}
        return {**spec, "goal": goal, "resolved_from": scenario or "auto", "temporary_order_created": False}

    def _pos_order_line_count(self, page: "_CdpPage") -> int:
        value = page.eval_value(
            """
            (() => {
              const selectors = [
                '.pos .orderline', '.pos .order-line', '.pos .orderlines .orderline',
                '.pos .order-container .orderline', '.pos-theme-trolley-line'
              ];
              let count = 0;
              for (const selector of selectors) count = Math.max(count, document.querySelectorAll(selector).length);
              return count;
            })()
            """
        )
        return int(value or 0)

    def _click_safe_visible_product(self, page: "_CdpPage", excluded_names: list[str]) -> dict[str, Any]:
        value = page.eval_value(
            """
            (() => {
              const excluded = new Set(%s.map((x) => String(x).trim().toLowerCase()));
              const visible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 30 && r.height > 30 && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const candidates = Array.from(document.querySelectorAll(
                '.pos .product, .pos .product-card, .pos .product-list .product, .pos .products-widget .product'
              ));
              for (const card of candidates) {
                if (!visible(card) || card.matches('.disabled, [disabled], .not-available, .out-of-stock')) continue;
                const nameEl = card.querySelector('.product-name, .product-content, .name');
                const name = String((nameEl && nameEl.textContent) || card.getAttribute('data-name') || card.textContent || '').trim();
                if (!name || excluded.has(name.toLowerCase())) continue;
                const lower = name.toLowerCase();
                if (/lot|serial|scale|configur|combo|variant/.test(lower)) continue;
                card.click();
                return {clicked: true, name, className: card.className || ''};
              }
              return {clicked: false, candidate_count: candidates.length};
            })()
            """ % json.dumps(excluded_names)
        )
        return value if isinstance(value, dict) else {"clicked": False, "raw": value}

    def _click_first_visible_text(self, page: "_CdpPage", labels: list[str]) -> dict[str, Any]:
        value = page.eval_value(
            """
            (() => {
              const labels = %s.map((x) => String(x).trim().toLowerCase());
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 20 && r.height > 20 && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const nodes = Array.from(document.querySelectorAll('button, [role="button"], .button, .control-button, .btn'));
              for (const wanted of labels) {
                const exact = nodes.find((el) => visible(el) && String(el.innerText || el.textContent || '').trim().toLowerCase() === wanted);
                const partial = exact || nodes.find((el) => visible(el) && String(el.innerText || el.textContent || '').trim().toLowerCase().includes(wanted));
                if (partial) {
                  const text = String(partial.innerText || partial.textContent || '').trim();
                  partial.click();
                  return {clicked: true, text};
                }
              }
              return {clicked: false, labels};
            })()
            """ % json.dumps(labels)
        )
        return value if isinstance(value, dict) else {"clicked": False, "raw": value}

    def _run_visual_scenario(
        self,
        page: "_CdpPage",
        spec: dict[str, Any],
        timeout_seconds: int,
        *,
        require_functional_proof: bool,
    ) -> dict[str, Any]:
        name = str(spec.get("name") or "none")
        trace: list[dict[str, Any]] = []
        before_lines = self._pos_order_line_count(page)
        clicked_names: list[str] = []
        target_products = int(spec.get("product_count") or 0)
        for _ in range(target_products):
            before = self._pos_order_line_count(page)
            clicked = self._click_safe_visible_product(page, clicked_names)
            trace.append({"action": "click_visible_product", **clicked, "order_lines_before": before})
            if not clicked.get("clicked"):
                break
            clicked_names.append(str(clicked.get("name") or ""))
            changed = page.wait_for_condition(
                "document.querySelectorAll('.pos .orderline, .pos .order-line, .pos-theme-trolley-line').length > %d" % before,
                min(timeout_seconds, 12),
            )
            after = self._pos_order_line_count(page)
            trace[-1]["order_lines_after"] = after
            trace[-1]["state_changed"] = bool(changed or after > before)
            if require_functional_proof and after <= before:
                return {
                    "status": "failed",
                    "scenario": name,
                    "reason": "product_click_did_not_add_order_line",
                    "products_clicked": clicked_names,
                    "order_lines_before": before_lines,
                    "order_lines_after": after,
                    "trace": trace,
                }

        after_products = self._pos_order_line_count(page)
        if require_functional_proof and target_products and after_products < before_lines + target_products:
            return {
                "status": "failed",
                "scenario": name,
                "reason": "insufficient_order_lines_after_setup",
                "products_clicked": clicked_names,
                "order_lines_before": before_lines,
                "order_lines_after": after_products,
                "trace": trace,
            }

        if name == "payment_screen":
            clicked = self._click_first_visible_text(page, ["Payment", "Pay"])
            trace.append({"action": "open_payment", **clicked})
            if not clicked.get("clicked") or not page.wait_for_condition("!!document.querySelector('.pos .payment-screen, .payment-screen')", min(timeout_seconds, 20)):
                return {"status": "failed", "scenario": name, "reason": "payment_screen_not_opened", "trace": trace}
        elif name == "customer_popup":
            clicked = self._click_first_visible_text(page, ["Customer", "Set Customer", "Client"])
            trace.append({"action": "open_customer", **clicked})
            if not clicked.get("clicked") or not page.wait_for_condition("!!document.querySelector('.partner-list, .partner-screen, .modal-dialog, .popup')", min(timeout_seconds, 15)):
                return {"status": "failed", "scenario": name, "reason": "customer_ui_not_opened", "trace": trace}
        elif name == "receipt_screen":
            clicked = self._click_first_visible_text(page, ["Payment", "Pay"])
            trace.append({"action": "open_payment", **clicked})
            if not clicked.get("clicked") or not page.wait_for_condition("!!document.querySelector('.pos .payment-screen, .payment-screen')", min(timeout_seconds, 20)):
                return {"status": "failed", "scenario": name, "reason": "payment_screen_not_opened", "trace": trace}
            return {
                "status": "failed",
                "scenario": name,
                "reason": "receipt_scenario_requires_explicit_payment_method_and_validate_order",
                "trace": trace,
            }

        return {
            "status": "ok",
            "scenario": name,
            "products_clicked": clicked_names,
            "order_lines_before": before_lines,
            "order_lines_after": self._pos_order_line_count(page),
            "trace": trace,
        }

    def _cleanup_visual_scenario(self, page: "_CdpPage", spec: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        # Cleanup is intentionally conservative. Only scenarios that explicitly
        # record creation of a temporary order may discard it.
        if not spec.get("temporary_order_created"):
            return {"status": "skipped", "reason": "no_temporary_order_created"}
        clicked = self._click_first_visible_text(page, ["Delete Order", "Discard", "Cancel"])
        page.drain(0.5)
        return {"status": "ok" if clicked.get("clicked") else "failed", **clicked}

    def _run_actions(self, page: "_CdpPage", actions: list[BrowserAction] | list[dict[str, Any]], timeout_seconds: int, full_page: bool) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for index, raw in enumerate(actions or [], start=1):
            action = raw if isinstance(raw, BrowserAction) else BrowserAction.model_validate(raw)
            item: dict[str, Any] = {"index": index, "type": action.type, "status": "ok", "optional": action.optional}
            try:
                timeout = action.timeout_seconds or timeout_seconds
                if action.type == "wait":
                    if not action.selector:
                        raise ValueError("wait action requires selector")
                    page.wait_for_selector(action.selector, timeout)
                elif action.type == "click":
                    clicked = page.click_selector(action.selector) if action.selector else page.click_text(action.text)
                    if not clicked:
                        raise RuntimeError(f"click target not found: {action.selector or action.text}")
                    page.drain(0.5)
                elif action.type == "hover":
                    if not action.selector:
                        raise ValueError("hover action requires selector")
                    if not page.hover_selector(action.selector):
                        raise RuntimeError(f"hover target not found: {action.selector}")
                    page.drain(0.5)
                elif action.type == "scroll":
                    if not page.scroll_selector(action.selector, action.delta_y):
                        raise RuntimeError(f"scroll target not found or not scrollable: {action.selector or 'window'}")
                    page.drain(0.5)
                elif action.type == "screenshot":
                    name = action.name or f"action_{index}.png"
                    item["screenshot_path"] = self._capture_screenshot(page, name, full_page)
                elif action.type == "evaluate":
                    if not action.expression:
                        raise ValueError("evaluate action requires expression")
                    item["value"] = page.eval_value(action.expression)
                elif action.type == "inject_css":
                    if not action.css:
                        raise ValueError("inject_css action requires css")
                    page.inject_css(action.css, action.style_id or f"odoo-agent-visual-css-{index}")
                    page.drain(0.2)
                else:
                    raise ValueError(f"unsupported action type: {action.type}")
            except Exception as exc:  # noqa: BLE001
                item["status"] = "failed"
                item["error"] = str(exc)
            results.append(item)
        return results

    def _run_visual_diagnostics(self, page: "_CdpPage", diagnostics: list[str]) -> dict[str, Any]:
        requested = {str(item).strip().lower() for item in (diagnostics or []) if str(item).strip()}
        if not requested:
            return {}
        payload = page.eval_value(_visual_diagnostics_expression(sorted(requested)))
        result = payload if isinstance(payload, dict) else {"raw": payload}
        failures: list[str] = []
        if "overlap" in requested and result.get("overlaps"):
            failures.append("overlap")
        if "double_scrollbar" in requested and result.get("nested_visible_scrollbars"):
            failures.append("double_scrollbar")
        if "clipping" in requested and result.get("clipped"):
            failures.append("clipping")
        if "text_overflow" in requested and result.get("text_overflow"):
            failures.append("text_overflow")
        result["requested"] = sorted(requested)
        result["failures"] = failures
        result["status"] = "failed" if failures else "ok"
        return result

    def _detect_odoo_breakage(self, page: "_CdpPage") -> list[str]:
        markers = page.evaluate(
            """
            (() => {
              const markers = [];
              for (const selector of ['.o_error_dialog', '.o_dialog_error', '.o_technical_modal', '.o_notification_manager .border-danger', '.o_notification_manager .text-danger']) {
                if (document.querySelector(selector)) markers.push(`odoo_ui_error_selector:${selector}`);
              }
              const body = (document.body && document.body.innerText || '').slice(0, 5000);
              for (const token of ['Odoo Client Error', 'shell_ERROR', 'UncaughtPromiseError', 'Traceback', 'Internal Server Error']) {
                if (body.includes(token)) markers.push(`odoo_ui_error_text:${token}`);
              }
              return markers;
            })()
            """
        )
        value = markers.get("result", {}).get("value", [])
        return [str(item) for item in value] if isinstance(value, list) else []


class _CdpPage:
    def __init__(self, ws_url: str | None = None, tab_id: str | None = None, timeout_seconds: int = 45, error: str | None = None) -> None:
        self.ws_url = ws_url
        self.tab_id = tab_id
        self.timeout_seconds = timeout_seconds
        self.error = error
        self._ws = None
        self._next_id = 0
        self.console_errors: list[str] = []
        self.console_all: list[str] = []
        self.network_errors: list[str] = []
        self._step_console_errors: list[str] = []
        self._step_console_all: list[str] = []
        self._step_network_errors: list[str] = []

    @classmethod
    def new(cls, timeout_seconds: int) -> "_CdpPage":
        cdp_url = (os.environ.get("ODOO_AGENT_CDP_URL") or settings.cdp_url or "").rstrip("/")
        if not cdp_url:
            return cls(error="CDP browser is not configured. Set ODOO_AGENT_CDP_URL to a Chrome remote debugging endpoint.")
        try:
            payload = _cdp_json(cdp_url, "/json/new?about:blank", timeout_seconds)
            ws_url = payload.get("webSocketDebuggerUrl")
            tab_id = payload.get("id")
            if not ws_url:
                return cls(error="CDP browser did not return webSocketDebuggerUrl from /json/new")
            page = cls(ws_url=ws_url, tab_id=tab_id, timeout_seconds=timeout_seconds)
            page._connect()
            return page
        except Exception as exc:  # noqa: BLE001
            return cls(error=f"Could not open CDP browser tab: {exc}")

    def _connect(self) -> None:
        import websocket

        self._ws = websocket.create_connection(self.ws_url, timeout=max(1, min(self.timeout_seconds, 30)))

    def enable(self) -> None:
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Log.enable")
        self.call("Network.enable")

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        if self._ws is None:
            raise RuntimeError("CDP socket is not connected")
        self._next_id += 1
        message_id = self._next_id
        self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + (timeout or self.timeout_seconds)
        while time.monotonic() < deadline:
            raw = self._ws.recv()
            message = json.loads(raw)
            if "method" in message:
                self._handle_event(message)
                continue
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(message["error"].get("message", str(message["error"])))
                return message.get("result") or {}
        raise TimeoutError(f"Timed out waiting for CDP response to {method}")

    def set_viewport(self, width: int, height: int, device_scale_factor: float = 1.0) -> None:
        width = max(320, int(width or 1280))
        height = max(240, int(height or 720))
        scale = max(0.25, float(device_scale_factor or 1.0))
        self.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": scale,
                "mobile": False,
                "screenWidth": width,
                "screenHeight": height,
            },
            timeout=5,
        )

    def navigate(self, url: str, timeout_seconds: int) -> None:
        self.call("Page.navigate", {"url": url}, timeout=timeout_seconds)
        self.wait_for_event("Page.loadEventFired", min(timeout_seconds, 30))
        self.drain(0.5)

    def wait_for_event(self, event_name: str, timeout_seconds: float) -> bool:
        if self._ws is None:
            return False
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                raw = self._ws.recv()
            except Exception:
                return False
            message = json.loads(raw)
            if message.get("method") == event_name:
                return True
            if "method" in message:
                self._handle_event(message)
        return False

    def drain(self, seconds: float) -> None:
        if self._ws is None:
            return
        old_timeout = getattr(self._ws, "timeout", None)
        self._ws.settimeout(0.2)
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                try:
                    raw = self._ws.recv()
                except Exception:
                    continue
                message = json.loads(raw)
                if "method" in message:
                    self._handle_event(message)
        finally:
            self._ws.settimeout(old_timeout)

    def evaluate(self, expression: str, await_promise: bool = False) -> dict[str, Any]:
        return self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": await_promise, "returnByValue": True},
        )

    def eval_value(self, expression: str, await_promise: bool = False) -> Any:
        try:
            return self.evaluate(expression, await_promise=await_promise).get("result", {}).get("value")
        except Exception:
            return None

    def wait_for_condition(self, expression: str, timeout_seconds: int) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.eval_value(f"Boolean({expression})"):
                return True
            self.drain(0.25)
        return False

    def wait_for_selector(self, selector: str, timeout_seconds: int) -> bool:
        escaped = json.dumps(selector)
        ok = self.wait_for_condition(f"document.querySelector({escaped})", timeout_seconds)
        if not ok:
            raise TimeoutError(f"Selector not found: {selector}")
        return True


    def focus_selector(self, selector: str) -> bool:
        return bool(self.eval_value(
            """
            (() => {
              const el = document.querySelector(%s);
              if (!el) return false;
              el.scrollIntoView({block: 'center', inline: 'center'});
              el.focus();
              return document.activeElement === el;
            })()
            """
            % json.dumps(selector)
        ))

    def insert_text(self, text: str) -> None:
        self.call("Input.insertText", {"text": text}, timeout=5)

    def press_key(self, key: str) -> None:
        key_map = {
            "Enter": {"windowsVirtualKeyCode": 13, "code": "Enter"},
            "Backspace": {"windowsVirtualKeyCode": 8, "code": "Backspace"},
            "Delete": {"windowsVirtualKeyCode": 46, "code": "Delete"},
            "A": {"windowsVirtualKeyCode": 65, "code": "KeyA"},
        }
        meta = key_map.get(key, {"windowsVirtualKeyCode": 0, "code": key})
        params = {"key": key, "code": meta["code"], "windowsVirtualKeyCode": meta["windowsVirtualKeyCode"], "nativeVirtualKeyCode": meta["windowsVirtualKeyCode"]}
        self.call("Input.dispatchKeyEvent", {**params, "type": "keyDown"}, timeout=5)
        self.call("Input.dispatchKeyEvent", {**params, "type": "keyUp"}, timeout=5)

    def select_all(self) -> None:
        self.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Control", "code": "ControlLeft", "windowsVirtualKeyCode": 17, "nativeVirtualKeyCode": 17, "modifiers": 2}, timeout=5)
        self.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": 2}, timeout=5)
        self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": 2}, timeout=5)
        self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Control", "code": "ControlLeft", "windowsVirtualKeyCode": 17, "nativeVirtualKeyCode": 17}, timeout=5)

    def click_selector(self, selector: str) -> bool:
        return bool(self.eval_value(
            """
            (() => {
              const el = document.querySelector(%s);
              if (!el) return false;
              el.click();
              return true;
            })()
            """
            % json.dumps(selector)
        ))

    def click_text(self, text: str | None) -> bool:
        if not text:
            return False
        return bool(self.eval_value(_click_text_expression([text])))

    def hover_selector(self, selector: str) -> bool:
        return bool(self.eval_value(
            """
            (() => {
              const el = document.querySelector(%s);
              if (!el) return false;
              el.scrollIntoView({block: 'center', inline: 'center'});
              for (const type of ['pointerover', 'mouseover', 'mouseenter', 'pointermove', 'mousemove']) {
                el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
              }
              return true;
            })()
            """
            % json.dumps(selector)
        ))

    def inject_css(self, css: str, style_id: str) -> bool:
        return bool(self.eval_value(
            """
            (() => {
              const id = %s;
              let tag = document.getElementById(id);
              if (!tag) {
                tag = document.createElement('style');
                tag.id = id;
                document.head.appendChild(tag);
              }
              tag.textContent = %s;
              return true;
            })()
            """
            % (json.dumps(style_id), json.dumps(css))
        ))

    def scroll_selector(self, selector: str | None, delta_y: int) -> bool:
        return bool(self.eval_value(
            """
            (() => {
              const el = %s ? document.querySelector(%s) : (document.scrollingElement || document.documentElement);
              if (!el) return false;
              const before = el.scrollTop || window.scrollY || 0;
              if (el === document.scrollingElement || el === document.documentElement || el === document.body) {
                window.scrollBy(0, %s);
              } else {
                el.scrollTop = before + %s;
                el.dispatchEvent(new Event('scroll', {bubbles: true}));
              }
              const after = el.scrollTop || window.scrollY || 0;
              return true;
            })()
            """
            % ("true" if selector else "false", json.dumps(selector or ""), int(delta_y), int(delta_y))
        ))

    def click_first_text(self, labels: list[str]) -> bool:
        return bool(self.eval_value(_click_text_expression(labels)))

    def snapshot_and_clear(self) -> tuple[list[str], list[str], list[str]]:
        console_errors = list(self._step_console_errors)
        console_all = list(self._step_console_all)
        network_errors = list(self._step_network_errors)
        self._step_console_errors.clear()
        self._step_console_all.clear()
        self._step_network_errors.clear()
        return console_errors, console_all, network_errors

    def best_effort_url(self) -> str | None:
        value = self.eval_value("location.href")
        return str(value) if value else None

    def best_effort_title(self) -> str | None:
        value = self.eval_value("document.title")
        return str(value) if value else None

    def close(self) -> None:
        try:
            if self._ws is not None:
                self._ws.close()
        finally:
            cdp_url = (os.environ.get("ODOO_AGENT_CDP_URL") or settings.cdp_url or "").rstrip("/")
            if cdp_url and self.tab_id:
                try:
                    _cdp_json(cdp_url, f"/json/close/{quote(self.tab_id)}", 5)
                except Exception:
                    pass

    def _handle_event(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "Runtime.consoleAPICalled":
            level = str(params.get("type") or "log")
            args = params.get("args") or []
            text = " ".join(_remote_object_text(item) for item in args) or level
            self._record_console(f"{level}: {text}", level)
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            text = details.get("text") or details.get("exception", {}).get("description") or json.dumps(details, default=str)
            self._record_console(f"pageerror: {text}", "error")
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            level = str(entry.get("level") or "info")
            text = str(entry.get("text") or entry)
            self._record_console(f"log.{level}: {text}", level)
        elif method == "Network.responseReceived":
            response = params.get("response") or {}
            status = int(response.get("status") or 0)
            if status >= 400:
                self._record_network(f"{status} {response.get('url')}")
        elif method == "Network.loadingFailed":
            self._record_network(f"failed {params.get('errorText')} {params.get('requestId')}")

    def _record_console(self, entry: str, level: str) -> None:
        self.console_all.append(entry)
        self._step_console_all.append(entry)
        if level in {"error", "warning", "warn"}:
            self.console_errors.append(entry)
            self._step_console_errors.append(entry)

    def _record_network(self, entry: str) -> None:
        self.network_errors.append(entry)
        self._step_network_errors.append(entry)



def _is_absolute_http_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(str(value))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _build_target_url(url_base: str, start_path: str) -> str:
    path = str(start_path or "/web").strip()
    if _is_absolute_http_url(path):
        return path
    base = (url_base or "").rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return urljoin(base + "/", path.lstrip("/"))


def _debug_contrast_expression(enable: bool) -> str:
    if not enable:
        return """
        (() => {
          document.documentElement.classList.remove('odoo-agent-debug-contrast');
          document.body && document.body.classList.remove('odoo-agent-debug-contrast');
          const style = document.getElementById('odoo-agent-debug-contrast-style');
          if (style) style.remove();
          return true;
        })()
        """
    css = '''
html.odoo-agent-debug-contrast,
body.odoo-agent-debug-contrast {
  background: #101010 !important;
}
html.odoo-agent-debug-contrast body,
body.odoo-agent-debug-contrast {
  color: #111111 !important;
}
html.odoo-agent-debug-contrast .pos,
html.odoo-agent-debug-contrast .product-screen,
html.odoo-agent-debug-contrast .leftpane,
html.odoo-agent-debug-contrast .rightpane,
html.odoo-agent-debug-contrast .order-container,
html.odoo-agent-debug-contrast .pos-theme-trolley-container {
  background: #f8f9fa !important;
  color: #111111 !important;
}
html.odoo-agent-debug-contrast .pos-theme-trolley,
html.odoo-agent-debug-contrast .pos-theme-trolley-packed,
html.odoo-agent-debug-contrast .pos-theme-trolley-active,
html.odoo-agent-debug-contrast .pos-theme-trolley-packed-preview {
  background: #fff3bf !important;
  color: #111111 !important;
  outline: 3px solid #ff006e !important;
  outline-offset: -3px !important;
  box-shadow: inset 0 0 0 2px #00aaff, 0 0 0 2px rgba(0, 0, 0, .16) !important;
}
html.odoo-agent-debug-contrast .pos-theme-trolley-preview-lines,
html.odoo-agent-debug-contrast .pos-theme-trolley-lines,
html.odoo-agent-debug-contrast .orderlines,
html.odoo-agent-debug-contrast .order-container {
  outline: 3px dashed #ff6b00 !important;
  outline-offset: -4px !important;
}
html.odoo-agent-debug-contrast .pos-theme-trolley-preview-line,
html.odoo-agent-debug-contrast .pos-theme-trolley-line,
html.odoo-agent-debug-contrast .orderline {
  background: #ffec99 !important;
  color: #111111 !important;
  outline: 2px solid #0057ff !important;
  outline-offset: -2px !important;
  text-shadow: none !important;
}
html.odoo-agent-debug-contrast .pos-theme-trolley-preview-line:nth-child(even),
html.odoo-agent-debug-contrast .pos-theme-trolley-line:nth-child(even),
html.odoo-agent-debug-contrast .orderline:nth-child(even) {
  background: #b2f2bb !important;
}
html.odoo-agent-debug-contrast .product-name,
html.odoo-agent-debug-contrast .pos-theme-trolley-preview-name,
html.odoo-agent-debug-contrast .pos-theme-trolley-preview-meta,
html.odoo-agent-debug-contrast .pos-theme-trolley-preview-unit,
html.odoo-agent-debug-contrast .price,
html.odoo-agent-debug-contrast .product-price {
  color: #000000 !important;
  background: rgba(255, 255, 255, .78) !important;
  text-shadow: none !important;
}
html.odoo-agent-debug-contrast .pos-theme-trolley-preview-total,
html.odoo-agent-debug-contrast .pos-theme-trolley-total,
html.odoo-agent-debug-contrast .pos-theme-trolley-preview-qty,
html.odoo-agent-debug-contrast .qty {
  background: #111111 !important;
  color: #ffffff !important;
  outline: 2px solid #ff006e !important;
  outline-offset: -2px !important;
  text-shadow: none !important;
}
html.odoo-agent-debug-contrast *:focus,
html.odoo-agent-debug-contrast *:hover {
  outline-color: #ff0000 !important;
}
'''
    return f"""
    (() => {{
      let style = document.getElementById('odoo-agent-debug-contrast-style');
      if (!style) {{
        style = document.createElement('style');
        style.id = 'odoo-agent-debug-contrast-style';
        document.head.appendChild(style);
      }}
      style.textContent = {json.dumps(css)};
      document.documentElement.classList.add('odoo-agent-debug-contrast');
      document.body && document.body.classList.add('odoo-agent-debug-contrast');
      return true;
    }})()
    """

def _visual_diagnostics_expression(requested: list[str]) -> str:
    requested_json = json.dumps(requested)
    return f"""
    (() => {{
      const requested = new Set({requested_json});
      const rectOf = (el) => {{
        const r = el.getBoundingClientRect();
        return {{x:r.x, y:r.y, width:r.width, height:r.height, top:r.top, left:r.left, right:r.right, bottom:r.bottom}};
      }};
      const visible = (el) => {{
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
      }};
      const label = (el, i) => {{
        const cls = String(el.className || '').trim().replace(/\\s+/g, '.');
        const tag = el.tagName ? el.tagName.toLowerCase() : 'node';
        const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
        return `${{tag}}${{cls ? '.' + cls : ''}}[${{i}}]${{text ? ':' + text : ''}}`;
      }};
      const result = {{overlaps: [], nested_visible_scrollbars: [], visible_scroll_containers: [], clipped: [], text_overflow: [], hidden_scrollable: []}};

      if (requested.has('overlap')) {{
        const items = Array.from(document.querySelectorAll('.pos-theme-trolley-preview-line, .pos-theme-trolley-line, .orderline'))
          .filter(visible);
        for (let i = 0; i < items.length; i++) {{
          const a = items[i];
          const ar = a.getBoundingClientRect();
          for (let j = i + 1; j < items.length; j++) {{
            const b = items[j];
            if (a.parentElement !== b.parentElement) continue;
            const br = b.getBoundingClientRect();
            const xOverlap = Math.max(0, Math.min(ar.right, br.right) - Math.max(ar.left, br.left));
            const yOverlap = Math.max(0, Math.min(ar.bottom, br.bottom) - Math.max(ar.top, br.top));
            if (xOverlap > 6 && yOverlap > 3) {{
              result.overlaps.push({{a: label(a, i), b: label(b, j), overlap: {{x: Math.round(xOverlap), y: Math.round(yOverlap)}}}});
            }}
          }}
        }}
      }}

      const scrollables = Array.from(document.querySelectorAll('body, body *')).filter((el) => {{
        if (!visible(el)) return false;
        const cs = getComputedStyle(el);
        const overflowY = cs.overflowY;
        const canScroll = el.scrollHeight > el.clientHeight + 2 && ['auto', 'scroll', 'overlay'].includes(overflowY);
        return canScroll;
      }});
      for (let i = 0; i < scrollables.length; i++) {{
        const el = scrollables[i];
        const visibleBar = el.offsetWidth - el.clientWidth > 0 || getComputedStyle(el).scrollbarWidth !== 'none';
        const entry = {{selector: label(el, i), visible_scrollbar: !!visibleBar, rect: rectOf(el)}};
        if (visibleBar) result.visible_scroll_containers.push(entry);
        else result.hidden_scrollable.push(entry);
      }}
      if (requested.has('double_scrollbar')) {{
        const visibles = result.visible_scroll_containers.map((entry, i) => ({{entry, el: scrollables[i]}})).filter(x => x.entry.visible_scrollbar);
        for (let i = 0; i < scrollables.length; i++) {{
          const parent = scrollables[i];
          const pVisible = parent.offsetWidth - parent.clientWidth > 0 || getComputedStyle(parent).scrollbarWidth !== 'none';
          if (!pVisible) continue;
          for (let j = 0; j < scrollables.length; j++) {{
            if (i === j) continue;
            const child = scrollables[j];
            const cVisible = child.offsetWidth - child.clientWidth > 0 || getComputedStyle(child).scrollbarWidth !== 'none';
            if (cVisible && parent.contains(child)) {{
              result.nested_visible_scrollbars.push({{outer: label(parent, i), inner: label(child, j)}});
            }}
          }}
        }}
      }}

      if (requested.has('clipping')) {{
        const items = Array.from(document.querySelectorAll('.pos-theme-trolley-preview-line, .orderline, .product-name, .pos-theme-trolley-preview-name, .pos-theme-trolley-preview-total')).filter(visible);
        for (let i = 0; i < items.length; i++) {{
          const el = items[i];
          const parent = el.parentElement;
          if (!parent) continue;
          const cs = getComputedStyle(parent);
          if (!['hidden', 'clip', 'auto', 'scroll'].includes(cs.overflow) && !['hidden', 'clip', 'auto', 'scroll'].includes(cs.overflowY)) continue;
          const r = el.getBoundingClientRect();
          const pr = parent.getBoundingClientRect();
          if (r.top < pr.top - 2 || r.bottom > pr.bottom + 2 || r.left < pr.left - 2 || r.right > pr.right + 2) {{
            result.clipped.push({{element: label(el, i), parent: label(parent, i), rect: rectOf(el), parent_rect: rectOf(parent)}});
          }}
        }}
      }}

      if (requested.has('text_overflow')) {{
        const texts = Array.from(document.querySelectorAll('.product-name, .pos-theme-trolley-preview-name, .pos-theme-trolley-preview-total, .pos-theme-trolley-preview-meta, .price, .product-price')).filter(visible);
        for (let i = 0; i < texts.length; i++) {{
          const el = texts[i];
          if (el.scrollWidth > el.clientWidth + 2 || el.scrollHeight > el.clientHeight + 2) {{
            result.text_overflow.push({{element: label(el, i), scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight}});
          }}
        }}
      }}
      return result;
    }})()
    """

def _remote_object_text(item: dict[str, Any]) -> str:
    if "value" in item:
        return str(item["value"])
    if "description" in item:
        return str(item["description"])
    return json.dumps(item, default=str)


def _click_text_expression(labels: list[str]) -> str:
    labels_json = json.dumps([label.lower() for label in labels])
    return f"""
    (() => {{
      const labels = {labels_json};
      const candidates = Array.from(document.querySelectorAll('button, a, [role="button"], .product-card, .o_pos_product, article.product, .paymentmethod, .payment-method-button'));
      const match = candidates.find((el) => {{
        const text = (el.innerText || el.textContent || '').toLowerCase();
        return labels.some((label) => text.includes(label));
      }});
      if (!match) return false;
      match.click();
      return true;
    }})()
    """


def _cdp_json(cdp_url: str, path: str, timeout_seconds: int) -> dict[str, Any]:
    parsed = urlparse(cdp_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("ODOO_AGENT_CDP_URL must be an http(s) Chrome remote debugging endpoint")
    url = cdp_url.rstrip("/") + path
    errors: list[str] = []
    for method in ("PUT", "POST", "GET"):
        try:
            req = Request(url, method=method)
            with urlopen(req, timeout=max(1, min(timeout_seconds, 30))) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw.strip().startswith(("{", "[")) else {"result": raw}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{method}: {exc}")
    raise RuntimeError("; ".join(errors))
