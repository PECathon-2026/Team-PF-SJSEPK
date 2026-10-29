import pytest

from devpilot.mission_state import create_mission, load_mission
from devpilot.planner import PlannerAgent
from devpilot.repository_tools import RepositoryTools


def test_planner_rejects_empty_requests(tmp_path):
    root = tmp_path / "demo_repo"
    root.mkdir()
    planner = PlannerAgent(RepositoryTools(root))
    for request in ("", "  ", "\n"):
        with pytest.raises(ValueError, match="empty"):
            planner.create_plan(request)


def test_structured_plan_has_criteria_and_relevant_files(tmp_path):
    root = tmp_path / "demo_repo"
    root.mkdir()
    (root / "auth.py").write_text("def login(): pass\n", encoding="utf-8")
    planner = PlannerAgent(RepositoryTools(root))
    plan = planner.create_plan("Improve login handling")
    assert plan["tasks"][0]["task_id"] == "task-1"
    assert plan["tasks"][0]["acceptance_criteria"]
    assert "auth.py" in plan["relevant_files"]
    assert plan["tasks"][0]["relevant_files"] == ["auth.py"]


def test_plan_saves_tasks_to_shared_mission(tmp_path):
    repo = tmp_path / "demo_repo"
    repo.mkdir()
    state_dir = tmp_path / "state"
    mission = create_mission("Update login", state_dir=state_dir)
    plan = PlannerAgent(RepositoryTools(repo), state_dir=state_dir).plan_mission(mission["mission_id"])
    saved = load_mission(mission["mission_id"], state_dir=state_dir)
    assert saved["tasks"] == plan["tasks"]
    stored_plan = next(item for item in saved["findings"] if item["type"] == "plan")
    assert stored_plan["acceptance_criteria"] == plan["acceptance_criteria"]
    assert stored_plan["relevant_files"] == plan["relevant_files"]
