# Installer terminal preview

Run the checked-out installer against synthetic, offline Kubernetes, Helm, HTTP,
and clock commands. Requires macOS or Linux, Python 3.10+, Bash, jq, and the usual
system utilities. A real terminal is required for both stdin and stdout.
No installed Helm or Kubernetes CLI, cluster access, or credentials are needed.

```sh
python3 tests/preview_install.py --list
python3 tests/preview_install.py success
python3 tests/preview_install.py existing-agent --yes
python3 tests/preview_install.py slow --speed 30
python3 tests/preview_install.py success --source stdin --speed 1
```

Answer confirmations yourself by default; `--yes` supplies affirmative answers
after prompts appear. Decline a prompt to review cancellation. The default
simulation speed is 10×: a ten-second progress tick takes about one second plus
command execution time. Use `--speed 1` for normal simulated waiting intervals.
The existing automated installer tests still advance mock time immediately.

Choose `success`, `existing-agent`, `slow`, `helm-failure`, `union-failure`,
`timeout`, or `interrupt`. The timeout scenario uses a 75-second simulated wait;
the interruption scenario sends SIGINT after two Helm progress ticks. Failures
return status 1, and interruption returns 130. Manual Ctrl-C works in every
scenario. The tool restores terminal settings and removes its temporary files
when it exits.

`--source stdin` pipes the local script into Bash, including its real terminal
confirmations; it does not download anything. Both source modes stream the
installer's PTY output without decoding or rewriting it. Terminal dimensions and
resizing are forwarded. Rerun the command after editing `install.sh`.

For visual review, check wrapping, prompt placement and answer echo, progress
line endings, resizing, diagnostics, and terminal restoration after Ctrl-C. The
current installer appends dots and periodic lines rather than redrawing in place.
The preview can relay ANSI escapes and carriage returns for future renderer work;
the current installer tests continue to reject them.

Run repeatable offline checks with `bash tests/test.sh`, or select another Bash
with `BASH_BIN=/path/to/bash bash tests/test.sh`. The preview tests run inside an
outer controlling terminal and cover both source modes, scenario outcomes,
manual input, automatic confirmations, pacing, byte fidelity, resizing, signals,
and cleanup. These checks do not establish real-cluster acceptance.
