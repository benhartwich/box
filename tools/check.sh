#!/usr/bin/env bash
# All checks required before a commit (CLAUDE.md). Stops at the first failure.
set -euo pipefail
cd "$(dirname "$0")/.."
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q "$@"
uvx --from 'reuse[charset-normalizer]' reuse lint --quiet
echo "all checks passed"
