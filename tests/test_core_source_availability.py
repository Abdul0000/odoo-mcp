"""Tests for the core-source-availability guard in syntax_verifier_service.

Bug this fixes: comodel_module_exists, js_import_resolves, and
manifest_depends all hit their CRITICAL branch whenever a module/import
"isn't found" under /home/odoo/src/*. But that hardcoded tree only exists
locally on Odoo.sh; on a Render deployment (or any environment where
ODOOSH_SSH_HOST isn't configured), it never exists and core_sync_service's
rsync silently no-ops every time. Before this fix, that meant PERFECTLY
VALID Odoo code (a real comodel_name, a real @web/ import, a real
depends[] entry) would be reported as CRITICAL and block push — not
because the code was wrong, but because the verifier couldn't reach the
source tree to check it.

These tests simulate "core source unreachable" (no local roots, sync
disabled) and confirm the same findings are downgraded to WARNING with an
explanatory message, instead of silently blocking a legitimate push.
"""
from __future__ import annotations

import pytest

from odoo_agent_server.config import settings
from odoo_agent_server.schemas import SyntaxVerifyRequest, VerifySeverity
from odoo_agent_server.services import syntax_verifier_service as svs


@pytest.fixture
def core_source_unreachable(monkeypatch):
    """Simulate the Render-without-Odoo.sh-SSH scenario: no local source
    roots exist, and sync is disabled so it can never fetch them either.
    """
    monkeypatch.setattr(svs, "_SRC_ROOTS", [
        __import__("pathlib").Path("/nonexistent/src/user"),
        __import__("pathlib").Path("/nonexistent/src/enterprise"),
        __import__("pathlib").Path("/nonexistent/src/odoo"),
    ])
    monkeypatch.setattr(settings, "core_sync_enabled", False)
    monkeypatch.setattr(settings, "odoosh_ssh_host", None)


def test_core_source_available_is_false_when_unreachable(core_source_unreachable):
    assert svs._core_source_available() is False


def test_core_source_available_is_true_when_sync_configured(monkeypatch):
    monkeypatch.setattr(svs, "_SRC_ROOTS", [
        __import__("pathlib").Path("/nonexistent/src/user"),
        __import__("pathlib").Path("/nonexistent/src/enterprise"),
        __import__("pathlib").Path("/nonexistent/src/odoo"),
    ])
    monkeypatch.setattr(settings, "core_sync_enabled", True)
    monkeypatch.setattr(settings, "odoosh_ssh_host", "odoosh-build-host.example.com")
    assert svs._core_source_available() is True


def test_composite_qa_can_disable_remote_core_sync_per_request(tmp_path, monkeypatch):
    monkeypatch.setattr(svs, "_SRC_ROOTS", [tmp_path / "missing_user", tmp_path / "missing_core"])
    monkeypatch.setattr(settings, "core_sync_enabled", True)
    monkeypatch.setattr(settings, "odoosh_ssh_host", "live-build.dev.odoo.com")
    monkeypatch.setattr(
        svs.core_sync_service,
        "ensure_module_synced",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("composite QA must remain local")),
    )
    manifest = tmp_path / "__manifest__.py"
    manifest.write_text("{'name': 'X', 'depends': ['website']}\n", encoding="utf-8")

    result = svs.SyntaxVerifierService().verify(SyntaxVerifyRequest(
        written_files=[str(manifest)],
        allow_remote_core_sync=False,
    ))

    assert result.push_blocked is False
    assert any(finding.check == "core_source_unavailable" for finding in result.findings)


def test_comodel_check_downgrades_to_warning_when_source_unreachable(tmp_path, core_source_unreachable):
    """This is the exact false-positive: a real comodel_name in a model
    file, checked with no way to reach core source, must not be CRITICAL.
    """
    py_file = tmp_path / "sale_order.py"
    py_file.write_text(
        "from odoo import fields, models\n"
        "class SaleOrder(models.Model):\n"
        "    _inherit = 'sale.order'\n"
        "    custom_partner_id = fields.Many2one(comodel_name='res.partner')\n",
        encoding="utf-8",
    )
    result = svs.SyntaxVerifierService().verify(SyntaxVerifyRequest(written_files=[str(py_file)]))
    comodel_findings = [f for f in result.findings if f.check == "comodel_module_exists"]
    assert comodel_findings, "Expected a comodel_module_exists finding"
    assert all(f.severity != VerifySeverity.CRITICAL for f in comodel_findings), (
        "comodel_module_exists must not be CRITICAL when core source is unreachable — "
        "this would false-positive block a valid push."
    )
    assert result.push_blocked is False


