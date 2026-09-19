#!/usr/bin/env bash
#
# Run the test suite. Stdlib unittest only — no pytest, no extra dependencies.
#
#   ./run_tests.sh                      run everything (quiet)
#   ./run_tests.sh -v                   verbose, per-test names
#   ./run_tests.sh tests.test_validation      one module
#   UPDATE_GOLDEN=1 ./run_tests.sh      rewrite tests/golden/*.conf fixtures
#
# The suite needs `requests` and `flask` importable (they are imported by the
# modules under test). On the host, build the local env once, then run through
# it (uv puts .venv/bin first on PATH, so `python3` below is the venv's):
#
#   uv venv --python 3.14 && uv pip install -r web-ui/requirements.txt
#   uv run ./run_tests.sh
#
# Or run inside the image:
#
#   docker run --rm -v "$PWD":/src -w /src --entrypoint sh \
#     amneziawg-web-ui:local -c './run_tests.sh'
#
set -euo pipefail

cd "$(dirname "$0")"

if [[ -n "${UPDATE_GOLDEN:-}" ]]; then
	echo "UPDATE_GOLDEN=1 — golden fixtures will be rewritten; review the diff."
fi

# -b buffers stdout so the app's print() calls only surface for failing tests.
if [[ $# -gt 0 && "$1" != "-v" ]]; then
	exec python3 -m unittest -b "$@"
fi

exec python3 -m unittest discover -b -s tests -t . "$@"
