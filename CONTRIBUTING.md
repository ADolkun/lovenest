# Contributing to lovenest

Thanks for your interest in contributing to **lovenest**!

## What lovenest is

lovenest is a downstream fork of [Securo](https://github.com/securo-finance/securo) — the
self-hosted personal finance app. It tracks upstream Securo closely and adds a small set of
changes on top:

- An **OIDC-primary login layout** (single sign-on front and center).
- A **Google OIDC `at_hash` fix** (also offered upstream as PR #350).
- A **SimpleFIN credit-card balance-sign fix** (also offered upstream as PR #351).

## Where to Start

New here? The smoothest first contribution is a small, self-contained one:

- Browse the [open issues](https://github.com/securo-finance/securo/issues), especially those labeled `good first issue` or `help wanted`, and pick something that already has a clear scope.
- Small bug fixes, docs improvements, and translation updates are always welcome and don't need any prior discussion, just open the PR.
- Comment on an issue to let others know you're picking it up, so two people don't work on the same thing.

Starting from an existing issue means the work is already something we want, so your PR has a clear path to being merged.

## Before Large or Core Changes

For anything bigger, a new feature, a refactor, or a change to a core mechanism (accounts, transactions, budgets, the rules engine, workspaces, sync, and similar), we'd love to talk it through **before** you write the code. It helps us confirm the idea fits the project's direction and that it's the right moment to build it, and it saves you from investing time in a PR that might not land.

Good ways to align first:

- Open a [feature request](.github/ISSUE_TEMPLATE/feature_request.md) describing what you'd like to build.
- Comment on the related issue if one already exists.
- Chat with us on [Discord](https://discord.gg/rUqTKtQ9S4).

Once there's a shared understanding, go ahead and build. Large PRs that arrive without any prior discussion are harder to review and sometimes don't align with where the project is heading, so a quick conversation up front is the best way to make your contribution count.

## Using AI

Use it. We do. Parts of this codebase were written with AI. This isn't a policy against the tools.

It's a policy about ownership. **We don't review the AI, we review you.** When a PR arrives, the questions are the same as they've always been: does this person understand what they're proposing, can they explain why it's built this way, and will they still be around if it breaks. Whatever produced the diff doesn't change any of that.

So whatever you use, before you open the PR:

- **You own the approach, not just the output.** You decided the strategy and delegated the typing. If the model picked the architecture and you went along with it, you don't know the change well enough to defend it in review.
- **You're the quality gate.** The change holds to the standards of the code already here: naming, structure, tests, error handling. AI writes plausible code, and plausible isn't the bar.
- **It fits where the product is going.** A change can work and still be wrong for lovenest. Whether it belongs here is your call before it's ours.
- **You ran it.** Not "the tests should pass" — you ran them, you ran the app, you saw the change work.
- **The scope is what the issue asked for.** AI is generous with refactors nobody requested. Strip them. A thirty-file diff for a one-line bug goes back.
- **You're accountable after it merges.** If it breaks in three weeks, you're who we come to.

We won't ask which tools you used and we won't try to detect them. We'll read the code and ask questions. Contributors who understand their own work pass easily, and that was true long before any of this.

The same applies to issues. An issue produced by pointing a model at the repository and asking it to find problems is not a bug report. Tell us what you did, what happened, and what you expected.

## Development Workflow

All of the underlying application (FastAPI backend, React frontend, Celery workers, etc.) comes
from Securo. For questions about how the core app works, the upstream
[securo-finance/securo](https://github.com/securo-finance/securo) repo and docs are the source of
truth.

### License

lovenest is licensed under **AGPL-3.0**, the same copyleft license as Securo. AGPL-3.0 is
network-copyleft: if you run a modified version and let others interact with it over a network, you
must offer them the corresponding source. **By contributing, you agree your contributions are
licensed under [AGPL-3.0](LICENSE).**

## Branch model

This fork has a strict two-branch model. Read this before you open a PR.

```
 securo-finance/securo (upstream)
            │  git fetch upstream && git merge upstream/main
            ▼
          main ───────────────► (clean mirror of upstream; fast-forward only)
            │  maintainer merges main → lovenest
            ▼
       lovenest ◄────── PR ────── your-fork/feature-branch   ← ALL contributions land here
         (default branch + working branch)
```

- **`main`** is a clean mirror of upstream `securo-finance/securo` `main`. **Nobody commits to or
  opens PRs against `main`.** It only ever fast-forwards from upstream during a sync.
- **`lovenest`** is lovenest's working branch and the GitHub **default branch**. **All contributions
  target `lovenest`.**

### Contributor flow

1. Fork lovenest on GitHub.
2. Clone your fork and branch off `lovenest`:
   ```bash
   git clone https://github.com/your-username/lovenest.git
   cd lovenest
   git checkout lovenest
   git checkout -b feature/your-feature
   ```
3. Make your changes, run the checks below, and open a PR **into `lovenest`**.

## Development environment (build from source)

lovenest builds the backend and frontend images locally — there are no pre-pinned images on this
branch. You need Docker with Compose v2.

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
```

Then open the app on the configured port. Configure OIDC and any other secrets via environment as
described in the deployment docs before logging in.

## Running the checks CI runs

CI runs two jobs. Reproduce both locally before opening a PR.

### Backend (Python version from `backend/.python-version`, run from `backend/`)

```bash
cd backend
uv sync --all-extras   # first time only — builds .venv from uv.lock, same versions as CI
.venv/bin/ruff check .
.venv/bin/ty check .
.venv/bin/pytest --cov=app --cov-report=term-missing --cov-fail-under=60

# After changing dependencies in pyproject.toml: regenerate the lock and
# commit uv.lock along with it (CI enforces this)
./scripts/lock.sh

# After adding a migration: check the revision chain is still a single line
python3 scripts/check_migration_chain.py
```

CI fails the build if `ruff check` reports any issues or if coverage drops below **60%**. Add tests
for new backend behavior.

### Adding a migration

Number the file after the current head and chain it there, so
`backend/alembic/versions/` sorts in apply order:

```python
revision: str = "089"
down_revision: Union[str, None] = "088"
```

lovenest's head is ahead of upstream's because this fork carries its own
migrations, so an upstream migration arriving in a sync is renumbered onto
lovenest's head rather than kept at the number Securo gave it. CI catches a
clash: the Migration Chain job runs against your branch merged with the base,
so it fails there rather than on someone's `alembic upgrade head`.

### Frontend (Node 22, from `frontend/`)

```bash
cd frontend
npm ci
npm run lint                   # ESLint (must be clean)
npm run build                  # type-check + production build (must succeed)
npm test                       # Vitest (must pass)
```

Vitest and Testing Library tests should render through `renderWithProviders`
from `@/test/utils`, which wires up TanStack Query, the router, and i18n.
Assert on user-visible behavior.

### Adding a frontend dependency

`frontend/.npmrc` disables package install scripts and skips releases younger than seven days.
The cooldown requires npm 11.10 or newer, so upgrade npm before adding a dependency:

```bash
npm install --global npm@latest
cd frontend && npm install <package>
```

Commit both `package.json` and `package-lock.json`. If the newest package release is less than a
week old, wait or explain the exception in the PR.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org). Keep the subject imperative and
scoped to one change:

- `feat: make OIDC the primary login option`
- `fix: correct SimpleFIN credit-card balance sign`
- `fix(auth): validate Google OIDC at_hash`
- `docs: update contributor branch model`
- `refactor: simplify rule engine matching`

PR titles follow the same convention.

## Pull request guidelines

- Open PRs against **`lovenest`** (never `main`).
- Keep PRs focused — one feature or fix each.
- Make sure both CI jobs pass: `ruff` + `ty` + `pytest` clean, frontend lint + build + tests green.
- Add tests for new backend functionality.
- Update translations if you add user-facing strings (EN + PT-BR).
- The PR template asks **"Could this also benefit upstream Securo?"** — answer honestly (see below).

## Syncing upstream (maintainer)

Only the maintainer syncs upstream. The flow is:

```bash
git fetch upstream
git checkout main && git merge upstream/main     # fast-forward the mirror
git checkout lovenest && git merge main          # bring upstream changes into lovenest
docker compose -f docker-compose.prod.yml build  # rebuild images
```

## Contributing changes upstream

Some changes belong upstream, not just in lovenest — bug fixes and broadly useful features that
aren't specific to this fork's OIDC-first focus (the `at_hash` and SimpleFIN fixes are good
examples, submitted upstream as #350/#351). **If your change makes sense for Securo generally, flag
it in your PR** so the maintainer can offer it to
[securo-finance/securo](https://github.com/securo-finance/securo). Land it in lovenest's `lovenest`
first; upstreaming happens separately against Securo's own contribution process.

## Code of conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By
participating, you agree to uphold it.
