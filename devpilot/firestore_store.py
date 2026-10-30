"""Cloud Firestore persistence for DevPilot mission records."""
from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any

from dotenv import load_dotenv

PROJECT_ID = "devpilot-shinobi-tech"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECTION = "devpilot_missions"
_SUBCOLLECTIONS = {
    "tasks": "tasks",
    "findings": "findings",
    "activity_log": "activity",
    "test_results": "test_results",
}
_client: Any = None
_client_lock = Lock()


def load_firestore_environment() -> None:
    if os.environ.get("DEVPILOT_DISABLE_DOTENV") == "1":
        return
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)


class FirestoreError(RuntimeError):
    """Base error for safe, user-facing Firestore failures."""


class FirestoreConfigurationError(FirestoreError):
    """Firestore is selected but its service-account configuration is invalid."""


class FirestoreOperationError(FirestoreError):
    """A Firestore operation failed."""


def _read_streamlit_firebase_settings() -> Mapping[str, Any]:
    try:
        import streamlit as st
        from streamlit.errors import StreamlitSecretNotFoundError
    except ImportError:
        return {}
    try:
        return st.secrets.get("firebase", {})
    except StreamlitSecretNotFoundError:
        return {}


def _service_account_info() -> dict[str, Any]:
    load_firestore_environment()
    serialized = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if serialized:
        try:
            data = json.loads(serialized)
        except json.JSONDecodeError:
            raise FirestoreConfigurationError(
                "FIREBASE_SERVICE_ACCOUNT_JSON must contain valid service-account JSON."
            ) from None
        if not isinstance(data, dict):
            raise FirestoreConfigurationError(
                "FIREBASE_SERVICE_ACCOUNT_JSON must contain a JSON object."
            )
        return data

    account_path = (
        os.environ.get("FIREBASE_SERVICE_ACCOUNT_FILE")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    )
    if account_path:
        try:
            data = json.loads(Path(account_path).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise FirestoreConfigurationError(
                "Could not read the configured Firebase service-account file."
            ) from None
        if not isinstance(data, dict):
            raise FirestoreConfigurationError(
                "The configured Firebase service-account file must contain a JSON object."
            )
        return data

    settings = _read_streamlit_firebase_settings()
    if not isinstance(settings, Mapping):
        raise FirestoreConfigurationError(
            "The [firebase] section in Streamlit secrets must be a table."
        )
    account = settings.get("service_account", settings)
    if isinstance(account, Mapping) and account.get("private_key") and account.get("client_email"):
        return dict(account)
    raise FirestoreConfigurationError(
        "Firestore credentials are missing. Configure firebase.service_account in "
        ".streamlit/secrets.toml, GOOGLE_APPLICATION_CREDENTIALS, "
        "FIREBASE_SERVICE_ACCOUNT_FILE, or FIREBASE_SERVICE_ACCOUNT_JSON."
    )


def configured_project_id() -> str:
    load_firestore_environment()
    settings = _read_streamlit_firebase_settings()
    selected = os.environ.get("FIREBASE_PROJECT_ID")
    if not selected and isinstance(settings, Mapping):
        selected = settings.get("project_id")
    return str(selected or PROJECT_ID)


def _project_id(account: Mapping[str, Any]) -> str:
    project_id = configured_project_id()
    account_project = account.get("project_id")
    if account_project and str(account_project) != project_id:
        raise FirestoreConfigurationError(
            "FIREBASE_PROJECT_ID must match the service account's project_id."
        )
    return project_id


def _get_client() -> Any:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        account = _service_account_info()
        project_id = _project_id(account)
        try:
            import firebase_admin
            from firebase_admin import credentials, firestore

            credential = credentials.Certificate(account)
            app = firebase_admin.initialize_app(
                credential,
                {"projectId": project_id},
                name="devpilot-firestore",
            )
            _client = firestore.client(app=app)
        except FirestoreError:
            raise
        except Exception as exc:
            raise FirestoreConfigurationError(
                f"Could not initialize Firebase Admin credentials ({type(exc).__name__}). "
                "Verify the service-account JSON and project ID."
            ) from None
    return _client


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        raise ValueError("Mission data must contain JSON-serializable values.") from None


class FirestoreMissionStore:
    """Stores a mission header and its variable-length data in subcollections."""

    def __init__(self, client: Any = None):
        self._client = client

    @property
    def client(self) -> Any:
        return self._client if self._client is not None else _get_client()

    def _mission_ref(self, mission_id: str) -> Any:
        if (
            not isinstance(mission_id, str)
            or not mission_id
            or Path(mission_id).name != mission_id
            or mission_id in {".", ".."}
        ):
            raise ValueError("invalid mission id")
        return self.client.collection(COLLECTION).document(mission_id)

    @staticmethod
    def _root_document(mission: dict[str, Any]) -> dict[str, Any]:
        activity = mission.get("activity_log") or []
        created_at = (
            activity[0].get("timestamp")
            if activity and isinstance(activity[0], dict)
            else None
        )
        updated_at = (
            activity[-1].get("timestamp")
            if activity and isinstance(activity[-1], dict)
            else None
        )
        return {
            "mission_id": mission["mission_id"],
            "user_request": mission["user_request"],
            "status": mission["status"],
            "retry_count": mission["retry_count"],
            "created_at": created_at,
            "updated_at": updated_at,
            "schema_version": 1,
            "record_counts": {
                "tasks": len(mission.get("tasks") or []),
                "findings": len(mission.get("findings") or []),
                "activity_log": len(mission.get("activity_log") or []),
                "test_results": (
                    len(mission["test_results"])
                    if isinstance(mission.get("test_results"), list)
                    else 0
                ),
            },
            "test_results_is_none": mission.get("test_results") is None,
            "test_results_inline": (
                _json_copy(mission["test_results"])
                if mission.get("test_results") is not None
                and not isinstance(mission.get("test_results"), list)
                else None
            ),
        }

    @staticmethod
    def _records(mission: dict[str, Any]) -> dict[str, list[Any]]:
        return {
            "tasks": mission.get("tasks") or [],
            "findings": mission.get("findings") or [],
            "activity_log": mission.get("activity_log") or [],
            "test_results": (
                mission.get("test_results")
                if isinstance(mission.get("test_results"), list)
                else []
            ),
        }

    def _write_records(
        self,
        mission_id: str,
        mission: dict[str, Any],
        previous: dict[str, Any] | None,
    ) -> list[tuple[Any, dict[str, Any]]]:
        records: list[tuple[Any, dict[str, Any]]] = []
        current_groups = self._records(mission)
        previous_groups = self._records(previous) if previous is not None else {}
        ref = self._mission_ref(mission_id)
        for field, collection_name in _SUBCOLLECTIONS.items():
            old_items = previous_groups.get(field, [])
            for index, item in enumerate(current_groups[field]):
                if index < len(old_items) and item == old_items[index]:
                    continue
                record_ref = ref.collection(collection_name).document(f"{index:08d}")
                records.append((record_ref, {"index": index, "value": _json_copy(item)}))

        old_approvals = [
            item for item in previous_groups.get("findings", [])
            if isinstance(item, dict) and item.get("type") in {"task_approval", "patch_proposal"}
        ]
        approvals = [
            item for item in current_groups["findings"]
            if isinstance(item, dict) and item.get("type") in {"task_approval", "patch_proposal"}
        ]
        for index, item in enumerate(approvals):
            if index < len(old_approvals) and item == old_approvals[index]:
                continue
            record_ref = ref.collection("approvals").document(f"{index:08d}")
            records.append((record_ref, {"index": index, "value": _json_copy(item)}))
        return records

    @staticmethod
    def _commit(
        client: Any,
        records: list[tuple[Any, dict[str, Any]]],
        root_ref: Any,
        root: dict[str, Any],
        *,
        create: bool,
    ) -> None:
        batch_size = 450
        remaining = list(records)
        while len(remaining) > batch_size - 1:
            batch = client.batch()
            for ref, data in remaining[:batch_size]:
                batch.set(ref, data)
            batch.commit()
            remaining = remaining[batch_size:]

        batch = client.batch()
        for ref, data in remaining:
            batch.set(ref, data)
        if create:
            batch.create(root_ref, root)
        else:
            batch.set(root_ref, root)
        batch.commit()

    def save(
        self,
        mission: dict[str, Any],
        *,
        previous: dict[str, Any] | None = None,
        create_only: bool = False,
    ) -> None:
        root_ref = self._mission_ref(mission["mission_id"])
        root = self._root_document(mission)
        records = self._write_records(mission["mission_id"], mission, previous)
        try:
            self._commit(self.client, records, root_ref, root, create=create_only)
        except FirestoreError:
            raise
        except Exception as exc:
            raise FirestoreOperationError(
                f"Cloud Firestore could not save the mission ({type(exc).__name__}). "
                "Check service-account access and Firestore availability."
            ) from None

    def get(self, mission_id: str) -> dict[str, Any] | None:
        ref = self._mission_ref(mission_id)
        try:
            snapshot = ref.get()
            if not snapshot.exists:
                return None
            root = snapshot.to_dict() or {}
            counts = root.get("record_counts") or {}
            mission: dict[str, Any] = {
                "mission_id": root.get("mission_id", mission_id),
                "user_request": root.get("user_request", ""),
                "status": root.get("status"),
                "retry_count": root.get("retry_count", 0),
            }
            for field, collection_name in _SUBCOLLECTIONS.items():
                count = max(0, int(counts.get(field, 0)))
                if count:
                    snapshots = (
                        ref.collection(collection_name)
                        .order_by("index")
                        .limit(count)
                        .stream()
                    )
                    values = sorted(
                        (snapshot.to_dict() or {} for snapshot in snapshots),
                        key=lambda item: int(item.get("index", 0)),
                    )
                    mission[field] = [item.get("value") for item in values]
                else:
                    mission[field] = []
            if root.get("test_results_is_none"):
                mission["test_results"] = None
            elif root.get("test_results_inline") is not None:
                mission["test_results"] = root["test_results_inline"]
            return mission
        except FirestoreError:
            raise
        except Exception as exc:
            raise FirestoreOperationError(
                f"Cloud Firestore could not load the mission ({type(exc).__name__}). "
                "Check service-account access and Firestore availability."
            ) from None

    def list(self, limit: int | None = None) -> list[dict[str, Any]]:
        try:
            snapshots = list(self.client.collection(COLLECTION).stream())
            snapshots.sort(
                key=lambda snapshot: str(
                    (snapshot.to_dict() or {}).get("updated_at") or ""
                ),
                reverse=True,
            )
            if limit is not None:
                snapshots = snapshots[:limit]
            missions = []
            for snapshot in snapshots:
                mission_id = str((snapshot.to_dict() or {}).get("mission_id") or snapshot.id)
                mission = self.get(mission_id)
                if mission is not None:
                    missions.append(mission)
            return missions
        except FirestoreError:
            raise
        except Exception as exc:
            raise FirestoreOperationError(
                f"Cloud Firestore could not list missions ({type(exc).__name__}). "
                "Check service-account access and Firestore availability."
            ) from None


def test_firestore_connection() -> None:
    try:
        _get_client().collection(COLLECTION).limit(1).get(timeout=10)
    except FirestoreError:
        raise
    except Exception as exc:
        raise FirestoreOperationError(
            f"Cloud Firestore connection check failed ({type(exc).__name__}). "
            "Check service-account access, the project ID, and Firestore availability."
        ) from None
