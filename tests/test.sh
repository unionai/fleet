#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
"${BASH_BIN:-/bin/bash}" -n install.sh tests/test.sh
shellcheck --shell=bash install.sh tests/test.sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py' -v
