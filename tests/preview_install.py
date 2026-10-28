#!/usr/bin/env python3
"""Run the local installer in a live terminal using offline command doubles."""

import argparse
import errno
import fcntl
import math
import os
from pathlib import Path
import pty
import select
import signal
import sys
import tempfile
import termios
import time
import tty

from installer_harness import BASH, ROOT, fixture_environment

SCENARIOS = {
    "success": "Successful install and stability verification",
    "existing-agent": "Existing-resource warning and second confirmation",
    "slow": "Delayed instructions, elapsed timers, and changing readiness",
    "helm-failure": "Agent Helm failure with diagnostics",
    "union-failure": "Union deployment failure after agent installation",
    "timeout": "Unchanged status and a 75-second simulated timeout",
    "interrupt": "Automatic Ctrl-C after two Helm progress ticks",
}


def scenario_options(name):
    options = {"helm_ticks": 2}
    timeout = None
    if name == "existing-agent":
        options["existing"] = True
    elif name == "slow":
        complete = {"state": "LIFECYCLE_STATE_COMPLETED", "result": "LIFECYCLE_RESULT_SUCCESS"}
        options.update(instructions_unavailable=13, helm_ticks=7, agent_ready_after=3,
                       status_sequence=[
                           {"agentInstallation": {"state": "LIFECYCLE_STATE_INSTALLING"}},
                           {"unionInstallation": {"state": "LIFECYCLE_STATE_INSTALLING"}},
                           {"unionInstallation": complete, "connectivity": "CLUSTER_CONNECTIVITY_DISCONNECTED", "health": 0},
                           {"unionInstallation": complete, "health": "CLUSTER_HEALTH_UNHEALTHY"},
                           {"unionInstallation": complete},
                       ])
    elif name == "helm-failure":
        options["helm_failure"] = True
    elif name == "union-failure":
        options["union_failure"] = True
    elif name == "timeout":
        options["wait_forever"] = True
        timeout = 75
    elif name == "interrupt":
        options.update(helm_ticks=2, helm_hold_after_ticks=True)
    return options, timeout


def write_all(fd, data):
    remaining = memoryview(data)
    while remaining:
        remaining = remaining[os.write(fd, remaining):]


def copy_window_size(source, target):
    size = fcntl.ioctl(source, termios.TIOCGWINSZ, b"\0" * 8)
    fcntl.ioctl(target, termios.TIOCSWINSZ, size)


def signal_group(pid, signum):
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        pass


def run_terminal(command, env, *, automatic=False, source=None, interrupt_root=None):
    """Relay PTY bytes without decoding or applying output transformations."""
    attributes = termios.tcgetattr(0)
    window_size = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)
    source_read = source_write = None
    if source is not None:
        source_read, source_write = os.pipe()
    pid, master = pty.fork()
    if pid == 0:
        try:
            termios.tcsetattr(0, termios.TCSANOW, attributes)
            fcntl.ioctl(0, termios.TIOCSWINSZ, window_size)
            if source_read is not None:
                os.close(source_write)
                os.dup2(source_read, 0)
                os.close(source_read)
            if "FAKE_ROOT" in env:
                env = {**env, "FAKE_INSTALLER_PID": str(os.getpid()),
                       "FAKE_REDRAW": "1" if env.get("TERM") != "dumb" else "0"}
            os.execve(command[0], command, env)
        except Exception as exc:
            print(f"Cannot start preview: {exc}", file=sys.stderr, flush=True)
            os._exit(1)
    if source_read is not None:
        os.close(source_read)
        os.set_blocking(source_write, False)
    original_handlers = {}
    status = None
    prompt_tail = b""
    prompt = b"[y/N] "
    source_offset = 0
    interrupted = False
    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            original_handlers[signum] = signal.signal(signum, lambda received, frame: signal_group(pid, received))
        original_handlers[signal.SIGWINCH] = signal.signal(
            signal.SIGWINCH, lambda received, frame: copy_window_size(0, master))
        copy_window_size(0, master)
        tty.setraw(0, when=termios.TCSANOW)
        while True:
            readable, writable, _ = select.select(
                [master, 0], [source_write] if source_write is not None else [], [], 0.05)
            if source_write in writable:
                try:
                    source_offset += os.write(source_write, source[source_offset:source_offset + 65536])
                except BrokenPipeError:
                    source_offset = len(source)
                if source_offset == len(source):
                    os.close(source_write)
                    source_write = None
            if master in readable:
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                write_all(1, chunk)
                if automatic:
                    pending = prompt_tail + chunk
                    for _ in range(pending.count(prompt)):
                        write_all(master, b"yes\n")
                    prompt_tail = pending[-(len(prompt) - 1):]
            if 0 in readable:
                chunk = os.read(0, 65536)
                if not chunk:
                    signal_group(pid, signal.SIGHUP)
                    break
                write_all(master, chunk)
            if interrupt_root is not None and not interrupted:
                if (interrupt_root / "helm-paused").exists():
                    signal_group(pid, signal.SIGINT)
                    interrupted = True
        _, status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(status)
        return code if code >= 0 else 128 - code
    finally:
        try:
            signal_group(pid, signal.SIGTERM)
            if status is None:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    waited, status = os.waitpid(pid, os.WNOHANG)
                    if waited:
                        break
                    status = None
                    time.sleep(0.01)
            signal_group(pid, signal.SIGKILL)
            if status is None:
                os.waitpid(pid, 0)
        finally:
            for signum, handler in original_handlers.items():
                signal.signal(signum, handler)
            os.close(master)
            if source_write is not None:
                os.close(source_write)
            termios.tcsetattr(0, termios.TCSANOW, attributes)


def positive_speed(value):
    try:
        speed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("speed must be a positive finite number") from None
    if not math.isfinite(speed) or speed <= 0:
        raise argparse.ArgumentTypeError("speed must be a positive finite number")
    return speed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", choices=SCENARIOS)
    parser.add_argument("--list", action="store_true", help="list available mock scenarios")
    parser.add_argument("--yes", action="store_true", help="automatically answer confirmation prompts")
    parser.add_argument("--speed", type=positive_speed, default=10, help="simulation speed multiplier (default: 10; normal timing: 1)")
    parser.add_argument("--source", choices=("file", "stdin"), default="file", help="run the local script as a file or pipe its source to Bash")
    args = parser.parse_args()
    if args.list:
        for name, description in SCENARIOS.items():
            print(f"{name:16} {description}")
        return 0
    if args.scenario is None:
        parser.error("choose a scenario, or use --list")
    if not os.isatty(0) or not os.isatty(1):
        parser.error("a live terminal is required for stdin and stdout")
    options, timeout = scenario_options(args.scenario)
    try:
        with tempfile.TemporaryDirectory(prefix="fleet-preview-") as directory:
            root = Path(directory)
            env = fixture_environment(root, options, isolated=True, speed=args.speed)
            command = [BASH, *(["-s", "--"] if args.source == "stdin" else [str(ROOT / "install.sh")]),
                       "--cluster", "test-cluster"]
            if timeout is not None:
                command.extend(["--wait-timeout", str(timeout)])
            return run_terminal(command, env, automatic=args.yes,
                                source=(ROOT / "install.sh").read_bytes() if args.source == "stdin" else None,
                                interrupt_root=root if args.scenario == "interrupt" else None)
    except (OSError, RuntimeError) as exc:
        print(f"Preview failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
