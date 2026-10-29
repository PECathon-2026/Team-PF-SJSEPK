"""Small, demo_repo-scoped repository operations."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


class RepositoryTools:
    def __init__(self, repo_root: str | Path | None = None):
        default_root = Path(__file__).resolve().parent.parent / "demo_repo"
        self.repo_root = Path(repo_root or default_root).resolve()
        if self.repo_root.name.casefold() != "demo_repo" or not self.repo_root.is_dir():
            raise ValueError("repository root must be the demo_repo directory")

    def _path(self, relative_path: str) -> Path:
        candidate = (self.repo_root / relative_path).resolve()
        try:
            candidate.relative_to(self.repo_root)
        except ValueError as exc:
            raise ValueError("path must stay inside demo_repo") from exc
        if candidate == self.repo_root:
            raise ValueError("a file path is required")
        return candidate

    def read_file(self, relative_path: str) -> str:
        path = self._path(relative_path)
        if not path.is_file():
            raise FileNotFoundError(relative_path)
        return path.read_text(encoding="utf-8")

    def search(self, text: str) -> list[tuple[str, int, str]]:
        matches = []
        for path in sorted(self.repo_root.rglob("*")):
            if not path.is_file():
                continue
            resolved = path.resolve()
            try:
                rel = resolved.relative_to(self.repo_root).as_posix()
            except ValueError:
                continue
            try:
                lines = resolved.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            matches.extend((rel, number, line) for number, line in enumerate(lines, 1) if text in line)
        return matches

    def apply_patch(self, patch: str) -> None:
        lines = patch.splitlines(keepends=True)
        if len(lines) < 3 or not lines[0].startswith("--- ") or not lines[1].startswith("+++ "):
            raise ValueError("expected a unified diff for one existing file")
        old_name, new_name = lines[0][4:].strip(), lines[1][4:].strip()
        if old_name.startswith("a/"):
            old_name = old_name[2:]
        if new_name.startswith("b/"):
            new_name = new_name[2:]
        if old_name != new_name or old_name in ("/dev/null", ""):
            raise ValueError("patch must modify one existing file")
        path = self._path(old_name)
        original = path.read_text(encoding="utf-8")
        source = original.splitlines(keepends=True)
        output: list[str] = []
        cursor = 0
        i = 2
        while i < len(lines):
            header = lines[i]
            if not header.startswith("@@"):
                raise ValueError("malformed unified diff")
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", header)
            if not match:
                raise ValueError("malformed hunk header")
            i += 1
            start = max(int(match.group(1)) - 1, 0)
            if start < cursor:
                raise ValueError("overlapping or unordered hunks")
            output.extend(source[cursor:start])
            cursor = start
            while i < len(lines) and not lines[i].startswith("@@"):
                row = lines[i]
                i += 1
                if row.startswith("\\ No newline"):
                    continue
                if not row or row[0] not in " +-":
                    raise ValueError("malformed patch line")
                content = row[1:]
                if row[0] == " ":
                    if cursor >= len(source) or source[cursor] != content:
                        raise ValueError("patch context does not match file")
                    output.append(content)
                    cursor += 1
                elif row[0] == "-":
                    if cursor >= len(source) or source[cursor] != content:
                        raise ValueError("patch content does not match file")
                    cursor += 1
                else:
                    output.append(content)
        output.extend(source[cursor:])
        path.write_text("".join(output), encoding="utf-8")

    def run_tests(self) -> subprocess.CompletedProcess[str]:
        """Run pytest only in demo_repo, with a fixed command and no shell."""
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(self.repo_root)],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=120,
        )
