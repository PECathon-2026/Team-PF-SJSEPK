import sys
from queue import Queue
from pathlib import Path

from streamlit.testing.v1 import AppTest

from devpilot.gui_bridge import Interaction
from devpilot.mission_state import DEFAULT_STATE_DIR, create_mission, update_mission


def test_streamlit_dashboard_renders_without_errors():
    app = AppTest.from_file(Path(__file__).parents[1] / "devpilot" / "gui.py").run()
    assert not app.exception
    assert any(button.label == "Start Mission" for button in app.button)
    assert any(widget.label == "Mission request" for widget in app.text_area)
    assert any("dp-hero" in item.value for item in app.markdown)
    assert any("THE HIDDEN LEAF COMMAND CENTER" in item.value for item in app.markdown)
    assert {radio.label for radio in app.radio} == {"Command navigation"}


def test_mission_dashboard_renders_without_pandas(monkeypatch):
    app = AppTest.from_file(Path(__file__).parents[1] / "devpilot" / "gui.py")
    app.session_state["dp_result"] = {
        "mission_id": "mission-test",
        "status": "running",
        "tasks": [],
        "findings": [],
        "test_results": [],
        "activity_log": [],
    }
    monkeypatch.setitem(sys.modules, "pandas", None)
    app.run()
    assert not app.exception
    assert any("MISSION DOSSIER" in item.value for item in app.markdown)
    assert any("Mission plan" in item.value for item in app.markdown)


def test_cancelled_mission_does_not_leave_task_awaiting_approval():
    app = AppTest.from_file(Path(__file__).parents[1] / "devpilot" / "gui.py")
    app.session_state["dp_result"] = {
        "mission_id": "mission-cancelled",
        "user_request": "Test cancellation display",
        "status": "cancelled",
        "tasks": [{"task_id": "task-1", "description": "Review cancellation state"}],
        "findings": [
            {
                "type": "patch_proposal",
                "task_id": "task-1",
                "status": "proposed",
                "patch": "",
            }
        ],
        "test_results": [],
        "activity_log": [],
    }

    app.run()

    assert not app.exception
    assert any("Cancelled" in item.value for item in app.markdown)
    assert not any("Awaiting approval" in item.value for item in app.markdown)


def test_village_settings_shows_gemini_config_without_secret(monkeypatch):
    from devpilot import config

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-ui-test-model")
    monkeypatch.setattr(config, "load_dotenv", lambda **_kwargs: None)
    app = AppTest.from_file(Path(__file__).parents[1] / "devpilot" / "gui.py").run()
    app.radio[0].set_value("Village Settings").run()
    assert not app.exception
    assert any("Village Settings" in item.value for item in app.markdown)
    assert any("Google Gemini" in item.value for item in app.markdown)
    assert any("Missing" in item.value for item in app.markdown)
    assert any("gemini-ui-test-model" in item.value for item in app.markdown)
    assert any("Not tested" in item.value for item in app.markdown)
    assert not any("sk-" in item.value or "AIza" in item.value for item in app.markdown)


def test_village_settings_reports_firestore_connection_only_after_check(monkeypatch):
    from devpilot import firestore_store

    monkeypatch.setattr(firestore_store, "test_firestore_connection", lambda: None)
    app = AppTest.from_file(Path(__file__).parents[1] / "devpilot" / "gui.py").run()
    app.radio[0].set_value("Village Settings").run()
    assert any("Not tested" in item.value for item in app.markdown)
    next(
        button for button in app.button
        if button.label == "Test Firestore connection"
    ).click().run()
    assert not app.exception
    assert any("Connected" in item.value for item in app.markdown)
    assert any("Cloud Firestore" in item.value for item in app.markdown)


def test_village_settings_displays_firestore_error_without_credentials(monkeypatch):
    from devpilot import firestore_store

    monkeypatch.setattr(
        firestore_store,
        "test_firestore_connection",
        lambda: (_ for _ in ()).throw(
            firestore_store.FirestoreConfigurationError("Firestore credentials are missing.")
        ),
    )
    app = AppTest.from_file(Path(__file__).parents[1] / "devpilot" / "gui.py").run()
    app.radio[0].set_value("Village Settings").run()
    next(
        button for button in app.button
        if button.label == "Test Firestore connection"
    ).click().run()
    assert not app.exception
    assert any("Firestore credentials are missing" in item.value for item in app.error)


def test_firestore_storage_error_is_visible_without_local_fallback(monkeypatch):
    from devpilot import mission_state
    from devpilot.firestore_store import FirestoreConfigurationError

    monkeypatch.setenv("DEVPILOT_STORAGE_BACKEND", "firestore")
    monkeypatch.setattr(
        mission_state,
        "recover_stale_missions",
        lambda **_kwargs: (_ for _ in ()).throw(
            FirestoreConfigurationError("Firestore credentials are missing.")
        ),
    )
    app = AppTest.from_file(Path(__file__).parents[1] / "devpilot" / "gui.py").run()

    assert not app.exception
    assert any("Mission storage is unavailable" in item.value for item in app.error)
    assert any("No mission data was loaded or written" in item.value for item in app.info)


