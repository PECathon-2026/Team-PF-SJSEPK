import builtins
import json
import subprocess

import pytest

from devpilot import orchestrator
from devpilot.mission_state import load_mission
from devpilot.orchestrator import run_mission
from devpilot.repository_tools import RepositoryTools


class StubRepository(RepositoryTools):
    def __init__(self, root, results):
        super().__init__(root)
        self.results = iter(results)

    def run_tests(self):
        return next(self.results)


def make_patch(before, after):
    return f"--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-{before}\n+{after}\n"


def execute(tmp_path, results, patches, approvals=None, max_retries=1):
    repo_root = tmp_path / "demo_repo"
    repo_root.mkdir()
    sample = repo_root / "sample.py"
    sample.write_text("answer = 41\n", encoding="utf-8")
    repository = StubRepository(repo_root, results)
    state_dir = tmp_path / "state"
    approval_calls = []
    activity = []

    def approve(stage, detail):
        approval_calls.append((stage, detail))
        return True if approvals is None else approvals.pop(0)

    mission = run_mission(
        "Set answer to 42",
        lambda _task, attempt: patches[attempt],
        approve,
        state_dir=state_dir,
        repository=repository,
        max_retries=max_retries,
        activity_callback=activity.append,
    )
    return mission, sample, approval_calls, activity


def test_orchestration_completes_and_records_real_test_result(tmp_path):
    result = subprocess.CompletedProcess(["pytest"], 0, "1 passed", "")
    mission, sample, approvals, activity = execute(tmp_path, [result], [make_patch("answer = 41", "answer = 42")])
    assert mission["status"] == "completed"
    test_result = mission["test_results"][0]
    assert test_result["command"][1:4] == ["-m", "pytest", "-q"]
    assert test_result["command"][-1].endswith("demo_repo")
    assert test_result["returncode"] == 0
    assert test_result["passed"] is True
    assert test_result["stdout"] == "1 passed"
    assert [call[0] for call in approvals] == ["task", "patch"]
    assert sample.read_text(encoding="utf-8") == "answer = 42\n"
    assert any(entry["event"] == "verification_completed" for entry in activity)
    assert load_mission(mission["mission_id"], state_dir=tmp_path / "state")["status"] == "completed"


def test_failed_test_retries_once_then_passes(tmp_path):
    results = [
        subprocess.CompletedProcess(["pytest"], 1, "1 failed", "failure details"),
        subprocess.CompletedProcess(["pytest"], 0, "1 passed", ""),
    ]
    patches = [make_patch("answer = 41", "answer = 42"), make_patch("answer = 42", "answer = 43")]
    mission, sample, _, _ = execute(tmp_path, results, patches, max_retries=1)
    assert mission["status"] == "completed"
    assert mission["retry_count"] == 1
    assert [attempt["returncode"] for attempt in mission["test_results"]] == [1, 0]
    assert mission["test_results"][0]["stderr"] == "failure details"
    assert sample.read_text(encoding="utf-8") == "answer = 43\n"


def test_bounded_retry_never_reports_success_when_tests_fail(tmp_path):
    results = [subprocess.CompletedProcess(["pytest"], 1, "failed", "") for _ in range(2)]
    patches = [make_patch("answer = 41", "answer = 42"), make_patch("answer = 42", "answer = 43")]
    mission, _, _, _ = execute(tmp_path, results, patches, max_retries=1)
    assert mission["status"] == "failed"
    assert mission["retry_count"] == 1
    assert len(mission["test_results"]) == 2
    assert not any(item["passed"] for item in mission["test_results"])


def test_patch_approval_is_required_before_changes(tmp_path):
    result = subprocess.CompletedProcess(["pytest"], 0, "", "")
    mission, sample, approvals, _ = execute(
        tmp_path, [result], [make_patch("answer = 41", "answer = 42")], approvals=[True, False]
    )
    assert mission["status"] == "cancelled"
    assert [call[0] for call in approvals] == ["task", "patch"]
    assert sample.read_text(encoding="utf-8") == "answer = 41\n"
    assert mission["test_results"] == []


