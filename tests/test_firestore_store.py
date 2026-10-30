import json

import pytest

from devpilot import firestore_store, mission_state
from devpilot.firestore_store import (
    FirestoreConfigurationError,
    FirestoreMissionStore,
    FirestoreOperationError,
)


class Snapshot:
    def __init__(self, path, data):
        self.path = path
        self.id = path[-1]
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class Batch:
    def __init__(self, client):
        self.client = client
        self.writes = []

    def set(self, reference, data):
        self.writes.append(("set", reference.path, data))

    def create(self, reference, data):
        self.writes.append(("create", reference.path, data))

    def commit(self):
        for operation, path, _data in self.writes:
            if operation == "create" and path in self.client.documents:
                raise RuntimeError("already exists")
        for operation, path, data in self.writes:
            self.client.documents[path] = data


class DocumentReference:
    def __init__(self, client, path):
        self.client = client
        self.path = path
        self.id = path[-1]

    def collection(self, name):
        return CollectionReference(self.client, self.path + (name,))

    def get(self):
        return Snapshot(self.path, self.client.documents.get(self.path))


class CollectionReference:
    def __init__(self, client, path):
        self.client = client
        self.path = path

    def document(self, document_id):
        return DocumentReference(self.client, self.path + (document_id,))

    def stream(self):
        return self._matching_snapshots()

    def _matching_snapshots(self):
        return [
            Snapshot(path, data)
            for path, data in self.client.documents.items()
            if len(path) == len(self.path) + 1 and path[:-1] == self.path
        ]

    def order_by(self, _field):
        return Query(self, None)

    def limit(self, count):
        return Query(self, count)

    def get(self, timeout=None):
        assert timeout == 10
        return list(self.stream())


class Query:
    def __init__(self, collection, count):
        self.collection = collection
        self.count = count

    def order_by(self, _field):
        return self

    def limit(self, count):
        return Query(self.collection, count)

    def stream(self):
        values = self.collection._matching_snapshots()
        if self.count is not None:
            values = values[:self.count]
        return values

    def get(self, timeout=None):
        assert timeout == 10
        return list(self.stream())


class FakeFirestore:
    def __init__(self):
        self.documents = {}

    def collection(self, name):
        return CollectionReference(self, (name,))

    def batch(self):
        return Batch(self)


def sample_mission():
    return {
        "mission_id": "mission-123",
        "user_request": "Add a feature",
        "status": "running",
        "tasks": [{"task_id": "task-1", "description": "Implement the feature"}],
        "findings": [
            {"type": "task_approval", "task_id": "task-1", "approved": True},
            {"type": "patch_proposal", "task_id": "task-1", "status": "proposed", "patch": "+value = 1"},
            {"type": "review", "task_id": "task-1", "status": "pass", "findings": []},
        ],
        "test_results": [{"task_id": "task-1", "passed": True, "stdout": "1 passed"}],
        "retry_count": 1,
        "activity_log": [
            {"timestamp": "2026-10-30T01:00:00+00:00", "event": "mission_created"},
            {"timestamp": "2026-10-30T01:01:00+00:00", "event": "plan_created"},
        ],
    }


def test_firestore_persists_mission_collections_and_reconstructs_existing_shape():
    client = FakeFirestore()
    store = FirestoreMissionStore(client)
    mission = sample_mission()

    store.save(mission, create_only=True)

    assert store.get(mission["mission_id"]) == mission
    assert len(client.documents[("devpilot_missions", "mission-123", "tasks", "00000000")]["value"]) == 2
    approvals = [
        path for path in client.documents
        if len(path) == 4 and path[2] == "approvals"
    ]
    assert len(approvals) == 2
    assert store.list(limit=1) == [mission]


def test_firestore_preserves_non_list_test_result_values_supported_by_local_state():
    client = FakeFirestore()
    store = FirestoreMissionStore(client)
    mission = {**sample_mission(), "test_results": {"passed": 2}}

    store.save(mission, create_only=True)

    assert store.get(mission["mission_id"]) == mission
    root = client.documents[("devpilot_missions", mission["mission_id"])]
    assert root["record_counts"]["test_results"] == 0


def test_firestore_updates_write_changed_task_approval_and_test_result():
    client = FakeFirestore()
    store = FirestoreMissionStore(client)
    previous = sample_mission()
    store.save(previous, create_only=True)
    updated = {
        **previous,
        "status": "completed",
        "findings": [*previous["findings"], {"type": "task_approval", "task_id": "task-1", "stage": "patch", "approved": True}],
        "test_results": [*previous["test_results"], {"task_id": "task-1", "passed": True, "attempt": 2}],
        "activity_log": [*previous["activity_log"], {"timestamp": "2026-10-30T01:02:00+00:00", "event": "mission_completed"}],
    }

    store.save(updated, previous=previous)

    assert store.get(previous["mission_id"]) == updated
    assert len([path for path in client.documents if len(path) == 4 and path[2] == "approvals"]) == 3


