"""Shared synthetic environment for installer tests and terminal previews."""

import base64
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys

ROOT = Path(__file__).resolve().parents[1]
BASH = os.environ.get("BASH_BIN", "/bin/bash")
KEY = base64.b64encode(b"control.example.test:client:CLIENT-SECRET-SENTINEL:test-org").decode()


def terminal_transcript(output):
    """Apply the installer's carriage returns and erase-to-end redraws."""
    lines, line, cursor = [], [], 0
    for token in re.split(r"(\x1b\[J|\x1b\[\?25[lh]|\r|\n)", output.decode(errors="replace")):
        if token == "\x1b[J":
            del line[cursor:]
        elif token in ("\x1b[?25l", "\x1b[?25h"):
            continue
        elif token == "\r":
            cursor = 0
        elif token == "\n":
            lines.append("".join(line) + "\n")
            line, cursor = [], 0
        else:
            for character in token:
                if cursor < len(line):
                    line[cursor] = character
                else:
                    line.append(character)
                cursor += 1
    return "".join(lines) + "".join(line)


def progress_frames(output):
    return re.findall(rb"\r\x1b\[J([^\r\n]*)\r", output)


def assert_cursor_visibility(test, output):
    visible = True
    termination_notice = signal.strsignal(signal.SIGTERM).encode()
    for match in re.finditer(rb"\x1b\[\?25[lh]|\r\x1b\[J[^\r\n]*|[^\x1b\r\n]+", output):
        token = match.group()
        if token == b"\x1b[?25l":
            test.assertTrue(visible, "cursor was hidden twice without restoration")
            visible = False
        elif token == b"\x1b[?25h":
            test.assertFalse(visible, "cursor restoration was repeated")
            visible = True
        elif token.startswith(b"\r\x1b[J"):
            test.assertFalse(visible, f"cursor visible during progress: {token!r}")
        elif token != b"^C" and termination_notice not in token:
            test.assertTrue(visible, f"cursor hidden during ordinary output: {token!r}")
    test.assertTrue(visible, "cursor left hidden on exit")


def fixture_environment(root, options=None, key=KEY, *, isolated=False, speed=None):
    options = options or {}
    (root / "bin").mkdir()
    (root / "tmp").mkdir()
    (root / "options.json").write_text(json.dumps(options))
    for tool in ("curl", "kubectl", "helm", "date", "sleep"):
        (root / "bin" / tool).symlink_to(ROOT / "tests/fake_command.py")
    (root / "bin" / "python3").symlink_to(sys.executable)
    if options.get("stty_error"):
        (root / "bin" / "stty").symlink_to(ROOT / "tests/fake_command.py")
    if isolated or options.get("missing_tool"):
        for tool in ("bash", "jq", "base64", "tr", "mktemp", "cp", "rm", "cat"):
            if tool == options.get("missing_tool"):
                continue
            executable = shutil.which(tool)
            if executable is None:
                raise RuntimeError(f"Required preview tool is missing: {tool}")
            (root / "bin" / tool).symlink_to(executable)
        if not options.get("stty_error") and options.get("missing_tool") != "stty":
            executable = shutil.which("stty")
            if executable:
                (root / "bin" / "stty").symlink_to(executable)
        search_path = str(root / "bin")
    else:
        search_path = f"{root / 'bin'}:{os.environ['PATH']}"
    env = {**os.environ, "PATH": search_path, "FAKE_ROOT": str(root),
           "FLYTE_API_KEY": key, "TMPDIR": str(root / "tmp"),
           "TERM": options.get("term", "xterm-256color")}
    env.pop("FAKE_SPEED", None)
    if speed is not None:
        env["FAKE_SPEED"] = str(speed)
    return env
