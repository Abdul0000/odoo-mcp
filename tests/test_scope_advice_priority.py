from pathlib import Path

from odoo_agent_server.services.core_finder_service import CoreFinderService


def test_scope_advice_reads_manifest_dependencies_core_before_enterprise(tmp_path):
    project = tmp_path / "repo"
    module = project / "x_module"
    module.mkdir(parents=True)
    manifest = module / "__manifest__.py"
    manifest.write_text("{'name': 'X', 'depends': ['sale']}\n", encoding="utf-8")

    core = tmp_path / "src" / "odoo"
    enterprise = tmp_path / "src" / "enterprise"
    user = tmp_path / "src" / "user"
    (core / "addons" / "sale").mkdir(parents=True)
    (enterprise / "sale").mkdir(parents=True)
    (core / "addons" / "sale" / "__manifest__.py").write_text("{'name': 'Sale Core'}\n", encoding="utf-8")
    (enterprise / "sale" / "__manifest__.py").write_text("{'name': 'Sale Enterprise'}\n", encoding="utf-8")

    result = CoreFinderService().scope_advice(
        "x_module",
        [str(manifest)],
        "small UI change",
        search_roots=[str(core), str(enterprise), str(user)],
    )

    mandatory = [item.path for item in result.instructions if item.mandatory]
    assert mandatory
    assert mandatory[0] == str(core / "addons" / "sale" / "__manifest__.py")
    assert all("enterprise/sale/__manifest__.py" not in path for path in mandatory)
