from __future__ import annotations

import argparse
from getpass import getpass
import os
from pathlib import Path
import sys
from typing import Any

from devpilot.codegen import generate_patch
from devpilot.config import load_gemini_config
from devpilot.firestore_store import FirestoreError
from devpilot.orchestrator import MAX_RETRIES, run_mission
from devpilot.repository_tools import RepositoryTools


def _read_patch_from_stdin(task: dict[str, Any], attempt: int) -> str | None:
    print(f"[agent] Attempt {attempt + 1} for task {task['task_id']}")
    print("Paste a unified diff, then finish with a line containing only '.'")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            return None
        if line == ".":
            return "".join(lines)
        lines.append(line + "\n")


def _read_patch_from_file(task: dict[str, Any], attempt: int, patch_file: str | None) -> str | None:
    if patch_file is None:
        return _read_patch_from_stdin(task, attempt)
    path = Path(patch_file).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Patch file not found: {patch_file}")
    text = path.read_text(encoding="utf-8")
    return text.rstrip("\r\n") + "\n"


def _gemini_patch_provider(repo_root: Path, request: str):
    def patch_provider(task: dict[str, Any], attempt: int) -> str | None:
        return generate_patch(
            RepositoryTools(repo_root),
            task,
            request,
            attempt=attempt,
        )

    return patch_provider


def _approval_provider():
    def approve(stage: str, details: dict[str, Any]) -> bool:
        if stage == "task":
            print(f"\n[approval] Task: {details.get('description', details)}")
            for criterion in details.get("acceptance_criteria", []):
                print(f"  - {criterion}")
        else:
            print(f"\n[approval] Patch approval for {details.get('task', {}).get('task_id', 'task')}")
            print(details.get("patch", ""))
        try:
            answer = input("Approve? [y/N]: ").strip().lower()
        except EOFError:
            return False
        return answer in {"y", "yes"}

    return approve


def _clarification_provider(question: str) -> str | None:
    print(f"\n[clarification] {question}")
    try:
        answer = input("Clarification (blank to cancel): ").strip()
    except EOFError:
        return None
    return answer or None


def _ensure_gemini_key() -> None:
    if load_gemini_config().key_configured:
        return
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Gemini API key is missing. Set GEMINI_API_KEY in the environment "
            "or project .env file; see .env.example."
        )
    api_key = getpass("Gemini API key (input hidden; not saved): ").strip()
    if not api_key:
        raise RuntimeError("No Gemini API key was entered.")
    os.environ["GEMINI_API_KEY"] = api_key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an approval-gated AI coding loop against a repository.")
    parser.add_argument("--request", required=True, help="Natural-language software task")
    parser.add_argument("--repo", default="demo_repo", help="Existing repository directory to edit")
    parser.add_argument("--max-retries", type=int, default=1, help=f"Retry budget, 0-{MAX_RETRIES}")
    parser.add_argument("--patch-file", help="Optional path to a unified diff file. If omitted, the CLI asks interactively.")
    parser.add_argument("--use-llm", action="store_true", help="Use Google Gemini to generate patches")
    args = parser.parse_args(argv)
    if not 0 <= args.max_retries <= MAX_RETRIES:
        parser.error(f"--max-retries must be between 0 and {MAX_RETRIES}")
    if args.use_llm:
        try:
            _ensure_gemini_key()
        except RuntimeError as exc:
            parser.error(str(exc))

    repo_root = Path(args.repo).resolve()
    repository = RepositoryTools(repo_root)

    def patch_provider(task: dict[str, Any], attempt: int) -> str | None:
        if args.patch_file is not None:
            return _read_patch_from_file(task, attempt, args.patch_file)
        if args.use_llm:
            return _gemini_patch_provider(repo_root, args.request)(task, attempt)
        return _read_patch_from_stdin(task, attempt)

    try:
        mission = run_mission(
            args.request,
            patch_provider,
            _approval_provider(),
            repository=repository,
            max_retries=args.max_retries,
            activity_callback=lambda entry: print(f"[activity] {entry['event']} :: {entry.get('task_id', '')}"),
            clarification_provider=_clarification_provider,
        )
    except FirestoreError as exc:
        parser.error(str(exc))

    print("\n=== Mission summary ===")
    print(f"status: {mission['status']}")
    print(f"mission_id: {mission['mission_id']}")
    print(f"tasks: {len(mission.get('tasks', []))}")
    for result in mission.get("test_results") or []:
        print(f"- test {result.get('task_id')}: passed={result.get('passed')} exit={result.get('returncode')}")
    return 0 if mission["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
