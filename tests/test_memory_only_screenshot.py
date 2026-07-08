import base64
import io
from pathlib import Path

from PIL import Image

from odoo_agent_server.schemas import BrowserCheckRequest
from odoo_agent_server.services.browser_cdp_service import CdpBrowserService


class FakePage:
    def __init__(self, encoded: str):
        self.encoded = encoded
        self.console_errors = []

    def call(self, method, params, timeout=0):
        assert method == "Page.captureScreenshot"
        assert params["format"] == "jpeg"
        assert params["captureBeyondViewport"] is False
        return {"data": self.encoded}


def test_memory_only_is_default_and_does_not_require_filename():
    request = BrowserCheckRequest()
    assert request.screenshot_mode == "memory_only"
    assert request.screenshot_format == "jpeg"
    assert request.screenshot_quality == 50


def test_memory_capture_returns_bytes_without_writing(tmp_path, monkeypatch):
    image = Image.new("RGB", (32, 24), (240, 230, 220))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=50)
    encoded = base64.b64encode(buffer.getvalue()).decode()

    service = CdpBrowserService()
    monkeypatch.chdir(tmp_path)
    before = list(tmp_path.rglob("*"))
    raw, analysis = service._capture_screenshot_memory(
        FakePage(encoded), full_page=False, image_format="jpeg", quality=50
    )
    after = list(tmp_path.rglob("*"))

    assert raw
    assert analysis["captured"] is True
    assert analysis["persisted"] is False
    assert analysis["width"] == 32
    assert analysis["height"] == 24
    assert before == after