def test_gemini_connection_status_requires_successful_connection(monkeypatch):
    from devpilot import config

    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-gemini-secret")
    monkeypatch.setattr(config, "load_dotenv", lambda **_kwargs: None)
    monkeypatch.setattr("devpilot.llm_bridge.ask_gemini", lambda *_args, **_kwargs: "OK")
    app = AppTest.from_file(Path(__file__).parents[1] / "devpilot" / "gui.py").run()
    app.radio[0].set_value("Village Settings").run()
    next(button for button in app.button if button.label == "Test Gemini connection").click().run()
    assert not app.exception
    assert any("Connected" in item.value for item in app.markdown)
    assert not any("unit-test-gemini-secret" in item.value for item in app.markdown)


def test_gemini_connection_failure_is_visible_without_secret(monkeypatch):
    from devpilot import config

    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-gemini-secret")
    monkeypatch.setattr(config, "load_dotenv", lambda **_kwargs: None)
    monkeypatch.setattr(
        "devpilot.llm_bridge.ask_gemini",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("Gemini request failed (403): invalid API key")
        ),
    )
    app = AppTest.from_file(Path(__file__).parents[1] / "devpilot" / "gui.py").run()
    app.radio[0].set_value("Village Settings").run()
    next(button for button in app.button if button.label == "Test Gemini connection").click().run()
    assert not app.exception
    assert any("Failed" in item.value for item in app.markdown)
    assert any("403" in item.value for item in app.error)
    assert not any("unit-test-gemini-secret" in item.value for item in app.markdown)
    assert not any("unit-test-gemini-secret" in item.value for item in app.error)


def test_fragment_activity_refreshes_dashboard_to_show_mission():
    mission = create_mission("Dashboard refresh check")
    update_mission(mission["mission_id"], {"status": "running"})
    events = Queue()
    events.put({
        "kind": "activity",
        "entry": {"mission_id": mission["mission_id"], "event": "plan_created"},
    })

    class ActiveWorker:
        @staticmethod
        def is_alive():
            return True

    app = AppTest.from_file(Path(__file__).parents[1] / "devpilot" / "gui.py")
    app.session_state["dp_active"] = True
    app.session_state["dp_thread"] = ActiveWorker()
    app.session_state["dp_events"] = events
    try:
        app.run()
        assert not app.exception
        assert app.session_state["dp_mission_id"] == mission["mission_id"]
        assert any(
            f"MISSION DOSSIER · {mission['mission_id'][:8]}" in item.value
            for item in app.markdown
        )
        assert not any("Planning your mission" in item.value for item in app.markdown)
    finally:
        (DEFAULT_STATE_DIR / f"{mission['mission_id']}.json").unlink(missing_ok=True)


def test_new_mission_starts_worker_and_clears_previous_error(monkeypatch):
    import time

    from devpilot import gui_bridge

    def finish_worker(request, events, **_kwargs):
        assert request == "Add a module docstring to sample.py."
        events.put({"kind": "error", "message": "worker smoke test finished"})

    monkeypatch.setattr(gui_bridge, "run_mission_worker", finish_worker)
    app = AppTest.from_file(Path(__file__).parents[1] / "devpilot" / "gui.py")
    app.session_state["dp_error"] = "old worker error"
    app.run()
    app.text_area[0].set_value("Add a module docstring to sample.py.")
    next(button for button in app.form_submit_button if button.label == "Start Mission").click().run()
    for _ in range(20):
        if app.session_state["dp_error"] == "worker smoke test finished":
            break
        time.sleep(0.05)
        app.run()

    assert not app.exception
    assert app.session_state["dp_error"] == "worker smoke test finished"
    assert app.session_state["dp_active"] is False


def test_patch_authorization_requires_explicit_acknowledgement():
    app = AppTest.from_file(Path(__file__).parents[1] / "devpilot" / "gui.py")
    app.session_state["dp_result"] = {
        "mission_id": "mission-test",
        "status": "running",
        "tasks": [],
        "findings": [],
        "test_results": [],
        "activity_log": [],
    }
    interaction = Interaction(
        "approval",
        {
            "stage": "patch",
            "details": {
                "task": {"task_id": "task-1"},
                "patch": (
                    "--- a/sample.py\n+++ b/sample.py\n@@ -5 +5 @@\n"
                    '-    return f"Hello, {name}!"\n'
                    '+    return f"Hi, {name}!"\n'
                ),
            },
        },
    )
    app.session_state["dp_pending"] = interaction
    app.run()
    assert not app.exception
    assert any("HOKAGE AUTHORIZATION" in item.value for item in app.markdown)
    authorize = next(button for button in app.button if button.label == "Authorize mission action")
    assert authorize.disabled
    acknowledgement = next(
        checkbox for checkbox in app.checkbox
        if checkbox.label.startswith("I reviewed this mission action")
    )
    acknowledgement.check().run()
    authorize = next(button for button in app.button if button.label == "Authorize mission action")
    assert not authorize.disabled
    authorize.click().run()
    assert not app.exception
    assert interaction.response.get_nowait() is True
