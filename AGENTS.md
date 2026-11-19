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
uv run --no-project --python 3.14 --with-requirements .code-intel/requirements.txt code-intel capture --if-stale  # the pre-commit hook and backend/scripts/lock.sh run this; starts the app only when the capture is stale (CI runs `capture --check`)
docker compose up -d                                # dev stack
```

Keep each checkout's installs in step with its lockfiles after a branch switch, pull, rebase or `git worktree add`. Hooks are per clone (its worktrees share them), and nothing runs until they are installed. Install `prek` once, then the hooks from the repository root:

```bash
uv tool install prek
prek install --config tools/dev/git-hooks.yaml -t pre-commit -t post-checkout -t post-merge -t post-rewrite
```

`tools/dev/sync-deps.sh` runs `uv sync --locked` in `backend/` and `extras/tax/`, and `npm ci` in `frontend/` when its manifest or lockfile changed. Run it by hand after a fresh clone, or skip it for one command with `SKIP=sync-deps git checkout <branch>`. The `code-intel-capture` pre-commit hook refreshes `.code-intel/library-capture.json` when a commit leaves it stale; it then fails the commit, so stage the file and commit again.

`docker-compose.yml` and `docker-compose.prod.yml` both use the project name `securo`, the live stack. Do not bring a stack up from a worktree while the live one is running.

**Per-run test command (one file):**

```bash
(cd backend && uv run --no-sync pytest tests/test_<name>.py -W error)
(cd frontend && npx vitest run src/<path>.test.ts)
```

Postgres-backed backend tests need `POSTGRES_TEST_URL`. They isolate themselves (a schema per test through `postgres_sessions`, a temporary database per migration test in `backend/tests/test_recovery_evidence_integration.py`), so one disposable container serves every worktree. Never point it at the `securo` stack's database. The PostgreSQL cases in `backend/tests/test_instance_secrets.py` drop and recreate tables in the `public` schema, so do not run that file from two worktrees at once.

```bash
docker run -d --rm --name lovenest-test-pg -e POSTGRES_USER=securo_test -e POSTGRES_PASSWORD=securo_test \
  -e POSTGRES_DB=securo_test -p 127.0.0.1:55433:5432 pgvector/pgvector:pg18
export POSTGRES_TEST_URL=postgresql+asyncpg://securo_test:securo_test@127.0.0.1:55433/securo_test
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
- Local image tags: `lovenest-backend:0.20.0-lovenest`, `lovenest-frontend:0.20.0-lovenest`.

## Upstream Securo contributor guide

Below is upstream Securo's `AGENTS.md`, kept verbatim in one block so each sync merges upstream's edits without conflicts. Where it disagrees with the sections above, the sections above win. In particular:

- `docker compose up --build` from any checkout or worktree starts the live `securo` stack; see Development Commands above.
- Installs and type checks follow this fork: `uv sync --locked --group dev` and `uv run --no-sync pyrefly check`, not `uv sync --all-extras` and `ty check`.
- The fork's CI is `.github/workflows/lovenest-ci.yml`. It shards the backend suite its own way, without `pytest-split`.
- The ADR numbers it cites are upstream's.

# Securo

An open-source, self-hosted personal finance manager. This file is for contributors and their coding agents: the rules the code keeps and where each feature lives. Start with `README.md` and `CONTRIBUTING.md`. The reasons behind the rules are in `docs/adr/` (cited below as ADR NNNN); read the ones that touch the area you are changing.

## Stack

FastAPI, SQLAlchemy (async), Alembic and Celery on Redis in `backend/`; React 19, TypeScript, Vite, Tailwind 4, TanStack Query and react-router in `frontend/`; PostgreSQL with the `vector` extension. Docker Compose runs everything: frontend on :3000, backend on :8000.

## Rules the code keeps

