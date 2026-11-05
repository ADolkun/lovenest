#!/usr/bin/env bash
# Regenerate uv.lock, the source of truth for backend dependencies.
#
# CI and development use `uv sync --locked --group dev`; Docker uses
# `uv sync --locked --no-dev`. Both install directly from the hashed lock.
#
# Run this after any change to [project.dependencies] or the dev group in
# pyproject.toml and commit the updated uv.lock — CI fails if it drifts.
# Extra arguments are passed through, e.g.:  ./scripts/lock.sh --upgrade
#
# The uv version is pinned so resolution is reproducible; Renovate bumps it.
set -euo pipefail
cd "$(dirname "$0")/.."

# renovate: datasource=pypi depName=uv
uvx uv@0.13.7 lock "$@"

# The code-intel library capture records the locked dist versions of the
# packages that build routes (fastapi-users), and CI runs `capture --check`,
# so a re-lock refreshes it here.
refresh=1
for arg in "$@"; do
  case "$arg" in --check | --locked | --dry-run) refresh=0 ;; esac
done
if [[ $refresh == 1 && -f ../.code-intel/requirements.txt ]]; then
  uv sync --locked --group dev || { echo "lock.sh: 'uv sync --locked --group dev' failed" >&2; exit 1; }
  (cd .. && uv run --no-project --python 3.14 --with-requirements .code-intel/requirements.txt code-intel capture) \
    || { echo "lock.sh: 'code-intel capture' failed" >&2; exit 1; }
fi
