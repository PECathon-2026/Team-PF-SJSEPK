"""JSON persistence for shared DevPilot mission state."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

STATUSES = {"pending", "running", "completed", "failed", "cancelled"}
FIELDS = {"user_request", "tasks", "status", "findings", "test_results", "retry_count", "activity_log"}
DEFAULT_STATE_DIR = Path(__file__).resolve().parent / "state"


def _mission_path(mission_id: str, state_dir: str | Path | None) -> Path:
    if not mission_id or Path(mission_id).name != mission_id or mission_id in {".", ".."}:
        raise ValueError("invalid mission id")
    return Path(state_dir or DEFAULT_STATE_DIR).resolve() / f"{mission_id}.json"


def _validate(mission: dict[str, Any]) -> None:
    if mission.get("status") not in STATUSES:
        raise ValueError("invalid mission status")
    if isinstance(mission.get("retry_count"), bool) or not isinstance(mission.get("retry_count"), int) or mission["retry_count"] < 0:
        raise ValueError("retry_count must be a non-negative integer")
    if not isinstance(mission.get("tasks"), list):
        raise ValueError("tasks must be a list")
    if not isinstance(mission.get("findings"), list) or not isinstance(mission.get("activity_log"), list):
        raise ValueError("findings and activity_log must be lists")


def _write(path: Path, mission: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(mission, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def create_mission(
    user_request: str,
    tasks: list[Any] | None = None,
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    mission_id = uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    mission = {
        "mission_id": mission_id,
        "user_request": user_request,
        "tasks": tasks or [],
        "status": "pending",
        "findings": [],
        "test_results": None,
        "retry_count": 0,
        "activity_log": [{"timestamp": now, "event": "mission_created"}],
    }
    _validate(mission)
    _write(_mission_path(mission_id, state_dir), mission)
    return mission


def load_mission(mission_id: str, *, state_dir: str | Path | None = None) -> dict[str, Any] | None:
    path = _mission_path(mission_id, state_dir)
    try:
        mission = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"corrupted mission state: {mission_id}") from exc
    if not isinstance(mission, dict) or mission.get("mission_id") != mission_id:
        raise ValueError(f"invalid mission state: {mission_id}")
    _validate(mission)
    return mission


def update_mission(
    mission_id: str,
    updates: dict[str, Any],
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    if not isinstance(updates, dict):
        raise ValueError("updates must be a mapping")
    mission = load_mission(mission_id, state_dir=state_dir)
    if mission is None:
        raise FileNotFoundError(f"mission not found: {mission_id}")
    unknown = set(updates) - FIELDS
    if unknown:
        raise ValueError(f"unsupported mission fields: {', '.join(sorted(unknown))}")
    mission.update(updates)
    _validate(mission)
    mission["activity_log"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "mission_updated",
        "fields": sorted(updates),
    })
    _write(_mission_path(mission_id, state_dir), mission)
    return mission


