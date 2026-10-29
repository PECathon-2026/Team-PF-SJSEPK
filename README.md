# DevPilot

DevPilot is a local, approval-gated coding workflow for the PECathon PS-001
sample repository. It plans and decomposes a request, gathers relevant files,
requests human approval before each task and code change, runs repository tests,
and records independent review and security findings in shared mission state.

Planning uses three deterministic local roles (requirements analyst, repository
scout, and acceptance-criteria analyst); it does not call an LLM or claim
autonomous code generation. A developer supplies each proposed unified diff.
Repository reads, writes, and test discovery are constrained to `demo_repo/`.
Pytest configuration hooks, `conftest.py` files, and third-party pytest plugin
autoload are disabled for mission test runs. Pytest and collected test code still
execute as local Python processes (not an OS sandbox), so only approve and test
trusted changes.

## Windows setup (PowerShell)

Run these commands from the repository root. Python 3.10 or newer is required.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run the complete collected suite, including the demo repository tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Run the real failure-recovery end-to-end scenario. The test creates an isolated
temporary repository, applies an intentionally incorrect greeting, observes the
real pytest failure, applies the correction, and requires passing tests and an
independent review before reporting completion:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_orchestrator.py::test_cli_end_to_end_failure_recovery_and_final_review
```

## Run a CLI mission

The CLI operates on the checked-in `demo_repo/`:

```powershell
.\.venv\Scripts\python.exe -m devpilot --request "Update sample.py to add a new feature." --max-retries 1
```

Review the proposed task and approve it with `y`. Paste a unified diff at each
patch prompt and finish the diff with a line containing only `.`. Review and
approve each patch before it is applied. A failed test or review can be
corrected up to the configured retry count (`0` through `3`). A mission is
reported as completed only when every task has passing tests and a passing
review.

## Run the Streamlit dashboard

```powershell
.\.venv\Scripts\python.exe -m streamlit run devpilot\gui.py
```

Enter a request and select **Start Mission**. The dashboard exposes the bounded
recovery count, task approval, a patch editor, a before/after diff preview and
explicit patch approval. Use **Cancel Mission** to stop at the next safe
checkpoint. Agent activity, test output, review/security findings, and persisted
mission status are shown as the worker progresses.

## Project layout

- `devpilot/` — planner, developer, safe repository tools, orchestrator,
  independent reviewer, mission persistence, and Streamlit interface.
- `devpilot/state/` — default location for JSON-backed shared mission records.
- `tests/` — unit, workflow, GUI, and end-to-end recovery tests.
- `demo_repo/` — the only repository scope the built-in tools can access.
