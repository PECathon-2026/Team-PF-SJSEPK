from pathlib import Path

import pytest

from devpilot.repository_tools import RepositoryTools


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "demo_repo"
    root.mkdir()
    (root / "sample.py").write_text("answer = 41\n", encoding="utf-8")
    return root, RepositoryTools(root)


def test_read_and_search(repo):
    root, tools = repo
    assert tools.read_file("sample.py") == "answer = 41\n"
    assert tools.search("answer") == [("sample.py", 1, "answer = 41")]


def test_repository_scope_can_be_an_explicitly_selected_existing_project_root(tmp_path):
    root = tmp_path / "my-project"
    root.mkdir()
    (root / "main.py").write_text("value = 1\n", encoding="utf-8")
    tools = RepositoryTools(root)
    assert tools.read_file("main.py") == "value = 1\n"


def test_search_rejects_empty_query(repo):
    _, tools = repo
    with pytest.raises(ValueError, match="cannot be empty"):
        tools.search("")


def test_search_ignores_repository_metadata_and_test_cache(repo):
    root, tools = repo
    cache = root / ".pytest_cache"
    cache.mkdir()
    (cache / "nodeids").write_text("answer = cached\n", encoding="utf-8")
    metadata = root / ".git"
    metadata.mkdir()
    (metadata / "config").write_text("answer = metadata\n", encoding="utf-8")
    assert tools.search("answer") == [("sample.py", 1, "answer = 41")]


def test_apply_unified_diff(repo):
    root, tools = repo
    tools.apply_patch("--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-answer = 41\n+answer = 42\n")
    assert (root / "sample.py").read_text(encoding="utf-8") == "answer = 42\n"


def test_apply_unified_diff_can_create_a_file_inside_the_repository(repo):
    root, tools = repo
    tools.apply_patch("--- /dev/null\n+++ b/new_module.py\n@@ -0,0 +1 @@\n+value = 42\n")
    assert (root / "new_module.py").read_text(encoding="utf-8") == "value = 42\n"


def test_new_file_patch_rejects_escape_paths(repo):
    _, tools = repo
    with pytest.raises(ValueError, match="path traversal"):
        tools.validate_patch("--- /dev/null\n+++ b/../outside.py\n@@ -0,0 +1 @@\n+value = 42\n")


def test_preview_new_file_patch_does_not_write(repo):
    root, tools = repo
    patch = "--- /dev/null\n+++ b/new_module.py\n@@ -0,0 +1 @@\n+value = 42\n"
    preview = tools.preview_patch(patch)
    assert preview["before"] == ""
    assert preview["after"] == "value = 42\n"
    assert not (root / "new_module.py").exists()


def test_patch_preview_is_read_only_and_shows_exact_change(repo):
    root, tools = repo
    patch = "--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-answer = 41\n+answer = 42\n"
    preview = tools.preview_patch(patch)
    assert preview == {
        "path": "sample.py",
        "before": "answer = 41\n",
        "after": "answer = 42\n",
        "diff": patch,
    }
    assert (root / "sample.py").read_text(encoding="utf-8") == "answer = 41\n"


def test_test_runner_disables_repository_hooks_and_external_plugins(repo, monkeypatch):
    _, tools = repo
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return "completed"

    monkeypatch.setattr("devpilot.repository_tools.subprocess.run", fake_run)
    assert tools.run_tests() == "completed"
    assert observed["command"][1:4] == ["-m", "pytest", "-q"]
    assert "--noconftest" in observed["command"]
    assert "-c" in observed["command"]
    assert Path(observed["command"][-1]).name == tools.repo_root.name
    assert observed["command"][-1] != str(tools.repo_root)
    assert observed["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert observed["shell"] is False
    assert observed["timeout"] == 120
    assert observed["cwd"] != tools.repo_root


def test_test_runner_executes_in_a_temporary_repository_copy(repo):
    root, tools = repo
    (root / "test_mutation.py").write_text(
        "from pathlib import Path\n"
        "def test_local_mutation():\n"
        "    Path('sample.py').write_text('changed = True\\n')\n",
        encoding="utf-8",
    )
    result = tools.run_tests()
    assert result.returncode == 0
    assert (root / "sample.py").read_text(encoding="utf-8") == "answer = 41\n"


@pytest.mark.parametrize("path", ["../outside.txt", "sub/../../outside.txt", "sub/../sample.py", "/outside.txt"])
def test_reject_escape_paths(repo, path):
    _, tools = repo
    with pytest.raises(ValueError):
        tools.read_file(path)


def test_patch_rejects_escape(repo):
    _, tools = repo
    with pytest.raises(ValueError):
        tools.apply_patch("--- a/../outside.txt\n+++ b/../outside.txt\n@@ -1 +1 @@\n-x\n+y\n")
