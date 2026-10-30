# DevPilot · Shinobi Tech

DevPilot is an approval-gated, multi-role coding agent for small existing
Python repositories. It plans a request, inspects repository context, proposes
code, applies approved diffs, runs tests, performs an independent static review,
and feeds verification findings back into bounded retries.

The Streamlit command center uses an original Shinobi Tech visual identity:
obsidian surfaces, vermilion seals, parchment-toned dossier panels, and a
CSS-drawn insignia. It does not use external artwork, downloaded assets, or
copied franchise logos. Its navigation exposes command center, mission
scrolls, code changes, ANBU review, training results, intelligence activity,
and village settings. Mission metrics and evidence are read from persisted
mission state.

## Architecture

```text
User request
    |
    v
Clarifier -> Requirements analyst -> Repository scout -> Acceptance analyst
    |                 |                    |                     |
    +-----------------+---- shared mission state ----------------+
                                      |
                                      v
                           Developer (Gemini or human)
                                      |
                               human approval
                                      |
                                      v
                         Safe repository patch tools
                            /                \
                           v                  v
                    Static reviewer       Test sentinel
                           \                  /
                            -- findings -----
                                  |
                      shared agent message/retry
                                  |
                           Developer revision
```

### Roles and communication

- **Clarifier** stops on an obviously vague request and asks for a concrete
  behavior before planning.
- **Requirements analyst** decomposes a request into tasks and criteria.
- **Repository scout** searches the selected root and identifies relevant
  files.
- **Acceptance analyst** records a criterion for every task.
- **Developer** reads context and supplies a unified diff, using the configured
  Gemini model or an interactive human-provided diff.
- **Reviewer** independently checks the changed source for syntax, secrets,
  unsafe execution, SQL construction, and destructive operations.
- **Sentinel** runs the repository test suite with pytest hooks and third-party
  plugin autoload disabled.
- **Coordinator** persists agent activity, approvals, findings, test evidence,
  and reviewer/sentinel messages in Cloud Firestore mission state. A failed
  test or review is sent to the developer before the next retry.

Agents communicate through persisted mission findings and activity records;
the recovery path is not a sequence of unrelated persona prompts. The GUI
and CLI use Cloud Firestore by default. Local JSON remains available only
when explicitly selected, and a Firestore error never switches storage modes.

## Windows setup (PowerShell)

Run from the repository root. Python 3.10 or newer is required.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
```

## Gemini-only configuration

DevPilot uses Google Gemini for every model request. Create a Gemini API key
through Google AI Studio, then either add it to a local `.env` file (ignored by
Git) or set it in the process environment. Start from the checked-in template:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set `GEMINI_API_KEY` to your own key. Alternatively, set it for
the current PowerShell session:

```powershell
$env:GEMINI_API_KEY = "<your Gemini key>"
```

The model is controlled centrally by `GEMINI_MODEL`; its default is
`gemini-flash-latest`. No key is included in the repository or shown in the UI.
The CLI can also prompt for a missing key using hidden input; that key remains
in the current process and is not saved.

The dashboard reports a connection as **Not tested** until you explicitly
choose **Test Gemini connection** in Village Settings. A failed request reports
Gemini's HTTP status and error detail without revealing the credential.
Unavailable models, invalid keys, and quota failures do not trigger a fallback.
Model availability and quotas are controlled by Google and can change.

The deterministic mission planner and static independent reviewer do not make
model requests. Gemini is used for code generation and retry corrections, with
test/review evidence included in the follow-up prompt. Enabling generation
sends the request and relevant, secret-redacted source snippets to Google; do
not enable it for code you are not authorized to send externally.

## Cloud Firestore setup

DevPilot uses the Firebase Admin SDK and Cloud Firestore only. It does not use
Firebase Authentication, OAuth, Firebase Analytics, or a browser-side Firebase
SDK. The project ID defaults to `devpilot-shinobi-tech`; the Firebase web-app
configuration and its public API key are not used by this Python backend.

1. In the Firebase console, select `devpilot-shinobi-tech` and create a
   Firestore database in Native mode.
2. Create/download a Firebase **Admin SDK service-account JSON** for the
   project. Keep the file outside the repository; never commit or paste it
   into source code.
3. Grant that service account only the Firestore read/write IAM permissions
   DevPilot needs.
4. Configure credentials using either Streamlit secrets or environment
   variables.

For Streamlit, create `.streamlit/secrets.toml` (ignored by Git) with the
following shape, copying the values from the downloaded service-account JSON:

```toml
[firebase]
project_id = "devpilot-shinobi-tech"

[firebase.service_account]
type = "service_account"
project_id = "devpilot-shinobi-tech"
private_key_id = "..."
private_key = """-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----
"""
client_email = "..."
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
```

For the CLI or another server environment, point to the credential file:

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\secure\devpilot-service-account.json"
$env:FIREBASE_PROJECT_ID = "devpilot-shinobi-tech"
```

Alternatively, `FIREBASE_SERVICE_ACCOUNT_FILE` accepts the same file path, or
`FIREBASE_SERVICE_ACCOUNT_JSON` accepts the full service-account JSON from a
secret manager. Never put credential values in a checked-in `.env` file.
`DEVPILOT_STORAGE_BACKEND` defaults to `firestore`; set it to `json` only to
explicitly use the preserved local JSON backend. When Firestore is selected,
missing credentials, invalid credentials, permission errors, and service
outages are reported; they do not fall back to JSON. Village Settings includes
an explicit **Test Firestore connection** action and never displays credential
values.

