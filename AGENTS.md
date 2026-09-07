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
- Local image tags: `lovenest-backend:0.15.1-lovenest`, `lovenest-frontend:0.15.1-lovenest`.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **lovenest** (18296 symbols, 44017 relationships, 854 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "lovenest"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "lovenest" --repo .`.
- MUST warn on HIGH/CRITICAL `risk` pre-edit; never use `riskSharedAxes` to waive a HIGH/CRITICAL `risk` warning. Compare File/symbol: MCP File omits axes; Graph-RAG expands File.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set is not evidence the symbol is unused — it can also mean the callers are not resolvable by the index (plain-object property access, dynamic dispatch, cross-language calls). `impact` pairs `UNKNOWN` with a `riskNote` saying so. Confirm with a text search before treating the symbol as safe to change or delete; do not proceed on the strength of a zero.
- **MUST use `query({search_query: "concept"})` for concepts/flows, `context({name: "symbolName"})` for a named symbol, or `impact` for blast radius, on read-only callers, dependencies, imports, or execution flow.** Graph first; text search only for empty/`UNKNOWN`/literals.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis, and never read `UNKNOWN` as an all-clear — it means the walk could not answer, which is the one verdict that requires confirming by other means.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.

## Resources

| Resource | Use for |
| --- | --- |
| `gitnexus://repo/lovenest/context` | Codebase overview, check index freshness |
| `gitnexus://repo/lovenest/clusters` | All functional areas |
| `gitnexus://repo/lovenest/processes` | All execution flows |
| `gitnexus://repo/lovenest/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
