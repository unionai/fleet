"""Shared synthetic environment for installer tests and terminal previews."""

import base64
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
BASH = os.environ.get("BASH_BIN", "/bin/bash")
KEY = base64.b64encode(b"control.example.test:client:CLIENT-SECRET-SENTINEL:test-org").decode()


def fixture_environment(root, options=None, key=KEY, *, isolated=False, speed=None):
    options = options or {}
    (root / "bin").mkdir()
    (root / "tmp").mkdir()
    (root / "options.json").write_text(json.dumps(options))
    for tool in ("curl", "kubectl", "helm", "date", "sleep"):
        (root / "bin" / tool).symlink_to(ROOT / "tests/fake_command.py")
    (root / "bin" / "python3").symlink_to(sys.executable)
    if isolated or options.get("missing_tool"):
        for tool in ("bash", "jq", "base64", "tr", "mktemp", "cp", "rm", "cat"):
            if tool == options.get("missing_tool"):
                continue
            executable = shutil.which(tool)
            if executable is None:
                raise RuntimeError(f"Required preview tool is missing: {tool}")
            (root / "bin" / tool).symlink_to(executable)
        search_path = str(root / "bin")
    else:
        search_path = f"{root / 'bin'}:{os.environ['PATH']}"
    env = {**os.environ, "PATH": search_path, "FAKE_ROOT": str(root),
           "FLYTE_API_KEY": key, "TMPDIR": str(root / "tmp")}
    env.pop("FAKE_SPEED", None)
    if speed is not None:
        env["FAKE_SPEED"] = str(speed)
    return env
