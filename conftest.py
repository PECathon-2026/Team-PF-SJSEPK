from __future__ import annotations

import os
import tempfile
from pathlib import Path


repo_root = Path(__file__).resolve().parent
local_temp = repo_root / ".devpilot-pytest-temp"
local_temp.mkdir(exist_ok=True)
os.environ["DEVPILOT_STORAGE_BACKEND"] = "json"
os.environ["DEVPILOT_DISABLE_DOTENV"] = "1"
os.environ["DEVPILOT_STATE_DIR"] = str(local_temp / "mission-state")

for env_var in ("TMPDIR", "TMP", "TEMP"):
    os.environ[env_var] = str(local_temp)

tempfile.tempdir = str(local_temp)
