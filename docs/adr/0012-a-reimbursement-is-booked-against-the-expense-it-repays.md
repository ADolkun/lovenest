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

## The unlinked remainder follows ADR 0011

A $259 credit with $117.09 linked contributes its remaining $141.91 exactly as
an unlinked credit would: income if its category is an income category, money
back on its category otherwise. Linking changes where the linked part is
counted, never what the rest of the credit means.

## Only rows that are plainly a credit and an expense

Both rows sit in the same workspace and the same currency (cross-currency
links are refused in this version), are posted and dated today or earlier,
and are neither group settlements, opening balances, transfers nor card
payments. Link amounts are in that shared currency; when its FX rate moved
between the two dates, primary-currency totals are conserved only
approximately. Cash-flow reports are left alone: they report when cash moved,
and a link does not move cash.

## Group splits and reimbursements are exclusive

A split already says who owes what and is repaid through group settlements. A
row that is both split and reimbursed would have its cost reduced twice, so a
row with splits cannot be linked and a linked row cannot be split, made into a
transfer, or picked up by automatic transfer detection.

## Consequences

An expense marked ignored or excluded from P&L after it was linked takes the
linked part of its credit with it, which is the honest reading: the credit
repaid something the user chose not to count. Projected figures in accrual
mode still count a linked, not-yet-due card purchase at its full amount until
its due date, because the forecast paths read rows in Python and do not apply
the share.
