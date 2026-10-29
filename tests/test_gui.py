import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_streamlit_dashboard_renders_without_errors():
    app = AppTest.from_file(Path(__file__).parents[1] / "devpilot" / "gui.py").run()
    assert not app.exception
    assert any(button.label == "Start Mission" for button in app.button)
    assert any(widget.label == "Mission request" for widget in app.text_area)
    assert any("devpilot-hero" in item.value for item in app.markdown)


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
    assert any("devpilot-stage-grid" in item.value for item in app.markdown)
