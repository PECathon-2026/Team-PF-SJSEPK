"""A small structured planner that saves plans in shared mission state."""
from __future__ import annotations

from collections.abc import Callable
import re
from pathlib import Path
from typing import Any

from .mission_state import load_mission, update_mission
from .repository_tools import RepositoryTools


class RequirementsAnalystAgent:
    def run(self, user_request: str) -> list[str]:
        return [
            part.strip(" \t-*\r\n")
            for part in re.split(r"[\n]+|(?<=[.!?])\s+|;\s+", user_request)
            if part.strip(" \t-*\r\n")
        ]


class RepositoryScoutAgent:
    def __init__(self, repository: RepositoryTools):
        self.repository = repository

    def run(self, descriptions: list[str]) -> list[list[str]]:
        relevant_by_task = []
        for description in descriptions:
            terms = re.findall(r"[A-Za-z_][A-Za-z_0-9]*", description)
            relevant_by_task.append(
                sorted({path for term in terms for path, _, _ in self.repository.search(term)})
            )
        return relevant_by_task


class AcceptanceCriteriaAgent:
    @staticmethod
    def run(descriptions: list[str], relevant_by_task: list[list[str]]) -> list[dict[str, Any]]:
        return [
            {
                "task_id": f"task-{index}",
                "description": description,
                "acceptance_criteria": [f"Verify that: {description}"],
                "relevant_files": relevant_by_task[index - 1],
            }
            for index, description in enumerate(descriptions, 1)
        ]


class PlannerAgent:
    def __init__(self, repository: RepositoryTools | None = None, state_dir: str | Path | None = None):
        self.repository = repository or RepositoryTools()
        self.state_dir = state_dir
        self.requirements_analyst = RequirementsAnalystAgent()
        self.repository_scout = RepositoryScoutAgent(self.repository)
        self.acceptance_analyst = AcceptanceCriteriaAgent()

    def create_plan(
        self,
        user_request: str,
        *,
        activity_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(user_request, str) or not user_request.strip():
            raise ValueError("user request cannot be empty")
        descriptions = self.requirements_analyst.run(user_request)
        agents = [
            {
                "agent": "requirements_analyst",
                "role": "Decompose the request into actionable tasks",
                "status": "completed",
                "output": f"Identified {len(descriptions)} task(s)",
            },
        ]
        if activity_callback:
            activity_callback({"agent": "requirements_analyst", "status": "completed", "summary": agents[-1]["output"]})

        relevant_by_task = self.repository_scout.run(descriptions)
        agents.append({
            "agent": "repository_scout",
            "role": "Find relevant files using read-only repository search",
            "status": "completed",
            "output": f"Mapped {len(set(path for files in relevant_by_task for path in files))} relevant file(s)",
        })
        if activity_callback:
            activity_callback({"agent": "repository_scout", "status": "completed", "summary": agents[-1]["output"]})

        tasks = self.acceptance_analyst.run(descriptions, relevant_by_task)
        agents.append({
            "agent": "acceptance_criteria_analyst",
            "role": "Attach a verifiable criterion to every task",
            "status": "completed",
            "output": f"Prepared {sum(len(task['acceptance_criteria']) for task in tasks)} acceptance criterion/criteria",
        })
        if activity_callback:
            activity_callback({"agent": "acceptance_criteria_analyst", "status": "completed", "summary": agents[-1]["output"]})
        return {
            "summary": user_request.strip(),
            "acceptance_criteria": [criterion for task in tasks for criterion in task["acceptance_criteria"]],
            "relevant_files": sorted({path for task in tasks for path in task["relevant_files"]}),
            "tasks": tasks,
            "planning_agents": agents,
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
            "planning_agents": plan.get("planning_agents", []),
        }]
        return update_mission(mission_id, {"tasks": plan["tasks"], "findings": findings}, state_dir=self.state_dir)

    def plan_mission(
        self,
        mission_id: str,
        *,
        activity_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        mission = load_mission(mission_id, state_dir=self.state_dir)
        if mission is None:
            raise FileNotFoundError(f"mission not found: {mission_id}")
        plan = self.create_plan(mission["user_request"], activity_callback=activity_callback)
        self.save_plan(mission_id, plan)
        return plan
