#!/usr/bin/env bash
# Moves a compose install from the PostgreSQL 16 `pgdata` volume to the volume
# the current compose file declares for the db service. Run once from the
# repository root after pulling and before `docker compose up`.
# See docs/postgres-upgrade.md.
set -euo pipefail

export COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
OLD_DB_IMAGE="${OLD_DB_IMAGE:-docker.io/pgvector/pgvector:pg16}"

die() { printf 'upgrade-postgres: %s\n' "$*" >&2; exit 1; }
log() { printf '==> %s\n' "$*"; }

docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required."
db_config=$(docker compose config db) || die "'docker compose config db' failed (COMPOSE_FILE=$COMPOSE_FILE)."

db_env() {
  awk -v key="$1:" '$1 == key { sub(/^[^:]*:[ ]*/, ""); gsub(/^["'\'']|["'\'']$/, ""); print; exit }' <<<"$db_config"
}

project=$(sed -n 's/^name: //p' <<<"$db_config")
new_image=$(docker compose config --images db)
new_volume=${NEW_VOLUME:-$(awk '/^volumes:/ { v = 1; next } /^[^ ]/ { v = 0 } v && $1 == "name:" { print $2 }' <<<"$db_config")}
old_volume=${OLD_VOLUME:-${project}_pgdata}
pg_user=$(db_env POSTGRES_USER)
pg_user=${pg_user:-postgres}
pg_db=$(db_env POSTGRES_DB)
pg_db=${pg_db:-$pg_user}
temp="${project}-pg-upgrade-old"

[[ -n $project && -n $new_image ]] || die "Could not read the project name and db image from the compose config."
[[ -n $new_volume && $new_volume != *[[:space:]]* ]] \
  || die "Could not tell which volume the db service uses; set NEW_VOLUME."
if [[ $new_volume == "$old_volume" ]] \
  || ! awk '$1 == "target:" && $2 == "/var/lib/postgresql" { found = 1 } END { exit !found }' <<<"$db_config"; then
  die "$COMPOSE_FILE still uses the old database layout. Pull the version that runs PostgreSQL 18 first."
fi
docker volume inspect "$old_volume" >/dev/null 2>&1 \
  || die "Volume $old_volume does not exist, so there is nothing to upgrade. A new install needs only 'docker compose up -d'."
if docker volume inspect "$new_volume" >/dev/null 2>&1; then
  die "Volume $new_volume already exists: either this upgrade already ran or 'docker compose up' created it first. Nothing was changed.
If you are sure $new_volume holds nothing you need, remove it with 'docker compose rm -sf db && docker volume rm $new_volume' and run this again."
fi

docker image inspect "$OLD_DB_IMAGE" >/dev/null 2>&1 || docker pull "$OLD_DB_IMAGE"
old_major=$(docker run --rm --network none -v "$old_volume:/old:ro" --entrypoint cat "$OLD_DB_IMAGE" /old/PG_VERSION 2>/dev/null) \
  || die "Volume $old_volume holds no PostgreSQL data directory."
old_image_major=$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$OLD_DB_IMAGE" | sed -n 's/^PG_MAJOR=//p')
[[ $old_image_major == "$old_major" ]] \
  || die "Volume $old_volume holds PostgreSQL $old_major data but $OLD_DB_IMAGE is PostgreSQL ${old_image_major:-unknown}; set OLD_DB_IMAGE to a $old_major image."

running=$(docker ps --filter "label=com.docker.compose.project=$project" \
  --format '{{.Names}} {{.Label "com.docker.compose.service"}}')
app_containers=$(awk '$2 != "db" { print $1 }' <<<"$running")
db_containers=$(awk '$2 == "db" { print $1 }' <<<"$running")
all_containers=$(awk '{ printf "%s ", $1 }' <<<"$running")

stage=checks
on_exit() {
  local status=$?
  docker rm -fv "$temp" >/dev/null 2>&1 || true
  (( status == 0 )) && return
  case $stage in
    stopped)
      echo "Nothing was migrated and $old_volume is unchanged." >&2
      [[ -z $all_containers ]] || echo "To bring the stack back as it was: docker start $all_containers" >&2
      ;;
    new-db)
      echo "The new database on $new_volume is incomplete; $old_volume is unchanged." >&2
      echo "To retry: docker compose rm -sf db && docker volume rm $new_volume, then run this again." >&2
      echo "To go back instead: restore the previous compose file and run 'docker compose up -d'." >&2
      ;;
  esac
}
trap on_exit EXIT

log "Project $project: PostgreSQL $old_major on $old_volume -> $new_image on $new_volume"
stage=stopped
if [[ -n $app_containers ]]; then
  log "Stopping $(tr '\n' ' ' <<<"$app_containers")"
  xargs docker stop <<<"$app_containers" >/dev/null
