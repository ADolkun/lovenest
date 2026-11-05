#!/usr/bin/env bash
# Tracked deployment guard; native Git/prek dispatch stays local to each clone.
set -euo pipefail
invoked_root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
skip() { note "Pull deployment skipped: $*"; exit 0; }
case ",${SKIP:-}," in *,deploy-lovenest,*) skip 'SKIP includes deploy-lovenest' ;; esac
enabled=$(git -C "$ROOT" config --local --bool --get lovenest.deployOnPull 2>/dev/null || true)
[[ "$enabled" == true ]] || exit 0
configured_root=$(git -C "$ROOT" config --local --get lovenest.deployRoot || true)
[[ "$(cd "$invoked_root" && pwd -P)" == "$ROOT" && "$ROOT" == "$configured_root" ]] || skip 'not the opted-in main checkout'
require_main_checkout
branch=$(git -C "$ROOT" symbolic-ref --quiet --short HEAD) || skip 'HEAD is detached'
configured_branch=$(git -C "$ROOT" config --local --get lovenest.deployBranch || printf lovenest)
[[ "$branch" == "$configured_branch" ]] || skip "current branch is $branch"
[[ -z "$(git -C "$ROOT" --no-optional-locks status --porcelain --untracked-files=normal)" ]] || skip 'checkout has uncommitted or untracked files'
exec "$ROOT/tools/deploy/deploy.sh"
