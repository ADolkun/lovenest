#!/usr/bin/env bash
# Brings a checkout's installed dependencies in line with its lockfiles:
# backend/.venv and extras/tax/.venv (uv) and frontend/node_modules (npm).
#
# The post-checkout, post-merge and post-rewrite hooks in tools/dev/git-hooks.yaml
# run it, so switching branches, pulling, rebasing and `git worktree add` keep the
# checkout in sync. Run it by hand after a fresh clone. Skip the hook for one git
# command with SKIP=sync-deps. Tests: tools/dev/test_sync_deps.py.
#
# npm ci deletes node_modules first, so it runs only when package.json or
# package-lock.json changed since it last ran, or node_modules is missing (stamp in
# the checkout's own git dir, so every worktree has its own). uv sync is a no-op
# when a venv already matches. Every step is attempted; if any fails the script
# exits 1 and the hook runner prints the output. The checkout has already happened.
set -u

# A file checkout (`git checkout -- path`) doesn't switch commits, and
# `git commit --amend` doesn't touch the working tree, so neither can leave the
# installs out of step with it.
if [ "${PRE_COMMIT_CHECKOUT_TYPE:-1}" = "0" ] || [ "${PRE_COMMIT_REWRITE_COMMAND:-}" = "amend" ]; then
  exit 0
fi

root=$(git rev-parse --show-toplevel) || exit 1
cd "$root" || exit 1
stamp_dir=$(git rev-parse --git-path sync-deps) || exit 1
failures=0

note() { printf 'sync-deps: %s\n' "$*"; }
fail() {
  printf 'sync-deps: failed, run by hand: %s\n' "$*" >&2
  failures=$((failures + 1))
}

if command -v uv >/dev/null 2>&1; then
  for dir in backend extras/tax; do
    (cd "$dir" && uv sync --locked) || fail "cd $dir && uv sync --locked"
  done
else
  fail "install uv (https://docs.astral.sh/uv/), then rerun tools/dev/sync-deps.sh"
fi

if command -v npm >/dev/null 2>&1; then
  if ! key=$(git hash-object -- frontend/package.json frontend/package-lock.json 2>/dev/null); then
    fail "restore frontend/package.json and frontend/package-lock.json, then rerun tools/dev/sync-deps.sh"
  else
    key=$(printf '%s' "$key" | tr '\n' ' ')
    if [ "$(cat "$stamp_dir/frontend" 2>/dev/null)" != "$key" ] || [ ! -d frontend/node_modules ]; then
      note "running npm ci in frontend"
      if (cd frontend && npm ci --no-audit --no-fund); then
        mkdir -p "$stamp_dir" && printf '%s\n' "$key" >"$stamp_dir/frontend"
      else
        fail "cd frontend && npm ci"
      fi
    fi
  fi
else
  fail "install Node.js, then rerun tools/dev/sync-deps.sh"
fi

[ "$failures" -eq 0 ] || exit 1
