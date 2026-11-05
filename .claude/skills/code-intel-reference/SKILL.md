---
name: code-intel-reference
description: >
  Reference for lovenest's code-search and code-intelligence tooling: the code-intel MCP server
  (DI chains, frontend-to-backend routes, JSX usage, symbol resolution, doc references), probe,
  and the pyrefly and TypeScript language servers. Use when a code-intel, probe or LSP call errors
  or returns empty, when setting up or debugging a checkout or worktree for them, when choosing
  which search tool answers a question, or when a code-intel answer looks stale or incomplete.
---

# Code intelligence: lovenest reference

[AGENTS.md, Code Search & Navigation](../../../AGENTS.md#code-search--navigation) holds the day-to-day routing. This skill holds setup, blind spots and troubleshooting.

## Authority

Check these before changing the tooling:

- Registration: [`.mcp.json`](../../../.mcp.json). Config: [`.code-intel/config.toml`](../../../.code-intel/config.toml). Pin: [`.code-intel/requirements.txt`](../../../.code-intel/requirements.txt).
- The tool itself lives in <https://github.com/ADolkun/code-intel>. Its README has the config reference, response contract and analysis notes; the pinned commit is the one in the pin file.
- Installs of `backend/.venv` and `frontend/node_modules`: [`tools/dev/sync-deps.sh`](../../../tools/dev/sync-deps.sh). It does not touch code-intel.
- `backend/mcp_server/` is the product's MCP server for users' agents. It is unrelated to this tooling.

## Tool routing

| Question | Tool |
|----------|------|
| A known identifier or literal: definitions, construction sites (`rg 'ClassName\('`), rename sites including comments and docs | `rg` with a path |
| A concept when you do not know the identifier; a whole function or class body | probe `search_code` / `extract_code` |
| Callers of a method whose name is defined in several places, shadowed or re-exported symbols, multi-hop callers | LSP |
| FastAPI `Depends` chains and the routes a dependency guards | code-intel `di_deps` / `di_reverse` |
| A route's full URL, and which frontend call reaches it | `route_match` / `route_unmatched` |
| JSX render sites of a component; which same-name definition a name means | `jsx_usage` / `symbol_resolve` |
| Docs that mention a file, route or symbol; docs to update after a change | `doc_refs` |
| Searching Markdown | `rg` for a term, probe with `path` set to `docs/` for a concept |

LSP sees `Depends(fn)` as a reference, not a call, and an `Annotated` alias consumer points at the alias. That is why DI questions go to `di_reverse`.

## code-intel

Claude Code shows the tools as `mcp__code-intel__<tool>`. The server is `git -c alias.code-intel-mcp=!exec uv run ... code-intel mcp`: git runs an alias from the work tree's top level, so the relative pin path resolves even when the client starts in a subdirectory.

- **Arguments are strict.** An unknown name is rejected (`di_reverse` takes `dep_function`, `jsx_usage` takes `component_name`). Read the tool's schema before the first call.
- **Lists default to `limit=50`.** Truncation is always stated. Pass `limit` for more, or `null` for all.
- **Replies lead with a notice** when something limits the answer: `ⓘ` is a known blind spot, `⚠ INCOMPLETE` means content was lost or the frontend was not analyzed. Run `status` for the details and confirm gaps with `rg` or LSP.
- **`status` reports `Complete: unknown`** until the first analysis has run. Call any query tool, then `status` again.
- **Analysis walks the files git sees** (tracked, or untracked and not ignored), so a new file counts before it is staged. Caches invalidate themselves when files change; `refresh` is for ruling out a cache fault.
- **Config edits need no restart.**
- **Restart after a pin bump.** A reply that starts with "Analyzer code changed since this server started" means the pin file or the tool's code changed after the server loaded it. Restart the server (or start a new session).
- **Bump the pin deliberately.** Edit `.code-intel/requirements.txt` to the new commit and tag, run `config-check` and `verify` (below), and mention the bump in the PR.

Command line, from the repository root, with the launcher the MCP entry uses:

```bash
uv run --no-project --python 3.14 --with-requirements .code-intel/requirements.txt code-intel <command>
```

| Command | Use |
|---------|-----|
| `setup` | Provisions the TypeScript the frontend analysis needs now, in `~/.cache/code-intel` (shared by every checkout, keyed by the tool's lockfile). Optional: the first use provisions it anyway, and CI runs `setup` explicitly to stay deterministic |
| `config-check` | Validates `.code-intel/config.toml`; an error names the key and file |
| `verify` | Cross-checks the analyzers against each other and the working tree; expect every check to pass (`library-capture` needs `backend/.venv`) |
| `doc-check` | The docs check: broken links, stale code references, orphaned docs, agent files |
| `summary` | Counts for the current tree |
| `capture` | Rewrites the library capture by importing the app with `backend/.venv` and the synthetic env in the config; needs a venv synced to the lock (`uv sync --locked` in `backend/`). `--if-stale` checks statically first and starts the app only when the capture is stale; `--check` imports the app and exits non-zero when the file differs |

### Known limits on lovenest

- **Library routes come from a committed capture.** The fastapi-users routers and the `current_active_user` and `current_superuser` dependencies are built by installed packages. [`.code-intel/library-capture.json`](../../../.code-intel/library-capture.json) records them from the real app, so `route_match`, `di_deps` and the docs check see them. The MCP server and `doc-check` only read the file; they never import the app. Library routes match frontend calls but stay out of the backend-only list and the coverage figure.
- **The capture refreshes itself on commit and re-lock.** The `code-intel-capture` pre-commit hook in `tools/dev/git-hooks.yaml` and `backend/scripts/lock.sh` run `capture --if-stale`, which recaptures after a lock bump of a captured package or an edit to a library `include_router(...)` or a captured binding (such as `current_active_user` in `app/core/auth.py`). A rewrite fails the commit; review the JSON diff, stage it and commit again. Without the hook installed, run `capture --if-stale` by hand. The `Backend (lint + tests)` job runs `capture --check` and fails when the file is out of date; it also catches a changed value passed to a library factory, which static checks cannot see.
- **`library_capture_stale`** means the capture no longer matches the tree: a captured package's version differs from `backend/uv.lock`, or an include or binding it describes changed. The analysis still applies the capture, so answers can be wrong until you run `capture` again. An include with no entry in the capture is reported as `unresolved_include`.
- **Flag-mounted routers.** The agents and on-chain routers are mounted behind flags in `app/main.py` and are reported as `conditional_router_mount` / `conditional_import`; their routes are analyzed as if enabled.
- **Frontend calls** are recognized through the `api` axios instance, bare `axios` calls and `fetch`, as `[[frontend.clients]]` declares. A URL string that is never passed to one of them (a link or download built into a component) is a backend-only route.
- **Frontend pages** come from the literal `<Route path>` values in `frontend/src/App.tsx`. A computed `path={...}` is invisible.

## LSP setup

Claude Code's `LSP` tool takes an operation, a file and a 1-based line and character on the identifier's name. It needs language servers registered by a user-scope plugin; the repository carries no LSP config. The plugin should start:

- Python: `pyrefly lsp` from `backend/.venv` (the [`backend/pyproject.toml`](../../../backend/pyproject.toml) dev group pins `pyrefly>=1.4.1`).
- TypeScript: the native server of the frontend's TypeScript 7, `node frontend/node_modules/typescript/bin/tsc --lsp --stdio`, rooted at `frontend/`.

Both read the checkout's own installs, so a worktree needs `backend/.venv` and `frontend/node_modules`. The sync hook ([`tools/dev/sync-deps.sh`](../../../tools/dev/sync-deps.sh), installed per clone as AGENTS.md describes) creates them after `git worktree add`. Without them the server starts, cannot resolve `app` or third-party imports, and answers with quietly missing references.

- **Cold start.** The first call per language can fail or return a partial answer. Retry once.
- **Python calls one at a time.** pyrefly cancels a pending request when another file opens ("canceled due to subsequent mutation"). Re-run the cancelled call alone.
- **Protocol dispatch.** A call on a variable typed as a `Protocol` resolves to the Protocol's method, so `findReferences` on the implementing class's method misses those calls. `symbol_resolve` on the method name lists the Protocols to query too; `rg -n 'def NAME\b' backend/app` finds every same-name definition.
- **Decorated callers.** `incomingCalls` can report a decorated function at its decorator line. Get the `def` line with `rg -n '^\s*(async\s+)?def NAME\b'` before the next hop.
- **Construction sites.** LSP does not separate construction from annotations: use `rg 'ClassName\('`.
- **TypeScript call hierarchy** stops at `forwardRef` and anonymous callbacks; continue with `findReferences` on the component.
- **Rename** is not an `LSP` operation. List references and edit each site; comments and docs need `rg`.

## probe

Pinned in `.mcp.json` (no registry query at session start); bump it deliberately.

- `search_code` needs `path` to be an **absolute** directory. Use the narrowest directory that answers: a query scoped to all of `backend/app` returns a large block.
- Use `exact: true` for identifiers and single words; the default stemmed mode misses or floods them.
- Queries use ElasticSearch syntax: write `AND` / `OR` and quote phrases.
- The `grep` tool takes only `pattern`, `paths`, `ignoreCase`, `count` and `context`. `rg` filters by type and glob and handles multiline patterns, so prefer it.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Server `code-intel` or `probe` is missing, or "needs approval" | Claude Code asks you to approve project MCP servers once per user | Approve both when prompted, or run `/mcp` |
| `uv` or `npx` refuses to start in a worktree ("not trusted") | mise does not trust the directory, which happens for worktree roots outside the main checkout | `mise trust` in the worktree, or add the root to `trusted_config_paths` |
| `frontend_provisioning` | First use on this machine: the MCP server is installing TypeScript (about 5 s) | Retry in a few seconds; the CLI provisions synchronously instead |
| `frontend_unavailable` | No Node, or provisioning failed | Run the `setup` command above to see the error, then `status` |
| `runtime_incompatible` | The server runs on a Python older than `backend/.python-version` | The launcher passes `--python 3.14`; do not drop it |
| LSP returns empty, or only the symbol itself | Cold start | Retry once |
| LSP results miss imports and cross-file references | `backend/.venv` is missing or empty, so the server could not resolve `app` | `uv sync --locked` in `backend/` (or `tools/dev/sync-deps.sh`), then restart the client |
| TypeScript server exits before initializing | No `frontend/node_modules` | `npm ci` in `frontend/`, then restart the client |
| Reply starts "Analyzer code changed since this server started" | The pin or the tool's code changed after the server loaded it | Restart the MCP server |
| `doc-check` prints findings | A doc names a link, path, route or symbol that no longer exists, or an agent file is malformed | Fix the doc in the same change. Use `[[docs.ignore]]` or `[[docs.rules]]` in the config, with a reason, only for upstream-owned docs |