- **The workspace is the data boundary** (ADR 0002). Financial tables carry `workspace_id`; routes take `WorkspaceContext` from `current_workspace` or `current_writable_workspace`; services filter by `ctx.id`. Queries that read rows shared from other workspaces add `in_bound_workspace(...)`. New code never relies on `workspace_autostamp`. `tests/test_workspace_boundary_schema.py` fails on a table without `workspace_id` unless it is listed there with a reason.
- **Every mutating route has a write gate** (ADR 0003): it depends on `current_writable_workspace`, directly or through `require_module_write`, or sits in the test's `ALLOWLIST` with a reason. `tests/test_write_permission_coverage.py` fails otherwise. Roles are `owner`, `editor`, `viewer`, plus the virtual `manager`.
- **Optional means one switch, off by default, free when off** (ADR 0004): a setting in `backend/app/core/config.py`, no imports, routes, jobs or containers when off (Compose profiles for services), documented in `.env.example` and the README.
- **Feature areas are modules** (ADR 0005), listed in `module_service.py` and `frontend/src/lib/modules.ts`; their routes use `require_module` or `require_module_write`.
- **Logic lives in services** (ADR 0011). Routers parse, check access, call a service, return a schema. What counts as income or expense is defined once, in `backend/app/services/_query_filters.py`.
- **Money is exact** (ADR 0008): `Numeric(15, 2)` and `Decimal`, never `float`. Every row keeps its `currency`; cross-currency sums use `amount_primary`.
- **"Today" comes from `app_today()`** (ADR 0009), never `date.today()`. Stored timestamps are UTC.
- **Migrations are one numbered line** (ADR 0010): next number after the head, `down_revision` the previous one; a merged migration is never edited. `tests/test_schema_matches_migrations_postgres.py` fails when the migrations and the models disagree; fix it with a migration, never by adding to `tests/schema_drift_baseline.txt`.
- **Every outbound call is gated, and there is no telemetry** (ADR 0016). A new external call needs a switch and a superseding ADR for the list.
- **Files go through `get_storage_provider()`** with keys that start with the workspace id (ADR 0019).
- **New routes are closed to access tokens** until mapped in `backend/app/core/access_token_scopes.py` (ADR 0012).
- **Every user-facing string is translated.** Add each key to `frontend/src/locales/en.json` and every other locale file; `src/locales/i18n.test.ts` fails on a missing or extra key or a placeholder mismatch.
- **A new architecture decision, or a change to one, comes with an ADR** (`docs/adr/README.md`).
- **Conventional commits** (`feat:`, `fix:`, `docs:`, `refactor:`), one feature or fix per pull request, backend tests for new behavior.

## Commands

```bash
docker compose up --build                 # full stack; migrations run in the one-shot `migrate` service
docker compose run --rm migrate           # apply a new migration to a running stack
docker compose restart celery-worker      # the worker does not hot reload

cd backend && uv sync --all-extras        # first time
uv run pytest                             # CI requires coverage >= 60%
uv run pytest tests/test_some_file.py     # one file while fixing
uv run pytest tests/api_contract -n auto  # route contract sweep
uv run ruff check . && uv run ty check .
./scripts/lock.sh                         # after changing dependencies; commit uv.lock

cd ../frontend && npm run lint && npm run build && npm test
```

Run lint, type check and the tests you touched before calling work done; CI runs the rest.

## Tests

- **Backend** (`backend/tests/conftest.py`): in-memory SQLite by default, schema from `Base.metadata.create_all` (not from migrations), pgvector shimmed to JSON, Redis and external FX sync mocked, `AGENTS_ENABLED` forced on. Fixtures: `session`, `client`, `test_user`, `test_workspace`, `auth_headers`, `viewer_auth_headers`, `admin_auth_headers`, `test_account`, `test_transactions`, `test_categories`, `test_rules`. Files matching `tests/*_postgres.py` use `postgres_sessions` and need `POSTGRES_TEST_URL` (skipped locally without it, required in CI).
- **CI shards** (`.github/workflows/ci.yml`): the backend suite runs in four parallel jobs split by `pytest-split`, balanced by `backend/.test_durations`. New tests are given the average duration, so the balance drifts slowly; when one shard runs well past the others, refresh it from a CI run's own timings with `.github/ci/merge_test_durations.py` (usage in its docstring). The Backend Tests check combines the shards' coverage and enforces the 60% floor.
- **Contract sweep** (`backend/tests/api_contract/`): every OpenAPI route and method against the expected status in `baseline.json`, as owner and as viewer.
- **Frontend** (`frontend/vitest.config.ts`): a `node` project for `src/lib/*.test.ts` and a jsdom `dom` project for the rest, setup in `src/test/setup.ts`. Render with `renderWithProviders` from `src/test/utils.tsx` (real English strings). The API is stubbed with `vi.mock('@/lib/api', ...)`; there is no MSW. `src/pages/pages.contract.test.ts` and `src/components/components.contract.test.ts` import every page and component to catch modules that break on import.

