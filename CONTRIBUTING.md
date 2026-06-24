# Contributing to lovenest

Thanks for your interest in contributing to **lovenest**!

## What lovenest is

lovenest is a downstream fork of [Securo](https://github.com/securo-finance/securo) — the
self-hosted personal finance app. It tracks upstream Securo closely and adds a small set of
changes on top:

- An **OIDC-primary login layout** (single sign-on front and center).
- A **Google OIDC `at_hash` fix** (also offered upstream as PR #350).
- A **SimpleFIN credit-card balance-sign fix** (also offered upstream as PR #351).

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
            │  maintainer merges main → custom
            ▼
         custom ◄────── PR ────── your-fork/feature-branch   ← ALL contributions land here
         (default branch + working branch)
```

- **`main`** is a clean mirror of upstream `securo-finance/securo` `main`. **Nobody commits to or
  opens PRs against `main`.** It only ever fast-forwards from upstream during a sync.
- **`custom`** is lovenest's working branch and the GitHub **default branch**. **All contributions
  target `custom`.**

### Contributor flow

1. Fork lovenest on GitHub.
2. Clone your fork and branch off `custom`:
   ```bash
   git clone https://github.com/your-username/lovenest.git
   cd lovenest
   git checkout custom
   git checkout -b feature/your-feature
   ```
3. Make your changes, run the checks below, and open a PR **into `custom`**.

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

### Backend (Python 3.13, from `backend/`)

```bash
cd backend
pip install -e ".[dev]"        # first time only — installs ruff, pytest, dev deps
ruff check .                   # lint (must be clean)
pytest --cov=app --cov-report=term-missing --cov-fail-under=60   # tests + coverage gate
```

CI fails the build if `ruff check` reports any issues or if coverage drops below **60%**. Add tests
for new backend behavior.

### Frontend (Node 22, from `frontend/`)

```bash
cd frontend
npm ci
npm run lint                   # ESLint (must be clean)
npm run build                  # type-check + production build (must succeed)
```

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

- Open PRs against **`custom`** (never `main`).
- Keep PRs focused — one feature or fix each.
- Make sure both CI jobs pass: `ruff check` + `pytest` clean, frontend lint + build green.
- Add tests for new backend functionality.
- Update translations if you add user-facing strings (EN + PT-BR).
- The PR template asks **"Could this also benefit upstream Securo?"** — answer honestly (see below).

## Syncing upstream (maintainer)

Only the maintainer syncs upstream. The flow is:

```bash
git fetch upstream
git checkout main && git merge upstream/main     # fast-forward the mirror
git checkout custom && git merge main            # bring upstream changes into custom
docker compose -f docker-compose.prod.yml build  # rebuild images
```

## Contributing changes upstream

Some changes belong upstream, not just in lovenest — bug fixes and broadly useful features that
aren't specific to this fork's OIDC-first focus (the `at_hash` and SimpleFIN fixes are good
examples, submitted upstream as #350/#351). **If your change makes sense for Securo generally, flag
it in your PR** so the maintainer can offer it to
[securo-finance/securo](https://github.com/securo-finance/securo). Land it in lovenest's `custom`
first; upstreaming happens separately against Securo's own contribution process.

## Code of conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By
participating, you agree to uphold it.
