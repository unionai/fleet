"""Exercise the live preview inside an outer controlling terminal."""

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

from installer_harness import KEY, ROOT, assert_cursor_visibility, fixture_environment, terminal_transcript
from preview_install import SCENARIOS, run_terminal


class PreviewTests(unittest.TestCase):
    def run_preview(self, arguments=(), answers=(), action=None, command=None):
        directory = tempfile.TemporaryDirectory(prefix="preview-test-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "tmp").mkdir()
        pid, master = pty.fork()
        if pid == 0:
            before = termios.tcgetattr(0)
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            env = {**os.environ, "TMPDIR": str(root / "tmp"), "PYTHONDONTWRITEBYTECODE": "1"}
            report = {}
            try:
                if command is None:
                    process = subprocess.Popen([sys.executable, str(ROOT / "tests/preview_install.py"),
                                                *arguments], env=env)
                    (root / "preview-pid").write_text(str(process.pid))
                    report["code"] = process.wait()
                else:
                    report["code"] = run_terminal(command(root), env)
            except BaseException as exc:
                report["error"] = repr(exc)
            after = termios.tcgetattr(0)
            # PENDIN records pending input state on macOS, not terminal configuration.
            for attributes in (before, after):
                attributes[3] &= ~getattr(termios, "PENDIN", 0)
            report["restored"] = before == after
            report["attributes"] = [(index, repr(left), repr(right)) for index, (left, right) in enumerate(zip(before, after)) if left != right]
            (root / "report.json").write_text(json.dumps(report))
            os._exit(0)
        output = bytearray()
        pending = list(answers)
        prompts = 0
        processes = set()
        deadline = time.monotonic() + 30
        status = None
        try:
            while time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], 0.05)
                if readable:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                    for process_file in (root / "tmp").glob("fleet-preview-*/ticker-processes-*.json"):
                        try:
                            processes.update(json.loads(process_file.read_text()).values())
                        except FileNotFoundError:
                            pass
                    count = output.count(b"[y/N] ")
                    if count > prompts and pending:
                        os.write(master, (pending.pop(0) + "\n").encode())
                        prompts = count
                    if action:
                        action(bytes(output), master, root, pid)
            while time.monotonic() < deadline:
                waited, value = os.waitpid(pid, os.WNOHANG)
                if waited:
                    status = value
                    break
                time.sleep(0.01)
            self.assertIsNotNone(status, f"preview hung: {output!r}")
        finally:
            if status is None:
                os.killpg(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
            os.close(master)
        report = json.loads((root / "report.json").read_text())
        self.assertNotIn("error", report, report)
        self.assertTrue(report["restored"], f"terminal attributes changed: {report['attributes']}; output: {output!r}")
        self.assertEqual(list((root / "tmp").iterdir()), [], "preview fixtures were left behind")
        for process_id in processes:
            with self.assertRaises(ProcessLookupError, msg=f"progress process {process_id} left behind"):
                os.kill(process_id, 0)
        for secret in (KEY, "PRIVATE-KEY-SENTINEL", "CLIENT-SECRET-SENTINEL", "ACCESS-TOKEN-SENTINEL"):
            self.assertNotIn(secret.encode(), output)
        if command is None:
            assert_cursor_visibility(self, output)
        return report["code"], bytes(output)

    def test_all_scenarios_and_source_modes(self):
        expected = {
            "success": (0, b"SUCCESS:"),
            "existing-agent": (0, b"not currently supported"),
            "slow": (0, b"[1m0s] Installing the cluster agent"),
            "helm-failure": (1, b"The agent Helm command failed"),
            "union-failure": (1, b"Union installation has failed"),
            "timeout": (1, b"Timed out after 75 seconds"),
            "interrupt": (130, b"Installed resources were preserved"),
        }
        for source in ("file", "stdin"):
            for scenario, (expected_code, marker) in expected.items():
                with self.subTest(source=source, scenario=scenario):
                    code, output = self.run_preview([scenario, "--source", source, "--yes", "--speed", "1000"])
                    self.assertEqual(code, expected_code, output)
                    self.assertIn(marker, output)
                    self.assertEqual(output.count(b"[y/N] "), 2 if scenario == "existing-agent" else 1)
                    if scenario == "slow":
                        for phase in (b"prepare agent instructions", b"cluster to connect", b"cluster to report healthy",
                                      b"Waiting for the cluster agent", b"Verifying 60 seconds", b"SUCCESS:"):
                            self.assertIn(phase, output)
                    if expected_code:
                        self.assertNotIn(b"SUCCESS:", output)

    def test_manual_answers_can_decline_either_confirmation(self):
        for source in ("file", "stdin"):
            for answers in (("no",), ("yes", "no")):
                with self.subTest(source=source, answers=answers):
                    code, output = self.run_preview(["existing-agent", "--source", source, "--speed", "1000"], answers)
                    self.assertEqual(code, 0, output)
                    self.assertIn(b"Cancelled; no installation changes made.", output)
                    self.assertNotIn(b"Installing the cluster agent", output)
                    self.assertEqual(output.count(b"[y/N] "), len(answers))

    def test_automatic_interruption_occurs_after_twenty_simulated_seconds(self):
        for source in ("file", "stdin"):
            for speed in (20, 1000):
                with self.subTest(source=source, speed=speed):
                    code, output = self.run_preview(["interrupt", "--source", source, "--yes", "--speed", str(speed)])
                    self.assertEqual(code, 130, output)
                    self.assertIn("[20s] Installing the cluster agent..\n", terminal_transcript(output))

    def test_manual_ctrl_c_and_external_term_restore_terminal(self):
        for source in ("file", "stdin"):
            for signum in (signal.SIGINT, signal.SIGTERM):
                with self.subTest(source=source, signum=signum):
                    triggered = False

                    def interrupt(output, master, root, pid):
                        nonlocal triggered
                        if not triggered and b"[10s] Installing the cluster agent." in output:
                            triggered = True
                            if signum == signal.SIGINT:
                                os.write(master, b"\x03")
                            else:
                                os.kill(int((root / "preview-pid").read_text()), signum)

                    code, output = self.run_preview(["success", "--source", source, "--yes", "--speed", "20"], action=interrupt)
                    self.assertTrue(triggered, output)
                    self.assertEqual(code, 128 + signum, output)
                    self.assertNotIn(b"SUCCESS:", output)

    def test_output_bytes_are_streamed_without_rewriting(self):
        payload = b"\x1b[2K\rfirst\rshort\x1b[0m"

        def command(root):
            code = (
                "import os, pathlib, time\n"
                f"os.write(1, {payload!r})\n"
                f"gate = pathlib.Path({str(root / 'continue')!r})\n"
                "deadline = time.monotonic() + 5\n"
                "while not gate.exists() and time.monotonic() < deadline: time.sleep(.01)\n"
                "raise SystemExit(0 if gate.exists() else 1)\n"
            )
            return [sys.executable, "-c", code]

        def release(output, master, root, pid):
            if payload in output:
                (root / "continue").touch()

        code, output = self.run_preview(command=command, action=release)
        self.assertEqual(code, 0, output)
        self.assertEqual(output, payload)

    def test_window_size_is_inherited_and_updates_on_resize(self):
        resized = False

        def command(root):
            code = (
                "import os, signal, time\n"
                "done = False\n"
                "def resized(signum, frame):\n"
                "    global done\n"
                "    print('RESIZED:%sx%s' % os.get_terminal_size(1), flush=True)\n"
                "    done = True\n"
                "signal.signal(signal.SIGWINCH, resized)\n"
                "print('INITIAL:%sx%s' % os.get_terminal_size(1), flush=True)\n"
                "deadline = time.monotonic() + 5\n"
                "while not done and time.monotonic() < deadline: time.sleep(.01)\n"
                "raise SystemExit(0 if done else 1)\n"
            )
            return [sys.executable, "-c", code]

        def resize(output, master, root, pid):
            nonlocal resized
            if not resized and b"INITIAL:" in output:
                resized = True
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 60, 0, 0))
                os.kill(pid, signal.SIGWINCH)

        code, output = self.run_preview(command=command, action=resize)
        self.assertEqual(code, 0, output)
        self.assertIn(b"INITIAL:80x24", output)
        self.assertIn(b"RESIZED:60x40", output)

    def test_pacing_preserves_simulated_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = fixture_environment(root, isolated=True, speed=5)
            started = time.monotonic()
            subprocess.run([str(root / "bin/sleep"), "1"], env=env, check=True, timeout=5)
            self.assertGreaterEqual(time.monotonic() - started, 0.18)
            self.assertEqual((root / "clock").read_text(), "1001")

    def test_fixture_uses_only_doubles_for_external_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = fixture_environment(root, isolated=True)
            self.assertEqual(env["PATH"], str(root / "bin"))
            self.assertEqual(env["FLYTE_API_KEY"], KEY)
            self.assertNotIn("FAKE_SPEED", env)
            for tool in ("curl", "kubectl", "helm", "date", "sleep"):
                self.assertEqual((root / "bin" / tool).resolve(), ROOT / "tests/fake_command.py")

    def test_help_listing_and_invalid_arguments_need_no_terminal(self):
        command = [sys.executable, str(ROOT / "tests/preview_install.py")]
        for argument in ("--help", "--list"):
            result = subprocess.run([*command, argument], stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            if argument == "--list":
                for name in SCENARIOS:
                    self.assertIn(name.encode(), result.stdout)
        for arguments in ([], ["unknown"], ["success"],
                          *(["success", "--speed", speed] for speed in ("0", "-1", "nan", "inf", "text"))):
            with self.subTest(arguments=arguments):
                result = subprocess.run([*command, *arguments], stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
