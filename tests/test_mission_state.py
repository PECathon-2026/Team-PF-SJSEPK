import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from devpilot import mission_state
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


def test_state_write_retries_transient_windows_replace_lock(tmp_path, monkeypatch):
    mission = create_mission("Request", state_dir=tmp_path)
    original_replace = os.replace
    attempts = 0

    def replace_with_transient_lock(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("file is briefly locked")
        original_replace(source, destination)

    monkeypatch.setattr(mission_state.os, "replace", replace_with_transient_lock)
    updated = update_mission(mission["mission_id"], {"status": "running"}, state_dir=tmp_path)
    assert attempts == 2
    assert load_mission(mission["mission_id"], state_dir=tmp_path) == updated
    assert list(tmp_path.glob("*.tmp")) == []


def test_persistent_state_write_lock_preserves_previous_mission(tmp_path, monkeypatch):
    mission = create_mission("Request", state_dir=tmp_path)

    def deny_replace(_source, _destination):
        raise PermissionError("access denied")

    monkeypatch.setattr(mission_state.os, "replace", deny_replace)
    with pytest.raises(PermissionError, match="check folder permissions"):
        update_mission(mission["mission_id"], {"status": "running"}, state_dir=tmp_path)
    assert load_mission(mission["mission_id"], state_dir=tmp_path) == mission
    assert list(tmp_path.glob("*.tmp")) == []


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


def test_recover_stale_missions_closes_only_inactive_running_records(tmp_path):
    stale = create_mission("stale run", state_dir=tmp_path)
    recent = create_mission("recent run", state_dir=tmp_path)
    active = create_mission("active run", state_dir=tmp_path)
    for mission in (stale, recent, active):
        update_mission(mission["mission_id"], {"status": "running"}, state_dir=tmp_path)

    stale_path = tmp_path / f"{stale['mission_id']}.json"
    stale_data = json.loads(stale_path.read_text(encoding="utf-8"))
    old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    for entry in stale_data["activity_log"]:
        entry["timestamp"] = old_time
    stale_path.write_text(json.dumps(stale_data), encoding="utf-8")

    recovered = mission_state.recover_stale_missions(
        state_dir=tmp_path,
        exclude_ids={active["mission_id"]},
    )

    assert recovered == [stale["mission_id"]]
    assert load_mission(stale["mission_id"], state_dir=tmp_path)["status"] == "cancelled"
    assert load_mission(recent["mission_id"], state_dir=tmp_path)["status"] == "running"
    assert load_mission(active["mission_id"], state_dir=tmp_path)["status"] == "running"
    assert load_mission(stale["mission_id"], state_dir=tmp_path)["activity_log"][-2]["event"] == "mission_recovered"


def test_recover_mission_when_its_worker_is_orphaned(tmp_path):
    mission = create_mission("interrupted run", state_dir=tmp_path)
    update_mission(mission["mission_id"], {"status": "running"}, state_dir=tmp_path)

    recovered = mission_state.recover_stale_missions(
        state_dir=tmp_path,
        orphaned_ids={mission["mission_id"]},
    )

    closed = load_mission(mission["mission_id"], state_dir=tmp_path)
    assert recovered == [mission["mission_id"]]
    assert closed["status"] == "cancelled"
    assert any(
        entry.get("event") == "mission_recovered"
        and "stopped unexpectedly" in entry.get("reason", "")
        for entry in closed["activity_log"]
    )
