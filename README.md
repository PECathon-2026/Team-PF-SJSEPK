# DevPilot

DevPilot is a multi-agent AI software engineering system for PECathon PS-001.

## Project layout

- `devpilot/` — core package with demo_repo-scoped repository tools and JSON-backed shared mission state.
- `devpilot/state/` — default location for persisted mission JSON files.
- `tests/` — focused automated tests.
- `demo_repo/` — small sample repository used for demonstrations.

## Development

Install dependencies with `python -m pip install -r requirements.txt`, then run `python -m pytest -q tests`.

Agents and UI are not included yet.
