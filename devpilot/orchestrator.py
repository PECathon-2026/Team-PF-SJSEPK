"""Minimum mission flow connecting the existing DevPilot components."""
from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
import sys
from typing import Any

from .developer import DeveloperAgent
from .mission_state import create_mission, load_mission, update_mission
from .planner import PlannerAgent
from .repository_tools import RepositoryTools
from .reviewer import ReviewerAgent

PatchProvider = Callable[[dict[str, Any], int], str | None]
ApprovalProvider = Callable[[str, dict[str, Any]], bool]


def run_mission(
    user_request: str,
    patch_provider: PatchProvider,
    approval_provider: ApprovalProvider,
    *,
    state_dir: str | Path | None = None,
    repository: RepositoryTools | None = None,
    max_retries: int = 1,
    activity_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run plan, approve, patch, review, and test steps with bounded retries."""
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("max_retries must be a non-negative integer")
    repo = repository or RepositoryTools()
    planner = PlannerAgent(repo, state_dir=state_dir)
    developer = DeveloperAgent(repo, state_dir=state_dir)
    reviewer = ReviewerAgent(repo, state_dir=state_dir)
    mission = create_mission(user_request, state_dir=state_dir)
    mission_id = mission["mission_id"]

    def log(event: str, **details: Any) -> None:
        current = load_mission(mission_id, state_dir=state_dir)
        entry = {"event": event, **details}
        update_mission(mission_id, {"activity_log": [*current["activity_log"], entry]}, state_dir=state_dir)
        if activity_callback:
            activity_callback(entry)

    def increment_retry() -> None:
        current = load_mission(mission_id, state_dir=state_dir)
        update_mission(mission_id, {"retry_count": current["retry_count"] + 1}, state_dir=state_dir)

    update_mission(mission_id, {"status": "running"}, state_dir=state_dir)
    plan = planner.plan_mission(mission_id)
    log("plan_created", task_count=len(plan["tasks"]))
    if not plan["tasks"]:
        update_mission(mission_id, {"status": "failed"}, state_dir=state_dir)
        log("mission_failed", reason="planner returned no tasks")
        return load_mission(mission_id, state_dir=state_dir)

    test_attempts: list[dict[str, Any]] = []
    final_pass = True
    for task in plan["tasks"]:
        task_id = task["task_id"]
        if not approval_provider("task", task):
            update_mission(mission_id, {"status": "cancelled", "test_results": test_attempts}, state_dir=state_dir)
            log("approval_denied", stage="task", task_id=task_id)
            return load_mission(mission_id, state_dir=state_dir)
        developer.accept_task(mission_id, task_id, approved=True)
        task_passed = False
        for attempt in range(max_retries + 1):
            patch = patch_provider(task, attempt)
            if not patch:
                update_mission(mission_id, {"status": "cancelled", "test_results": test_attempts}, state_dir=state_dir)
                log("patch_not_provided", task_id=task_id, attempt=attempt + 1)
                return load_mission(mission_id, state_dir=state_dir)
            try:
                proposal_id = developer.propose_patch(mission_id, task_id, patch)
            except (ValueError, FileNotFoundError) as exc:
                log("patch_rejected", task_id=task_id, attempt=attempt + 1, reason=str(exc))
                if attempt == max_retries:
                    final_pass = False
                    break
                increment_retry()
                continue
            log("patch_proposed", task_id=task_id, proposal_id=proposal_id, attempt=attempt + 1)
            if not approval_provider("patch", {"task": task, "patch": patch, "proposal_id": proposal_id}):
                update_mission(mission_id, {"status": "cancelled", "test_results": test_attempts}, state_dir=state_dir)
                log("approval_denied", stage="patch", task_id=task_id, proposal_id=proposal_id)
                return load_mission(mission_id, state_dir=state_dir)
            developer.apply_proposal(mission_id, proposal_id, approved=True)
            log("patch_applied", task_id=task_id, proposal_id=proposal_id)
            review = reviewer.review_task(mission_id, task_id)
            try:
                completed = repo.run_tests()
                result = {
                    "task_id": task_id,
                    "attempt": attempt + 1,
                    "command": [sys.executable, "-m", "pytest", "-q", str(repo.repo_root)],
                    "returncode": completed.returncode,
                    "passed": completed.returncode == 0,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                }
            except Exception as exc:
                result = {"task_id": task_id, "attempt": attempt + 1, "passed": False, "error": f"{type(exc).__name__}: {exc}"}
            test_attempts.append(result)
            verified = result["passed"] and review["status"] == "pass"
            log("verification_completed", task_id=task_id, attempt=attempt + 1, tests_passed=result["passed"], review_status=review["status"])
            if verified:
                task_passed = True
                break
            if attempt < max_retries:
                increment_retry()
                log("retry_started", task_id=task_id, retry=attempt + 1)
        if not task_passed:
            final_pass = False
            break

    status = "completed" if final_pass else "failed"
    update_mission(mission_id, {"status": status, "test_results": test_attempts}, state_dir=state_dir)
    log("mission_finished", status=status)
    return load_mission(mission_id, state_dir=state_dir)


def _read_patch(_: dict[str, Any], attempt: int) -> str | None:
    print(f"Provide unified diff for attempt {attempt + 1}; enter a single '.' line when finished:")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            return None
        if line == ".":
            return "".join(lines)
        lines.append(line + "\n")


def _approve(stage: str, details: dict[str, Any]) -> bool:
    if stage == "task":
        print(f"Planned task: {details.get('description', details)}")
    else:
        print("Proposed patch:\n" + details["patch"])
    try:
        return input(f"Approve {stage}? [y/N] ").strip().casefold() in {"y", "yes"}
    except EOFError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a DevPilot coding mission against demo_repo")
    parser.add_argument("--request", required=True, help="coding task request")
    parser.add_argument("--max-retries", type=int, default=1, help="maximum retry attempts after the first patch")
    args = parser.parse_args(argv)
    if args.max_retries < 0:
        parser.error("--max-retries must be non-negative")

    def show(entry: dict[str, Any]) -> None:
        event = entry["event"]
        if event == "plan_created":
            print(f"Plan ready: {entry['task_count']} task(s)")
        elif event == "patch_proposed":
            print(f"Patch proposed for {entry['task_id']} (attempt {entry['attempt']})")
        elif event == "patch_applied":
            print(f"Approved patch applied for {entry['task_id']}")
        elif event == "verification_completed":
            test_state = "passed" if entry["tests_passed"] else "FAILED"
            print(f"Verification: tests {test_state}; review {entry['review_status']} ({entry['task_id']}, attempt {entry['attempt']})")
        elif event == "retry_started":
            print(f"Retrying {entry['task_id']} (retry {entry['retry']})")
        elif event == "patch_rejected":
            print(f"Patch rejected for {entry['task_id']}: {entry['reason']}")
        elif event == "approval_denied":
            print(f"Approval denied at {entry['stage']} stage")
        elif event == "mission_finished":
            print(f"Mission finished: {entry['status']}")

    mission = run_mission(args.request, _read_patch, _approve, max_retries=args.max_retries, activity_callback=show)
    print("Activity log:")
    for item in mission["activity_log"]:
        print(f"- {item['event']}")
    print("Test results:")
    for result in mission.get("test_results") or []:
        label = "PASS" if result.get("passed") else "FAIL"
        exit_code = result.get("returncode", "unavailable")
        print(f"- {result.get('task_id', 'task')} attempt {result['attempt']}: {label} (exit {exit_code})")
        if result.get("stdout", "").strip():
            print(result["stdout"].strip())
        if result.get("stderr", "").strip():
            print(result["stderr"].strip())
        if result.get("error"):
            print(result["error"])
    reviews = [item for item in mission["findings"] if item.get("type") == "review"]
    latest_tests = {}
    for result in mission.get("test_results") or []:
        latest_tests[result.get("task_id")] = result
    latest_reviews = {review["task_id"]: review for review in reviews}
    verified = bool(mission.get("tasks")) and all(
        task.get("task_id") in latest_tests and latest_tests[task["task_id"]].get("passed")
        and task.get("task_id") in latest_reviews and latest_reviews[task["task_id"]]["status"] == "pass"
        for task in mission["tasks"]
    )
    if reviews:
        for task_id, review in latest_reviews.items():
            print(f"Final review for {task_id}: {review['status']}")
            if not review["findings"]:
                print("- No review findings")
            for finding in review["findings"]:
                print(f"- {finding['category']}/{finding['code']}: {finding['message']}")
    status = mission["status"]
    if status == "completed" and not verified:
        status = "failed (verification incomplete)"
    print(f"Mission {mission['mission_id']}: {status}")
    return 0 if status == "completed" and verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
