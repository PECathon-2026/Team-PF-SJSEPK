from queue import Queue
import threading

from devpilot.gui_bridge import mission_stages, run_mission_worker
from devpilot.mission_state import load_mission
from devpilot.repository_tools import RepositoryTools

PATCH = (
    "--- a/sample.py\n+++ b/sample.py\n@@ -1,2 +1,3 @@\n"
    " # sample demo repository file\n message = \"hello from demo_repo\"\n+value = 7\n"
)


def _run_to_result(events, answer):
    while True:
        event = events.get(timeout=10)
        if event["kind"] == "interaction":
            interaction = event["interaction"]
            interaction.answer(answer(interaction))
        elif event["kind"] in {"result", "error"}:
            return event


def test_gui_bridge_routes_patch_and_approval_through_orchestrator(tmp_path):
    root = tmp_path / "demo_repo"
    root.mkdir()
    (root / "sample.py").write_text('# sample demo repository file\nmessage = "hello from demo_repo"\n', encoding="utf-8")
    (root / "test_sample.py").write_text("import sample\n\ndef test_value():\n    assert sample.value == 7\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    events = Queue()
    thread = threading.Thread(
        target=run_mission_worker,
        args=("Add value to sample.py", events),
        kwargs={"repository": RepositoryTools(root), "state_dir": state_dir},
        daemon=True,
    )
    thread.start()

    def respond(interaction):
        if interaction.kind == "patch":
            return PATCH
        if interaction.details["stage"] == "task":
            return True
        return True

    result = _run_to_result(events, respond)
    thread.join(timeout=10)
    assert result["kind"] == "result"
    mission = result["mission"]
    assert mission["status"] == "completed"
    assert mission["test_results"][0]["passed"] is True
    stages = mission_stages(load_mission(mission["mission_id"], state_dir=state_dir))
    assert stages == {"Planner": "Planned", "Developer": "Applied", "Reviewer": "Pass", "Tests": "Passed", "Mission": "completed"}


def test_gui_rejection_uses_existing_approval_and_leaves_code_unchanged(tmp_path):
    root = tmp_path / "demo_repo"
    root.mkdir()
    source = root / "sample.py"
    source.write_text('# sample demo repository file\nmessage = "hello from demo_repo"\n', encoding="utf-8")
    events = Queue()
    thread = threading.Thread(
        target=run_mission_worker,
        args=("Add value to sample.py", events),
        kwargs={"repository": RepositoryTools(root), "state_dir": tmp_path / "state"},
        daemon=True,
    )
    thread.start()

    def reject_task(interaction):
        assert interaction.kind == "approval"
        assert interaction.details["stage"] == "task"
        return False

    result = _run_to_result(events, reject_task)
    thread.join(timeout=10)
    assert result["kind"] == "result"
    assert result["mission"]["status"] == "cancelled"
    assert source.read_text(encoding="utf-8") == '# sample demo repository file\nmessage = "hello from demo_repo"\n'
    assert mission_stages(result["mission"])["Mission"] == "cancelled"


def test_gui_cancellation_releases_pending_approval(tmp_path):
    root = tmp_path / "demo_repo"
    root.mkdir()
    (root / "sample.py").write_text("value = 1\n", encoding="utf-8")
    events = Queue()
    cancel_event = threading.Event()
    thread = threading.Thread(
        target=run_mission_worker,
        args=("Update value", events),
        kwargs={
            "repository": RepositoryTools(root),
            "state_dir": tmp_path / "state",
            "cancel_event": cancel_event,
        },
        daemon=True,
    )
    thread.start()
    interaction = events.get(timeout=10)
    while interaction["kind"] != "interaction":
        interaction = events.get(timeout=10)
    assert interaction["kind"] == "interaction"
    cancel_event.set()
    result = events.get(timeout=10)
    while result["kind"] not in {"result", "error"}:
        result = events.get(timeout=10)
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["kind"] == "result"
    assert result["mission"]["status"] == "cancelled"
