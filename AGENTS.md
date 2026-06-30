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

## Verify before PR

Run the smallest relevant check and report it clearly. Useful checks:

```bash
docker compose -f docker-compose.prod.yml config --quiet
(cd backend && ruff check . && pytest --cov=app --cov-report=term-missing --cov-fail-under=60)  # backend checks
(cd frontend && npm run lint && npm run build)  # frontend checks
```

## Safety

- Do not commit secrets, `.env`, backups, local credentials, or ignored handoff files.
- `HANDOFF.md` and `CONTINUE-HERE.md` are local operational docs and intentionally gitignored.
- Keep `docker-compose.prod.yml` project name as `securo` unless explicitly migrating Docker volumes/networks.
- Local image tags: `lovenest-backend:0.13.7-lovenest`, `lovenest-frontend:0.13.7-lovenest`.
