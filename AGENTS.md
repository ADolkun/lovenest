# Agent instructions for ADolkun/lovenest

Downstream product fork of `securo-finance/securo`. Keep this workflow intact.

## Branches

- `main`: clean mirror of `securo-finance/securo:main`. Do not develop here or target Lovenest PRs here. Sync by fast-forward only.
- `lovenest`: product/default branch. Branch Lovenest work from here and open PRs into `ADolkun/lovenest:lovenest`.
- Upstream Securo fixes: branch from clean `main`/`upstream/main` and PR to `securo-finance/securo:main` separately.

## Normal PR flow

```bash
git fetch origin upstream
git switch lovenest
git pull --ff-only origin lovenest
git switch -c <type>/<short-description>
# make focused changes
git add <changed-files>
git commit -m "<type>: <short imperative summary>"
git push -u origin HEAD
gh pr create --repo ADolkun/lovenest --base lovenest --head <branch>
```

Use Conventional Commit titles, e.g. `docs: update branch workflow`.

## Upstream sync flow

```bash
git fetch upstream origin
git switch main
git merge --ff-only upstream/main
git push origin main

git switch lovenest
git pull --ff-only origin lovenest
git switch -c sync-main-into-lovenest-YYYYMMDD
git merge origin/main
git push -u origin HEAD
gh pr create --repo ADolkun/lovenest --base lovenest --head sync-main-into-lovenest-YYYYMMDD
```

Merge upstream-sync PRs with a merge commit, not squash/rebase.

## Knowledge Authority & Routing

Route a question to the source that owns that kind of fact. This is not a precedence order.

- Executable behaviour and data shape: code, Alembic migrations (`backend/alembic/versions/`), `backend/app/schemas/`, tests.
- Repository rules: this file.
- Path-scoped guidance: `.claude/rules/`.
- Task workflows and tool reference: `.claude/skills/`.
- Domain vocabulary: `CONTEXT.md`.
- Decisions: `docs/adr/`.
- Doc routing: `docs/README.md`.
- Upstream-owned versus fork-owned files: `git diff --stat upstream/main...HEAD -- <path>` is empty for a file that is still upstream's. Each edit to an upstream-owned file costs a conflict on every sync.
- Advisory, verify before acting: plans, `HANDOFF.md`, `CONTINUE-HERE.md`, agent memory.

### Scoped instruction map

Claude Code loads a rule when you read a file matching its `paths`. In a client that does not, read each matching rule yourself.

- `backend`: `backend/**`
- `backend-tests`: `backend/tests/**`, `backend/pyproject.toml`
- `frontend`: `frontend/**`
- `frontend-ui`: `frontend/src/components/**`, `frontend/src/pages/**`
- `frontend-tests`: `frontend/src/**/*.test.ts`, `frontend/src/**/*.test.tsx`, `frontend/src/test/**`, `frontend/vitest.config.ts`

### Writing scoped rules

A rule loads into every session that touches a matching file, so it holds only what the code cannot say: a convention or pitfall, with the reason when it is not obvious.

- Name the function, file, setting or workflow that owns a fact instead of copying its value. Copied lists and limits go stale without failing a check.
- How a component works belongs in `docs/` or a comment beside the code.
- A change that makes a rule or doc wrong fixes it in the same PR. `doc_refs` (code-intel) lists the docs and rules that name a file, route or symbol you changed.

## Development Commands

Run from the repository root. All Python goes through `uv run --no-sync` after a locked sync; `CONTRIBUTING.md` has the full list.

```bash
(cd backend && uv sync --locked --group dev)        # install
(cd frontend && npm ci)
(cd extras/tax && uv sync --locked)
(cd backend && uv run --no-sync ruff check .)       # lint
(cd frontend && npm run lint)
(cd backend && uv run --no-sync pyrefly check)         # type gate
(cd frontend && npm run typecheck)
(cd backend && uv run --no-sync python scripts/check_migration_chain.py)  # migration chain
uv run --no-project --python 3.14 --with-requirements .code-intel/requirements.txt code-intel doc-check  # docs, links, agent files
uv run --no-project --python 3.14 --with-requirements .code-intel/requirements.txt code-intel capture  # by hand only after editing a library include or captured binding; backend/scripts/lock.sh refreshes it on re-lock (CI runs `capture --check`)
docker compose up -d                                # dev stack
```

Keep each checkout's installs in step with its lockfiles after a branch switch, pull, rebase or `git worktree add`. Hooks are per clone (its worktrees share them), and nothing runs until they are installed. Install `prek` once, then the hooks from the repository root:

