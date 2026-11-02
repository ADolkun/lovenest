# Upgrading PostgreSQL

The compose files run PostgreSQL 18. Installs set up before this change run
PostgreSQL 16, and a new major version cannot read the old one's data files, so
the data has to be dumped from the old server and restored into the new one.

The PostgreSQL 18 image also keeps its data in a different place
(`/var/lib/postgresql/18/docker`, with the volume mounted at
`/var/lib/postgresql`), so the compose files now use a new volume,
`postgres-data`. The old `pgdata` volume is left exactly as it was, which makes
it your rollback.

## Upgrade

Run the script once, from the repository root, after pulling and before
`docker compose up`:

```bash
git pull
scripts/upgrade-postgres.sh
docker compose -f docker-compose.prod.yml up -d --build
```

For the development stack, point it at the other compose file:
`COMPOSE_FILE=docker-compose.yml scripts/upgrade-postgres.sh`.

The script:

1. Refuses to run if the old volume does not exist (a new install needs only
   `docker compose up -d`) or the new one already does. It never deletes a
   volume.
2. Stops the project's running containers, so nothing writes during the copy.
3. Starts PostgreSQL 16 on the old volume in a temporary container with no
   network, dumps the database to `backups/`, and records the row count of
   every table.
4. Starts the new `db` service, which creates the new volume, checks that every
   extension the old database uses (such as `vector`) is available, restores
   the dump in a single transaction, and compares the row counts. Any
   difference stops the script before the app comes back.
5. Starts the containers it stopped again, so the app runs on PostgreSQL 18
   without a rebuild, and prints the dump path and the rollback steps.

It moves the application database only. Roles or databases you created by hand
on the old server are not copied.

Settings, all optional: `COMPOSE_FILE` (default `docker-compose.prod.yml`),
`COMPOSE_PROJECT_NAME`, `OLD_DB_IMAGE` (default
`docker.io/pgvector/pgvector:pg16`), `OLD_VOLUME` (default `<project>_pgdata`),
`NEW_VOLUME` (default: the volume the compose file gives the `db` service), and
`DUMP_FILE`.

## If `docker compose up` ran first

Starting the new compose file before the script creates the new volume and an
empty database in it, and the script then refuses to run. If you are sure
nothing you need was written to that database, remove it and run the script:

```bash
docker compose -f docker-compose.prod.yml rm -sf db
docker volume rm securo_postgres-data
scripts/upgrade-postgres.sh
```

## Rollback

The old volume still holds the PostgreSQL 16 data. To go back, stop the stack,
check out the previous compose file, and start it again; it mounts the old
volume. Anything written after the upgrade is not in it.

## Clean up

Once you are satisfied with the upgrade, delete the old volume and the dump:

```bash
docker volume rm securo_pgdata
rm backups/securo-pg16-*.dump
```

For Kubernetes, see "Upgrading to PostgreSQL 18" in the
[chart README](../charts/securo/README.md).
