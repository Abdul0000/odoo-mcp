from odoo_agent_server.services.browser_cdp_service import CdpBrowserService


class _FakePage:
    def wait_for_condition(self, expression, timeout):
        return True

    def drain(self, seconds):
        return None


def test_auto_resolves_trolley_scenario_from_feature_selector():
    service = CdpBrowserService()
    spec = service._resolve_visual_scenario(
        scenario="auto",
        goal="verify the custom trolley after adding products",
        profile="pos_product_screen",
        required_selectors=[".pos-theme-trolley-container"],
        required_text=[],
        product_count=0,
    )
    assert spec["name"] == "trolley_with_products"
    assert spec["product_count"] == 2


def test_explicit_payment_scenario_requires_one_product():
    service = CdpBrowserService()
    spec = service._resolve_visual_scenario(
        scenario="payment_screen",
        goal=None,
        profile="pos_product_screen",
        required_selectors=[],
        required_text=[],
        product_count=0,
    )
    assert spec["name"] == "payment_screen"
    assert spec["product_count"] == 1


def test_trolley_scenario_proves_order_line_growth(monkeypatch):
    service = CdpBrowserService()
    page = _FakePage()
    line_counts = iter([0, 0, 1, 1, 2, 2, 2])
    monkeypatch.setattr(service, "_pos_order_line_count", lambda _page: next(line_counts))
    clicks = iter([
        {"clicked": True, "name": "Product A"},
        {"clicked": True, "name": "Product B"},
    ])
    monkeypatch.setattr(service, "_click_safe_visible_product", lambda _page, excluded: next(clicks))

    result = service._run_visual_scenario(
        page,
        {"name": "trolley_with_products", "product_count": 2},
        30,
        require_functional_proof=True,
    )

    assert result["status"] == "ok"
    assert result["products_clicked"] == ["Product A", "Product B"]
    assert result["order_lines_before"] == 0
    assert result["order_lines_after"] == 2


def test_scenario_fails_when_click_does_not_change_order(monkeypatch):
    service = CdpBrowserService()
    page = _FakePage()
    monkeypatch.setattr(service, "_pos_order_line_count", lambda _page: 0)
    monkeypatch.setattr(service, "_click_safe_visible_product", lambda _page, excluded: {"clicked": True, "name": "Product A"})

    result = service._run_visual_scenario(
        page,
        {"name": "product_screen_with_order", "product_count": 1},
        30,
        require_functional_proof=True,
    )

    assert result["status"] == "failed"
    assert result["reason"] == "product_click_did_not_add_order_line"
