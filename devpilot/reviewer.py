"""Read-only, static review of proposed or applied task changes."""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .mission_state import load_mission, update_mission
from .repository_tools import RepositoryTools

_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "complete", "for", "from",
    "in", "into", "is", "it", "of", "on", "or", "that", "the", "this", "to",
    "verify", "with", "must", "should", "ensure", "support", "handle", "update",
}
_RISK_PATTERNS = (
    (re.compile(r"\beval\s*\("), "dynamic_eval", "Dynamic eval can execute untrusted input."),
    (re.compile(r"\bexec\s*\("), "dynamic_exec", "Dynamic exec can execute untrusted input."),
    (re.compile(r"\bos\.system\s*\("), "shell_execution", "os.system executes a shell command."),
    (re.compile(r"\bshell\s*=\s*True\b"), "shell_enabled", "shell=True enables shell interpretation."),
    (re.compile(r"\bpickle\.loads?\s*\("), "unsafe_deserialization", "Pickle can execute code while deserializing untrusted data."),
    (re.compile(r"\byaml\.load\s*\("), "unsafe_yaml_load", "yaml.load may construct unsafe Python objects."),
    (re.compile(r"\bverify\s*=\s*False\b"), "tls_verification_disabled", "Disabling TLS verification permits man-in-the-middle attacks."),
)


class ReviewerAgent:
    """Review recorded task changes without editing files or running commands."""

    def __init__(self, repository: RepositoryTools | None = None, state_dir: str | Path | None = None):
        self.repository = repository or RepositoryTools()
        self.state_dir = state_dir

    def _mission(self, mission_id: str) -> dict[str, Any]:
        mission = load_mission(mission_id, state_dir=self.state_dir)
        if mission is None:
            raise FileNotFoundError(f"mission not found: {mission_id}")
        return mission

    @staticmethod
    def _patch_path(patch: str) -> str:
        lines = patch.splitlines()
        if len(lines) < 3 or not lines[0].startswith("--- ") or not lines[1].startswith("+++ "):
            raise ValueError("review requires a single-file unified diff")
        old = lines[0][4:].split("\t", 1)[0].strip()
        new = lines[1][4:].split("\t", 1)[0].strip()
        if old.startswith("a/"):
            old = old[2:]
        if new.startswith("b/"):
            new = new[2:]
        if old != new or old in ("", "/dev/null"):
            raise ValueError("review requires a modification to one existing file")
        return old

    def review_task(self, mission_id: str, task_id: str) -> dict[str, Any]:
        mission = self._mission(mission_id)
        task = next((item for item in mission["tasks"] if isinstance(item, dict) and item.get("task_id") == task_id), None)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        criteria = task.get("acceptance_criteria")
        paths = task.get("relevant_files")
        if not isinstance(criteria, list) or not criteria or any(not isinstance(c, str) or not c.strip() for c in criteria):
            raise ValueError("task must have non-empty acceptance criteria")
        if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise ValueError("task relevant_files must be a list of paths")
        proposals = [item for item in mission["findings"] if isinstance(item, dict) and item.get("type") == "patch_proposal" and item.get("task_id") == task_id and item.get("status") in {"proposed", "applied"}]
        if not proposals:
            raise ValueError("task has no reviewable patch proposal")
        proposal = proposals[-1]
        patch = proposal.get("patch")
        if not isinstance(patch, str):
            raise ValueError("patch proposal is malformed")
        patch_path = self._patch_path(patch)
        reviewed_paths = list(dict.fromkeys([*paths, patch_path]))
        issues: list[dict[str, str]] = []
        source: dict[str, str] = {}
        for name in reviewed_paths:
            try:
                source[name] = self.repository.read_file(name)
            except ValueError as exc:
                issues.append({"category": "security", "code": "unsafe_path", "severity": "high", "message": f"Rejected unsafe repository path {name!r}: {exc}"})
            except FileNotFoundError:
                issues.append({"category": "correctness", "code": "missing_file", "severity": "high", "message": f"Relevant file does not exist: {name}"})

        additions = "\n".join(line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
        for pattern, code, message in _RISK_PATTERNS:
            if pattern.search(additions):
                issues.append({"category": "security", "code": code, "severity": "high", "message": message})

        for name, content in source.items():
            if name.endswith(".py"):
                try:
                    ast.parse(content.removeprefix("\ufeff"), filename=name)
                except SyntaxError as exc:
                    issues.append({"category": "correctness", "code": "syntax_error", "severity": "high", "message": f"{name}:{exc.lineno}: {exc.msg}"})

        criteria_checks = []
        for criterion in criteria:
            keywords = {word.casefold() for word in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", criterion) if word.casefold() not in _STOP_WORDS and len(word) > 1}
            matched = sorted(word for word in keywords if re.search(rf"\b{re.escape(word)}\b", additions, re.IGNORECASE))
            addressed = bool(matched)
            criteria_checks.append({"criterion": criterion, "status": "addressed" if addressed else "not_demonstrated", "matched_terms": matched})
            if not addressed:
                issues.append({"category": "requirements", "code": "criterion_not_demonstrated", "severity": "medium", "message": f"The patch does not demonstrate acceptance criterion: {criterion}"})

        review = {
            "type": "review",
            "task_id": task_id,
            "proposal_id": proposal.get("proposal_id"),
            "status": "fail" if issues else "pass",
            "criteria": criteria_checks,
            "findings": issues,
            "reviewed_files": reviewed_paths,
        }
        update_mission(mission_id, {"findings": [*mission["findings"], review]}, state_dir=self.state_dir)
        return review
