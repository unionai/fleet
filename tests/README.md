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
simulation speed is 10×: ten simulated seconds take about one second plus
command execution time. Use `--speed 1` for normal simulated waiting intervals.
Operation waits advance the mock clock; renderer sleeps only observe it.

Choose `success`, `existing-agent`, `slow`, `helm-failure`, `union-failure`,
`timeout`, or `interrupt`. The timeout scenario uses a 75-second simulated wait;
the interruption scenario sends SIGINT after twenty simulated Helm seconds. Failures
return status 1, and interruption returns 130. Manual Ctrl-C works in every
scenario. The tool restores terminal settings and removes its temporary files
when it exits.

`--source stdin` pipes the local script into Bash, including its real terminal
confirmations; it does not download anything. Both source modes stream the
installer's PTY output without decoding or rewriting it. Terminal dimensions and
resizing are forwarded. Rerun the command after editing `install.sh`.

For visual review, check wrapping, prompt placement and answer echo, progress
line endings, resizing, diagnostics, and terminal restoration after Ctrl-C. The
installer redraws the current progress line once per simulated second, preserving
its final elapsed duration when a segment finishes. Dots advance every ten seconds
and restart at the available terminal width. Labels shorten on narrow terminals.
Each standalone message ends the current segment. The next segment starts at zero.
The cursor hides during animated progress and returns before messages and handled exits.
If `stty` cannot read the width or `TERM=dumb`, progress uses plain start/end lines.
Redirected installer stdout also uses plain start/end lines without redraw bytes.

Run repeatable offline checks with `bash tests/test.sh`, or select another Bash
with `BASH_BIN=/path/to/bash bash tests/test.sh`. The preview tests run inside an
outer controlling terminal and cover both source modes, scenario outcomes,
manual input, automatic confirmations, pacing, byte fidelity, resizing, signals,
and cleanup. These checks do not establish real-cluster acceptance.
