# Documentation index

Find the owner of a fact here before opening a doc. Code, migrations, schemas and tests decide what the system does; a doc that disagrees with them is the one that is wrong.

## Authority by fact type

| Question | Owner |
| --- | --- |
| What the system does, data shape | code, `backend/alembic/versions/`, `backend/app/schemas/`, tests |
| Repository rules, branch and PR workflow, commands | [`AGENTS.md`](../AGENTS.md), [`CONTRIBUTING.md`](../CONTRIBUTING.md) |
| Rules for editing a path | [`.claude/rules/`](../.claude/rules/) |
| What a domain term means | [`CONTEXT.md`](../CONTEXT.md) |
| Why a decision was made | [Decisions](#decisions) below |
| How a feature or operation works | feature docs below |
| Security reports | [`SECURITY.md`](../SECURITY.md) |

## Decisions

- [Lovenest decisions](lovenest/adr/README.md) (`docs/lovenest/adr/`, cited as "Lovenest ADR NNNN"): the fork's own, with their index.
- [Upstream decisions](adr/README.md) (`docs/adr/`, cited as "ADR NNNN"): upstream-owned, so the fork never edits or adds here. Includes upstream's [template](adr/0000-template.md).

## Feature and operations docs

- [Self-hosting and pull deployment](self-hosting.md)
- [Automatic bank synchronization](automatic-bank-sync.md)
- [Owned historical evidence](onchain-history.md)
- [Reviewed owned transfers](owned-transfers.md)
- [Recovery evidence review](recovery-evidence.md)
- [Investment ledger RFC #235](rfc-235-investment-ledger.md)
- [Upgrading PostgreSQL](postgres-upgrade.md)
- [Fork workflow options](lovenest-branch-workflow.html)
- [Bank provider plugins](plugins/bank-provider-plugins.md): writing a bank sync provider as a plugin (upstream)
- [Provider settings](provider-settings.md): per-provider configuration in the UI (upstream)

## Agent tooling (`docs/agents/`)

- [Domain docs](agents/domain.md): how skills read `CONTEXT.md` and the ADRs
- [Issue tracker](agents/issue-tracker.md)
- [Triage labels](agents/triage-labels.md)

## Elsewhere in the tree

- [`backend/scripts/README.md`](../backend/scripts/README.md)
- [`backend/tests/api_contract/README.md`](../backend/tests/api_contract/README.md): the API contract sweep and its baseline
- [`extras/tax/README.md`](../extras/tax/README.md): the tax planner sidecar
