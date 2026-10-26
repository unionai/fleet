# AGENTS.md

## Purpose and documentation

This public repository hosts Union's cluster installer at
https://raw.githubusercontent.com/unionai/fleet/main/install.sh.

Keep README.md small: direct customers to Union signup and public documentation,
explain that source is available for auditing, and link engineers to the
[development guide](https://app.notion.com/p/4058cc06513d81ca8d1de526109b3317).

Keep internal environment configuration and operational runbooks outside this
public repository. Use synthetic hosts and credentials in test fixtures.

## Repository layout

- `install.sh`: self-contained Bash installer and CLI.
- `tests/test_install.py`: installer tests using a controlling terminal.
- `tests/fake_command.py`: offline HTTP, Kubernetes, Helm and clock doubles.
- `tests/test.sh`: shell syntax, ShellCheck and Python test runner.
- `.github/workflows/test.yml`: Linux and macOS CI.

## Installer contract

- Support stock macOS Bash 3.2 and Linux Bash, with Helm 3 and Helm 4.
  Python is a test dependency, not an installer runtime dependency.
- Use existing FLYTE_API_KEY authentication and an already registered Fleet
  cluster. Registration and infrastructure provisioning are outside this script.
- Confirm the current Kubernetes context before installation. Pin subsequent
  Kubernetes and Helm operations to that confirmed context.
- Require another confirmation before updating an existing agent release.
- Execute the API-provided helm_command with its values.yaml in a private
  temporary directory. Do not reconstruct the command, parse chart versions,
  or introduce independent chart/version defaults.
- After successful agent installation, initiate initial Union deployment through
  ApplyDeployment, or observe a deployment already underway.
- Require both installation phases to succeed, the local agent to be ready,
  and Fleet to remain CONNECTED and HEALTHY for 60 seconds.
- Preserve the configurable 1800-second post-Helm wait default and the returned
  Helm command's own timeout. Keep retries and waits bounded.
- Preserve installed resources on failure, timeout or interruption. Do not
  automatically uninstall, roll back or retry failed Union deployments.
- Keep user-facing messages, prompts and errors agent-provider agnostic.
  Prefer "cluster agent"; preserve actual resource identifiers in diagnostics.
- Keep credentials, values, raw API error bodies and raw Helm output out of
  user-facing output. Clean up private temporary files on handled exits.
- Keep installation work behind the final main entry point.

## Validation

Tests require Python 3.10 or newer, jq and ShellCheck.

```sh
bash tests/test.sh
BASH_BIN=/path/to/bash bash tests/test.sh
```

Use the existing offline harness for behavior changes. Keep its Kubernetes,
Helm, HTTP and clock commands stubbed; tests require a controlling PTY.
Distinguish offline test results from real-cluster acceptance.

## Working rules

- Show the proposed diff or exact patch plan and obtain explicit approval before
  editing files.
- Do not run git commit, git push, live kubectl/oc commands or the live installer.
  Provide the maintainer with exact commands to run.
- Preserve unrelated local changes.
