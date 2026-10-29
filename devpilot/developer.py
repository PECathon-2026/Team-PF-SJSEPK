"""Developer task approval, inspection, patch proposal, and application."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from .mission_state import load_mission, update_mission
from .repository_tools import RepositoryTools


class DeveloperAgent:
    def __init__(self, repository: RepositoryTools | None = None, state_dir: str | Path | None = None):
        self.repository = repository or RepositoryTools()
        self.state_dir = state_dir

    def _mission(self, mission_id: str) -> dict[str, Any]:
        mission = load_mission(mission_id, state_dir=self.state_dir)
        if mission is None:
            raise FileNotFoundError(f"mission not found: {mission_id}")
        return mission

    def _append_finding(self, mission: dict[str, Any], record: dict[str, Any]) -> None:
        update_mission(mission["mission_id"], {"findings": [*mission["findings"], record]}, state_dir=self.state_dir)

    def accept_task(self, mission_id: str, task_id: str, *, approved: bool = False) -> dict[str, Any]:
        if approved is not True:
            raise PermissionError("explicit approval is required before accepting a task")
        mission = self._mission(mission_id)
        task = next((item for item in mission["tasks"] if item.get("task_id") == task_id), None)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        inspected = {name: self.repository.read_file(name) for name in task.get("relevant_files", [])}
        self._append_finding(mission, {"type": "task_approval", "task_id": task_id, "approved": True})
        return {"task": task, "files": inspected}

    def propose_patch(self, mission_id: str, task_id: str, patch: str) -> str:
        mission = self._mission(mission_id)
        approved = any(item.get("type") == "task_approval" and item.get("task_id") == task_id for item in mission["findings"])
        if not approved:
            raise PermissionError("task must be explicitly approved before proposing a patch")
        proposal_id = uuid4().hex
        try:
            self.repository.validate_patch(patch)
        except (ValueError, FileNotFoundError) as exc:
            self._append_finding(mission, {"type": "patch_proposal", "proposal_id": proposal_id, "task_id": task_id, "patch": patch, "status": "rejected", "outcome": str(exc)})
            raise
        self._append_finding(mission, {"type": "patch_proposal", "proposal_id": proposal_id, "task_id": task_id, "patch": patch, "status": "proposed"})
        return proposal_id

    def apply_proposal(self, mission_id: str, proposal_id: str, *, approved: bool = False) -> None:
        if approved is not True:
            raise PermissionError("explicit approval is required before applying a patch")
        mission = self._mission(mission_id)
        findings = list(mission["findings"])
        proposal = next((item for item in findings if item.get("type") == "patch_proposal" and item.get("proposal_id") == proposal_id), None)
        if proposal is None or proposal["status"] != "proposed":
            raise KeyError(f"active proposal not found: {proposal_id}")
        try:
            self.repository.apply_patch(proposal["patch"])
        except (ValueError, FileNotFoundError) as exc:
            proposal.update(status="failed", outcome=str(exc))
            update_mission(mission_id, {"findings": findings}, state_dir=self.state_dir)
            raise
        proposal.update(status="applied", outcome="patch applied")
        update_mission(mission_id, {"findings": findings}, state_dir=self.state_dir)
