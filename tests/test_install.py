#!/usr/bin/env python3
"""Exercise the real installer with a controlling terminal and offline doubles."""

import base64
import errno
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = os.environ.get("BASH_BIN", "/bin/bash")
REAL_HELM = shutil.which("helm")
KEY = base64.b64encode(b"control.example.test:client:CLIENT-SECRET-SENTINEL:test-org").decode()


class InstallerTests(unittest.TestCase):
    def run_installer(self, options=None, answers=("yes", "yes"), timeout=1800, tty=True,
                      arguments=None, key=KEY, interrupt=False):
        directory = tempfile.TemporaryDirectory(prefix="fleet-test-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "bin").mkdir()
        (root / "tmp").mkdir()
        (root / "options.json").write_text(json.dumps(options or {}))
        for tool in ("curl", "kubectl", "helm", "date", "sleep"):
            (root / "bin" / tool).symlink_to(ROOT / "tests/fake_command.py")
        (root / "bin" / "python3").symlink_to(sys.executable)
        if (options or {}).get("missing_tool"):
            for tool in ("bash", "jq", "base64", "tr", "mktemp", "cp", "rm"):
                if tool != options["missing_tool"]:
                    (root / "bin" / tool).symlink_to(shutil.which(tool))
            search_path = str(root / "bin")
        else:
            search_path = f"{root / 'bin'}:{os.environ['PATH']}"
        env = {**os.environ, "PATH": search_path,
               "FAKE_ROOT": str(root), "FLYTE_API_KEY": key, "TMPDIR": str(root / "tmp")}
        command = [BASH, str(ROOT / "install.sh"), *(arguments if arguments is not None else
                   ["--cluster", "test-cluster", "--wait-timeout", str(timeout)])]
        output = bytearray()
        if tty:
            pid, fd = pty.fork()
            if pid == 0:
                os.execve(BASH, command, env)
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
                    if interrupt and (root / "installed").exists() and not sent_signal:
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
        text = output.decode(errors="replace")
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()] \
            if (root / "calls.jsonl").exists() else []
        self.assertNotIn("PRIVATE-KEY-SENTINEL", text)
        self.assertNotIn("CLIENT-SECRET-SENTINEL", text)
        self.assertNotIn("ACCESS-TOKEN-SENTINEL", text)
        self.assertNotIn(KEY, text)
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

    def test_interruption_cleans_credentials(self):
        code, text, _, _ = self.run_installer({"wait_forever": True}, interrupt=True)
        self.assertEqual(code, 143, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
