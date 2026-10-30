from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
from typing import Any

from .orchestrator import run_mission
from .repository_tools import RepositoryTools

REQUEST = "Add greet(name) to sample.py and return the personalized greeting."


def _demo_patch(task: dict[str, Any], attempt: int) -> str:
    if attempt == 0:
        return (
            "--- a/sample.py\n+++ b/sample.py\n@@ -1,2 +1,5 @@\n"
            " # Demo repository for the DevPilot feedback-loop walkthrough.\n"
            ' message = "hello from demo_repo"\n'
            "+\n+def greet(name):\n+    return f\"Hi, {name}!\"\n"
        )
    feedback = task.get("retry_feedback")
    if not feedback or feedback.get("test_result", {}).get("passed") is not False:
        raise RuntimeError("The recovery patch requires recorded failing-test evidence.")
    return (
        "--- a/sample.py\n+++ b/sample.py\n@@ -5 +5 @@\n"
        '-    return f"Hi, {name}!"\n'
        '+    return f"Hello, {name}!"\n'
    )


def _approval(stage: str, details: dict[str, Any]) -> bool:
    if stage == "task":
        print(f"\nTask approval: {details['description']}")
        for criterion in details.get("acceptance_criteria", []):
            print(f"- {criterion}")
    else:
        print(f"\nPatch approval for {details['task']['task_id']}:")
        print(details["patch"])
    try:
        return input("Approve? [y/N]: ").strip().casefold() in {"y", "yes"}
    except EOFError:
        return False


def _portable_test_attempt(result: dict[str, Any]) -> dict[str, Any]:
    portable = {key: value for key, value in result.items() if key != "command"}
    portable["command"] = (
        "python -m pytest -q --noconftest -p no:cacheprovider "
        "-c <temporary pytest.ini> <temporary repository copy>"
    )
    for field in ("stdout", "stderr"):
        if isinstance(portable.get(field), str):
            portable[field] = re.sub(
                r"(?:\.\.[\\/])+(?:[A-Za-z0-9._ -]+[\\/])*test_sample\.py",
                "<temporary test file>",
                portable[field],
            )
    return portable


def run_demo(log_path: Path, *, approval_provider=_approval) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="devpilot-e2e-") as directory:
        repo_root = Path(directory) / "demo_repo"
        repo_root.mkdir()
        (repo_root / "sample.py").write_text(
            "# Demo repository for the DevPilot feedback-loop walkthrough.\n"
            'message = "hello from demo_repo"\n',
            encoding="utf-8",
        )
        (repo_root / "test_sample.py").write_text(
            "import sample\n\n"
            "def test_greeting_is_personalized():\n"
            '    assert sample.greet("Ada") == "Hello, Ada!"\n',
            encoding="utf-8",
        )
        activity: list[dict[str, Any]] = []
        mission = run_mission(
            REQUEST,
            _demo_patch,
            approval_provider,
            repository=RepositoryTools(repo_root),
            state_dir=Path(directory) / "state",
            max_retries=1,
            activity_callback=activity.append,
        )
        messages = [item for item in mission["findings"] if item.get("type") == "agent_message"]
        log = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "request": REQUEST,
            "result": mission["status"],
            "mission_id": mission["mission_id"],
            "tasks": mission["tasks"],
            "patch_proposals": [
                item for item in mission["findings"] if item.get("type") == "patch_proposal"
            ],
            "test_attempts": [
                _portable_test_attempt(result) for result in mission["test_results"]
            ],
            "agent_messages": messages,
            "review_findings": [item for item in mission["findings"] if item.get("type") == "review"],
            "activity": activity,
            "feedback_loop_verified": bool(messages)
            and len(mission["test_results"]) == 2
            and mission["test_results"][0]["passed"] is False
            and mission["test_results"][1]["passed"] is True,
            "note": "The recovery patch is selected only after the actual failing-test output is persisted and passed to the developer callback.",
        }
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")
        return log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run DevPilot's approval-gated test-recovery demonstration.")
    parser.add_argument("--log", type=Path, default=Path("demo_run_log.json"), help="Path to write the evidence log.")
    args = parser.parse_args(argv)
    log = run_demo(args.log)
    print(f"\nMission status: {log['result']}")
    print(f"Feedback loop verified: {log['feedback_loop_verified']}")
    print(f"Evidence log: {args.log.resolve()}")
    return 0 if log["result"] == "completed" and log["feedback_loop_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