def test_firestore_create_is_idempotency_guarded():
    store = FirestoreMissionStore(FakeFirestore())
    mission = sample_mission()
    store.save(mission, create_only=True)

    with pytest.raises(FirestoreOperationError, match="could not save"):
        store.save(mission, create_only=True)


def test_firestore_mission_state_routes_to_firestore_without_json_fallback(monkeypatch, tmp_path):
    store = FirestoreMissionStore(FakeFirestore())
    monkeypatch.setenv("DEVPILOT_STORAGE_BACKEND", "firestore")
    monkeypatch.setattr(mission_state, "FirestoreMissionStore", lambda: store)

    mission = mission_state.create_mission("Persist in Firestore")
    updated = mission_state.update_mission(
        mission["mission_id"],
        {"status": "running", "tasks": [{"task_id": "task-1"}]},
    )

    assert mission_state.load_mission(mission["mission_id"]) == updated
    assert mission_state.list_missions() == [updated]
    assert not list(tmp_path.glob("*.json"))


def test_json_storage_requires_explicit_selection(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVPILOT_STORAGE_BACKEND", "json")
    mission = mission_state.create_mission("Persist locally", state_dir=tmp_path)
    assert mission_state.load_mission(mission["mission_id"], state_dir=tmp_path) == mission
    assert mission_state.list_missions(state_dir=tmp_path) == [mission]


def test_firestore_is_default_and_invalid_backend_is_actionable(monkeypatch):
    monkeypatch.delenv("DEVPILOT_STORAGE_BACKEND", raising=False)
    monkeypatch.setattr(mission_state, "load_firestore_environment", lambda: None)
    assert mission_state.is_firestore_backend()

    monkeypatch.setenv("DEVPILOT_STORAGE_BACKEND", "unknown")
    with pytest.raises(FirestoreConfigurationError, match="must be 'firestore' or 'json'"):
        mission_state.is_firestore_backend()


def test_missing_firestore_credentials_are_actionable_and_do_not_reveal_values(monkeypatch):
    for key in ("FIREBASE_SERVICE_ACCOUNT_JSON", "FIREBASE_SERVICE_ACCOUNT_FILE", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(firestore_store, "_read_streamlit_firebase_settings", lambda: {})
    with pytest.raises(FirestoreConfigurationError, match="credentials are missing"):
        firestore_store._service_account_info()


def test_invalid_service_account_json_does_not_echo_secret(monkeypatch):
    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT_JSON", '{"private_key":"never-print-this"')
    with pytest.raises(FirestoreConfigurationError, match="valid service-account JSON") as error:
        firestore_store._service_account_info()
    assert "never-print-this" not in str(error.value)


def test_service_account_file_errors_do_not_expose_path(monkeypatch):
    monkeypatch.delenv("FIREBASE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "private-account-path.json")
    with pytest.raises(FirestoreConfigurationError) as error:
        firestore_store._service_account_info()
    assert "private-account-path.json" not in str(error.value)


def test_service_account_file_environment_variable_loads_credentials(monkeypatch, tmp_path):
    account = {
        "type": "service_account",
        "project_id": firestore_store.PROJECT_ID,
        "private_key": "test-key",
        "client_email": "devpilot@example.invalid",
    }
    account_path = tmp_path / "admin.json"
    account_path.write_text(json.dumps(account), encoding="utf-8")
    monkeypatch.delenv("FIREBASE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(account_path))

    assert firestore_store._service_account_info() == account


def test_service_account_json_environment_variable_loads_credentials(monkeypatch):
    account = {
        "type": "service_account",
        "project_id": firestore_store.PROJECT_ID,
        "private_key": "test-key",
        "client_email": "devpilot@example.invalid",
    }
    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT_JSON", json.dumps(account))

    assert firestore_store._service_account_info() == account


def test_service_account_project_must_match_configured_project(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "different-project")
    monkeypatch.setattr(firestore_store, "_read_streamlit_firebase_settings", lambda: {})
    with pytest.raises(FirestoreConfigurationError, match="must match"):
        firestore_store._project_id({"project_id": "service-account-project"})


def test_firestore_rejects_non_json_serializable_mission_values():
    with pytest.raises(ValueError, match="JSON-serializable"):
        firestore_store._json_copy({"not_finite": float("nan")})


def test_local_mission_migration_is_repeatable_and_keeps_source_files(tmp_path):
    from devpilot.migrate_firestore import migrate_local_missions

    local_state = tmp_path / "state"
    local_state.mkdir()
    mission = mission_state.create_mission("Keep local source", state_dir=local_state)
    store = FirestoreMissionStore(FakeFirestore())

    assert migrate_local_missions(local_state, store=store) == (1, 0)
    assert migrate_local_missions(local_state, store=store) == (0, 1)
    assert store.get(mission["mission_id"]) == mission
    assert (local_state / f"{mission['mission_id']}.json").exists()


def test_firestore_operation_failures_are_safe():
    class BrokenFirestore(FakeFirestore):
        def collection(self, _name):
            raise RuntimeError("private credential text")

    with pytest.raises(FirestoreOperationError) as error:
        FirestoreMissionStore(BrokenFirestore()).list()
    assert "private credential text" not in str(error.value)


def test_invalid_admin_credential_initialization_is_sanitized(monkeypatch):
    import firebase_admin

    monkeypatch.setattr(firestore_store, "_client", None)
    monkeypatch.setattr(
        firestore_store,
        "_service_account_info",
        lambda: {"project_id": firestore_store.PROJECT_ID},
    )
    monkeypatch.setattr(
        firestore_store,
        "configured_project_id",
        lambda: firestore_store.PROJECT_ID,
    )
    monkeypatch.setattr(
        firebase_admin,
        "initialize_app",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("never-print-this-private-key")
        ),
    )

    with pytest.raises(FirestoreConfigurationError, match="initialize") as error:
        firestore_store._get_client()
    assert "never-print-this-private-key" not in str(error.value)


def test_admin_sdk_initializes_with_configured_project(monkeypatch):
    import firebase_admin
    from firebase_admin import credentials, firestore

    client = FakeFirestore()
    monkeypatch.setattr(firestore_store, "_client", None)
    monkeypatch.setattr(
        firestore_store,
        "_service_account_info",
        lambda: {"project_id": firestore_store.PROJECT_ID, "private_key": "test-key"},
    )
    monkeypatch.setattr(
        firestore_store,
        "configured_project_id",
        lambda: firestore_store.PROJECT_ID,
    )
    monkeypatch.setattr(credentials, "Certificate", lambda _info: "test-credential")
    monkeypatch.setattr(firebase_admin, "initialize_app", lambda *_args, **_kwargs: "test-app")
    monkeypatch.setattr(firestore, "client", lambda *, app: client)

    assert firestore_store._get_client() is client


def test_connection_check_uses_a_live_firestore_query(monkeypatch):
    client = FakeFirestore()
    monkeypatch.setattr(firestore_store, "_client", client)
    firestore_store.test_firestore_connection()


def test_firestore_failure_does_not_fall_back_to_json(monkeypatch, tmp_path):
    class FailedStore:
        def save(self, *_args, **_kwargs):
            raise FirestoreOperationError("Cloud Firestore could not save the mission.")

    monkeypatch.setenv("DEVPILOT_STORAGE_BACKEND", "firestore")
    monkeypatch.setattr(mission_state, "FirestoreMissionStore", FailedStore)
    with pytest.raises(FirestoreOperationError, match="could not save"):
        mission_state.create_mission("Firestore is authoritative")
    assert not list(tmp_path.glob("*.json"))


def test_cli_uses_the_same_configured_firestore_backend(monkeypatch, tmp_path):
    import autonomous_coder

    captured = {}
    monkeypatch.setenv("DEVPILOT_STORAGE_BACKEND", "firestore")
    monkeypatch.setattr(autonomous_coder, "RepositoryTools", lambda _root: object())

    def run_mission(*_args, **kwargs):
        captured.update(kwargs)
        return {"status": "completed", "mission_id": "mission-cli", "tasks": [], "test_results": []}

    monkeypatch.setattr(autonomous_coder, "run_mission", run_mission)
    assert autonomous_coder.main(["--request", "CLI storage check", "--repo", str(tmp_path)]) == 0
    assert "state_dir" not in captured
    assert mission_state.is_firestore_backend()


def test_firestore_settings_accept_streamlit_service_account_mapping(monkeypatch):
    account = {
        "type": "service_account",
        "project_id": firestore_store.PROJECT_ID,
        "private_key": "private-value",
        "client_email": "devpilot@example.invalid",
    }
    monkeypatch.delenv("FIREBASE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("FIREBASE_SERVICE_ACCOUNT_FILE", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(
        firestore_store,
        "_read_streamlit_firebase_settings",
        lambda: {"project_id": firestore_store.PROJECT_ID, "service_account": account},
    )
    assert firestore_store._service_account_info() == account


def test_root_document_does_not_embed_variable_length_mission_records():
    root = FirestoreMissionStore._root_document(sample_mission())
    assert "tasks" not in root
    assert "findings" not in root
    assert "activity_log" not in root
    assert root["record_counts"] == {
        "tasks": 1,
        "findings": 3,
        "activity_log": 2,
        "test_results": 1,
    }