def test_manifest_depends_downgrades_to_warning_when_source_unreachable(tmp_path, core_source_unreachable):
    manifest = tmp_path / "__manifest__.py"
    manifest.write_text(
        "{'name': 'X', 'version': '1.0', 'depends': ['sale_management']}",
        encoding="utf-8",
    )
    result = svs.SyntaxVerifierService().verify(SyntaxVerifyRequest(written_files=[str(manifest)]))
    depends_findings = [f for f in result.findings if f.check == "manifest_depends"]
    assert depends_findings, "Expected a manifest_depends finding"
    assert all(f.severity != VerifySeverity.CRITICAL for f in depends_findings)
    assert result.push_blocked is False


def test_related_manifest_skips_unchanged_dependency_sync_for_scss(tmp_path, monkeypatch):
    module = tmp_path / "x_module"
    scss = module / "static" / "src" / "scss" / "theme.scss"
    scss.parent.mkdir(parents=True)
    scss.write_text(".card { border-color: #333; }\n", encoding="utf-8")
    manifest = module / "__manifest__.py"
    manifest.write_text(
        "{'name': 'X', 'depends': ['slow_custom_dependency'], "
        "'assets': {'web.assets_backend': ['x_module/static/src/scss/theme.scss']}}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        svs,
        "_module_exists_in_src",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dependency sync must be skipped")),
    )

    result = svs.SyntaxVerifierService().verify(SyntaxVerifyRequest(
        written_files=[str(scss)],
        project_path=str(tmp_path),
        impact_aware=True,
    ))

    assert str(manifest) in result.files_checked
    assert not [finding for finding in result.findings if finding.check == "manifest_depends"]
    assert result.push_blocked is False


def test_js_import_downgrades_to_warning_when_source_unreachable(tmp_path, core_source_unreachable):
    js_file = tmp_path / "product_screen_patch.js"
    js_file.write_text(
        "/** @odoo-module **/\n"
        "import { ProductScreen } from \"@point_of_sale/app/screens/product_screen/product_screen\";\n"
        "import { patch } from \"@web/core/utils/patch\";\n"
        "patch(ProductScreen.prototype, {});\n",
        encoding="utf-8",
    )
    result = svs.SyntaxVerifierService().verify(SyntaxVerifyRequest(written_files=[str(js_file)]))
    import_findings = [f for f in result.findings if f.check == "js_import_resolves" and f.severity != VerifySeverity.INFO]
    assert import_findings, "Expected a js_import_resolves finding"
    assert all(f.severity != VerifySeverity.CRITICAL for f in import_findings), (
        "js_import_resolves must not be CRITICAL when core source is unreachable — "
        "this is a real @point_of_sale/ and @web/ import that simply couldn't be checked."
    )
    assert result.push_blocked is False


def test_top_level_notice_present_when_source_unreachable(tmp_path, core_source_unreachable):
    py_file = tmp_path / "harmless.py"
    py_file.write_text("from odoo import models\nclass X(models.Model):\n    _name = 'x.y'\n", encoding="utf-8")
    result = svs.SyntaxVerifierService().verify(SyntaxVerifyRequest(written_files=[str(py_file)]))
    codes = [f.code if hasattr(f, "code") else f.check for f in result.findings]
    assert "core_source_unavailable" in [f.check for f in result.findings]


def test_still_critical_when_source_available_and_dep_genuinely_missing(tmp_path, monkeypatch):
    """Sanity check the other direction: when core source IS reachable and
    a module genuinely doesn't exist, it must still be CRITICAL. The fix
    should only affect the "can't verify" case, not weaken real detection.
    """
    fake_root = tmp_path / "fake_src_user"
    fake_root.mkdir()
    (fake_root / "some_other_module").mkdir()  # non-empty root = source genuinely reachable
    monkeypatch.setattr(svs, "_SRC_ROOTS", [fake_root])
    monkeypatch.setattr(settings, "core_sync_enabled", False)
    monkeypatch.setattr(settings, "odoosh_ssh_host", None)

    manifest = tmp_path / "__manifest__.py"
    manifest.write_text(
        "{'name': 'X', 'version': '1.0', 'depends': ['totally_made_up_module_xyz']}",
        encoding="utf-8",
    )
    result = svs.SyntaxVerifierService().verify(SyntaxVerifyRequest(written_files=[str(manifest)]))
    depends_findings = [f for f in result.findings if f.check == "manifest_depends"]
    assert depends_findings
    assert depends_findings[0].severity == VerifySeverity.CRITICAL, (
        "When a source root DOES exist locally (even if empty), that counts as "
        "'source available' — a genuinely missing module must still be CRITICAL."
    )
