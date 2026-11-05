#!/usr/bin/env bash
# Fresh-clone setup for Lovenest's source-built production stack.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
[[ "$#" -le 1 && ( "$#" == 0 || "$1" == --env-only ) ]] || die 'Usage: tools/deploy/setup.sh [--env-only]'
require_main_checkout
[[ "${1:-}" == --env-only ]] || require_docker

if [[ -e "$ROOT/.env" || -L "$ROOT/.env" ]]; then
  note 'Using existing .env without changing it.'
else
  umask 077
  temporary=$(mktemp "$ROOT/.env.XXXXXX")
  trap 'rm -f "$temporary"' EXIT
  random_secret() {
    local value
    value=$(od -An -N32 -tx1 /dev/urandom | tr -d '[:space:]')
    [[ ${#value} == 64 ]] || die 'Could not generate a random secret.'
    printf '%s' "$value"
  }
  secret=$(random_secret)
  password=$(random_secret)
  agent_secret=$(random_secret)
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      SECRET_KEY=*) printf 'SECRET_KEY=%s\n' "$secret" ;;
      POSTGRES_PASSWORD=*) printf 'POSTGRES_PASSWORD=%s\n' "$password" ;;
      AGENTS_MCP_JWT_SECRET=*) printf 'AGENTS_MCP_JWT_SECRET=%s\n' "$agent_secret" ;;
      *) printf '%s\n' "$line" ;;
    esac
  done < "$ROOT/.env.example" > "$temporary"
  # A hard link fails if another process created .env in the meantime.
  ln "$temporary" "$ROOT/.env" || die '.env appeared during setup; it was not overwritten. Rerun setup.'
  rm "$temporary"
  trap - EXIT
  note 'Created private .env with random secrets. Optional integrations remain disabled.'
fi
if [[ "${1:-}" == --env-only ]]; then
  note 'Edit .env as needed, then run tools/deploy/deploy.sh.'
  exit 0
fi
exec "$ROOT/tools/deploy/deploy.sh"