## Map: backend

Paths are under `backend/`.

- **Wiring**: routers included in `app/main.py`; Celery app and beat schedule in `app/worker.py` (ADR 0007); CLI `app/cli.py`; settings `app/core/config.py` (`get_settings`), flags `app/core/feature_flags.py`; database `app/core/database.py` (`Base`, `get_async_session`); rate limits `app/core/rate_limit.py`.
- **Workspace context**: `app/core/workspace_context.py` (`current_workspace`, `current_writable_workspace`, `WorkspaceContext`), `app/core/shared_scope.py` (`in_bound_workspace`), `app/core/module_gate.py`, `app/core/app_clock.py` (`app_today`, `today_in`).
- **Workspaces, members, modules**: router `app/api/workspaces.py`; `app/services/workspace_service.py` (`require_membership`, `add_member`, `update_member_role`, `archive_workspace`), `app/services/module_service.py` (`resolve_modules`); model `app/models/workspace.py`. Tests `tests/test_workspace*.py`, `tests/test_module_*.py`, `tests/test_write_permission_*.py`.
- **Auth**: `app/core/auth.py` (fastapi-users, JWT, `current_active_user`); routers `app/api/custom_auth.py` (login, 2FA step), `oidc_auth.py`, `passkeys.py` (+ `app/core/webauthn.py`), `two_factor.py`, `access_tokens.py`, `setup.py`; `app/services/access_token_service.py`, scopes `app/core/access_token_scopes.py`, OIDC-only switch `app/core/auth_policy.py`. Tests `tests/test_auth_api.py`, `test_oidc_auth.py`, `test_passkeys.py`, `test_two_factor.py`, `test_access_tokens.py`.
- **Accounts and balances**: router `app/api/accounts.py`; `app/services/account_service.py` (`get_account_with_balances`, `get_running_balances`, `get_account_balance_history`, `close_account`); model `app/models/account.py`. (`balance_service.py` is group balances, not accounts.) Tests `tests/test_account*.py`.
- **Credit cards and bills**: `app/services/credit_card_service.py` (`get_cycle_dates`, `compute_effective_date`), bills through `account_service.get_credit_card_bills`; model `app/models/credit_card_bill.py`; accounting mode in `app/services/admin_service.py` (ADR 0018). Tests `tests/test_credit_card_accounting_mode.py`, `tests/test_accounts_shared_line_bills.py`.
- **Loans and mortgages**: router `app/api/mortgage_payments.py`; `app/services/loan_schedule.py` (`build_schedule`), `app/services/mortgage_service.py`, `account_service.get_loan_schedule`. Tests `tests/test_loan_*.py`, `tests/test_mortgage_payments_*.py`.
- **Transactions**: router `app/api/transactions.py` (CRUD, calendar, transfers, counterparts); `app/services/transaction_service.py` (`create_transaction`, `update_transaction`, `create_transfer`, `create_installment_series`, `bulk_update_category`); model `app/models/transaction.py`. Tests `tests/test_transaction*.py`, `tests/test_transfer_api.py`.
  - Splits between people: `app/services/split_service.py` (`replace_splits`), model `transaction_split.py`.
  - Category lines (ADR 0017): `app/services/category_line_service.py` (`replace_lines`, `materialize`), models `transaction_category_line.py`, `recurring_transaction_category_line.py`. Tests `tests/test_category_lines_*.py`, `tests/test_category_parts_structure.py`.
  - Transfer detection (ADR 0018): `app/services/transfer_detection_service.py` (`detect_transfer_pairs`), exposed in `app/api/connections.py`; policy `app/services/reconciliation_policy.py`.
  - Attachments: router `app/api/attachments.py`, `app/services/attachment_service.py`. Calendar: `transaction_calendar_service.py`. Matching: `transaction_match_service.py`, `text_similarity.py`.
