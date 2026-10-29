from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_streamlit_dashboard_renders_without_errors():
    app = AppTest.from_file(Path(__file__).parents[1] / "devpilot" / "gui.py").run()
    assert not app.exception
    assert any(button.label == "Start Mission" for button in app.button)
    assert any(widget.label == "Mission request" for widget in app.text_area)
    assert any("devpilot-hero" in item.value for item in app.markdown)
