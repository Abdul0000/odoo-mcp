from __future__ import annotations

import asyncio

from odoo_agent_server.config import settings
from odoo_agent_server.services.browser_cdp_service import CdpBrowserService, _CdpPage


def test_cdp_browser_missing_config_returns_clear_failure(monkeypatch):
    monkeypatch.delenv("ODOO_AGENT_CDP_URL", raising=False)
    monkeypatch.setattr(settings, "cdp_url", None)

    result = asyncio.run(CdpBrowserService().screenshot_odoo_page(
        base_url="https://example.test",
        login=None,
        password=None,
        start_path="/web",
        screenshot_name="",
    ))

    assert result.status == "failed"
    assert "CDP browser is not configured" in result.error


def test_cdp_event_collection_splits_all_console_errors_and_network():
    page = _CdpPage(ws_url="ws://example.test/devtools/page/1")

    page._handle_event({
        "method": "Runtime.consoleAPICalled",
        "params": {"type": "log", "args": [{"value": "boot ok"}]},
    })
    page._handle_event({
        "method": "Runtime.consoleAPICalled",
        "params": {"type": "error", "args": [{"value": "owl exploded"}]},
    })
    page._handle_event({
        "method": "Runtime.exceptionThrown",
        "params": {"exceptionDetails": {"text": "UncaughtPromiseError"}},
    })
    page._handle_event({
        "method": "Network.responseReceived",
        "params": {"response": {"status": 500, "url": "https://example.test/web/assets"}},
    })

    assert "log: boot ok" in page.console_all
    assert "error: owl exploded" in page.console_all
    assert "error: owl exploded" in page.console_errors
    assert "pageerror: UncaughtPromiseError" in page.console_errors
    assert page.network_errors == ["500 https://example.test/web/assets"]

    console_errors, console_all, network_errors = page.snapshot_and_clear()
    assert console_errors == ["error: owl exploded", "pageerror: UncaughtPromiseError"]
    assert console_all == ["log: boot ok", "error: owl exploded", "pageerror: UncaughtPromiseError"]
    assert network_errors == ["500 https://example.test/web/assets"]
    assert page.snapshot_and_clear() == ([], [], [])


def test_screenshot_cleanup_removes_old_files_and_keeps_recent(tmp_path, monkeypatch):
    screenshot_dir = tmp_path / "shots"
    screenshot_dir.mkdir()
    old_file = screenshot_dir / "old.png"
    new_file = screenshot_dir / "new.png"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")

    import time as _time
    stale = _time.time() - 7200
    old_file.touch()
    new_file.touch()
    old_file.chmod(0o644)
    new_file.chmod(0o644)
    import os as _os
    _os.utime(old_file, (stale, stale))

    monkeypatch.setattr(settings, "screenshot_dir", str(screenshot_dir))
    monkeypatch.setattr(settings, "screenshot_max_files", 50)

    info = CdpBrowserService()._cleanup_screenshot_dir(retention_seconds=3600)

    assert info["deleted_old_count"] == 1
    assert not old_file.exists()
    assert new_file.exists()


def test_finalize_screenshot_cleanup_can_delete_generated_files(tmp_path, monkeypatch):
    from odoo_agent_server.schemas import BrowserCheckResult

    screenshot_dir = tmp_path / "shots"
    screenshot_dir.mkdir()
    final_file = screenshot_dir / "final.png"
    action_file = screenshot_dir / "hover.png"
    final_file.write_bytes(b"final")
    action_file.write_bytes(b"hover")
    monkeypatch.setattr(settings, "screenshot_dir", str(screenshot_dir))

    result = BrowserCheckResult(
        status="ok",
        screenshot_path=str(final_file),
        action_results=[{"type": "screenshot", "screenshot_path": str(action_file)}],
    )

    updated = CdpBrowserService()._finalize_screenshot_cleanup(result, {"enabled": True}, True)

    assert updated.screenshot_path is None
    assert updated.screenshots_deleted_after_verify is True
    assert not final_file.exists()
    assert not action_file.exists()
    assert updated.action_results[0]["deleted_screenshot_path"] == str(action_file)


