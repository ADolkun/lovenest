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
uvx uv@0.12.10 lock "$@"