```bash
uv tool install prek
prek install --config tools/dev/git-hooks.yaml -t post-checkout -t post-merge -t post-rewrite
```

`tools/dev/sync-deps.sh` runs `uv sync --locked` in `backend/` and `extras/tax/`, and `npm ci` in `frontend/` when its manifest or lockfile changed. Run it by hand after a fresh clone, or skip it for one command with `SKIP=sync-deps git checkout <branch>`.

`docker-compose.yml` and `docker-compose.prod.yml` both use the project name `securo`, the live stack. Do not bring a stack up from a worktree while the live one is running.

**Per-run test command (one file):**

```bash
(cd backend && uv run --no-sync pytest tests/test_<name>.py -W error)
(cd frontend && npx vitest run src/<path>.test.ts)
```

Postgres-backed backend tests need `EVIDENCE_TEST_DATABASE_URL`. They isolate themselves (a schema per test through `postgres_sessions`, a temporary database per migration test in `backend/tests/test_recovery_evidence_integration.py`), so one disposable container serves every worktree. Never point it at the `securo` stack's database.

```bash
docker run -d --rm --name lovenest-test-pg -e POSTGRES_USER=securo_test -e POSTGRES_PASSWORD=securo_test \
  -e POSTGRES_DB=securo_test -p 127.0.0.1:55433:5432 pgvector/pgvector:pg18
export EVIDENCE_TEST_DATABASE_URL=postgresql+asyncpg://securo_test:securo_test@127.0.0.1:55433/securo_test
```

Before a PR, run the smallest relevant check and report it clearly:

```bash
docker compose -f docker-compose.prod.yml config --quiet
(cd backend && ruff check . && pytest --cov=app --cov-report=term-missing --cov-fail-under=60)  # backend checks
(cd frontend && npm run lint && npm run build)  # frontend checks
```

## Code Search & Navigation

Use the cheapest tool that answers exactly. Read `.claude/skills/code-intel-reference/SKILL.md` when a tool errors, returns empty, or you need its blind spots.

- `rg` for a known identifier or literal, always with a path (without one it can wait on stdin). Construction sites: `rg 'ClassName\('`.
- probe (`search_code`, `extract_code`) for a concept when you do not know the identifier. `path` must be absolute; keep it narrow; `exact: true` for identifiers.
- LSP for callers of a method whose name is defined in several places, shadowed or re-exported symbols, and multi-hop callers.
- code-intel MCP server (`mcp__code-intel__<tool>`):
  - `di_deps` / `di_reverse`: FastAPI `Depends` chains and the routes a dependency guards.
  - `route_match` / `route_unmatched`: a route's full URL and its frontend callers; orphaned routes.
  - `jsx_usage` / `symbol_resolve`: JSX render sites; which same-name definition a name means.
  - `doc_refs`: docs that mention a file, route or symbol.
  - `status`: completeness and blind spots. Replies that lead with `ⓘ` or `⚠ INCOMPLETE` name what the answer may miss; confirm with `rg` or LSP.

### Using LSP

- Positions are 1-based and must sit on the name. A wrong column returns nothing.
- The first call per language can come back empty or partial; retry once.
- Run Python calls one at a time: pyrefly cancels a pending request when another file opens.
- LSP does not see `Depends(...)` as a call, nor calls through a `Protocol` to the implementing class. Use `di_reverse` and `symbol_resolve` for those, and check the receiver's type before dropping an `rg` hit.

### Prerequisites

Every checkout and worktree needs `backend/.venv` and `frontend/node_modules`; the sync hook above provides both, and without them LSP and the frontend tools quietly return less. code-intel provisions its own TypeScript on first use (cache shared by all checkouts), so its frontend tools answer `frontend_provisioning` for a few seconds once. Claude Code asks you to approve the project's `code-intel` and `probe` servers once.

### Before committing

Check callers and DI consumers of what you changed (`di_reverse` for a dependency, LSP or `rg` for a function), and run `doc-check` when you touched a route, a symbol that docs name, or an agent file.

### Briefing subagents

A subagent does not necessarily see this file. Put the routing lines it needs in its brief: which tool answers which question, the absolute-path rule for probe, and "retry LSP once".

## Safety

- Do not commit secrets, `.env`, backups, local credentials, or ignored handoff files.
- `HANDOFF.md` and `CONTINUE-HERE.md` are local operational docs and intentionally gitignored.
- Keep `docker-compose.prod.yml` project name as `securo` unless explicitly migrating Docker volumes/networks.
- Local image tags: `lovenest-backend:0.16.6-lovenest`, `lovenest-frontend:0.16.6-lovenest`.
