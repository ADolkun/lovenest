# Self-hosting Lovenest

Use Git, Bash and standard Unix tools on Linux or macOS, plus a running Docker
daemon and Docker Compose supporting `up --wait` and `--wait-timeout`. Docker
Desktop, OrbStack, or Docker Engine with the Compose plugin can provide these.
This flow builds from source; it does not require host Python, Node, uv or prek.
The upstream `install.sh` targets Securo rather than this fork.

## First install

```bash
git clone --branch lovenest https://github.com/ADolkun/lovenest.git
cd lovenest
tools/deploy/setup.sh
```

Setup creates `.env` from `.env.example` with mode `600` and random application,
database, and optional-agent secrets. It never replaces an existing `.env` or
prints the secrets. It then builds and starts the production stack, waits for
backend health, and checks the frontend, its API proxy, and the tax planner.
Open the printed URL (by default http://localhost:3000) and create the first
administrator account in the setup screen. No bank or identity-provider account
is required. Local authentication works by default.

To configure before the first start:

```bash
tools/deploy/setup.sh --env-only
# Edit .env in your editor.
tools/deploy/deploy.sh
```

For example, set both `FRONTEND_PORT=3132` and
`FRONTEND_URL=http://localhost:3132` when changing the frontend port. Set
`BACKEND_PORT` or `TAX_PLANNER_PORT` if their defaults conflict. For a domain,
set `FRONTEND_URL` to its HTTPS URL and configure your reverse proxy and OIDC
callback URLs accordingly. The backend port stays bound to localhost.

Bank sync, OIDC and agents remain optional; `.env.example` documents their keys.
Agents require both `AGENTS_ENABLED=true` and `COMPOSE_PROFILES=agents`, plus a
random `AGENTS_MCP_JWT_SECRET`. The deployment command respects those settings
and does not enable the agents profile by itself.

## Update and inspect

```bash
git switch lovenest
git pull --ff-only
tools/deploy/deploy.sh
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100
```

The deploy command uses the production Compose file and its existing project
name, `securo`. Run it from the main checkout. Linked worktrees share hooks and
the same stack name and cannot deploy through these helpers. Host developer
dependencies and their hooks are covered separately in
[CONTRIBUTING.md](../CONTRIBUTING.md#development-environment-build-from-source).

Deployments take an exclusive lock, return a nonzero status on failure, and keep
logs in `.git/lovenest-deploy/logs/`. `last-success` records the Git revision,
completion time, and whether manual deployment included local changes. Helpers
do not prune images, build caches, or volumes. Review Docker disk use and remove
unused assets separately when appropriate.

To stop while keeping database and uploaded data:

```bash
docker compose -f docker-compose.prod.yml down
```

Do not add `--volumes` unless you intend to delete persistent data. Keep `.env`
and regular PostgreSQL backups outside the clone as well. For a backup while
the database is running:

```bash
mkdir -p backups
docker compose -f docker-compose.prod.yml exec -T db pg_dump -U postgres -d securo > backups/securo.sql
```

That file contains financial data; restrict access and store an off-machine
copy. Existing PostgreSQL 16 installations need the explicit
[PostgreSQL upgrade](postgres-upgrade.md) before deploying. The helpers refuse
to start a new empty PostgreSQL 18 volume over a detected old `pgdata` layout;
they do not migrate the database automatically. Preserve an existing database's
password rather than changing `.env` alone to rotate it.

## Optional rebuild after pulls

Automatic deployment is **off by default**. Enable it only on the main checkout
that hosts your running stack:

```bash
tools/deploy/install-pull-hook.sh enable
tools/deploy/install-pull-hook.sh status
```

The default branch is `lovenest`; `enable branch-name` selects another branch.
The hook runs after a merge or a fast-forward pull that updates the checkout.
An already-up-to-date pull does not run it. It skips other branches, detached
HEADs, linked worktrees, and checkouts with staged, unstaged or untracked files.
Ignored `.env` files do not make the checkout dirty. Rebase pulls do not invoke
the post-merge hook; use `tools/deploy/deploy.sh` after a rebase.

```bash
SKIP=deploy-lovenest git pull --ff-only  # skip deployment once
tools/deploy/install-pull-hook.sh disable
```

The installer stores the opt-in, branch and absolute checkout path in local Git
config, and installs a small local dispatcher. It does not start a deployment or
install developer dependencies. Existing prek hooks are preserved: Lovenest
uses prek's `post-merge.legacy` chain. A later normal `prek install` also preserves
a native Lovenest hook this way. Avoid `prek install --force`, which removes
legacy hooks. `SKIP=sync-deps` independently skips developer dependency sync.

Custom hooks, symlinks, or `core.hooksPath` are refused without being changed. For
manual integration, keep your existing dispatcher and invoke the tracked
`tools/deploy/post-merge.sh` from it. Set the local config explicitly:

```bash
git config --local lovenest.deployRoot "$(git rev-parse --show-toplevel)"
git config --local lovenest.deployBranch lovenest
git config --local lovenest.deployOnPull true
```

The tracked guard still checks the opted-in checkout, branch and clean tree.

## Troubleshooting

- **Missing Docker/Compose or stopped daemon:** install/start Docker and rerun
  the same setup command; existing `.env` is preserved.
- **Placeholder secret:** edit the named key in `.env`; setup does not overwrite
  existing configuration. `openssl rand -hex 32` can generate a replacement.
- **Build/start/readiness failed:** inspect the deployment log and Compose logs,
  resolve the reported problem, then rerun `tools/deploy/deploy.sh`. A failed
  deployment may already have restarted some services.
- **No rebuild after a pull:** check hook `status`, the current branch and
  `git status --short`; an up-to-date pull, rebase or skipped/dirty checkout needs
  a manual deploy. After moving a clone, rerun the hook installer to update its
  stored path.
- **Deployment lock remains:** inspect `.git/lovenest-deploy.lock/pid` and confirm
  that process is no longer deploying before removing the stale lock directory.