- **Import**: router `app/api/import_transactions.py` (`/api/transactions/import/*`); `app/services/import_service.py` (`parse_csv`, `parse_ofx`, `parse_qif`, `parse_camt`, `import_transactions`); undo through `app/api/import_logs.py`. Tests `tests/test_import_*.py`.
- **Categories**: routers `app/api/categories.py`, `category_groups.py`; `app/services/category_service.py`, `category_group_service.py`, `category_defaults.py` (localized defaults), `category_statement_service.py`. Tests `tests/test_categor*.py`.
- **Payees**: router `app/api/payees.py`; `app/services/payee_service.py` (`get_or_create_payee`, `merge_payees`). Tests `tests/test_payee*.py`.
- **Rules**: router `app/api/rules.py`; `app/services/rule_service.py` (`apply_rules_to_transaction`, `apply_all_rules`, `preview_rule`), `rule_engine.py` (`evaluate_conditions`, `apply_rule_actions`), `rule_packs.py` (packs in `app/data/rule_packs/`). Tests `tests/test_rule*.py`.
- **Recurring**: router `app/api/recurring_transactions.py`; `app/services/recurring_transaction_service.py` (`generate_pending`), `recurring_detector_service.py`, `recurring_match_service.py`; task `app/tasks/recurring_tasks.py`. Tests `tests/test_recurring*.py`.
- **Budgets and goals**: routers `app/api/budgets.py`, `goals.py`; `app/services/budget_service.py` (`get_budget_vs_actual`, `get_budget_rollover_summary`, `get_budget_suggestions`), `goal_service.py`. Tests `tests/test_budget*.py`, `tests/test_goal*.py`.
- **Reports, dashboard, forecast**: routers `app/api/reports.py`, `dashboard.py`, `forecast.py`; `app/services/report_service.py`, `dashboard_service.py`, `cashflow_forecast_service.py`; income and expense semantics in `app/services/_query_filters.py`. Tests `tests/test_report*.py`, `tests/test_dashboard*.py`, `tests/test_cashflow_forecast.py`.
- **Search**: router `app/api/search.py`, `app/services/search_service.py` (`search_all`).
- **Assets**: routers `app/api/assets.py`, `asset_groups.py`; `app/services/asset_service.py`, `asset_transaction_service.py`, `asset_import_service.py`; prices `app/providers/market_price.py`, `tesouro_direto.py`; task `app/tasks/asset_tasks.py`. Tests `tests/test_asset*.py`, `tests/test_tesouro_direto_*.py`.
- **Collections**: router `app/api/collections.py`, `app/services/collection_service.py`.
- **Shared expense groups**: router `app/api/groups.py`; `app/services/group_service.py`, `settlement_service.py`, `group_summary_service.py`, `balance_service.py` (`compute_balances`). Tests `tests/test_group*.py`, `tests/test_settlement_service*.py`.
- **Bank connections and sync** (ADR 0006, ADR 0023): router `app/api/connections.py`; `app/services/connection_service.py` (`sync_connection`, `handle_oauth_callback`), `provider_settings.py`; providers `app/providers/base.py`, `pluggy.py`, `enable_banking.py`, `simplefin.py`, registry `app/providers/__init__.py`; task `app/tasks/sync_tasks.py`. New providers are plugins (ADR 0023): contract `app/plugins/contract.py`, `app/plugins/provider.py` (`PluginPayloadProvider`), plugin connections `app/plugins/connections.py`. Tests `tests/test_connection*.py`, `tests/test_providers*.py`, `tests/test_sync_*.py`, `tests/test_provider_plugins.py`.
- **FX rates** (ADR 0008): routers `app/api/fx_rates.py`, `currencies.py`; `app/services/fx_rate_service.py` (`sync_rates`, `convert`, `stamp_primary_amount`); provider `app/providers/openexchangerates.py`; task `app/tasks/fx_rate_tasks.py`. Tests `tests/test_fx_rates.py`.
- **Reconciliation**: router `app/api/reconciliation.py`; `app/services/reconciliation_engine.py` (`evaluate`, pure), `reconciliation_service.py`, `reconciliation_policy.py`, `reconciliation_rule_service.py`, `reconciliation_suggestion_service.py`, `reconciliation_history_service.py`. Tests `tests/test_reconciliation*.py`.
- **Invoices**: routers `app/api/invoices.py`, `invoice_attachments.py`, `invoice_schedules.py`, `products.py`, `public_invoices.py` (shared link, no auth); `app/services/invoice_service.py` (`issue_invoice`, `allocate`, `derive_state`), `invoice_schedule_service.py` (`generate_due`), `product_service.py`, `invoice_document.py`, `invoice_pdf.py`; task `app/tasks/invoice_schedule_tasks.py`. Tax ids by country: `app/fiscal/` (packs in `app/fiscal/jurisdictions/*.toml`). Tests `tests/test_invoice*.py`, `tests/test_fiscal_*.py`.
- **Admin and settings**: router `app/api/admin.py` (users, instance settings, provider settings, timezone); `app/services/admin_service.py`; widget layouts `app/api/ui_layouts.py`. Tests `tests/test_admin_*.py`.
- **Export and backup**: router `app/api/export.py`, `app/services/backup_service.py`. Test `tests/test_export_api.py`.
- **Storage** (ADR 0019): `app/providers/storage.py`, `local_storage.py`, `s3_storage.py`, `get_storage_provider` in `app/providers/__init__.py`.
- **Plugins** (ADR 0015): router `app/api/plugins.py`; `app/plugins/` (`service.py`, `manifest.py`, `index.py`, `sync.py`, `runner.py`, `vault.py`, `provenance.py`); sandbox `plugin_runner/`; task `app/tasks/plugin_tasks.py`; example bank sync plugin `examples/plugins/demo-bank/` (synced in `tests/test_example_provider_plugin.py`). Tests `tests/test_*_plugins.py`, `tests/test_plugin_runner.py`.
- **AI agents** (ADR 0014): `app/agents/` (`app/agents/api/`, `app/agents/services/`, `app/agents/runtime/executor.py`, `app/agents/providers/`, `app/agents/mcp/client.py`, `app/agents/tasks/ingest.py`); MCP server `mcp_server/` (`mcp_server/registry.py`, `mcp_server/auth.py`, `mcp_server/tools/`). Tests `tests/test_agents_*.py`, `tests/test_mcp_*.py`.
- **Migrations**: `alembic/versions/NNN_name.py`; chain check `scripts/check_migration_chain.py`.

