from __future__ import annotations

import os
import tempfile
from pathlib import Path


repo_root = Path(__file__).resolve().parent
local_temp = repo_root / ".pytest_local_temp"
local_temp.mkdir(exist_ok=True)

for env_var in ("TMPDIR", "TMP", "TEMP"):
    os.environ[env_var] = str(local_temp)

tempfile.tempdir = str(local_temp)
