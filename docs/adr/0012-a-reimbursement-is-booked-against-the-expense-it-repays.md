# A reimbursement is booked against the expense it repays

ADR 0011 nets a reimbursement against its category, but only in the month it
posts. Trip meals in September repaid by an employer in October overstate
September and understate October; a $100 Zelle covering four months of a
family-plan subscription distorts the one month it lands in. The category was
right and the month was wrong.

So the user can link a reimbursement credit to the expense debits it repays,
with an amount per expense (`transaction_reimbursements`). A link takes its
amount out of both rows: the expense counts for less in its own month and
category, and the credit counts for less wherever ADR 0011 put it. Each row
keeps `1 - linked / |amount|` of itself (`reimbursement_unlinked_share`), and
the shared P&L helpers apply that share, so the dashboard, category breakdown,
reports, budgets and the transaction list footer all move together. The
fraction is unitless, so it scales primary-currency amounts as well as native
ones.

## Net cash flow is conserved

The linked amount leaves expenses on one side and leaves income (or a
spending offset) on the other, so income minus expenses across the months
involved is unchanged. That holds only while no row's links exceed its own
amount, which is the invariant every write path keeps: a link edit is checked
against both rows, an edit that shrinks a linked row below its links is
refused, and a sync that re-reports a smaller amount shrinks the links
proportionally to fit. Links are never grown to follow a larger amount — the
money that changed hands is what the user linked.

It also holds only while both rows are counted. So a link is in effect only
while both of its rows are in user P&L (`counts_as_user_pnl`, via
`reimbursement_link_side_counts`); while either is not, the link is inert on
both sides and each row counts as if it were unlinked. This is
decided in the reporting SQL rather than by guarding edits, because a row
leaves user P&L in many ways — a recategorization (single, bulk or by a
rule), a category flagged ignored or as a transfer, the row ignored or
excluded, its description rewritten into a card payment —
and a guard missed on any one of them would move net cash flow. The link
itself is kept, and comes back into effect when the row counts again.

Closing an account does not take its rows out of user P&L: period reports
keep a closed account's history (upstream securo#1133), so its links stay in
effect and its rows can still be linked. Until v0.16.5 closed accounts were
left out of the dashboard but not of budgets, and links on them were switched
off so the two views agreed.

## The unlinked remainder follows ADR 0011

A $259 credit with $117.09 linked contributes its remaining $141.91 exactly as
an unlinked credit would: income if its category is an income category, money
back on its category otherwise. Linking changes where the linked part is
counted, never what the rest of the credit means.

## Only rows that are plainly a credit and an expense

Both rows sit in the same workspace and the same currency (cross-currency
links are refused in this version), are posted and dated today or earlier,
and are neither group settlements, opening balances, transfers nor card
payments. A new link also needs both rows in user P&L,
so the user is told why instead of making a link that would be inert from
the start. A link made earlier stays editable after one of its rows has left:
reporting ignores it meanwhile. Link amounts are in that shared currency; when its FX rate moved
between the two dates, primary-currency totals are conserved only
approximately. Cash-flow reports are left alone: they report when cash moved,
and a link does not move cash.

## Group splits and reimbursements are exclusive

A split already says who owes what and is repaid through group settlements. A
row that is both split and reimbursed would have its cost reduced twice, so a
row with splits cannot be linked and a linked row cannot be split, made into a
transfer, or picked up by automatic transfer detection.

## A link lives in its rows' workspace

Links are read, edited and deleted from the workspace their rows are in. When
a bank sync moves a connected account's rows to another workspace, the links
whose rows both moved follow them; a link that would join rows in two
workspaces is deleted, since neither workspace can see both of its sides.
The decision is made once every account the sync moves has moved, so a card
charge repaid from checking on the same connection keeps its link.

## Consequences

An expense marked ignored or excluded from P&L after it was linked leaves its
credit counting in full again: the credit repaid something the user chose
not to count, and keeping it reduced would change net cash flow. Projected
figures read pending and future-dated rows through the same helpers, so a
linked card purchase that accrual mode books on a bill not yet due counts at
its unlinked share there too, and projected net cash flow is conserved like
the actual one.
