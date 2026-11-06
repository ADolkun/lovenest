#!/usr/bin/env bash
# Build and deploy this checkout, respecting .env and COMPOSE_PROFILES.
set -euo pipefail
SECONDS=0
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
if_needed=false
case "$*" in
  '') [[ "$#" == 0 ]] || die 'Usage: tools/deploy/deploy.sh [--if-needed]' ;;
  --if-needed) [[ "$#" == 1 ]] || die 'Usage: tools/deploy/deploy.sh [--if-needed]'; if_needed=true ;;
  *) die 'Usage: tools/deploy/deploy.sh [--if-needed]' ;;
esac
require_main_checkout

common=$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)
state="$common/lovenest-deploy"
lock="$common/lovenest-deploy.lock"
mkdir "$lock" 2>/dev/null || die "Another deployment holds $lock; check its pid before removing a stale lock."
exec 3>&1 4>&2
cleanup() {
  local result=$?
  set +e
  if [[ "$result" != 0 ]]; then
    : > "$state/needs-deploy" 2>/dev/null
    if declare -F output_total >/dev/null && [[ -f "${log:-}" ]]; then
      [[ -z "${phase:-}" ]] || phase_end FAILED
      output_total FAILED
    fi
    printf 'Deployment failed after %ss.\n' "$SECONDS" >&4
    if [[ -f "${log:-}" ]]; then
      printf 'Failure details:\n' >&4
      if declare -F failure_excerpt >/dev/null; then
        failure_excerpt "$log"
      else
        tail -n 12 "$log"
      fi | sed -E $'s/\033\[[0-?]*[ -/]*[@-~]//g' | tr -d '\r' >&4
      printf 'Full log: %s\n' "$log" >&4
    fi
  fi
  rm -f "$lock/pid" "$state/last-success.tmp" "$state/last-success-state.tmp"
  rmdir "$lock"
  return "$result"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
printf '%s\n' "$$" > "$lock/pid"

# Keep command output private in the full log, not hundreds of progress lines.
umask 077
mkdir -p "$state/logs"
log="$state/logs/$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
: > "$log"
printf 'Phase\tSeconds\tStatus\n' > "${log%.log}.timings.tsv"
source "$(dirname "${BASH_SOURCE[0]}")/output.sh"
output_init
# Force plain Docker/BuildKit logs even when the progress summary uses color.
export COMPOSE_ANSI=never BUILDKIT_PROGRESS=plain

die() { printf 'lovenest: %s\n' "$*" >> "$log"; exit 1; }

phase_begin 'Prepare'
{
  require_docker
  check_config
  check_postgres_layout
  revision=$(git -C "$ROOT" rev-parse HEAD)
  status=$(git -C "$ROOT" --no-optional-locks status --porcelain --untracked-files=normal)
  config_before=$(config_fingerprint) || die 'Could not fingerprint Compose configuration.'
  log_event "Commit $revision"
} >> "$log" 2>&1
phase_end

check_endpoints() {
  "${COMPOSE[@]}" exec -T backend python - <<'PY'
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# Nginx canonicalizes Host against FRONTEND_URL. Reach this stack internally
# using that Host, and never follow a redirect to an unrelated public instance.
frontend_host = urllib.parse.urlsplit(os.environ.get("FRONTEND_URL", "http://localhost:3000")).hostname or "localhost"

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
pending = {
    "frontend": "http://frontend:8080/",
    "frontend API proxy": "http://frontend:8080/api/health",
    "tax planner": "http://tax-planner:8088/",
}
deadline = time.monotonic() + 60
while pending and time.monotonic() < deadline:
    for name, url in list(pending.items()):
        try:
            headers = {"Host": frontend_host} if name.startswith("frontend") else {}
            request = urllib.request.Request(url, headers=headers)
            with opener.open(request, timeout=3) as response:
                if response.status == 200:
                    del pending[name]
        except urllib.error.HTTPError as error:
            error.close()
        except (OSError, ValueError):
            pass
    if pending:
        time.sleep(2)
if pending:
    raise SystemExit("Not ready: " + ", ".join(pending))
print("Frontend, API proxy, and tax planner are ready.")
PY
}

checkout_unchanged() {
  [[ "$(git -C "$ROOT" rev-parse HEAD)" == "$revision" \
    && "$(git -C "$ROOT" --no-optional-locks status --porcelain --untracked-files=normal)" == "$status" \
    && "$(config_fingerprint)" == "$config_before" ]]
}

if [[ "$if_needed" == true ]]; then
  phase_begin 'Check for changes'
  skip_ready=false
  if can_skip_deploy >> "$log" 2>&1 && check_endpoints >> "$log" 2>&1; then
    checkout_unchanged >> "$log" 2>&1 || die 'Checkout or configuration changed during the readiness check; rerun tools/deploy/deploy.sh.'
    skip_ready=true
  fi
  phase_end
  if [[ "$skip_ready" == true ]]; then
    output_total SKIPPED
    printf 'Pull deployment skipped: only docs/CI files changed; images, configuration and readiness are unchanged (%ss total).\n' "$SECONDS" >&3
    printf 'Log: %s\n' "$log" >&3
    exit 0
  fi
fi

deploy() {
  : > "$state/needs-deploy"
  log_event "Building commit $revision"
  phase_begin 'Build images'
  # Each runtime image is built once. Worker/beat/MCP services reuse the backend
  # tag instead of exporting the same build four times via the Compose anchor.
  "${COMPOSE[@]}" build backend frontend tax-planner >> "$log" 2>&1 \
    || die 'Build failed; no services were restarted. Fix the error and rerun tools/deploy/deploy.sh.'
  phase_end
  phase_begin 'Start + health'
  "${COMPOSE[@]}" up -d --no-build --wait --wait-timeout 120 >> "$log" 2>&1 \
    || die 'Start/readiness failed. Inspect docker compose -f docker-compose.prod.yml logs --tail=100, fix the error, then rerun tools/deploy/deploy.sh.'
  phase_end
  phase_begin 'Check endpoints'
  check_endpoints >> "$log" 2>&1 || die 'Frontend/API/tax readiness failed; inspect Compose logs and rerun tools/deploy/deploy.sh.'
  phase_end
  phase_begin 'Verify deployment'
  checkout_unchanged >> "$log" 2>&1 || die 'Checkout or configuration changed during deployment. Inspect the log before retrying.'
  local url port dirty=false images containers
  images=$(image_fingerprint 2>> "$log") || die 'Could not verify deployed images.'
  containers=$(stack_fingerprint 2>> "$log") || die 'Could not verify running services.'
  [[ -z "$status" ]] || dirty=true
  url=$(env_value FRONTEND_URL)
  port=$("${COMPOSE[@]}" port frontend 8080 | sed -n '1s/.*://p') || die 'Could not determine the frontend port.'
  [[ -n "$url" ]] || url="http://localhost:${port:-3000}"
  printf '%s %s dirty=%s\n' "$revision" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$dirty" > "$state/last-success.tmp"
  printf 'schema=1\nrevision=%s\nconfig=%s\nimages=%s\ncontainers=%s\n' \
    "$revision" "$config_before" "$images" "$containers" > "$state/last-success-state.tmp"
  mv "$state/last-success.tmp" "$state/last-success"
  mv "$state/last-success-state.tmp" "$state/last-success-state"
  rm -f "$state/needs-deploy"
  phase_end
  output_total OK
  printf '%sReady:%s %s (%ss total)\n' "$color_ok" "$color_reset" "$url" "$SECONDS" >&3
  printf 'Log: %s\n' "$log" >&3
  log_event "Ready: $url"
}
deploy