def test_browser_action_failures_are_blocking_unless_optional():
    from odoo_agent_server.schemas import BrowserAction

    service = CdpBrowserService()
    blocking = service._run_actions(
        page=_FakeBrowserPage(selectors=set()),
        actions=[BrowserAction(type="wait", selector=".product-screen", timeout_seconds=1)],
        timeout_seconds=1,
        full_page=False,
    )
    assert blocking[0]["status"] == "failed"
    assert service._action_failure_markers(blocking)

    optional = service._run_actions(
        page=_FakeBrowserPage(selectors=set()),
        actions=[BrowserAction(type="wait", selector=".missing", timeout_seconds=1, optional=True)],
        timeout_seconds=1,
        full_page=False,
    )
    assert optional[0]["status"] == "failed"
    assert optional[0]["optional"] is True
    assert service._action_failure_markers(optional) == []


def test_ui_assertions_report_required_and_forbidden_failures():
    page = _FakeBrowserPage(
        selectors={".pos": 1, ".product-screen": 0, ".o_error_dialog": 1, ".product-list .product": 0},
        text="Taxes Total",
    )
    result = CdpBrowserService()._run_ui_assertions(
        page,
        profile="pos_product_screen",
        required_selectors=[".pos", ".product-screen"],
        forbidden_selectors=[".o_error_dialog"],
        required_text=["Total"],
        forbidden_text=["Taxes"],
        min_elements={".product-list .product": 1},
        wait_until="true",
    )
    assert result["status"] == "failed"
    assert "required_selector_missing:.product-screen" in result["failures"]
    assert "forbidden_selector_visible:.o_error_dialog" in result["failures"]
    assert "forbidden_text_visible:Taxes" in result["failures"]
    assert any(item.startswith("min_elements_not_met:.product-list .product") for item in result["failures"])


def test_ui_profile_inference_and_defaults_are_pos_strict():
    service = CdpBrowserService()
    assert service._infer_ui_profile("auto", "/pos/ui?config_id=1") == "pos_product_screen"
    defaults = service._ui_profile_defaults("pos_product_screen")
    assert any(".product-screen" in selector for selector in defaults["required_selectors"])
    assert "min_elements" not in defaults


def test_pos_visual_defaults_use_human_navigation_and_stable_viewport():
    from odoo_agent_server.schemas import PosVisualVerifyRequest

    request = PosVisualVerifyRequest()

    assert request.start_path == "/odoo/point-of-sale"
    assert request.human_pos_navigation is True
    assert (request.viewport_width, request.viewport_height) == (1440, 1000)
    assert request.include_screenshot_base64 is True


def test_human_pos_navigation_scopes_config_and_handles_opening_dialog(monkeypatch):
    service = CdpBrowserService()
    page = _FakePosNavigationPage()

    monkeypatch.setattr(
        service,
        "_resolve_pos_config",
        lambda *args, **kwargs: {"id": 7, "name": "Furniture Shop", "current_session_id": [12, "POS/00012"]},
    )

    def click_card(*args, **kwargs):
        page.state = "opening_control"
        return {"clicked": True, "scoped": True, "label": "Continue Selling"}

    monkeypatch.setattr(service, "_click_pos_card_action", click_card)

    result = service._navigate_pos_as_human(
        page,
        "https://odoo.example",
        5,
        pos_config_id=7,
    )

    assert result["status"] == "opened"
    assert result["config"]["id"] == 7
    assert result["config"]["name"] == "Furniture Shop"
    assert result["handled"] == ["Continue Selling", "Open Session"]
    assert page.navigated_to == "https://odoo.example/odoo/point-of-sale"


def test_pos_flow_is_non_destructive_by_default():
    from odoo_agent_server.schemas import PosFlowCheckRequest

    assert PosFlowCheckRequest().validate_order is False