### Firestore schema

Each mission is stored in `devpilot_missions/{mission_id}`. Its document holds
the request, status, retry count, schema version, and child-record counts.
Unbounded records use subcollections:

- `tasks/{index}` — planned task details and acceptance criteria.
- `findings/{index}` — task approvals, proposed diffs, and independent reviews.
- `approvals/{index}` — queryable copies of task-approval and patch-decision
  findings.
- `activity/{index}` — timestamped mission activity.
- `test_results/{index}` — test output and pass/fail evidence.

Existing missions in `devpilot/state/` are not deleted or automatically
uploaded. To copy them once after configuring Firestore, run:

```powershell
.\.venv\Scripts\python.exe -m devpilot.migrate_firestore
```

The migration skips mission IDs already present in Firestore, keeps all local
JSON files, and can be safely rerun. Use `--state-dir` to migrate a different
JSON state directory. Review the migration output and confirm the records in
the Firebase console before removing local data yourself.

To deliberately run against local JSON instead of Firestore:

```powershell
$env:DEVPILOT_STORAGE_BACKEND = "json"
```

The setting applies to both the Streamlit app and CLI. Restart the process
after changing storage configuration.

## Run a coding mission

The default demonstration repository is `demo_repo/`. A different existing
repository can be selected explicitly; all repository file operations remain
confined to that resolved root.

Interactive, human-supplied patch:

```powershell
.\.venv\Scripts\python.exe autonomous_coder.py --request "Add a greeting helper" --repo demo_repo
```

Gemini-generated patch:

```powershell
.\.venv\Scripts\python.exe autonomous_coder.py --request "Add a greeting helper" --repo demo_repo --use-llm --max-retries 2
```

Both modes require separate human approval of the task and every proposed
patch. The LLM only proposes a diff; DevPilot validates the target, previews
it in the dashboard or displays it in the CLI, and does not apply it until
approval. The bounded retry limit is 0–3. Test output and reviewer findings
are included in the next model prompt after a failed attempt.

## Streamlit command center

```powershell
.\.venv\Scripts\python.exe -m streamlit run devpilot\gui.py
```

Enter a mission briefing and optionally enable **AI-generated patch**. The
dashboard and CLI use the same Gemini configuration, and its key is never
displayed in the UI. Each task and every proposed patch still require explicit user
authorization. The workspace provides mission history, diff and before/after
views, independent review findings, test output, and a timestamped activity
timeline. Mission state is persisted in Firestore by default. Repository
editing remains restricted to `demo_repo/`.

The command palette opens from its visible control and filters the available
workspace sections. A browser-level Ctrl+K shortcut is not installed because
the application uses Streamlit's supported controls without injecting page
scripts.

## Demonstration and evaluation scenarios

Run the end-to-end feedback-loop demonstration:

```powershell
.\.venv\Scripts\python.exe -m devpilot.demo --log demo_run_log.json
```

Approve the task and both patch attempts when prompted. The first proposed
greeting deliberately fails its real pytest check. The sentinel records the
failure, sends its output to the developer, and the recovery patch changes the
greeting based on that evidence. The command writes `demo_run_log.json` only
after the run, including approvals-related activity, both test results, the
agent message, and the final review.

The tests cover feature implementation, a seeded test failure and recovery,
patch approval and rejection, path traversal, security-review detections,
ambiguous requests, unavailable patch generation, bounded repeated failures,
mission persistence, and GUI rendering:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Available tools and boundaries

- `repository.search` and `repository.read_file`: read-only access under the
  selected repository root.
- `repository.validate_patch` and `repository.apply_patch`: a single-file
  unified diff must target a path within that root; the patch is validated
  before human approval and application. Existing files can be modified and
  new files created; deletions are rejected.
- `repository.run_tests`: runs `python -m pytest` with shell execution disabled,
  a 120-second timeout, an isolated empty pytest config, conftest loading
  disabled, plugin autoload disabled, and a temporary copy of the selected
  repository (generated files, local secrets, and symlinks are excluded).
- Gemini `generateContent`: opt-in external model calls. The API key is sent
  only in Google's API-key header. Likely credentials in request/context
  snippets are redacted before the model prompt.

Patch paths are checked against traversal and symlink escapes, and high-risk
review findings block test execution. Test runs operate on a temporary copy so
ordinary test writes cannot modify the selected working tree. This is not an
OS sandbox: malicious Python tests could still access external paths or
processes. Do not run untrusted code. The application does not run arbitrary
shell commands or perform a Git commit/merge. Static security checks are
heuristics and are not a substitute for a full security audit.

## Project layout

- `devpilot/` — planner, code generation, centralized Gemini configuration, safe repository
  tools, reviewer, coordinator, shared mission state, Firestore persistence, demo, and dashboard.
- `devpilot/firestore_store.py` — Firebase Admin initialization and Firestore
  mission/subcollection persistence.
- `devpilot/state/` — preserved local JSON mission records and explicit local
  development storage.
- `tests/` — unit, integration, GUI, and evaluation-scenario tests.
- `demo_repo/` — default controlled repository for the sample workflow.
