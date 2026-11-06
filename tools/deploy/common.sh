#!/usr/bin/env bash
# Shared by the Docker-only self-hosting commands; never source .env as shell code.
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
COMPOSE=(docker compose --project-directory "$ROOT" --env-file "$ROOT/.env" -f "$ROOT/docker-compose.prod.yml")

die() { printf 'lovenest: %s\n' "$*" >&2; exit 1; }
note() { printf 'lovenest: %s\n' "$*"; }

require_main_checkout() {
  command -v git >/dev/null 2>&1 || die 'Git is required.'
  local current common
  current=$(git -C "$ROOT" rev-parse --path-format=absolute --git-dir) || die 'Run from a Git clone.'
  common=$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir) || exit 1
  [[ "$current" == "$common" ]] || die 'Deploy from the main checkout, not a linked worktree; Compose uses the shared securo stack.'
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die 'Install Docker with the Compose plugin, then rerun this command.'
  docker compose version >/dev/null 2>&1 || die 'Docker Compose is required (a version supporting up --wait).'
  docker info >/dev/null 2>&1 || die 'Docker is not running or is not accessible. Start it, then rerun this command.'
}

# Compose resolves quoting/interpolation. This value is never evaluated by a shell.
env_value() {
  local key value
  while IFS='=' read -r key value; do
    if [[ "$key" == "$1" ]]; then printf '%s' "$value"; return; fi
  done <<< "$COMPOSE_ENV"
}

check_config() {
  [[ -f "$ROOT/.env" ]] || die 'Missing .env. Run tools/deploy/setup.sh first.'
  "${COMPOSE[@]}" config --quiet || die 'Invalid Compose configuration; check .env.'
  COMPOSE_ENV=$("${COMPOSE[@]}" config --environment) || die 'Could not read the Compose environment.'
  local secret password agent profiles
  secret=$(env_value SECRET_KEY)
  case "$secret" in
    ''|change-me-in-production|dev-secret-change-in-production)
      die 'Set SECRET_KEY in .env to a random secret (for example, openssl rand -hex 32). Existing .env was preserved.' ;;
  esac
  password=$(env_value POSTGRES_PASSWORD)
  [[ "$password" != change-me-in-production ]] || die 'Replace POSTGRES_PASSWORD in .env before the first start. For an existing database, keep its current password.'
  agent=$(env_value AGENTS_ENABLED)
  profiles=",$(env_value COMPOSE_PROFILES),"
  case "$agent" in true|True|TRUE|1|yes|Yes|YES|on|On|ON) agent=true ;; esac
  if [[ "$agent" == true || "$profiles" == *,agents,* ]]; then
    secret=$(env_value AGENTS_MCP_JWT_SECRET)
    case "$secret" in
      ''|change-me-in-production|dev-mcp-secret-change-in-production)
        die 'Set a random AGENTS_MCP_JWT_SECRET in .env before enabling agents.' ;;
    esac
  fi
}

check_postgres_layout() {
  local project volumes
  project=$("${COMPOSE[@]}" config | sed -n 's/^name: //p') || die 'Could not determine the Compose project.'
  # YAML quotes names that resemble booleans/numbers. Project names themselves
  # contain only this restricted alphabet, so strip matching quotes and validate.
  case "$project" in
    \"*\") project=${project#\"}; project=${project%\"} ;;
    \'*\') project=${project#\'}; project=${project%\'} ;;
  esac
  [[ "$project" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die 'Compose did not report a valid project name.'
  volumes=$(docker volume ls --format '{{.Name}}') || die 'Could not inspect Docker volumes.'
  if printf '%s\n' "$volumes" | grep -Fx "${project}_pgdata" >/dev/null \
    && ! printf '%s\n' "$volumes" | grep -Fx "${project}_postgres-data" >/dev/null; then
    die 'Found the old PostgreSQL volume. Before deploying, follow docs/postgres-upgrade.md and run scripts/upgrade-postgres.sh. No services were started.'
  fi
}

# Fingerprint resolved runtime settings, including shell overrides and profiles.
# Hash only: never write resolved settings/secrets into the deployment receipts.
config_fingerprint() {
  {
    "${COMPOSE[@]}" config || return 1
    printf 'profiles=%s\n' "$(env_value COMPOSE_PROFILES)"
    git -C "$ROOT" hash-object -- "$ROOT/.env" || return 1
  } | git -C "$ROOT" hash-object --stdin
}

image_fingerprint() {
  local images image
  images=$("${COMPOSE[@]}" config --images) || return 1
  [[ -n "$images" ]] || return 1
  while IFS= read -r image; do
    printf '%s\n' "$image"
    docker image inspect --format '{{.Id}}' "$image" || return 1
  done <<< "$(printf '%s\n' "$images" | sort -u)" | git -C "$ROOT" hash-object --stdin
}

# Require exactly the configured services to be running and healthy where a
# healthcheck exists. Container IDs detect manual replacements/config changes;
# the HTTP probe additionally checks frontend/tax services without healthchecks.
stack_fingerprint() {
  local expected running ids id health service
  local services=()
  expected=$("${COMPOSE[@]}" config --services | sort -u) || return 1
  [[ -n "$expected" ]] || return 1
  while IFS= read -r service; do services+=("$service"); done <<< "$expected"
  running=$("${COMPOSE[@]}" ps --status running --services "${services[@]}" | sort -u) || return 1
  [[ -n "$expected" && "$expected" == "$running" ]] || return 1
  ids=$("${COMPOSE[@]}" ps --all --quiet "${services[@]}" | sort -u) || return 1
  [[ -n "$ids" ]] || return 1
  while IFS= read -r id; do
    health=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id") || return 1
    [[ "$health" == 'running healthy' || "$health" == 'running none' ]] || return 1
  done <<< "$ids"
  printf '%s\n' "$ids" | git -C "$ROOT" hash-object --stdin
}

receipt_value() {
  local key value
  while IFS='=' read -r key value; do
    if [[ "$key" == "$1" ]]; then printf '%s' "$value"; return; fi
  done < "$state/last-success-state"
}

# Only a known successful, clean ancestor plus a narrow non-runtime allowlist
# may skip. Unknown paths, old receipts, missing images, drift or failed attempts
# fall back to a full deployment. Manual deploy.sh never takes this fast path.
can_skip_deploy() {
  local previous completed dirty saved_config current_images current_stack
  [[ "$if_needed" == true && -z "$status" && ! -e "$state/needs-deploy" \
    && -f "$state/last-success" && -f "$state/last-success-state" ]] || return 1
  read -r previous completed dirty < "$state/last-success" || return 1
  [[ "$dirty" == dirty=false && "$(receipt_value schema)" == 1 \
    && "$(receipt_value revision)" == "$previous" ]] || return 1
  git -C "$ROOT" merge-base --is-ancestor "$previous" "$revision" || return 1
  git -C "$ROOT" diff --quiet "$previous" "$revision" -- . \
    ':(exclude)docs/' ':(exclude).github/' \
    ':(exclude)README.md' ':(exclude)CONTRIBUTING.md' \
    ':(exclude)CODE_OF_CONDUCT.md' ':(exclude)SECURITY.md' \
    ':(exclude)LICENSE' ':(exclude)AGENTS.md' || return 1
  saved_config=$(receipt_value config)
  [[ -n "$saved_config" && "$saved_config" == "$config_before" ]] || return 1
  current_images=$(image_fingerprint) || return 1
  [[ "$current_images" == "$(receipt_value images)" ]] || return 1
  current_stack=$(stack_fingerprint) || return 1
  [[ "$current_stack" == "$(receipt_value containers)" ]] || return 1
}
