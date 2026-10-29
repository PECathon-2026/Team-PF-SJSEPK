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


@pytest.mark.parametrize("path", ["../outside.txt", "sub/../../outside.txt", "sub/../sample.py", "/outside.txt"])
def test_reject_escape_paths(repo, path):
    _, tools = repo
    with pytest.raises(ValueError):
        tools.read_file(path)


def test_patch_rejects_escape(repo):
    _, tools = repo
    with pytest.raises(ValueError):
        tools.apply_patch("--- a/../outside.txt\n+++ b/../outside.txt\n@@ -1 +1 @@\n-x\n+y\n")