Every path cited in this file and in `docs/adr/` is checked by `.github/ci/check_doc_paths.py` in CI: when you rename or move a file, update the map in the same pull request.

## Map: frontend

Paths are under `frontend/src/`.

- **Routes**: all in `App.tsx`, pages lazy loaded. Guards in `components/`: `protected-route.tsx`, `admin-route.tsx`, `module-route.tsx`, `agents-route.tsx`, `plugins-route.tsx`.
- **Shell**: `components/app-layout.tsx` (sidebar, header, user menu, command palette, global chat, onboarding tour, update banner); sidebar items `lib/nav-items.ts` (`visibleNavItems`); `components/workspace-switcher.tsx`, `collection-selector.tsx`, `command-palette.tsx`.
- **Contexts** (`contexts/`): `useAuth()` (user, token, login, logout), `useWorkspace()` (current workspace, role, `canWrite`, `hasModule`, `switchWorkspace`), `useCollectionFilter()` (active collection's accounts).
- **API client**: `lib/api.ts`, one axios instance on `/api` with `Authorization` and `X-Workspace-Id` headers, grouped by resource (`api.transactions.*`, `api.budgets.*`, ...). DTO types in `types/index.ts`. Error messages in `lib/api-errors.ts`, `lib/api-error-translations.ts`.
- **Queries**: keys are inline arrays named after the resource (`['accounts']`, `['groups', id, 'balances']`); shared invalidation in `lib/invalidate-queries.ts` (`invalidateFinancialQueries`, `invalidateCategoryQueries`). A workspace switch resets the whole cache.
- **Formatting**: money and numbers `lib/format.ts` (`formatCurrency`, `parseAmountInput`); dates `lib/date-utils.ts`, `lib/report-period.ts`; privacy mode `hooks/use-privacy-mode.ts`.
- **i18n**: setup `lib/i18n.ts` (en and pt-BR bundled, others lazy); strings in `locales/*.json`, one `translation` namespace keyed by feature; parity test `locales/i18n.test.ts`.
- **UI kit**: `components/ui/` (shadcn style on Radix: button, dialog, select, popover, table, tabs, tooltip, sonner toasts, ...); shared pickers `category-select.tsx`, `currency-select.tsx`, `payee-select.tsx`, `icon-picker.tsx`. Theme `components/theme-provider.tsx`, tokens `index.css` and `lib/themes.ts`. Icon-only buttons need an accessible label (jsx-a11y rule is an error).
- **Charts**: Recharts in the pages that use them and `components/dashboard-widgets.tsx`; Sankey `components/reports/CashflowSankey.tsx`. Widget catalog and layouts `lib/library.ts`, `hooks/use-layout.ts`.
- **Pages** (`pages/`), each with its components in `components/`:
  - Dashboard `dashboard.tsx` (`widget-grid.tsx`, `dashboard-widgets.tsx`, `transaction-drill-down.tsx`).
  - Transactions `transactions.tsx` (`transaction-dialog.tsx`, `transactions-filter-bar.tsx`, `transfer-dialog.tsx`, `transaction-category-lines-section.tsx`, `transaction-attachments.tsx`).
  - Accounts `accounts.tsx`, `account-detail.tsx` (`bank-connect-dialog.tsx`, `loan-schedule-card.tsx`); bank OAuth return `oauth-callback.tsx`.
  - Import `import.tsx` (`import-review-table.tsx`, `import-history.tsx`, `asset-import-panel.tsx`).
  - Rules and reconciliation `rules.tsx` (`rule-dialog.tsx`, `components/reconciliation-*.tsx`).
  - Categories `categories.tsx`; payees `payees.tsx`; collections `collections.tsx`; budgets `budgets.tsx`; goals `goals.tsx`; recurring `recurring.tsx`.
  - Assets `assets.tsx`; reports `reports.tsx` (`components/reports/`).
  - Shared expense groups `groups.tsx`, `group-detail.tsx`.
  - Invoices `invoices.tsx`, `invoice-detail.tsx`, `invoice-schedules.tsx`, `invoice-schedule-detail.tsx`, `products.tsx` (`invoice-ui.tsx`, `invoice-line-editor.tsx`, `invoice-document.tsx`); public shared invoice `shared-invoice.tsx` (`/i/:token`, no auth).
  - Workspace settings `workspace-settings.tsx`; admin `pages/admin/settings.tsx` (`components/provider-settings*.tsx`).
  - Plugins `plugins.tsx`, `plugin-detail.tsx` (`components/plugin-*.tsx`); agents `agents-list.tsx`, `agent-detail.tsx`, `agent-connections.tsx` (`components/agents/`, streaming `lib/agents-stream.ts`).
  - Auth `login.tsx`, `register.tsx`, `setup.tsx` (first run), `oidc-callback.tsx`.
- **Onboarding tour**: `components/onboarding-tour.tsx`, targets marked with `data-tour="..."`.