class _FakeBrowserPage:
    def __init__(self, selectors=None, text=""):
        self.selectors = selectors or set()
        self.text = text

    def wait_for_selector(self, selector, timeout_seconds):
        if self._count(selector) <= 0:
            raise TimeoutError(f"Selector not found: {selector}")
        return True

    def eval_value(self, expression, await_promise=False):
        import json as _json
        if "querySelectorAll" in expression and ".length" in expression:
            selector = expression.split("querySelectorAll(", 1)[1].split(").length", 1)[0]
            return self._count(_json.loads(selector))
        if "querySelector" in expression:
            selector = expression.split("querySelector(", 1)[1].split(")", 1)[0]
            return self._count(_json.loads(selector)) > 0
        if "innerText" in expression and ".includes(" in expression:
            token = expression.split(".includes(", 1)[1].split(")", 1)[0]
            return _json.loads(token) in self.text
        if expression.startswith("Boolean("):
            return "true" in expression or "!document.querySelector" in expression
        return None

    def click_selector(self, selector):
        return self._count(selector) > 0

    def click_text(self, text):
        return bool(text and text in self.text)

    def hover_selector(self, selector):
        return self._count(selector) > 0

    def scroll_selector(self, selector, delta_y):
        return True

    def drain(self, seconds):
        return None

    def _count(self, selector):
        if isinstance(self.selectors, set):
            return 1 if selector in self.selectors else 0
        return int(self.selectors.get(selector, 0))


class _FakePosNavigationPage:
    def __init__(self):
        self.state = "blank"
        self.navigated_to = None

    def navigate(self, url, timeout_seconds):
        self.navigated_to = url
        self.state = "dashboard"

    def wait_for_condition(self, expression, timeout_seconds):
        return True

    def eval_value(self, expression, await_promise=False):
        if "product-screen" in expression:
            return self.state == "product"
        if "'open session', 'open register', 'continue selling'" in expression:
            if self.state == "opening_control":
                self.state = "product"
                return "Open Session"
            return None
        if "fatal_error" in expression:
            return {"url": self.best_effort_url(), "text": self.state, "fatal_error": False}
        if "location.href" in expression:
            return {"url": self.best_effort_url(), "text": self.state}
        return None

    def drain(self, seconds):
        return None

    def best_effort_url(self):
        return "https://odoo.example/pos/ui?config_id=7" if self.state != "dashboard" else self.navigated_to


def test_pos_visual_default_does_not_duplicate_strict_product_wait():
    from odoo_agent_server.schemas import PosVisualVerifyRequest

    request = PosVisualVerifyRequest()
    assert request.wait_selector is None


def test_pos_product_profile_accepts_odoo18_product_mount_variants():
    service = CdpBrowserService()
    defaults = service._ui_profile_defaults("pos_product_screen")
    selector = defaults["required_selectors"][1]
    assert ".product-screen" in selector
    assert ".products-widget" in selector
    assert "min_elements" not in defaults


def test_finalize_screenshot_cleanup_keeps_only_latest_final(tmp_path, monkeypatch):
    from odoo_agent_server.schemas import BrowserCheckResult

    screenshot_dir = tmp_path / "shots"
    screenshot_dir.mkdir()
    previous = screenshot_dir / "previous.png"
    debug = screenshot_dir / "debug.png"
    latest = screenshot_dir / "latest.png"
    previous.write_bytes(b"previous")
    debug.write_bytes(b"debug")
    latest.write_bytes(b"latest")

    monkeypatch.setattr(settings, "screenshot_dir", str(screenshot_dir))
    monkeypatch.setattr(settings, "screenshot_keep_latest_only", True)

    result = BrowserCheckResult(
        status="ok",
        screenshot_path=str(latest),
        debug_screenshot_path=str(debug),
        action_results=[{"type": "screenshot", "screenshot_path": str(debug)}],
    )

    updated = CdpBrowserService()._finalize_screenshot_cleanup(result, {"enabled": True}, False)

    assert latest.exists()
    assert not previous.exists()
    assert not debug.exists()
    assert updated.screenshot_path == str(latest)
    assert updated.debug_screenshot_path is None
    assert updated.screenshot_cleanup["deleted_replaced_count"] == 2
    assert updated.action_results[0]["deleted_screenshot_path"] == str(debug)
