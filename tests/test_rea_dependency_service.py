from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from odoo_agent_server.services.github_commit_service import GitHubRepo
from odoo_agent_server.services.rea_dependency_service import ReaDependencyService
from odoo_agent_server.services.rea_impact_service import ReaImpactService
from tests.support.fake_github import FakeGitHub


def _workspace(files):
    transport = FakeGitHub(files, branch="staging")
    repo = GitHubRepo("acme", "addons", "test-token", transport=transport, sleep=lambda _s: None)
    head = repo.head("staging")
    return transport, SimpleNamespace(
        repo=repo,
        tree=repo.tree(head["tree"]),
        root="addons/a_mod",
        module="a_mod",
        branch="staging",
        base_commit=head["commit"],
    )


def test_phase9_transitive_reverse_cycles_and_cross_module_model_fields():
    files = {
        "addons/a_mod/__manifest__.py": "{'depends': ['b_mod']}\n",
        "addons/a_mod/models/a.py": (
            "from odoo import fields, models\n"
            "class AExtension(models.Model):\n"
            "    _inherit = 'b.model'\n"
            "    linked_c = fields.Many2one('c.model')\n"
        ),
        "addons/b_mod/__manifest__.py": "{'depends': ['c_mod']}\n",
        "addons/b_mod/models/b.py": (
            "from odoo import models\n"
            "class BModel(models.Model):\n"
            "    _name = 'b.model'\n"
        ),
        "addons/c_mod/__manifest__.py": "{'depends': ['a_mod']}\n",
        "addons/c_mod/models/c.py": (
            "from odoo import models\n"
            "class CModel(models.Model):\n"
            "    _name = 'c.model'\n"
        ),
        "addons/d_mod/__manifest__.py": "{'depends': ['a_mod']}\n",
        "addons/d_mod/models/d.py": (
            "from odoo import models\n"
            "class DExtension(models.Model):\n"
            "    _inherit = 'a.model'\n"
        ),
        "addons/e_mod/__manifest__.py": "{'depends': ['d_mod']}\n",
        "addons/e_mod/models/e.py": "VALUE = 1\n",
    }
    _transport, workspace = _workspace(files)
    target_files = {
        "__manifest__.py": files["addons/a_mod/__manifest__.py"],
        "models/a.py": files["addons/a_mod/models/a.py"],
    }

    result = ReaDependencyService().analyze(
        workspace=workspace,
        files=target_files,
        module_name="a_mod",
    )

    assert {row["module"] for row in result["transitive_dependencies"]} == {"b_mod", "c_mod"}
    assert {row["module"] for row in result["reverse_dependents"]} == {"d_mod", "e_mod"}
    assert any(set(cycle) >= {"a_mod", "b_mod", "c_mod"} for cycle in result["dependency_cycles"])
    assert any(
        row["source_module"] == "a_mod" and row["target_module"] == "b_mod" and row["kind"] == "extends_model"
        for row in result["cross_module_relationships"]
    )
    assert any(
        row["source_module"] == "a_mod" and row["target_module"] == "c_mod"
        and row["kind"] == "relates_to_model" and row["field"] == "linked_c"
        for row in result["cross_module_relationships"]
    )
    assert result["coverage"]["complete"] is True




def test_phase9_uses_target_wip_overlay_for_manifest_and_model_files():
    files = {
        "addons/a_mod/__manifest__.py": "{'depends': ['b_mod']}\\n",
        "addons/a_mod/models/base.py": "VALUE = 1\\n",
        "addons/b_mod/__manifest__.py": "{'depends': []}\\n",
        "addons/c_mod/__manifest__.py": "{'depends': ['a_mod']}\\n",
        "addons/c_mod/models/c.py": (
            "from odoo import models\\n"
            "class CModel(models.Model):\\n"
            "    _name = 'c.model'\\n"
        ),
    }
    _transport, workspace = _workspace(files)
    workspace.overlay = {
        "addons/a_mod/__manifest__.py": "{'depends': ['c_mod']}\\n",
        "addons/a_mod/models/base.py": None,
        "addons/a_mod/models/new.py": (
            "from odoo import models\\n"
            "class OverlayModel(models.Model):\\n"
            "    _name = 'overlay.model'\\n"
        ),
    }
    result = ReaDependencyService().analyze(
        workspace=workspace,
        files={
            "__manifest__.py": files["addons/a_mod/__manifest__.py"],
            "models/base.py": files["addons/a_mod/models/base.py"],
        },
        module_name="a_mod",
    )

    assert {row["module"] for row in result["transitive_dependencies"]} == {"c_mod"}
    assert any(row["model"] == "overlay.model" and row["module"] == "a_mod" for row in result["model_ownership"])
    assert not any(row["path"].endswith("models/base.py") for row in result["model_ownership"])

def test_phase9_impact_prediction_includes_reverse_dependents_and_cycles():
    with TemporaryDirectory(prefix="rea-impact-") as temp:
        root = Path(temp)
        (root / "models").mkdir()
        (root / "__manifest__.py").write_text("{'depends': []}\n", encoding="utf-8")
        (root / "models" / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
        dependency_map = {
            "reverse_dependents": [
                {"module": "d_mod", "depth": 1},
                {"module": "e_mod", "depth": 2},
            ],
            "cross_module_relationships": [
                {"source_module": "a_mod", "target_module": "b_mod", "kind": "extends_model"}
            ],
            "dependency_cycles": [["a_mod", "b_mod", "a_mod"]],
            "cycle_modules": ["a_mod", "b_mod"],
            "coverage": {"complete": True},
        }
        result = ReaImpactService().predict(
            module_path=root,
            module_name="a_mod",
            changed_files=[{"path": "models/a.py", "diff": "+VALUE = 2"}],
            dependency_map=dependency_map,
        )

    assert result["risk"] == "high"
    assert {row["module"] for row in result["affected_modules"]} == {"a_mod", "d_mod", "e_mod"}
    assert result["summary"]["reverse_dependent_addons"] == 2
    assert result["summary"]["dependency_cycles"] == 1
    assert result["dependency_cycles"] == dependency_map["dependency_cycles"]

def test_phase9_reports_external_dependencies_and_respects_module_bound():
    files = {
        "addons/a_mod/__manifest__.py": "{'depends': ['b_mod', 'base', 'enterprise_secret']}\n",
        "addons/a_mod/models/a.py": "VALUE = 1\n",
        "addons/b_mod/__manifest__.py": "{'depends': []}\n",
        "addons/c_mod/__manifest__.py": "{'depends': ['a_mod']}\n",
    }
    _transport, workspace = _workspace(files)
    result = ReaDependencyService().analyze(
        workspace=workspace,
        files={"__manifest__.py": files["addons/a_mod/__manifest__.py"]},
        module_name="a_mod",
        max_modules=5,
    )

    assert "base" in result["unresolved_dependencies"]
    assert "enterprise_secret" in result["unresolved_dependencies"]
    assert {row["module"] for row in result["transitive_dependencies"]} == {"b_mod"}
    assert {row["module"] for row in result["reverse_dependents"]} == {"c_mod"}
