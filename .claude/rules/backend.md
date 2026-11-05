---
paths:
  - "backend/**"
---

# Backend

- **Migrations form one line.** `backend/scripts/check_migration_chain.py` fails on a second head; CI runs it (`.github/workflows/lovenest-ci.yml`) and so does the `migration-chain` hook in `.pre-commit-config.yaml`. An upstream migration arriving in a sync is renumbered onto lovenest's head (`CONTRIBUTING.md`, "Adding a migration"); check first whether the fork already carries an equivalent migration.
- **Workspace scoping.** Routes take `ctx` from `current_workspace` (reads) or `current_writable_workspace` (writes), both in `app/core/workspace_context.py`, and pass `ctx.workspace.id` to services, which filter on `workspace_id`. Owner-only actions call `ctx.require_owner()`.
- **Optional modules.** A route that belongs to a workspace module uses `require_module` / `require_module_write` from `app/core/module_gate.py`. They answer 404, not 403, so a workspace without the module cannot tell it exists. Which modules a workspace has is decided in `app/services/module_service.py`, not in route code.
- **Flag-mounted routers.** The on-chain router (`ONCHAIN_ENABLED`) and the agents routers (`AGENTS_ENABLED`) are mounted conditionally in `app/main.py`. Add a router for a gated feature inside that block, so an install without the flag has no route and imports nothing.
- **Lint contract.** Ruff's rule selection is explicit in `[tool.ruff.lint]` in `backend/pyproject.toml`; widen it deliberately, in its own change.
- **Type gate.** ty is the gate (`[tool.ty]`, CI step "Type check with ty"). pyrefly only serves the editor and agents (`[tool.pyrefly]`); its errors are not a CI signal.
- **Dependencies.** Change `pyproject.toml` and run `backend/scripts/lock.sh`; commit `uv.lock` with it, because CI uses `uv sync --locked`.
