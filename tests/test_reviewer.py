import pytest

from devpilot.developer import DeveloperAgent
from devpilot.mission_state import create_mission, load_mission, update_mission
from devpilot.repository_tools import RepositoryTools
from devpilot.reviewer import ReviewerAgent


@pytest.fixture
def review_setup(tmp_path):
    root = tmp_path / "demo_repo"
    root.mkdir()
    sample = root / "sample.py"
    sample.write_text("answer = 41\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    mission = create_mission("Set answer", [{
        "task_id": "task-1",
        "description": "Set answer to 42",
        "acceptance_criteria": ["answer is 42"],
        "relevant_files": ["sample.py"],
    }], state_dir=state_dir)
    repository = RepositoryTools(root)
    developer = DeveloperAgent(repository, state_dir=state_dir)
    developer.accept_task(mission["mission_id"], "task-1", approved=True)
    return root, sample, state_dir, mission, repository, developer


def test_successful_review_is_saved_and_read_only(review_setup, monkeypatch):
    root, sample, state_dir, mission, repository, developer = review_setup
    patch = "--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-answer = 41\n+answer = 42\n"
    proposal_id = developer.propose_patch(mission["mission_id"], "task-1", patch)
    developer.apply_proposal(mission["mission_id"], proposal_id, approved=True)
    reviewer = ReviewerAgent(repository, state_dir=state_dir)
    monkeypatch.setattr(repository, "apply_patch", lambda *_: pytest.fail("reviewer attempted to edit files"))
    monkeypatch.setattr(repository, "run_tests", lambda *_: pytest.fail("reviewer attempted to run a command"))
    result = reviewer.review_task(mission["mission_id"], "task-1")
    assert result["status"] == "pass"
    assert result["criteria"][0]["status"] == "addressed"
    assert sample.read_text(encoding="utf-8") == "answer = 42\n"
    assert load_mission(mission["mission_id"], state_dir=state_dir)["findings"][-1] == result


def test_review_detects_security_risk(review_setup):
    root, _, state_dir, mission, repository, developer = review_setup
    patch = "--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-answer = 41\n+answer = eval(input())\n"
    proposal_id = developer.propose_patch(mission["mission_id"], "task-1", patch)
    developer.apply_proposal(mission["mission_id"], proposal_id, approved=True)
    result = ReviewerAgent(repository, state_dir=state_dir).review_task(mission["mission_id"], "task-1")
    assert result["status"] == "fail"
    assert any(finding["code"] == "dynamic_eval" for finding in result["findings"])


def test_reviewer_accepts_utf8_bom_in_python_files(review_setup):
    root, _, state_dir, mission, repository, developer = review_setup
    bom_file = root / "bom_module.py"
    bom_file.write_bytes(b"\xef\xbb\xbfvalue = 42\n")
    task = load_mission(mission["mission_id"], state_dir=state_dir)["tasks"][0]
    task["relevant_files"].append("bom_module.py")
    update_mission(mission["mission_id"], {"tasks": [task]}, state_dir=state_dir)
    patch = "--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-answer = 41\n+answer = 42\n"
    proposal_id = developer.propose_patch(mission["mission_id"], "task-1", patch)
    developer.apply_proposal(mission["mission_id"], proposal_id, approved=True)
    review = ReviewerAgent(repository, state_dir=state_dir).review_task(mission["mission_id"], "task-1")
    assert review["status"] == "pass"


def test_invalid_task_is_rejected(review_setup):
    _, _, state_dir, mission, _, _ = review_setup
    update_mission(mission["mission_id"], {"tasks": [{"task_id": "task-1", "acceptance_criteria": [], "relevant_files": []}]}, state_dir=state_dir)
    with pytest.raises(ValueError, match="acceptance criteria"):
        ReviewerAgent(state_dir=state_dir).review_task(mission["mission_id"], "task-1")


def test_unsafe_task_path_is_reported_without_leaving_demo_repo(review_setup, tmp_path):
    _, _, state_dir, mission, repository, developer = review_setup
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    patch = "--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-answer = 41\n+answer = 42\n"
    proposal_id = developer.propose_patch(mission["mission_id"], "task-1", patch)
    developer.apply_proposal(mission["mission_id"], proposal_id, approved=True)
    task = load_mission(mission["mission_id"], state_dir=state_dir)["tasks"][0]
    task["relevant_files"] = ["../outside.py"]
    update_mission(mission["mission_id"], {"tasks": [task]}, state_dir=state_dir)
    result = ReviewerAgent(repository, state_dir=state_dir).review_task(mission["mission_id"], "task-1")
    assert result["status"] == "fail"
    assert any(finding["code"] == "unsafe_path" for finding in result["findings"])
    assert outside.read_text(encoding="utf-8") == "secret = True\n"
