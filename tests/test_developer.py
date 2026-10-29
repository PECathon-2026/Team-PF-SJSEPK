import pytest

from devpilot.developer import DeveloperAgent
from devpilot.mission_state import create_mission, load_mission
from devpilot.repository_tools import RepositoryTools

PATCH = "--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-answer = 41\n+answer = 42\n"


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "demo_repo"
    root.mkdir()
    file = root / "sample.py"
    file.write_text("answer = 41\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    mission = create_mission("Change answer", [{"task_id": "task-1", "relevant_files": ["sample.py"]}], state_dir=state_dir)
    agent = DeveloperAgent(RepositoryTools(root), state_dir=state_dir)
    return root, file, state_dir, mission, agent


def test_requires_task_and_patch_approval(setup):
    root, file, state_dir, mission, agent = setup
    with pytest.raises(PermissionError):
        agent.accept_task(mission["mission_id"], "task-1")
    with pytest.raises(PermissionError):
        agent.apply_proposal(mission["mission_id"], "unknown")
    assert file.read_text(encoding="utf-8") == "answer = 41\n"
    inspected = agent.accept_task(mission["mission_id"], "task-1", approved=True)
    assert inspected["files"]["sample.py"] == "answer = 41\n"
    proposal_id = agent.propose_patch(mission["mission_id"], "task-1", PATCH)
    with pytest.raises(PermissionError):
        agent.apply_proposal(mission["mission_id"], proposal_id)
    assert file.read_text(encoding="utf-8") == "answer = 41\n"
    assert load_mission(mission["mission_id"], state_dir=state_dir)["findings"][-1]["status"] == "proposed"


def test_approved_patch_application_records_outcome(setup):
    root, file, state_dir, mission, agent = setup
    agent.accept_task(mission["mission_id"], "task-1", approved=True)
    proposal_id = agent.propose_patch(mission["mission_id"], "task-1", PATCH)
    agent.apply_proposal(mission["mission_id"], proposal_id, approved=True)
    assert file.read_text(encoding="utf-8") == "answer = 42\n"
    assert load_mission(mission["mission_id"], state_dir=state_dir)["findings"][-1]["outcome"] == "patch applied"


def test_rejects_invalid_patch_and_unsafe_paths(setup):
    root, file, state_dir, mission, agent = setup
    agent.accept_task(mission["mission_id"], "task-1", approved=True)
    with pytest.raises(ValueError, match="context"):
        agent.propose_patch(mission["mission_id"], "task-1", PATCH.replace("answer = 41", "answer = 0"))
    with pytest.raises(ValueError):
        agent.propose_patch(mission["mission_id"], "task-1", PATCH.replace("sample.py", "../outside.py"))
    assert file.read_text(encoding="utf-8") == "answer = 41\n"
    with pytest.raises(ValueError):
        RepositoryTools(root).read_file("../outside.py")
    assert load_mission(mission["mission_id"], state_dir=state_dir)["findings"][-1]["status"] == "rejected"
