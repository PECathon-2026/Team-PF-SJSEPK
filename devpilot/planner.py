"""A small structured planner that saves plans in shared mission state."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .mission_state import load_mission, update_mission
from .repository_tools import RepositoryTools


class PlannerAgent:
    def __init__(self, repository: RepositoryTools | None = None, state_dir: str | Path | None = None):
        self.repository = repository or RepositoryTools()
        self.state_dir = state_dir

    def create_plan(self, user_request: str) -> dict[str, Any]:
        if not isinstance(user_request, str) or not user_request.strip():
            raise ValueError("user request cannot be empty")
        descriptions = [part.strip(" \t-*\r\n") for part in re.split(r"[\n]+|(?<=[.!?])\s+", user_request) if part.strip(" \t-*\r\n")]
        tasks = []
        for index, description in enumerate(descriptions, 1):
            terms = re.findall(r"[A-Za-z_][A-Za-z_0-9]*", description)
            files = sorted({path for term in terms for path, _, _ in self.repository.search(term)})
            criteria = [f"Verify that: {description}"]
            tasks.append({
                "task_id": f"task-{index}",
                "description": description,
                "acceptance_criteria": criteria,
                "relevant_files": files,
            })
        return {
            "summary": user_request.strip(),
            "acceptance_criteria": [criterion for task in tasks for criterion in task["acceptance_criteria"]],
            "relevant_files": sorted({path for task in tasks for path in task["relevant_files"]}),
            "tasks": tasks,
        }

    def save_plan(self, mission_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        mission = load_mission(mission_id, state_dir=self.state_dir)
        if mission is None:
            raise FileNotFoundError(f"mission not found: {mission_id}")
        if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
            raise ValueError("plan must contain a task list")
        findings = [*mission["findings"], {
            "type": "plan",
            "summary": plan.get("summary", mission["user_request"]),
            "acceptance_criteria": plan.get("acceptance_criteria", []),
            "relevant_files": plan.get("relevant_files", []),
        }]
        return update_mission(mission_id, {"tasks": plan["tasks"], "findings": findings}, state_dir=self.state_dir)

    def plan_mission(self, mission_id: str) -> dict[str, Any]:
        mission = load_mission(mission_id, state_dir=self.state_dir)
        if mission is None:
            raise FileNotFoundError(f"mission not found: {mission_id}")
        plan = self.create_plan(mission["user_request"])
        self.save_plan(mission_id, plan)
        return plan
