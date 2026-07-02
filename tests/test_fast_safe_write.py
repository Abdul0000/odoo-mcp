from types import SimpleNamespace

from odoo_agent_server.services.repo_service import RepoService


def item(path, content, create_dirs=True):
    return SimpleNamespace(relative_path=path, content=content, create_dirs=create_dirs)


def test_write_many_fast_safe_writes_small_files(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "a.txt").write_text("old", encoding="utf-8")

    result = RepoService().write_many_fast_safe(
        str(project),
        [item("a.txt", "new"), item("dir/b.txt", "created")],
    )

    assert result.exit_code == 0
    assert "changed=" in result.stdout
    assert (project / "a.txt").read_text(encoding="utf-8") == "new"
    assert (project / "dir/b.txt").read_text(encoding="utf-8") == "created"


def test_write_many_fast_safe_rolls_back_on_failure(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "a.txt").write_text("old", encoding="utf-8")
    (project / "blocked").mkdir()

    result = RepoService().write_many_fast_safe(
        str(project),
        [item("a.txt", "new"), item("blocked", "cannot overwrite dir")],
    )

    assert result.exit_code == 1
    assert (project / "a.txt").read_text(encoding="utf-8") == "old"


def test_write_many_smart_uses_fast_safe_for_small_files(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()

    result = RepoService().write_many_smart(
        str(project),
        [item("x.py", "print('ok')\n")],
    )

    assert result.exit_code == 0
    assert result.stdout.startswith("SMART_STRATEGY=fast_safe")
    assert (project / "x.py").read_text(encoding="utf-8") == "print('ok')\n"


def test_write_many_fast_safe_check_only_does_not_write(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()

    result = RepoService().write_many_fast_safe(
        str(project),
        [item("x.txt", "content")],
        check_only=True,
    )

    assert result.exit_code == 0
    assert not (project / "x.txt").exists()
