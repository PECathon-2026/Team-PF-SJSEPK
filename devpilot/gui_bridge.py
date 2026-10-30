"""Thread bridge between Streamlit controls and the existing orchestrator."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from threading import Event
from typing import Any

from .codegen import generate_patch
from .orchestrator import run_mission
from .repository_tools import RepositoryTools


@dataclass
class Interaction:
    kind: str
    details: dict[str, Any]
    response: Queue = field(default_factory=lambda: Queue(maxsize=1))

    def answer(self, value: Any) -> None:
        self.response.put(value)

    def wait(self, cancel_event: Event | None = None) -> Any:
        while True:
            if cancel_event and cancel_event.is_set():
                return None
            try:
                return self.response.get(timeout=0.1)
            except Empty:
                continue


def mission_stages(mission: dict[str, Any]) -> dict[str, str]:
    """Derive dashboard stage labels strictly from persisted mission records."""
    if mission.get("status") == "cancelled":
        return {
            "Planner": "Cancelled",
            "Developer": "Cancelled",
            "Reviewer": "Cancelled",
            "Tests": "Cancelled",
            "Mission": "cancelled",
        }

    findings = mission.get("findings", [])
    plan_ready = any(item.get("type") == "plan" for item in findings)
    approvals = [item for item in findings if item.get("type") == "task_approval"]
    proposals = [item for item in findings if item.get("type") == "patch_proposal"]
    reviews = [item for item in findings if item.get("type") == "review"]
    latest_reviews = {item["task_id"]: item for item in reviews}
    latest_tests = {}
    for result in mission.get("test_results") or []:
        latest_tests[result.get("task_id")] = result

    latest_proposal = proposals[-1] if proposals else None
    developer = {
        "applied": "Applied", "proposed": "Proposed", "rejected": "Rejected", "failed": "Failed"
    }.get(latest_proposal.get("status"), "Accepted" if approvals else "Waiting") if latest_proposal else (
        "Accepted" if approvals else "Waiting"
    )
    task_ids = {task.get("task_id") for task in mission.get("tasks", []) if isinstance(task, dict)}
    reviewer = "Pass" if task_ids and task_ids <= latest_reviews.keys() and all(
        latest_reviews[task_id]["status"] == "pass" for task_id in task_ids
    ) else ("Issues" if any(item["status"] == "fail" for item in latest_reviews.values()) else "Waiting")
    tests = "Failed" if any(not item.get("passed") for item in latest_tests.values()) else (
        "Passed" if task_ids and task_ids <= latest_tests.keys() and all(latest_tests[task_id].get("passed") for task_id in task_ids)
        else "Running" if latest_proposal and latest_proposal.get("status") == "applied" else "Waiting"
    )
    return {
        "Planner": "Planned" if plan_ready else "Waiting",
        "Developer": developer,
        "Reviewer": reviewer,
        "Tests": tests,
        "Mission": mission.get("status", "unknown"),
    }


def run_mission_worker(
    user_request: str,
    events: Queue,
    *,
    repository: RepositoryTools | None = None,
    state_dir: str | Path | None = None,
    max_retries: int = 1,
    cancel_event: Event | None = None,
    use_llm: bool = False,
) -> None:
    """Run the existing synchronous mission flow on a worker thread."""
    def ask(kind: str, **details: Any) -> Any:
        interaction = Interaction(kind, details)
        events.put({"kind": "interaction", "interaction": interaction})
        return interaction.wait(cancel_event)

    def patch_provider(task: dict[str, Any], attempt: int) -> str | None:
        if use_llm:
            return generate_patch(repository or RepositoryTools(), task, user_request, attempt=attempt)
        return ask("patch", task=task, attempt=attempt)

    def approval_provider(stage: str, details: dict[str, Any]) -> bool:
        return bool(ask("approval", stage=stage, details=details))

    def clarification_provider(question: str) -> str | None:
        answer = ask("clarification", question=question)
        return answer if isinstance(answer, str) else None

    try:
        mission = run_mission(
            user_request,
            patch_provider,
            approval_provider,
            repository=repository,
            state_dir=state_dir,
            max_retries=max_retries,
            cancel_event=cancel_event,
            clarification_provider=clarification_provider,
            activity_callback=lambda entry: events.put({"kind": "activity", "entry": entry}),
        )
        events.put({"kind": "result", "mission": mission})
    except Exception as exc:
        events.put({"kind": "error", "message": f"{type(exc).__name__}: {exc}"})
