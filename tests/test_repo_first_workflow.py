from pathlib import Path

from odoo_agent_server.schemas import CodingTaskPlanRequest, RepoPrepareRequest
from odoo_agent_server.routers.tools import _TOOL_DESCRIPTORS, tool_input_schema
from odoo_agent_server.services.repo_service import RepoService


def test_new_repo_first_tools_are_listed():
    names = {tool.name for tool in _TOOL_DESCRIPTORS}
    assert "repo.prepare" in names
    assert "repo.list_custom_modules" in names
    assert "workflow.coding_task_plan" in names


def test_repo_prepare_schema_contains_repo_branch():
    schema = tool_input_schema("repo.prepare")
    props = schema["properties"]
    assert "repo_url" in props
    assert "branch" in props


def test_coding_task_plan_schema_requires_task():
    model = CodingTaskPlanRequest(task="Add field test_mcp")
    assert model.task == "Add field test_mcp"


def test_list_custom_modules_discovers_manifest(tmp_path: Path):
    module = tmp_path / "my_custom_module"
    module.mkdir()
    (module / "__manifest__.py").write_text("{'name': 'My Custom Module', 'version': '18.0.1.0.0', 'installable': True, 'author': 'Tester'}", encoding="utf-8")
    modules = RepoService().list_custom_modules(str(tmp_path))
    assert [item.module_name for item in modules] == ["my_custom_module"]
    assert modules[0].name == "My Custom Module"
