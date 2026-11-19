# Lovenest decision records

Decisions this fork made about how Lovenest differs from upstream Securo: investment tracking, the ledger rules behind the tax planner, and how reimbursements and income are counted. Cite them as "Lovenest ADR NNNN" so they stay distinct from upstream's ADRs, whose numbers overlap.

`docs/adr/` is upstream's and upstream-owned: the fork never edits it, so a sync merges cleanly. Lovenest decisions go here.

## Adding one

- Take the next number in this directory and name the file `NNNN-the-decision-as-a-sentence.md`.
- Open with a `# Title` that states the decision, then the reasoning in prose. Name the code and tests that carry it.
- A merged record is not rewritten. A later decision amends or supersedes it with a note at the top of the older one, as in 0007.

## Index

- [0001 Investment tracking lives on the assets page](0001-investment-tracking-lives-on-the-assets-page.md)
- [0002 Tax lots are derived from the trade ledger](0002-tax-lots-are-derived-not-stored.md)
- [0003 FIFO decides which lots a sale consumed](0003-fifo-decides-which-lots-a-sale-consumed-not-what-it-gained.md)
- [0004 The wash sale warning fires only where a deduction is at stake](0004-the-wash-sale-warning-fires-only-where-a-deduction-is-at-stake.md)
- [0005 A cost-basis import keys on content and counts repeats](0005-a-cost-basis-import-keys-on-content-and-counts-repeats.md)
- [0006 A hand-set value outranks anything automatic](0006-a-hand-set-value-outranks-anything-automatic.md)
- [0007 A synced ledger is believed only when it reconciles](0007-a-synced-ledger-is-believed-only-when-it-reconciles.md)
- [0008 An exchange transaction is classified before it is believed](0008-an-exchange-transaction-is-classified-before-it-is-believed.md)
- [0009 A contribution is recorded, not derived](0009-a-contribution-is-recorded-not-derived.md)
- [0010 A transfer trace ends where funds are pooled](0010-a-transfer-trace-ends-where-funds-are-pooled.md)
- [0011 A credit's category decides whether it is income](0011-a-credits-category-decides-whether-it-is-income.md)
- [0012 A reimbursement is booked against the expense it repays](0012-a-reimbursement-is-booked-against-the-expense-it-repays.md)
