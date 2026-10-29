# DevPilot

DevPilot is a multi-agent AI software engineering system for PECathon PS-001.

## Project layout

- `devpilot/` — core package with JSON-backed mission state, structured planning, approval-gated patch proposals, read-only static review, and demo_repo-scoped repository tools.
- `devpilot/state/` — default location for persisted mission JSON files.
- `tests/` — focused automated tests.
- `demo_repo/` — small sample repository used for demonstrations.

## Setup and live demo

Create the Windows virtual environment, install dependencies, and run the full suite:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q tests
```

The task in `demo_repo/TASK.md` adds `greet(name)` to `sample.py`, returning `Hello, {name}!`. Run the CLI:

```powershell
.\.venv\Scripts\python.exe -m devpilot --request "Add a greet(name) function to sample.py that returns a personalized greeting." --max-retries 1
```

Approve the task. At each patch prompt, paste the diff and finish with a line containing only `.`; approve applying each patch. To demonstrate recovery, use this incorrect first patch:

```diff
--- a/sample.py
+++ b/sample.py
@@ -1,2 +1,5 @@
 # sample demo repository file
 message = "hello from demo_repo"
+
+def greet(name):
+    return f"Hi, {name}!"
```

The CLI records the failed test and asks for a retry. Paste this correction:

```diff
--- a/sample.py
+++ b/sample.py
@@ -5 +5 @@
-    return f"Hi, {name}!"
+    return f"Hello, {name}!"
```

Expected output includes a failed first test attempt, a retry notice, a passing second attempt, `Final review for task-1: pass`, and `Mission <id>: completed`. The CLI shows test output and review findings and returns success only when every task’s latest test and review pass. Test execution remains restricted to `demo_repo/`.

The Planner, Developer, read-only Reviewer, CLI orchestrator, and Streamlit dashboard provide the initial coding flow. No API key or external API is required.

## Streamlit dashboard

Install dependencies and launch the dashboard from the project root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run devpilot/gui.py
```

Enter a mission and select **Start Mission**. The light dashboard polls persisted mission state, accepts unified diffs, and routes task and patch approval or rejection through the existing orchestrator. Repository reads, patch validation, application, test scope, and retry bounds remain enforced by the backend.
