#!/usr/bin/env bash
#
# Run the test suite. Stdlib unittest only — no pytest.
#
#   ./run_tests.sh                      run everything (quiet)
#   ./run_tests.sh -v                   verbose, per-test names
#   ./run_tests.sh tests.test_validation      one module
#   ./run_tests.sh --cov                run everything, then print a coverage report
#   UPDATE_GOLDEN=1 ./run_tests.sh      rewrite tests/golden/*.conf fixtures
#
# With uv on PATH (Mac, ub) it lints first -- `ruff check` and `ruff format --check`,
# at a pinned ruff, since the formatter's output changes between releases -- then runs
# the suite in a uv-managed Python 3.14 env built from web-ui/requirements.txt, so the
# deps always match the image's pins and there is no .venv step; uv caches both after
# the first run. Without uv it skips the lint and runs python3 as is, which is how it
# runs inside the image:
#
#   docker run --rm -v "$PWD":/src -w /src --entrypoint sh \
#     amneziawg-web-ui:local -c './run_tests.sh'
#
set -euo pipefail

cd "$(dirname "$0")"

if [[ -n "${UPDATE_GOLDEN:-}" ]]; then
	echo "UPDATE_GOLDEN=1 — golden fixtures will be rewritten; review the diff."
fi

COVERAGE=0
if [[ "${1:-}" == "--cov" ]]; then
	COVERAGE=1
	shift
fi

RUFF_VERSION=0.16.8

if command -v uv >/dev/null 2>&1; then
	uv tool run --quiet "ruff@${RUFF_VERSION}" check --quiet .
	uv tool run --quiet "ruff@${RUFF_VERSION}" format --check --quiet .
	PYTHON=(uv run --no-project --python 3.14 --with-requirements web-ui/requirements.txt)
	if [[ "${COVERAGE}" == 1 ]]; then
		PYTHON+=(--with coverage)
	fi
	PYTHON+=(python)
elif [[ "${COVERAGE}" == 1 ]]; then
	echo "--cov needs uv on PATH" >&2
	exit 1
else
	PYTHON=(python3)
fi

# One module/test given: run just that. Otherwise discover the whole suite.
if [[ $# -gt 0 && "$1" != "-v" ]]; then
	UNITTEST=(-m unittest -b "$@")
else
	UNITTEST=(-m unittest discover -b -s tests -t . "$@")
fi

if [[ "${COVERAGE}" == 1 ]]; then
	# Keep the data file out of the tree.
	COVERAGE_FILE="$(mktemp -d)/.coverage"
	export COVERAGE_FILE
	"${PYTHON[@]}" -m coverage run --branch --source=web-ui "${UNITTEST[@]}"
	exec "${PYTHON[@]}" -m coverage report --skip-empty
fi

exec "${PYTHON[@]}" "${UNITTEST[@]}"
