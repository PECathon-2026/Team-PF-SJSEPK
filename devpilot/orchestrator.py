"""Minimum mission flow connecting the existing DevPilot components."""
from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
import re
import subprocess
from threading import Event
from typing import Any

from .developer import DeveloperAgent
from .mission_state import create_mission, load_mission, update_mission
from .planner import PlannerAgent
from .repository_tools import RepositoryTools
from .reviewer import ReviewerAgent

PatchProvider = Callable[[dict[str, Any], int], str | None]
ApprovalProvider = Callable[[str, dict[str, Any]], bool]
ClarificationProvider = Callable[[str], str | None]
MAX_RETRIES = 3


def _command_args(command: Any) -> list[str]:
    if isinstance(command, str):
        return [command]
    return [str(part) for part in command]


def run_mission(
    user_request: str,
    patch_provider: PatchProvider,
    approval_provider: ApprovalProvider,
    *,
    state_dir: str | Path | None = None,
    repository: RepositoryTools | None = None,
    max_retries: int = 1,
    activity_callback: Callable[[dict[str, Any]], None] | None = None,
    cancel_event: Event | None = None,
    clarification_provider: ClarificationProvider | None = None,
) -> dict[str, Any]:
    """Run plan, approve, patch, review, and test steps with bounded retries."""
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= MAX_RETRIES:
        raise ValueError(f"max_retries must be between 0 and {MAX_RETRIES}")
    repo = repository or RepositoryTools()
    planner = PlannerAgent(repo, state_dir=state_dir)
    developer = DeveloperAgent(repo, state_dir=state_dir)
    reviewer = ReviewerAgent(repo, state_dir=state_dir)
    mission = create_mission(user_request, state_dir=state_dir)
    mission_id = mission["mission_id"]

    def log(event: str, **details: Any) -> None:
        current = load_mission(mission_id, state_dir=state_dir)
        entry = {"event": event, "mission_id": mission_id, **details}
        update_mission(mission_id, {"activity_log": [*current["activity_log"], entry]}, state_dir=state_dir)
        if activity_callback:
            activity_callback(entry)

    def increment_retry() -> None:
        current = load_mission(mission_id, state_dir=state_dir)
        update_mission(mission_id, {"retry_count": current["retry_count"] + 1}, state_dir=state_dir)

    update_mission(mission_id, {"status": "running"}, state_dir=state_dir)
    if cancel_event and cancel_event.is_set():
        update_mission(mission_id, {"status": "cancelled"}, state_dir=state_dir)
        log("mission_cancelled")
        return load_mission(mission_id, state_dir=state_dir)
    effective_request = user_request
    question = planner.clarification_question(user_request)
    if question:
        log("clarification_requested", question=question)
        answer = clarification_provider(question) if clarification_provider else None
        if not isinstance(answer, str) or not answer.strip():
            current = load_mission(mission_id, state_dir=state_dir)
            update_mission(
                mission_id,
                {
                    "status": "cancelled",
                    "findings": [*current["findings"], {
                        "type": "clarification",
                        "question": question,
                        "status": "unanswered",
                    }],
                },
                state_dir=state_dir,
            )
            log("clarification_unanswered")
            return load_mission(mission_id, state_dir=state_dir)
        effective_request = answer.strip()
        current = load_mission(mission_id, state_dir=state_dir)
        update_mission(
            mission_id,
            {"findings": [*current["findings"], {
                "type": "clarification",
                "question": question,
                "answer": answer.strip(),
                "status": "answered",
            }]},
            state_dir=state_dir,
        )
        log("clarification_answered")
    log("tool_selected", agent="repository_scout", tool="repository.search")
    plan = planner.create_plan(
        effective_request,
        activity_callback=lambda item: log(
            "agent_activity",
            agent=item["agent"],
            status=item["status"],
            summary=item["summary"],
        ),
    )
    planner.save_plan(mission_id, plan)
    log("plan_created", task_count=len(plan["tasks"]))
    log("tool_used", agent="repository_scout", tool="repository.search")
    if not plan["tasks"]:
        update_mission(mission_id, {"status": "failed"}, state_dir=state_dir)
        log("mission_failed", reason="planner returned no tasks")
        return load_mission(mission_id, state_dir=state_dir)

    test_attempts: list[dict[str, Any]] = []
    final_pass = True
    for task in plan["tasks"]:
        task_id = task["task_id"]
        if cancel_event and cancel_event.is_set():
            update_mission(mission_id, {"status": "cancelled", "test_results": test_attempts}, state_dir=state_dir)
            log("mission_cancelled", task_id=task_id)
            return load_mission(mission_id, state_dir=state_dir)
        if not approval_provider("task", task):
            update_mission(mission_id, {"status": "cancelled", "test_results": test_attempts}, state_dir=state_dir)
            log("approval_denied", stage="task", task_id=task_id)
            return load_mission(mission_id, state_dir=state_dir)
        selected_tools = developer.select_tools(task)
        if "repository.search" in selected_tools:
            discovered = {
                path
                for term in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", task.get("description", ""))
                if len(term) > 2
                for path, _, _ in repo.search(term)
            }
            task = {**task, "relevant_files": sorted(discovered)}
            current = load_mission(mission_id, state_dir=state_dir)
            updated_tasks = [
                task if item.get("task_id") == task_id else item
                for item in current["tasks"]
            ]
            update_mission(mission_id, {"tasks": updated_tasks}, state_dir=state_dir)
            selected_tools = developer.select_tools(task, search_completed=True)
            selected_tools.insert(0, "repository.search")
            log("tool_used", agent="developer", tool="repository.search")
        log(
            "tool_selection",
            agent="developer",
            task_id=task_id,
            tools=selected_tools,
        )
        if "repository.read_file" in selected_tools:
            log("tool_used", agent="developer", tool="repository.read_file")
        developer.accept_task(mission_id, task_id, approved=True)
        task_passed = False
        retry_feedback: dict[str, Any] | None = None
        for attempt in range(max_retries + 1):
            task_context = {**task, "retry_feedback": retry_feedback}
            try:
                patch = patch_provider(task_context, attempt)
            except (OSError, RuntimeError, ValueError) as exc:
                update_mission(mission_id, {"status": "failed", "test_results": test_attempts}, state_dir=state_dir)
                log("patch_generation_failed", task_id=task_id, attempt=attempt + 1, error=f"{type(exc).__name__}: {exc}")
                return load_mission(mission_id, state_dir=state_dir)
            if not patch:
                update_mission(mission_id, {"status": "cancelled", "test_results": test_attempts}, state_dir=state_dir)
                log("patch_not_provided", task_id=task_id, attempt=attempt + 1)
                return load_mission(mission_id, state_dir=state_dir)
            try:
                log("tool_used", agent="developer", tool="repository.validate_patch")
                proposal_id = developer.propose_patch(mission_id, task_id, patch)
            except (ValueError, FileNotFoundError) as exc:
                log("patch_rejected", task_id=task_id, attempt=attempt + 1, reason=str(exc))
                if attempt == max_retries:
                    final_pass = False
                    break
                retry_feedback = {"patch_error": str(exc)}
                current = load_mission(mission_id, state_dir=state_dir)
                message = {
                    "type": "agent_message",
                    "from_agent": "developer",
                    "to_agent": "developer",
                    "task_id": task_id,
                    "attempt": attempt + 1,
                    "message": f"Patch validation rejected the proposal: {exc}",
                }
                update_mission(mission_id, {"findings": [*current["findings"], message]}, state_dir=state_dir)
                increment_retry()
                continue
            log("patch_proposed", task_id=task_id, proposal_id=proposal_id, attempt=attempt + 1)
            if not approval_provider("patch", {"task": task, "patch": patch, "proposal_id": proposal_id}):
                update_mission(mission_id, {"status": "cancelled", "test_results": test_attempts}, state_dir=state_dir)
                log("approval_denied", stage="patch", task_id=task_id, proposal_id=proposal_id)
                return load_mission(mission_id, state_dir=state_dir)
            if cancel_event and cancel_event.is_set():
                update_mission(mission_id, {"status": "cancelled", "test_results": test_attempts}, state_dir=state_dir)
                log("mission_cancelled", task_id=task_id, proposal_id=proposal_id)
                return load_mission(mission_id, state_dir=state_dir)
            log("tool_used", agent="developer", tool="repository.apply_patch")
            developer.apply_proposal(mission_id, proposal_id, approved=True)
            log("patch_applied", task_id=task_id, proposal_id=proposal_id)
            log("review_started", task_id=task_id, attempt=attempt + 1)
            review = reviewer.review_task(mission_id, task_id)
            log(
                "review_completed",
                task_id=task_id,
                attempt=attempt + 1,
                status=review["status"],
                finding_count=len(review["findings"]),
            )
            blocking_findings = [
                finding for finding in review["findings"]
                if finding.get("category") == "security"
                and finding.get("severity") in {"critical", "high"}
            ]
            if blocking_findings:
                result = {
                    "task_id": task_id,
                    "attempt": attempt + 1,
                    "passed": False,
                    "skipped": True,
                    "error": "tests were not run because the independent review found a high-risk security issue",
                }
            else:
                try:
                    log("tests_started", task_id=task_id, attempt=attempt + 1)
                    log("tool_used", agent="sentinel", tool="repository.run_tests")
                    completed = repo.run_tests()
                    result = {
                        "task_id": task_id,
                        "attempt": attempt + 1,
                        "command": _command_args(completed.args),
                        "returncode": completed.returncode,
                        "passed": completed.returncode == 0,
                        "stdout": completed.stdout,
                        "stderr": completed.stderr,
                    }
                except subprocess.TimeoutExpired as exc:
                    stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                    stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                    result = {
                        "task_id": task_id,
                        "attempt": attempt + 1,
                        "command": _command_args(exc.cmd),
                        "passed": False,
                        "timed_out": True,
                        "error": f"test command exceeded its time limit: {exc}",
                        "stdout": stdout,
                        "stderr": stderr,
                    }
                except OSError as exc:
                    result = {
                        "task_id": task_id,
                        "attempt": attempt + 1,
                        "passed": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
            test_attempts.append(result)
            update_mission(mission_id, {"test_results": test_attempts}, state_dir=state_dir)
            verified = result["passed"] and review["status"] == "pass"
            log(
                "tests_completed",
                task_id=task_id,
                attempt=attempt + 1,
                passed=result["passed"],
                returncode=result.get("returncode"),
                timed_out=result.get("timed_out", False),
            )
            log("verification_completed", task_id=task_id, attempt=attempt + 1, tests_passed=result["passed"], review_status=review["status"])
            if verified:
                task_passed = True
                break
            if attempt < max_retries:
                retry_feedback = {
                    "test_result": {
                        "passed": result["passed"],
                        "returncode": result.get("returncode"),
                        "stdout": result.get("stdout", ""),
                        "stderr": result.get("stderr", ""),
                        "error": result.get("error", ""),
                    },
                    "review_findings": review["findings"],
                }
                current = load_mission(mission_id, state_dir=state_dir)
                sources = []
                if not result["passed"]:
                    sources.append("sentinel")
                if review["status"] != "pass":
                    sources.append("reviewer")
                message = {
                    "type": "agent_message",
                    "from_agent": "+".join(sources) or "verification",
                    "to_agent": "developer",
                    "task_id": task_id,
                    "attempt": attempt + 1,
                    "message": "Use the recorded test output and review findings to revise the next patch.",
                    "evidence": retry_feedback,
                }
                update_mission(mission_id, {"findings": [*current["findings"], message]}, state_dir=state_dir)
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
        for criterion in details.get("acceptance_criteria", []):
            print(f"Acceptance: {criterion}")
        if details.get("relevant_files"):
            print("Relevant files: " + ", ".join(details["relevant_files"]))
    else:
        print("Proposed patch:\n" + details["patch"])
    try:
        return input(f"Approve {stage}? [y/N] ").strip().casefold() in {"y", "yes"}
    except EOFError:
        return False


def _clarify(question: str) -> str | None:
    print(f"Clarification needed: {question}")
    try:
        answer = input("Your clarification (blank to cancel): ").strip()
    except EOFError:
        return None
    return answer or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a DevPilot coding mission against demo_repo")
    parser.add_argument("--request", required=True, help="coding task request")
    parser.add_argument(
        "--max-retries",
        type=int,
        default=1,
        help=f"maximum retry attempts after the first patch (0-{MAX_RETRIES})",
    )
    args = parser.parse_args(argv)
    if not 0 <= args.max_retries <= MAX_RETRIES:
        parser.error(f"--max-retries must be between 0 and {MAX_RETRIES}")

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

    mission = run_mission(
        args.request,
        _read_patch,
        _approve,
        max_retries=args.max_retries,
        activity_callback=show,
        clarification_provider=_clarify,
    )
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
