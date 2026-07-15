from types import SimpleNamespace

from odoo_agent_server.config import settings
from odoo_agent_server.services import core_sync_service
from odoo_agent_server.services.core_finder_service import CoreFinderService
from odoo_agent_server.services.frontend_pattern_service import FrontendPatternService
from odoo_agent_server.schemas import FrontendPatternPreflightRequest


def test_find_core_file_syncs_when_search_root_does_not_exist(tmp_path, monkeypatch):
    remote_root = tmp_path / "home" / "odoo" / "src" / "enterprise"

    def fake_sync(root, module_name):
        assert root == str(remote_root)
        module_dir = remote_root / module_name / "models"
        module_dir.mkdir(parents=True)
        (module_dir / "payment_method.py").write_text("class PaymentMethod: pass\n", encoding="utf-8")
        return True

    monkeypatch.setattr("odoo_agent_server.services.core_finder_service.core_sync_service.ensure_module_synced", fake_sync)

    result = CoreFinderService().find_core_file(
        "point_of_sale",
        "model",
        "payment",
        search_roots=[str(remote_root)],
    )

    assert result.total_found == 1
    assert result.best_match and result.best_match.endswith("payment_method.py")


def test_composite_scope_and_frontend_preflight_never_start_remote_sync(tmp_path, monkeypatch):
    remote_root = tmp_path / "missing_core"
    custom = tmp_path / "x_module"
    custom.mkdir()
    manifest = custom / "__manifest__.py"
    manifest.write_text("{'name': 'X', 'depends': ['website']}\n", encoding="utf-8")
    monkeypatch.setattr(
        "odoo_agent_server.services.core_finder_service.core_sync_service.ensure_module_synced",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("inline remote sync is forbidden")),
    )

    scope = CoreFinderService().scope_advice(
        custom_module_name="x_module",
        custom_file_paths=[str(manifest)],
        task_description="implement website module",
        search_roots=[str(remote_root)],
        allow_remote_core_sync=False,
    )
    frontend = FrontendPatternService().preflight(FrontendPatternPreflightRequest(
        task_description="implement website SCSS and JavaScript",
        target_module_name="x_module",
        custom_file_paths=[str(manifest)],
        search_roots=[str(remote_root)],
        allow_remote_core_sync=False,
    ))

    assert scope.mandatory_count == 0
    assert frontend.is_frontend_task is True


def test_list_module_files_syncs_when_search_root_does_not_exist(tmp_path, monkeypatch):
    remote_root = tmp_path / "home" / "odoo" / "src" / "enterprise"

    def fake_sync(root, module_name):
        module_dir = remote_root / module_name / "views"
        module_dir.mkdir(parents=True)
        (module_dir / "pos_views.xml").write_text("<odoo/>\n", encoding="utf-8")
        return True

    monkeypatch.setattr("odoo_agent_server.services.core_finder_service.core_sync_service.ensure_module_synced", fake_sync)

    result = CoreFinderService().list_module_files(
        "point_of_sale",
        "views",
        search_roots=[str(remote_root)],
    )

    assert result.found is True
    assert result.total_found == 1


def test_find_core_file_stops_after_first_priority_root_match(tmp_path, monkeypatch):
    roots = [tmp_path / "community", tmp_path / "enterprise", tmp_path / "user"]
    calls = []

    def fake_sync(root, module_name, **kwargs):
        calls.append(root)
        if root == str(roots[0]):
            module_dir = roots[0] / module_name
            module_dir.mkdir(parents=True)
            (module_dir / "__manifest__.py").write_text("{}\n", encoding="utf-8")
        return True

    monkeypatch.setattr("odoo_agent_server.services.core_finder_service.core_sync_service.ensure_module_synced", fake_sync)
    result = CoreFinderService().find_core_file(
        "web",
        "manifest",
        "web",
        search_roots=[str(root) for root in roots],
    )

    assert result.best_match and result.best_match.endswith("__manifest__.py")
    assert calls == [str(roots[0])]


def test_core_sync_failure_is_bounded_and_negative_cached(tmp_path, monkeypatch):
    core_sync_service._negative_cache.clear()
    monkeypatch.setattr(settings, "core_sync_enabled", True)
    monkeypatch.setattr(core_sync_service, "_resolve_client_for_sync", lambda **kwargs: None)
    monkeypatch.setattr(core_sync_service, "_target_for_sync", lambda client: SimpleNamespace(
        user="odoo", host="example.invalid", port=22, key_path=None,
    ))
    monkeypatch.setattr(core_sync_service, "_deployed_sha", lambda client, timeout_seconds=8: None)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(core_sync_service.subprocess, "run", fake_run)
    root = str(tmp_path / "community")

    assert core_sync_service.ensure_module_synced(root, "web", timeout_seconds=45) is False
    assert core_sync_service.ensure_module_synced(root, "web", timeout_seconds=45) is False
    assert len(calls) == 1
    assert calls[0][0][calls[0][0].index("--timeout") + 1] == "8"
    assert calls[0][1]["timeout"] == 13
