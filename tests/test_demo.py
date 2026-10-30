from pathlib import Path

from devpilot.demo import run_demo


def test_end_to_end_demo_records_real_failure_feedback_and_recovery(tmp_path):
    approvals = []

    def approve(stage, details):
        approvals.append((stage, details))
        return True

    log_path = tmp_path / "demo_run_log.json"
    result = run_demo(log_path, approval_provider=approve)
    assert result["result"] == "completed"
    assert result["feedback_loop_verified"] is True
    assert [attempt["passed"] for attempt in result["test_attempts"]] == [False, True]
    assert result["test_attempts"][0]["returncode"] != 0
    assert result["test_attempts"][1]["returncode"] == 0
    assert result["agent_messages"][-1]["to_agent"] == "developer"
    assert [stage for stage, _ in approvals] == ["task", "patch", "patch"]
    assert log_path.is_file()
    assert "Hi, Ada!" in result["test_attempts"][0]["stdout"]
