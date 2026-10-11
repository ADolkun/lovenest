# A credit's category decides whether it is income

Amounts are stored unsigned and direction lives in `Transaction.type`, so user
P&L has to decide what a credit *means*. It used to decide by account: a credit
on a credit card that was not a repayment was a refund and reduced spending;
every other credit was income. An employer reimbursement into checking filed
under Dining, or a family member's Zelle filed under Subscriptions, therefore
counted as income and the category's spending never came down.

The user already said what those credits are when they chose the category, so
the category is what decides. `Category.is_income` marks the categories where a
credit is income. A credit filed under any other category is money back on that
category's spending and nets against it (`spending_offset_filter` in
`_query_filters.py`). Debits are spending wherever they are filed.

## The category wins when there is one

A card credit filed under an income category is income, even though the card
account alone would have called it a refund. The account is a guess about
intent; the category is the user's statement of it.

## Without a category, the old guess stands

An uncategorized credit carries no statement, so the account rule still
applies to it: an uncategorized card refund offsets spending, and an
uncategorized credit anywhere else stays income. Card repayments are excluded
throughout, by description, exactly as before.

## One definition, every figure

The dashboard card, the category breakdown, reports, the transaction list
footer and budget actuals all read the same helpers. Budgets used to subtract
every credit in a category, which disagreed with the dashboard about income
categories; they now use `user_pnl_expense_amount` too. The transaction list
takes `pnl_group` so a category drill-down can show the credits that reduced
its figure alongside the debits that made it.

Per-account statistics are not user P&L — they track cash through one account —
and keep their own rule.

## Consequences

A category is not born knowing it is income. The default salary category is
seeded with the flag, and migration 115 sets it on every category in a group
carrying a default income-group name. A category the user creates for income
has to be flagged, or its credits will read as offsets — visible as spending
that shrinks or goes negative, which is a louder failure than income that
silently inflates. A category's spending can now come out negative in a month
where a reimbursement lands after the expense it repays; the breakdown drops
categories that net to zero or less.
