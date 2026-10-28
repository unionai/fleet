#!/usr/bin/env python3
"""Exercise the real installer with a controlling terminal and offline doubles."""

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time
import unittest

from installer_harness import (BASH, KEY, ROOT, assert_cursor_visibility, fixture_environment,
                               progress_frames, terminal_transcript)

REAL_HELM = shutil.which("helm")


class DurationTests(unittest.TestCase):
    def test_compact_duration_boundaries_always_keep_seconds(self):
        definitions = (ROOT / "install.sh").read_text().rsplit('\nmain "$@"', 1)[0]
        result = subprocess.run(
            [BASH, "-c", definitions + '\nfor value in "$@"; do duration "$value"; printf "\\n"; done',
             "duration", "-1", "0", "59", "60", "64", "3601"],
            capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(result.stdout.splitlines(), ["0s", "0s", "59s", "1m0s", "1m4s", "60m1s"])


class InstallerTests(unittest.TestCase):
    def run_installer(self, options=None, answers=("yes", "yes"), timeout=None, tty=True,
                      arguments=None, key=KEY, interrupt=False, buffered_stdout=False, piped_script=False,
                      columns=120, action=None):
        directory = tempfile.TemporaryDirectory(prefix="fleet-test-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        env = fixture_environment(root, options, key)
        env["FAKE_REDRAW"] = "1" if (tty and not buffered_stdout and columns >= 11 and
                                            env["TERM"] != "dumb" and
                                            not (options or {}).get("stty_error") and
                                            (options or {}).get("missing_tool") != "stty") else "0"
        default_arguments = ["--cluster", "test-cluster"]
        if timeout is not None:
            default_arguments += ["--wait-timeout", str(timeout)]
        command = [BASH, *(["-s", "--"] if piped_script else [str(ROOT / "install.sh")]),
                   *(arguments if arguments is not None else default_arguments)]
        output = bytearray()
        if tty:
            if piped_script:
                source_read, source_write = os.pipe()
            pid, fd = pty.fork()
            if pid == 0:
                fcntl.ioctl(1, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))
                if piped_script:
                    os.close(source_write)
                    os.dup2(source_read, 0)
                    os.close(source_read)
                if buffered_stdout:
                    read_fd, write_fd = os.pipe()
                    child = os.fork()
                    if child == 0:
                        os.dup2(write_fd, 1)
                        os.close(read_fd)
                        os.close(write_fd)
                        env["FAKE_INSTALLER_PID"] = str(os.getpid())
                        os.execve(BASH, command, env)
                    os.close(write_fd)
                    # Delay forwarding stdout until exit to model a slow tee.
                    _, child_status = os.waitpid(child, 0)
                    while True:
                        chunk = os.read(read_fd, 65536)
                        if not chunk:
                            break
                        os.write(1, chunk)
                    os.close(read_fd)
                    child_code = os.waitstatus_to_exitcode(child_status)
                    os._exit(child_code if child_code >= 0 else 128 - child_code)
                env["FAKE_INSTALLER_PID"] = str(os.getpid())
                os.execve(BASH, command, env)
            if piped_script:
                os.close(source_read)
                with os.fdopen(source_write, "wb") as source_pipe:
                    source_pipe.write((ROOT / "install.sh").read_bytes())
            pending = list(answers)
            prompts = 0
            sent_signal = False
            finish_by = time.monotonic() + 30
            status = None
            try:
                while time.monotonic() < finish_by:
                    readable, _, _ = select.select([fd], [], [], 0.05)
                    if readable:
                        try:
                            chunk = os.read(fd, 65536)
                        except OSError as exc:
                            if exc.errno != errno.EIO:
                                raise
                            break
                        if not chunk:
                            break
                        output.extend(chunk)
                        count = output.count(b"[y/N]")
                        if count > prompts and pending:
                            os.write(fd, (pending.pop(0) + "\n").encode())
                            prompts = count
                    if action:
                        action(bytes(output), fd, root)
                    if not sent_signal:
                        if interrupt in ("helm-term", "helm-int") and (root / "helm-started").exists() and list(root.glob("ticker-processes-*.json")):
                            os.killpg(pid, signal.SIGINT if interrupt == "helm-int" else signal.SIGTERM)
                            sent_signal = True
                        elif interrupt is True and (root / "installed").exists():
                            os.kill(pid, signal.SIGTERM)
                            sent_signal = True
                    waited, value = os.waitpid(pid, os.WNOHANG)
                    if waited:
                        status = value
                        # Drain output buffered between the final read and exit.
                        while select.select([fd], [], [], 0)[0]:
                            try:
                                chunk = os.read(fd, 65536)
                            except OSError:
                                break
                            if not chunk:
                                break
                            output.extend(chunk)
                        break
                if status is None:
                    waited = 0
                    while not waited and time.monotonic() < finish_by:
                        waited, value = os.waitpid(pid, os.WNOHANG)
                        if not waited:
                            time.sleep(0.005)
                    if not waited:
                        os.kill(pid, signal.SIGKILL)
                        _, value = os.waitpid(pid, 0)
                        self.fail(f"installer hung:\n{output.decode(errors='replace')}")
                    status = value
            finally:
                os.close(fd)
            code = os.waitstatus_to_exitcode(status)
        else:
            process = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, start_new_session=True, timeout=15)
            code = process.returncode
            output.extend(process.stdout)
        self.last_output = bytes(output)
        assert_cursor_visibility(self, output)
        text = terminal_transcript(output)
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()] \
            if (root / "calls.jsonl").exists() else []
        for secret in ("PRIVATE-KEY-SENTINEL", "CLIENT-SECRET-SENTINEL", "ACCESS-TOKEN-SENTINEL",
                       KEY, "LIFECYCLE_STATE_", "CLUSTER_CONNECTIVITY_", "CLUSTER_HEALTH_"):
            self.assertNotIn(secret.encode(), output)
        if env["FAKE_REDRAW"] == "0":
            self.assertNotIn(b"\x1b", output)
            self.assertNotIn(b"\r", output.replace(b"\r\n", b"\n"))
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\r", text)
        for process_file in root.glob("ticker-processes-*.json"):
            for process_id in json.loads(process_file.read_text()).values():
                with self.assertRaises(ProcessLookupError, msg=f"progress process {process_id} left behind"):
                    os.kill(process_id, 0)
        for call in calls:
            self.assertNotIn("CLIENT-SECRET-SENTINEL", json.dumps(call["args"]))
            self.assertNotIn("ACCESS-TOKEN-SENTINEL", json.dumps(call["args"]))
        self.assertEqual(list((root / "tmp").glob("union-fleet.*")), [], f"temporary credentials left behind: {text}")
        return code, text, calls, root

    @staticmethod
    def methods(calls):
        return [item["args"][-1].rsplit("/", 1)[-1] for item in calls if item["tool"] == "curl"]

    def test_fresh_install_on_helm_3_and_4(self):
        for version in ("v3.19.0", "v4.2.4", "v4.3.1"):
            with self.subTest(version=version):
                code, text, calls, root = self.run_installer({"helm_version": version})
                self.assertEqual(code, 0, text)
                self.assertIn("SUCCESS", text)
                self.assertIn("Starting Union deployment", text)
                self.assertEqual((root / "apply-count").read_text(), "1")
                self.assertIn("StartAgentInstallation", self.methods(calls))
                self.assertGreaterEqual(int((root / "readiness-time").read_text()) -
                                        int((root / "helm-time").read_text()), 60)
                installs = [item for item in calls if item["tool"] == "helm" and item["args"][0] == "upgrade"]
                self.assertEqual(len(installs), 1)
                self.assertEqual(installs[0]["context"], "test-context")
                self.assertIn("1.99.7", installs[0]["args"])

    def test_existing_release_is_confirmed_then_updated(self):
        code, text, calls, _ = self.run_installer({"existing": True})
        self.assertEqual(code, 0, text)
        self.assertIn("not currently supported", text)
        self.assertIn("namespace dataplane-agent", text)
        self.assertEqual(text.count("[y/N]"), 2)
        self.assertIn("GetAgentInstallationInstructions", self.methods(calls))
        self.assertNotIn("StartAgentInstallation", self.methods(calls))

    def assert_inspection_only(self, calls, root):
        self.assertEqual(self.methods(calls), [])
        self.assertFalse((root / "installed").exists())
        self.assertFalse((root / "applied").exists())
        for call in calls:
            if call["tool"] == "helm":
                self.assertIn(call["args"][0], ("version", "list"))
            if call["tool"] == "kubectl":
                if call["args"] == ["config", "current-context"]:
                    continue
                self.assertIn(call["args"][3], ("config", "get", "auth"))

    def test_any_helm_release_requires_warning_and_confirmation(self):
        for status in ("deployed", "failed", "pending-upgrade", "uninstalled"):
            with self.subTest(status=status):
                release = {"name": "previous-agent", "namespace": "dataplane-agent", "status": status}
                code, text, calls, root = self.run_installer({"releases": [release]}, answers=("yes", "no"))
                self.assertEqual(code, 0, text)
                self.assertIn("not currently supported", text)
                self.assertEqual(text.count("[y/N]"), 2)
                self.assertIn("Cancelled", text)
                self.assert_inspection_only(calls, root)

    def test_workloads_without_helm_records_require_confirmation(self):
        for kind in ("Pod", "Deployment", "DaemonSet", "StatefulSet"):
            with self.subTest(kind=kind):
                workload = {"kind": kind, "metadata": {"name": "previous-agent", "namespace": "dataplane-agent"},
                            "spec": {"replicas": 0}, "status": {"phase": "Failed"}}
                code, text, calls, root = self.run_installer({"workloads": [workload]}, answers=("yes", "no"))
                self.assertEqual(code, 0, text)
                self.assertIn("not currently supported", text)
                self.assertEqual(text.count("[y/N]"), 2)
                self.assert_inspection_only(calls, root)

    def test_workload_only_installation_can_be_explicitly_confirmed(self):
        workload = {"kind": "Deployment", "metadata": {"name": "previous-agent", "namespace": "dataplane-agent"}}
        code, text, calls, root = self.run_installer({"workloads": [workload]})
        self.assertEqual(code, 0, text)
        self.assertIn("not currently supported", text)
        self.assertIn("SUCCESS", text)
        self.assertTrue((root / "installed").exists())
        self.assertEqual(self.methods(calls).count("ApplyDeployment"), 1)

    def test_empty_or_missing_namespace_needs_no_second_confirmation(self):
        for options in ({}, {"namespace_absent": True}):
            with self.subTest(options=options):
                code, text, _, _ = self.run_installer(options, answers=("yes",))
                self.assertEqual(code, 0, text)
                self.assertIn("SUCCESS", text)
                self.assertNotIn("not currently supported", text)
                self.assertEqual(text.count("[y/N]"), 1)

    def test_resources_in_another_namespace_do_not_trigger_warning(self):
        options = {"releases": [{"name": "dp-agent", "namespace": "another-namespace"}],
                   "workloads": [{"kind": "Deployment", "metadata": {
                       "name": "dp-agent", "namespace": "another-namespace"}}]}
        code, text, _, _ = self.run_installer(options, answers=("yes",))
        self.assertEqual(code, 0, text)
        self.assertIn("SUCCESS", text)
        self.assertNotIn("not currently supported", text)
        self.assertEqual(text.count("[y/N]"), 1)

    def test_agent_inspection_errors_stop_before_authentication(self):
        for option in ("namespace_error", "helm_list_error", "workload_list_error"):
            with self.subTest(option=option):
                code, text, calls, root = self.run_installer({option: True})
                self.assertNotEqual(code, 0, text)
                self.assertIn("Cannot inspect", text)
                self.assert_inspection_only(calls, root)

    def test_malformed_agent_inspection_stops_before_authentication(self):
        for options in ({"namespace_response": None}, {"namespace_response": []},
                        {"namespace_response": {"kind": "Namespace", "metadata": {"name": "wrong-namespace"}}},
                        {"helm_response": {}}, {"helm_response": [{}]},
                        {"workload_response": {"items": None}}, {"workload_response": {"items": [{}]}}):
            with self.subTest(options=options):
                code, text, calls, root = self.run_installer(options)
                self.assertNotEqual(code, 0, text)
                self.assertIn("invalid", text)
                self.assert_inspection_only(calls, root)

    def test_declined_context_makes_no_installation_changes(self):
        code, text, calls, _ = self.run_installer(answers=("no",))
        self.assertEqual(code, 0, text)
        self.assertIn("Cancelled", text)
        self.assertFalse(any(item["tool"] in ("curl", "helm") and item["args"][0] != "version" for item in calls))

    def test_declined_existing_agent_makes_no_installation_changes(self):
        for version in ("v3.19.0", "v4.2.4", "v4.3.1"):
            with self.subTest(version=version):
                code, text, calls, root = self.run_installer(
                    {"existing": True, "helm_version": version}, answers=("yes", "no"))
                self.assertEqual(code, 0, text)
                self.assertIn("not currently supported", text)
                self.assert_inspection_only(calls, root)

    def test_confirmation_details_and_warning_precede_prompts_with_buffered_stdout(self):
        code, text, calls, root = self.run_installer(
            {"existing": True, "helm_version": "v4.3.1"}, answers=("yes", "no"), buffered_stdout=True)
        self.assertEqual(code, 0, text)
        markers = ("Union target:", "Kubernetes context:", "Kubernetes API server:",
                   "Is context test-context", "Installing into a namespace that already contains an agent",
                   "Existing resources were found", "Cancelled;")
        positions = [text.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions), text)
        self.assert_inspection_only(calls, root)

    @unittest.skipUnless(REAL_HELM, "real Helm CLI is not installed")
    def test_lookup_flags_are_accepted_by_installed_helm(self):
        version = subprocess.run([REAL_HELM, "version", "--short"], check=True,
                                 capture_output=True, text=True, timeout=15).stdout.strip()
        code, text, calls, root = self.run_installer(
            {"existing": True, "helm_version": version}, answers=("yes", "no"))
        self.assertEqual(code, 0, text)
        self.assert_inspection_only(calls, root)
        lookup = next(call["args"] for call in calls if call["tool"] == "helm" and call["args"][0] == "list")
        result = subprocess.run([REAL_HELM, *lookup, "--help"], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_terminal_fails(self):
        code, text, _, _ = self.run_installer(tty=False)
        self.assertNotEqual(code, 0)
        self.assertIn("interactive terminal", text)

    def test_missing_tool_reports_install_guidance(self):
        code, text, calls, _ = self.run_installer({"missing_tool": "jq"}, tty=False)
        self.assertNotEqual(code, 0)
        self.assertIn("Required tool is missing: jq", text)
        self.assertIn("https://www.union.ai/docs/v2/union/user-guide/", text)
        self.assertEqual(calls, [])

    def test_executes_returned_command_without_rebuilding_it(self):
        code, text, _, root = self.run_installer({"command_suffix": True})
        self.assertEqual(code, 0, text)
        self.assertEqual((root / "command-proof").read_text(), "returned-command-was-executed")

    def test_instruction_change_after_helm_stops_before_apply(self):
        code, text, calls, _ = self.run_installer({"instructions_changed_after_install": True})
        self.assertNotEqual(code, 0)
        self.assertIn("changed during installation", text)
        self.assertNotIn("ApplyDeployment", self.methods(calls))

    def test_preflight_failures(self):
        for option, expected in (("no_nodes", "No Ready"), ("api_unreachable", "Cannot read cluster nodes"),
                                 ("rbac_denied", "Missing permission"), ("auth_failure", "Authentication failed")):
            with self.subTest(option=option):
                code, text, calls, _ = self.run_installer({option: True})
                self.assertNotEqual(code, 0, text)
                self.assertIn(expected, text)
                self.assertFalse(any(item["tool"] == "helm" and item["args"][0] == "upgrade" for item in calls))

    def test_specific_permissions_when_not_cluster_admin(self):
        code, text, calls, _ = self.run_installer({"limited_rbac": True, "namespace_absent": True})
        self.assertEqual(code, 0, text)
        self.assertTrue(any("bind" in item["args"] for item in calls if item["tool"] == "kubectl"))
        self.assertTrue(any("namespaces" in item["args"] and "create" in item["args"] for item in calls))

    def test_provider_instructions_not_ready_then_ready(self):
        code, text, _, root = self.run_installer({"instructions_unavailable": 4})
        self.assertEqual(code, 0, text)
        self.assertIn("prepare agent instructions", text)
        self.assertEqual((root / "instruction-count").read_text(), "5")

    def test_instruction_wait_is_bounded(self):
        code, text, _, _ = self.run_installer({"instructions_unavailable": 100}, timeout=15)
        self.assertNotEqual(code, 0)
        self.assertIn("Timed out", text)

    def test_changed_and_aborted_instructions_stop(self):
        for option in ("instructions_changed", "instructions_aborted"):
            with self.subTest(option=option):
                code, text, calls, _ = self.run_installer({option: True})
                self.assertNotEqual(code, 0)
                self.assertFalse(any(item["tool"] == "helm" and item["args"][0] == "upgrade" for item in calls))

    def test_helm_failure_preserves_resources_and_reports_safe_diagnostics(self):
        code, text, calls, _ = self.run_installer({"helm_failure": True})
        self.assertNotEqual(code, 0)
        self.assertIn("ImagePullBackOff", text)
        self.assertIn("FailedScheduling", text)
        self.assertNotIn("ApplyDeployment", self.methods(calls))
        self.assertFalse(any("uninstall" in item["args"] or "rollback" in item["args"] for item in calls))

    def test_existing_union_deployment_is_observed_without_apply(self):
        code, text, calls, _ = self.run_installer({"existing": True, "union_existing": True})
        self.assertEqual(code, 0, text)
        self.assertNotIn("ApplyDeployment", self.methods(calls))

    def test_terminal_failures_stop(self):
        for option in ("agent_failure", "union_failure", "union_failed_before", "apply_failure"):
            with self.subTest(option=option):
                code, text, calls, _ = self.run_installer({option: True})
                self.assertNotEqual(code, 0, text)
                self.assertLessEqual(self.methods(calls).count("ApplyDeployment"), 1)
                if option == "union_failed_before":
                    self.assertNotIn("StartAgentInstallation", self.methods(calls))

    def test_wait_timeout(self):
        for option in ("wait_forever", "agent_not_ready", "status_outage"):
            with self.subTest(option=option):
                code, text, _, _ = self.run_installer({option: True}, timeout=20)
                self.assertNotEqual(code, 0)
                self.assertIn("Timed out", text)

    def test_health_drop_resets_stability_window(self):
        code, text, _, root = self.run_installer({"unhealthy_poll": 5})
        self.assertEqual(code, 0, text)
        self.assertEqual(text.count("Verifying 60 seconds"), 2)
        self.assertGreaterEqual(int((root / "readiness-time").read_text()) -
                                int((root / "helm-time").read_text()), 80)

    def test_numeric_enum_encoding(self):
        code, text, _, _ = self.run_installer({"numeric_enums": True})
        self.assertEqual(code, 0, text)

    def test_short_lived_access_token_is_refreshed(self):
        code, text, _, root = self.run_installer({"token_lifetime": 20})
        self.assertEqual(code, 0, text)
        self.assertGreater(int((root / "auth-count").read_text()), 1)

    def test_unauthenticated_rpc_stops(self):
        code, text, calls, _ = self.run_installer({"rpc_unauthenticated": True})
        self.assertNotEqual(code, 0)
        self.assertIn("unauthenticated", text)
        self.assertEqual(self.methods(calls).count("GetLifecycleStatus"), 1)

    def test_wrong_union_target_stops(self):
        code, _, calls, _ = self.run_installer({"wrong_target": True})
        self.assertNotEqual(code, 0)
        self.assertNotIn("StartAgentInstallation", self.methods(calls))

    def test_bad_arguments_and_credentials(self):
        for arguments, key in (([], KEY), (["--cluster"], KEY), (["--unknown"], KEY),
                               (["--cluster", "test", "--wait-timeout", "0"], KEY),
                               (["--cluster", "test"], "invalid"), (["--cluster", "test"], "")):
            with self.subTest(arguments=arguments, key=bool(key)):
                code, _, _, _ = self.run_installer(arguments=arguments, key=key, tty=False)
                self.assertNotEqual(code, 0)

    def test_help_needs_no_credentials(self):
        code, text, _, _ = self.run_installer(arguments=["--help"], key="", tty=False)
        self.assertEqual(code, 0, text)
        self.assertIn("Usage", text)


    def test_default_preparation_budget_includes_authentication(self):
        code, text, calls, root = self.run_installer({"authentication_clock_jump": 600})
        self.assertNotEqual(code, 0, text)
        self.assertIn("Timed out after 600 seconds", text)
        self.assertNotIn("StartAgentInstallation", self.methods(calls))
        self.assertFalse((root / "installed").exists())

    def test_default_instruction_timeout_is_ten_minutes(self):
        code, text, _, root = self.run_installer({"instructions_unavailable": 100, "instruction_clock_jump": 590})
        self.assertNotEqual(code, 0, text)
        self.assertIn("Timed out after 600 seconds", text)
        self.assertEqual(int((root / "clock").read_text()), 1600)
        self.assertFalse((root / "installed").exists())

    def test_post_helm_gets_a_fresh_thirty_minute_budget(self):
        code, text, _, root = self.run_installer({"instructions_unavailable": 1, "instruction_clock_jump": 590,
                                                "wait_forever": True, "status_clock_jump": 1795})
        self.assertNotEqual(code, 0, text)
        self.assertIn("Timed out after 1800 seconds", text)
        self.assertEqual(int((root / "clock").read_text()) - int((root / "post-start-time").read_text()), 1800)

    def test_explicit_wait_timeout_overrides_both_budgets(self):
        for options in ({"instructions_unavailable": 100, "instruction_clock_jump": 30},
                        {"wait_forever": True, "status_clock_jump": 35}):
            with self.subTest(options=options):
                code, text, _, _ = self.run_installer(options, timeout=40)
                self.assertNotEqual(code, 0, text)
                self.assertIn("Timed out after 40 seconds", text)

    def test_help_explains_both_defaults_and_the_shared_override(self):
        code, text, _, _ = self.run_installer(arguments=["--help"], key="", tty=False)
        self.assertEqual(code, 0, text)
        self.assertIn("Override both preparation and post-Helm", text)
        self.assertIn("preparation 600s; post-Helm 1800s", text)

    def test_unchanged_status_keeps_one_timed_line(self):
        code, text, _, _ = self.run_installer({"wait_forever": True}, timeout=75)
        self.assertNotEqual(code, 0, text)
        label = "Waiting for Union to confirm the cluster agent installation"
        self.assertIn("[1m15s] " + label + ".......\nERROR", text)
        self.assertEqual(text.count(label), 1)
        self.assertNotIn("no status change", text)
        self.assertEqual(text.count("Cluster agent: waiting;"), 1)

    def test_friendly_phases_distinguish_installation_health_and_readiness(self):
        complete = {"state": "LIFECYCLE_STATE_COMPLETED", "result": "LIFECYCLE_RESULT_SUCCESS"}
        code, text, _, _ = self.run_installer({"agent_ready_after": 1, "status_sequence": [
            {"agentInstallation": {"state": "LIFECYCLE_STATE_INSTALLING"}},
            {"unionInstallation": {"state": "LIFECYCLE_STATE_INSTALLING"}},
            {"unionInstallation": complete, "connectivity": "CLUSTER_CONNECTIVITY_DISCONNECTED", "health": 0},
            {"unionInstallation": complete, "health": "CLUSTER_HEALTH_UNHEALTHY"},
            {"unionInstallation": complete},
        ]})
        self.assertEqual(code, 0, text)
        markers = ["Waiting for Union to confirm", "Waiting for Union services to finish",
                   "The cluster agent and Union services are installed.",
                   "Installation is complete. Waiting for the cluster to connect",
                   "Installation is complete. Waiting for the cluster to report healthy",
                   "Installation is complete. Waiting for the cluster agent to become ready",
                   "Verifying 60 seconds of stability", "SUCCESS:"]
        positions = [text.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions), text)
        for marker in markers[3:6]:
            self.assertIn(("[0s] " + marker).encode(), self.last_output)

    def test_status_outage_is_distinct_from_unchanged_status_and_recovers(self):
        code, text, _, _ = self.run_installer({"status_failure_attempts": [4, 5, 6]})
        self.assertEqual(code, 0, text)
        self.assertIn("Unable to check installation status. Retrying.", text)
        self.assertIn("Last reported status: Cluster agent: installed;", text)
        self.assertIn("Waiting for installation status to become available", text)
        self.assertNotIn("last status change observed", text)
        self.assertIn("Installation status checks are available again.", text)
        self.assertEqual(text.count("Verifying 60 seconds"), 2)

    def test_recovered_http_retry_announces_stability_restart(self):
        code, text, _, _ = self.run_installer({"status_failure_attempts": [4]})
        self.assertEqual(code, 0, text)
        self.assertIn("A status check needed a retry. The 60-second stability check will restart.", text)
        self.assertEqual(text.count("Verifying 60 seconds"), 2)

    def test_timeout_closes_redirected_progress_line(self):
        code, text, _, _ = self.run_installer({"wait_forever": True}, timeout=20, buffered_stdout=True)
        self.assertNotEqual(code, 0, text)
        self.assertTrue(text.endswith("[20s] Waiting for Union to confirm the cluster agent installation\n"), text)

    def test_unknown_statuses_are_unconfirmed_without_raw_values(self):
        code, text, _, _ = self.run_installer({"status_sequence": [{
            "agentInstallation": {"state": "FUTURE-STATE-SENTINEL"},
            "connectivity": 999, "health": "FUTURE-HEALTH-SENTINEL"}]}, timeout=20)
        self.assertNotEqual(code, 0, text)
        self.assertIn("Cluster agent: not yet confirmed;", text)
        self.assertIn("connection: not yet confirmed; health: not yet reported.", text)
        self.assertNotIn("FUTURE-", text)
        self.assertNotIn("SUCCESS:", text)

    def test_equivalent_enum_encodings_and_activity_timestamps_do_not_reset_segment(self):
        string_status = {"agentInstallation": {"state": "LIFECYCLE_STATE_INSTALLING"}}
        numeric_status = {"agentInstallation": {"state": 1, "observedAt": "2030-01-01T00:00:00Z"},
                          "connectivity": 1, "health": 1}
        code, text, _, _ = self.run_installer({"status_sequence": [string_status, numeric_status]}, timeout=75)
        self.assertNotEqual(code, 0, text)
        self.assertEqual(text.count("Cluster agent: installing;"), 1)
        self.assertIn("[1m15s] Waiting for Union to confirm", text)

    def test_helm_redraws_each_second_and_freezes_its_duration(self):
        code, text, _, _ = self.run_installer({"helm_ticks": 7, "render_each_second": True})
        self.assertEqual(code, 0, text)
        self.assertIn("[1m10s] Installing the cluster agent.......\nThe cluster agent installation command finished.", text)
        frames = progress_frames(self.last_output)
        for second in range(71):
            duration = f"{second}s" if second < 60 else f"1m{second - 60}s"
            frame = f"[{duration}] Installing the cluster agent" + "." * (second // 10)
            self.assertIn(frame.encode(), frames)
        self.assertEqual(text.count("Installing the cluster agent"), 1)

    def test_helm_failure_closes_progress_and_stops_ticker(self):
        code, text, calls, _ = self.run_installer({"helm_ticks": 1, "helm_failure": True})
        self.assertNotEqual(code, 0, text)
        self.assertIn("[10s] Installing the cluster agent.\nERROR", text)
        self.assertNotIn("ApplyDeployment", self.methods(calls))

    def test_helm_interruptions_stop_ticker_and_sleeper(self):
        for interrupt, expected in (("helm-term", 143), ("helm-int", 130)):
            with self.subTest(interrupt=interrupt):
                code, text, calls, _ = self.run_installer({"helm_hold": True}, interrupt=interrupt)
                self.assertEqual(code, expected, text)
                self.assertNotIn("ApplyDeployment", self.methods(calls))
                self.assertIn("[0s] Installing the cluster agent\nAgent diagnostics", text)

    def test_piped_source_preserves_prompts_and_progress_in_redirected_stdout(self):
        code, text, _, _ = self.run_installer({"helm_ticks": 1}, piped_script=True, buffered_stdout=True)
        self.assertEqual(code, 0, text)
        self.assertIn("Is context test-context", text)
        self.assertIn("[0s] Installing the cluster agent\n[10s] Installing the cluster agent\n", text)
        self.assertIn("SUCCESS:", text)

    def test_status_change_starts_a_fresh_segment_with_the_same_label(self):
        installing = {"state": "LIFECYCLE_STATE_INSTALLING"}
        code, text, _, _ = self.run_installer({"status_sequence": [
            {"unionInstallation": installing, "connectivity": 2},
            {"unionInstallation": installing, "connectivity": 1},
        ]}, timeout=20)
        self.assertEqual(code, 1, text)
        label = "Waiting for Union services to finish installing"
        self.assertIn("[5s] " + label + "\nCluster agent:", text)
        self.assertIn("[15s] " + label + ".\nERROR", text)
        self.assertEqual(progress_frames(self.last_output).count(("[0s] " + label).encode()), 2)

    def test_timer_redraws_while_an_http_request_is_blocked(self):
        code, text, _, _ = self.run_installer({"instructions_delay": 3, "render_each_second": True})
        self.assertEqual(code, 0, text)
        frames = progress_frames(self.last_output)
        for second in range(4):
            self.assertIn(f"[{second}s] Waiting for the control plane to prepare agent instructions".encode(), frames)
        self.assertIn("[3s] Waiting for the control plane to prepare agent instructions\n", text)

    def test_narrow_lines_cycle_dots_without_resetting_elapsed_time(self):
        code, text, _, _ = self.run_installer({"helm_seconds": 40, "render_each_second": True}, columns=37)
        self.assertEqual(code, 0, text)
        frames = progress_frames(self.last_output)
        self.assertTrue(frames)
        self.assertTrue(all(len(frame) < 37 for frame in frames))
        helm = [frame for frame in frames if b"Installing the cluster agent" in frame]
        self.assertIn(b"[20s] Installing the cluster agent..", helm)
        self.assertIn(b"[30s] Installing the cluster agent", helm)
        self.assertIn(b"[40s] Installing the cluster agent.", helm)
        self.assertTrue(any(b"Waiting for" in frame and frame.endswith(b"...") for frame in frames))

    def test_missing_width_and_dumb_term_use_plain_start_and_end_lines(self):
        for options in ({"term": "dumb"}, {"stty_error": True}, {"missing_tool": "stty"}):
            with self.subTest(options=options):
                code, text, _, _ = self.run_installer({**options, "helm_seconds": 4})
                self.assertEqual(code, 0, text)
                self.assertIn("[0s] Installing the cluster agent\n[4s] Installing the cluster agent\n", text)

    def test_resizing_keeps_progress_within_the_new_width(self):
        resized = False

        def resize(output, fd, root):
            nonlocal resized
            if not resized and b"[2s] Installing the cluster agent" in output:
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 30, 0, 0))
                resized = True

        code, text, _, _ = self.run_installer({"helm_seconds": 10, "render_each_second": True}, action=resize)
        self.assertEqual(code, 0, text)
        self.assertTrue(resized)
        frames = progress_frames(self.last_output)
        later = frames[frames.index(b"[4s] Installing the clust..."):]
        self.assertTrue(all(len(frame) < 30 for frame in later))

    def test_interruption_cleans_credentials(self):
        code, text, _, _ = self.run_installer({"wait_forever": True}, interrupt=True)
        self.assertEqual(code, 143, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