fi
[[ -z $db_containers ]] || xargs docker stop -t 60 <<<"$db_containers" >/dev/null

docker rm -fv "$temp" >/dev/null 2>&1 || true
in_use=$(docker ps --filter "volume=$old_volume" --format '{{.Names}}' | tr '\n' ' ')
[[ -z $in_use ]] || die "Volume $old_volume is still mounted by running container(s): $in_use"

wait_ready() {
  local _
  for _ in $(seq 1 90); do
    "$@" pg_isready -q -h 127.0.0.1 -U "$pg_user" && return 0
    sleep 1
  done
  return 1
}
old_psql() {
  docker exec -e PGOPTIONS=-cdefault_transaction_read_only=on "$temp" \
    psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$pg_user" -d "$pg_db" "$@"
}
new_psql() {
  docker compose exec -T db psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$pg_user" -d "$pg_db" "$@"
}
count_sql="SELECT table_name::text || '|' || (xpath('/row/n/text()',
    query_to_xml(format('SELECT count(*) AS n FROM public.%I', table_name), false, true, '')))[1]::text
  FROM information_schema.tables
  WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
  ORDER BY table_name::text COLLATE \"C\""
extensions_sql="SELECT string_agg(extname || ' ' || extversion, ', ' ORDER BY extname)
  FROM pg_extension WHERE extname <> 'plpgsql'"

log "Starting PostgreSQL $old_major on $old_volume with networking disabled"
docker run -d --name "$temp" --network none -v "$old_volume:/var/lib/postgresql/data" "$OLD_DB_IMAGE" >/dev/null
wait_ready docker exec "$temp" || { docker logs --tail 30 "$temp" >&2; die "PostgreSQL $old_major did not start on $old_volume."; }

umask 077
dump_file=${DUMP_FILE:-backups/${project}-pg${old_major}-$(date +%Y%m%d-%H%M%S).dump}
mkdir -p "$(dirname "$dump_file")"
log "Dumping $pg_db to $dump_file"
docker exec "$temp" pg_dump -U "$pg_user" -d "$pg_db" -Fc >"$dump_file"
[[ -s $dump_file ]] || die "pg_dump wrote nothing to $dump_file."
old_counts=$(old_psql -c "$count_sql")
old_extensions=$(old_psql -c "$extensions_sql")
old_extension_names=$(old_psql -c "SELECT extname FROM pg_extension WHERE extname <> 'plpgsql'")
docker stop -t 60 "$temp" >/dev/null
docker rm -fv "$temp" >/dev/null

log "Starting the new db service"
stage=new-db
docker compose up -d --no-deps --wait db
wait_ready docker compose exec -T db || die "The new db service is not accepting connections."
mounted=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql"}}{{.Name}}{{end}}{{end}}' \
  "$(docker compose ps -q db)")
[[ $mounted == "$new_volume" ]] || die "The db container mounts '$mounted' at /var/lib/postgresql, expected $new_volume."
new_version=$(new_psql -c "SELECT split_part(current_setting('server_version'), ' ', 1)")

for ext in $old_extension_names; do
  [[ $(new_psql -c "SELECT count(*) FROM pg_available_extensions WHERE name = '$ext'") == 1 ]] \
    || die "The old database uses the '$ext' extension, which $new_image does not provide."
done

log "Restoring into PostgreSQL $new_version"
docker compose exec -T db pg_restore -U "$pg_user" -d "$pg_db" --exit-on-error --single-transaction <"$dump_file"
new_psql -c 'ANALYZE'
new_counts=$(new_psql -c "$count_sql")
new_extensions=$(new_psql -c "$extensions_sql")
if [[ $new_counts != "$old_counts" ]]; then
  diff <(printf '%s\n' "$old_counts") <(printf '%s\n' "$new_counts") >&2 || true
  die "Row counts differ between the old and new database (diff above). The app was not restarted."
fi
summary=$(awk -F'|' 'NF == 2 { tables++; rows += $2 } END { printf "%d tables, %.0f rows", tables, rows }' <<<"$new_counts")
stage=restored

if [[ -n $app_containers ]]; then
  log "Starting $(tr '\n' ' ' <<<"$app_containers")"
  xargs docker start <<<"$app_containers" >/dev/null
fi

cat <<EOF

PostgreSQL $old_major -> $new_version done: $summary, every table's row count matches.
Extensions before: ${old_extensions:-none}
Extensions after:  ${new_extensions:-none}
Dump: $dump_file

Next: run your usual 'docker compose up -d'.
Rollback: $old_volume still holds the PostgreSQL $old_major data, untouched. To go back, stop
the stack, restore the previous compose file, and run 'docker compose up -d'. Anything
written after the upgrade is lost that way.
Once you are satisfied: docker volume rm $old_volume && rm $dump_file
EOF