def test_cli_displays_activity_and_final_status(monkeypatch, capsys):
    mission = {
        "mission_id": "mission-123",
        "status": "completed",
        "tasks": [{"task_id": "task-1"}],
        "test_results": [{"task_id": "task-1", "attempt": 1, "returncode": 0, "passed": True, "stdout": "1 passed", "stderr": ""}],
        "activity_log": [{"event": "plan_created"}, {"event": "mission_finished", "status": "completed"}],
        "findings": [{"type": "review", "task_id": "task-1", "status": "pass", "findings": []}],
    }
    monkeypatch.setattr(orchestrator, "run_mission", lambda *args, **kwargs: mission)
    assert orchestrator.main(["--request", "do work"]) == 0
    output = capsys.readouterr().out
    assert "plan_created" in output
    assert "Final review for task-1: pass" in output
    assert "Mission mission-123: completed" in output


def test_cli_does_not_report_unverified_completion(monkeypatch, capsys):
    mission = {
        "mission_id": "mission-unverified",
        "status": "completed",
        "tasks": [{"task_id": "task-1"}],
        "test_results": [],
        "findings": [],
        "activity_log": [],
    }
    monkeypatch.setattr(orchestrator, "run_mission", lambda *args, **kwargs: mission)
    assert orchestrator.main(["--request", "unfinished work"]) == 1
    assert "Mission mission-unverified: failed (verification incomplete)" in capsys.readouterr().out


def test_cli_end_to_end_failure_recovery_and_final_review(tmp_path, monkeypatch, capsys):
    repo_root = tmp_path / "demo_repo"
    repo_root.mkdir()
    (repo_root / "sample.py").write_text(
        '# sample demo repository file\nmessage = "hello from demo_repo"\n', encoding="utf-8"
    )
    (repo_root / "test_sample.py").write_text(
        'from sample import greet, message\n\n'
        'def test_demo_message_is_present():\n    assert message\n\n'
        'def test_greeting():\n    assert greet("Ada") == "Hello, Ada!"\n',
        encoding="utf-8",
    )

    actual_run_mission = orchestrator.run_mission
    state_dir = tmp_path / "state"

    def isolated_run_mission(request, patch_provider, approval_provider, **kwargs):
        return actual_run_mission(
            request,
            patch_provider,
            approval_provider,
            repository=RepositoryTools(repo_root),
            state_dir=state_dir,
            **kwargs,
        )

    first_patch = [
        "--- a/sample.py", "+++ b/sample.py", "@@ -1,2 +1,5 @@",
        " # sample demo repository file", ' message = "hello from demo_repo"', "+",
        "+def greet(name):", '+    return f"Hi, {name}!"', ".",
    ]
    recovery_patch = [
        "--- a/sample.py", "+++ b/sample.py", "@@ -5 +5 @@",
        '-    return f"Hi, {name}!"', '+    return f"Hello, {name}!"', ".",
    ]
    responses = iter(["y", *first_patch, "y", *recovery_patch, "y"])
    monkeypatch.setattr(orchestrator, "run_mission", isolated_run_mission)
    def fake_input(prompt=""):
        print(prompt, end="")
        return next(responses)

    monkeypatch.setattr(builtins, "input", fake_input)

    exit_code = orchestrator.main([
        "--request", "Add a greet(name) function to sample.py that returns a personalized greeting.",
        "--max-retries", "1",
    ])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Approve task?" in output
    assert output.count("Approve patch?") == 2
    assert "Final review for task-1: pass" in output
    assert "Mission " in output and ": completed" in output
    missions = list(state_dir.glob("*.json"))
    assert len(missions) == 1
    saved = json.loads(missions[0].read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
    assert saved["retry_count"] == 1
    assert [result["returncode"] for result in saved["test_results"]] == [1, 0]
    assert "failed" in saved["test_results"][0]["stdout"]
    assert saved["test_results"][1]["passed"] is True
    reviews = [item for item in saved["findings"] if item.get("type") == "review"]
    assert reviews[-1]["status"] == "pass"
    assert (repo_root / "sample.py").read_text(encoding="utf-8").endswith('return f"Hello, {name}!"\n')
