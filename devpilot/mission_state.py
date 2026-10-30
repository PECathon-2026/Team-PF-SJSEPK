"""Persistence for shared DevPilot mission state."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import copy
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any
from uuid import uuid4

from .firestore_store import (
    FirestoreConfigurationError,
    FirestoreMissionStore,
    load_firestore_environment,
)

STATUSES = {"pending", "running", "completed", "failed", "cancelled"}
FIELDS = {"user_request", "tasks", "status", "findings", "test_results", "retry_count", "activity_log"}
DEFAULT_STATE_DIR = Path(
    os.environ.get("DEVPILOT_STATE_DIR", Path(__file__).resolve().parent / "state")
).resolve()


def is_firestore_backend(state_dir: str | Path | None = None) -> bool:
    if state_dir is not None:
        return False
    load_firestore_environment()
    backend = os.environ.get("DEVPILOT_STORAGE_BACKEND", "firestore").strip().casefold()
    if backend not in {"firestore", "json"}:
        raise FirestoreConfigurationError(
            "DEVPILOT_STORAGE_BACKEND must be 'firestore' or 'json'."
        )
    return backend == "firestore"


def _mission_path(mission_id: str, state_dir: str | Path | None) -> Path:
    _validate_mission_id(mission_id)
    return Path(state_dir or DEFAULT_STATE_DIR).resolve() / f"{mission_id}.json"


def _validate_mission_id(mission_id: str) -> None:
    if not mission_id or Path(mission_id).name != mission_id or mission_id in {".", ".."}:
        raise ValueError("invalid mission id")


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
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            file.write(json.dumps(mission, indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())

        for attempt, delay in enumerate((0, 0.05, 0.1, 0.2, 0.4), start=1):
            if delay:
                time.sleep(delay)
            try:
                os.replace(temporary, path)
                temporary = None
                break
            except PermissionError as exc:
                if attempt == 5:
                    raise PermissionError(
                        f"Unable to save mission state to {path}. The file or its "
                        "directory is locked or write-protected; close other "
                        "processes using this mission and check folder permissions."
                    ) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


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
    if is_firestore_backend(state_dir):
        FirestoreMissionStore().save(mission, create_only=True)
    else:
        _write(_mission_path(mission_id, state_dir), mission)
    return mission


def load_mission(mission_id: str, *, state_dir: str | Path | None = None) -> dict[str, Any] | None:
    _validate_mission_id(mission_id)
    if is_firestore_backend(state_dir):
        mission = FirestoreMissionStore().get(mission_id)
        if mission is None:
            return None
        _validate(mission)
        return mission

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
    previous = load_mission(mission_id, state_dir=state_dir)
    if previous is None:
        raise FileNotFoundError(f"mission not found: {mission_id}")
    unknown = set(updates) - FIELDS
    if unknown:
        raise ValueError(f"unsupported mission fields: {', '.join(sorted(unknown))}")
    mission = copy.deepcopy(previous)
    mission.update(updates)
    _validate(mission)
    mission["activity_log"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "mission_updated",
        "fields": sorted(updates),
    })
    if is_firestore_backend(state_dir):
        FirestoreMissionStore().save(mission, previous=previous)
    else:
        _write(_mission_path(mission_id, state_dir), mission)
    return mission


def list_missions(
    *,
    state_dir: str | Path | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be a non-negative integer or None")
    if is_firestore_backend(state_dir):
        return FirestoreMissionStore().list(limit=limit)

    root = Path(state_dir or DEFAULT_STATE_DIR).resolve()
    missions = []
    if root.exists():
        for path in root.glob("*.json"):
            mission = load_mission(path.stem, state_dir=root)
            if mission is not None:
                missions.append(mission)
    missions.sort(
        key=lambda item: (
            (item.get("activity_log") or [{}])[-1].get("timestamp", ""),
            item.get("mission_id", ""),
        ),
        reverse=True,
    )
    return missions[:limit] if limit is not None else missions


def recover_stale_missions(
    *,
    state_dir: str | Path | None = None,
    stale_after: timedelta = timedelta(minutes=30),
    exclude_ids: set[str] | None = None,
    orphaned_ids: set[str] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Close persisted runs that stopped making progress before the cutoff."""
    cutoff = (now or datetime.now(timezone.utc)) - stale_after
    excluded = exclude_ids or set()
    orphaned = orphaned_ids or set()
    recovered: list[str] = []
    if is_firestore_backend(state_dir):
        missions = list_missions()
    else:
        root = Path(state_dir or DEFAULT_STATE_DIR).resolve()
        if not root.exists():
            return []
        missions = []
        for path in root.glob("*.json"):
            try:
                mission = load_mission(path.stem, state_dir=root)
            except (OSError, ValueError):
                continue
            if mission is not None:
                missions.append(mission)

    for mission in missions:
        mission_id = mission["mission_id"]
        if mission_id in excluded:
            continue
        if mission["status"] != "running":
            continue
        timestamps = []
        for entry in mission.get("activity_log", []):
            stamp = entry.get("timestamp") if isinstance(entry, dict) else None
            if not isinstance(stamp, str):
                continue
            try:
                parsed = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamps.append(parsed.astimezone(timezone.utc))
        if mission_id not in orphaned and timestamps and max(timestamps) >= cutoff:
            continue
        activity_log = [
            *mission["activity_log"],
            {
                "timestamp": (now or datetime.now(timezone.utc)).isoformat(),
                "event": "mission_recovered",
                "reason": (
                    "The mission worker stopped unexpectedly."
                    if mission_id in orphaned
                    else "No progress was recorded for more than 30 minutes."
                ),
            },
        ]
        update_mission(
            mission_id,
            {"status": "cancelled", "activity_log": activity_log},
            state_dir=state_dir,
        )
        recovered.append(mission_id)
    return recovered
