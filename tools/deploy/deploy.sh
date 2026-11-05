#!/usr/bin/env bash
# Build and deploy this checkout, respecting .env and COMPOSE_PROFILES.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
[[ "$#" == 0 ]] || die 'Usage: tools/deploy/deploy.sh'
require_main_checkout
require_docker
check_config
check_postgres_layout

common=$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)
state="$common/lovenest-deploy"
lock="$common/lovenest-deploy.lock"
mkdir "$lock" 2>/dev/null || die "Another deployment holds $lock; check its pid before removing a stale lock."
trap 'rm -f "$lock/pid"; rmdir "$lock"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
printf '%s\n' "$$" > "$lock/pid"
mkdir -p "$state/logs"
revision=$(git -C "$ROOT" rev-parse HEAD)
status=$(git -C "$ROOT" --no-optional-locks status --porcelain --untracked-files=normal)
log="$state/logs/$(date -u +%Y%m%dT%H%M%SZ)-$$.log"

deploy() {
  note "Building commit $revision. Log: $log"
  "${COMPOSE[@]}" up -d --build --wait --wait-timeout 120 \
    || die 'Build/start/readiness failed. Inspect docker compose -f docker-compose.prod.yml logs --tail=100, fix the error, then rerun tools/deploy/deploy.sh.'
  # Python is already in the backend image; no host Python or curl is required.
  "${COMPOSE[@]}" exec -T backend python - <<'PY' || die 'Frontend/API/tax readiness failed; inspect Compose logs and rerun tools/deploy/deploy.sh.'
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
  [[ "$(git -C "$ROOT" rev-parse HEAD)" == "$revision" \
    && "$(git -C "$ROOT" --no-optional-locks status --porcelain --untracked-files=normal)" == "$status" ]] \
    || die 'Checkout changed during deployment. Inspect the log before retrying.'
  local url port dirty=false
  [[ -z "$status" ]] || dirty=true
  url=$(env_value FRONTEND_URL)
  port=$("${COMPOSE[@]}" port frontend 8080 | sed -n '1s/.*://p') || die 'Could not determine the frontend port.'
  [[ -n "$url" ]] || url="http://localhost:${port:-3000}"
  printf '%s %s dirty=%s\n' "$revision" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$dirty" > "$state/last-success"
  note "Ready: $url — open the app (a fresh install shows first-admin setup)."
  note 'Logs: docker compose -f docker-compose.prod.yml logs --tail=100'
  note 'Stop, keeping data: docker compose -f docker-compose.prod.yml down (do not add --volumes).'
}
deploy 2>&1 | tee "$log"
