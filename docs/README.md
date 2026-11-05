# Documentation index

Find the owner of a fact here before opening a doc. Code, migrations, schemas and tests decide what the system does; a doc that disagrees with them is the one that is wrong.

## Authority by fact type

| Question | Owner |
| --- | --- |
| What the system does, data shape | code, `backend/alembic/versions/`, `backend/app/schemas/`, tests |
| Repository rules, branch and PR workflow, commands | [`AGENTS.md`](../AGENTS.md), [`CONTRIBUTING.md`](../CONTRIBUTING.md) |
| Rules for editing a path | [`.claude/rules/`](../.claude/rules/) |
| What a domain term means | [`CONTEXT.md`](../CONTEXT.md) |
| Why a decision was made | [`docs/adr/`](adr/) |
| How a feature or operation works | feature docs below |
| Security reports | [`SECURITY.md`](../SECURITY.md) |

## Decisions (`docs/adr/`)

- [0001 Investment tracking lives on the assets page](adr/0001-investment-tracking-lives-on-the-assets-page.md)
- [0002 Tax lots are derived from the trade ledger](adr/0002-tax-lots-are-derived-not-stored.md)
- [0003 FIFO decides which lots a sale consumed](adr/0003-fifo-decides-which-lots-a-sale-consumed-not-what-it-gained.md)
- [0004 The wash sale warning fires only where a deduction is at stake](adr/0004-the-wash-sale-warning-fires-only-where-a-deduction-is-at-stake.md)
- [0005 A cost-basis import keys on content and counts repeats](adr/0005-a-cost-basis-import-keys-on-content-and-counts-repeats.md)
- [0006 A hand-set value outranks anything automatic](adr/0006-a-hand-set-value-outranks-anything-automatic.md)
- [0007 A synced ledger is believed only when it reconciles](adr/0007-a-synced-ledger-is-believed-only-when-it-reconciles.md)
- [0008 An exchange transaction is classified before it is believed](adr/0008-an-exchange-transaction-is-classified-before-it-is-believed.md)
- [0009 A contribution is recorded, not derived](adr/0009-a-contribution-is-recorded-not-derived.md)
- [0010 A transfer trace ends where funds are pooled](adr/0010-a-transfer-trace-ends-where-funds-are-pooled.md)
- [0011 A credit's category decides whether it is income](adr/0011-a-credits-category-decides-whether-it-is-income.md)
- [0012 A reimbursement is booked against the expense it repays](adr/0012-a-reimbursement-is-booked-against-the-expense-it-repays.md)

## Feature and operations docs

- [Self-hosting and pull deployment](self-hosting.md)
- [Automatic bank synchronization](automatic-bank-sync.md)
- [Owned historical evidence](onchain-history.md)
- [Reviewed owned transfers](owned-transfers.md)
- [Recovery evidence review](recovery-evidence.md)
- [Investment ledger RFC #235](rfc-235-investment-ledger.md)
- [Upgrading PostgreSQL](postgres-upgrade.md)
- [Fork workflow options](lovenest-branch-workflow.html)

## Agent tooling (`docs/agents/`)

- [Domain docs](agents/domain.md): how skills read `CONTEXT.md` and the ADRs
- [Issue tracker](agents/issue-tracker.md)
- [Triage labels](agents/triage-labels.md)

## Elsewhere in the tree

- [`backend/scripts/README.md`](../backend/scripts/README.md)
- [`backend/tests/api_contract/README.md`](../backend/tests/api_contract/README.md): the API contract sweep and its baseline
- [`extras/tax/README.md`](../extras/tax/README.md): the tax planner sidecar
