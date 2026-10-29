import json

import pytest

from devpilot.mission_state import STATUSES, create_mission, load_mission, update_mission


def test_create_and_load_persist_all_state(tmp_path):
    mission = create_mission("Fix bug", ["inspect"], state_dir=tmp_path)
    assert load_mission(mission["mission_id"], state_dir=tmp_path) == mission
    saved = json.loads((tmp_path / f"{mission['mission_id']}.json").read_text())
    assert saved["user_request"] == "Fix bug"
    assert saved["tasks"] == ["inspect"]
    assert saved["status"] == "pending"
    assert {"findings", "test_results", "retry_count", "activity_log"} <= saved.keys()


@pytest.mark.parametrize("status", sorted(STATUSES))
def test_each_status_is_accepted(tmp_path, status):
    mission = create_mission("Request", state_dir=tmp_path)
    assert update_mission(mission["mission_id"], {"status": status}, state_dir=tmp_path)["status"] == status


def test_update_and_log_activity(tmp_path):
    mission = create_mission("Add feature", state_dir=tmp_path)
    updated = update_mission(mission["mission_id"], {
        "status": "running", "findings": ["found issue"], "test_results": {"passed": 2}, "retry_count": 1,
    }, state_dir=tmp_path)
    assert updated["findings"] == ["found issue"]
    assert updated["test_results"] == {"passed": 2}
    assert updated["retry_count"] == 1
    assert updated["activity_log"][-1]["fields"] == ["findings", "retry_count", "status", "test_results"]


def test_missing_and_corrupt_state_are_safe(tmp_path):
    assert load_mission("missing", state_dir=tmp_path) is None
    with pytest.raises(FileNotFoundError):
        update_mission("missing", {"status": "running"}, state_dir=tmp_path)
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupted"):
        load_mission("broken", state_dir=tmp_path)
    assert (tmp_path / "broken.json").read_text(encoding="utf-8") == "{"


def test_reject_invalid_state_and_mission_id(tmp_path):
    mission = create_mission("Request", state_dir=tmp_path)
    original = load_mission(mission["mission_id"], state_dir=tmp_path)
    with pytest.raises(ValueError, match="status"):
        update_mission(mission["mission_id"], {"status": "unknown"}, state_dir=tmp_path)
    with pytest.raises(ValueError, match="retry_count"):
        update_mission(mission["mission_id"], {"retry_count": True}, state_dir=tmp_path)
    with pytest.raises(ValueError, match="mapping"):
        update_mission(mission["mission_id"], None, state_dir=tmp_path)
    assert load_mission(mission["mission_id"], state_dir=tmp_path) == original
    with pytest.raises(ValueError, match="mission id"):
        load_mission("../outside", state_dir=tmp_path)
