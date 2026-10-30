"""Idempotently copy existing local mission JSON records to Cloud Firestore."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .firestore_store import FirestoreError, FirestoreMissionStore
from .mission_state import DEFAULT_STATE_DIR, load_mission


def migrate_local_missions(
    state_dir: str | Path = DEFAULT_STATE_DIR,
    *,
    store: FirestoreMissionStore | None = None,
) -> tuple[int, int]:
    root = Path(state_dir).resolve()
    firestore = store or FirestoreMissionStore()
    migrated = skipped = 0
    for path in sorted(root.glob("*.json")):
        mission = load_mission(path.stem, state_dir=root)
        if mission is None:
            continue
        if firestore.get(mission["mission_id"]) is not None:
            skipped += 1
            continue
        firestore.save(mission, create_only=True)
        migrated += 1
    return migrated, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy existing DevPilot JSON mission records into Cloud Firestore without deleting local files."
    )
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    args = parser.parse_args(argv)
    try:
        migrated, skipped = migrate_local_missions(args.state_dir)
    except (FirestoreError, OSError, ValueError) as exc:
        print(f"Firestore migration failed: {exc}", file=sys.stderr)
        return 1
    print(f"Migration complete: {migrated} copied, {skipped} already present; local JSON files were retained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
