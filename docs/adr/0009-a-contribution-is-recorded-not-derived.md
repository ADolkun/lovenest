# A Contribution is recorded, not derived

ADR 0002 settled that Tax Lots are derived by replaying Trades and never stored.
Ticket #71 adds Contributions and Distributions, and the obvious question is
whether they follow the same rule — derived from something already in the
database rather than written down. They do not, and four decisions follow.

## Nothing in the ledger can be replayed into a Contribution

A Trade says a quantity of a ticker changed hands inside an account. A
Contribution says money crossed the account's boundary. The trade ledger has no
row for the second event and never will: the money arrives, sits as Liquid Cash,
and only later becomes a buy — often in a different year, often in several
pieces, sometimes never. Replaying buys would count a deposit the day it was
invested rather than the day it was made, and would count nothing at all for
cash that is still uninvested.

The other candidate source is the transaction ledger, where the *other* leg
lives: the debit leaving the checking account. `Category.treat_as_transfer`
already exists to stop those debits being read as spending, and its comment
names this exact case — "one-sided movements like an investment application
where the counterpart lives in Assets, not Accounts". But that leg is in the
household's Workspace and the wallet is in the investment one, the boundary
between them is absolute for records, and most of the accounts that matter here
— a 401(k), an HSA, an IRA at an institution no aggregator reaches — have no leg
in this application at all. A figure that only exists when both halves happen to
be visible is not a figure.

So `asset_contributions` is a record of its own. The consequence to watch is
double counting: a Contribution recorded against a wallet and a
`treat_as_transfer` debit leaving a checking account are two views of one real
event, and the cash-flow report's "investments" node
(`report_service.py:775-800`) already draws the second. They must not be summed.
Nothing sums them today, and nothing should start.

## Direction is the sign of the amount, not the wording

`classify_flow` reads a broker's action text only to decide whether the row
crosses the boundary at all. Which way it crossed comes from the sign of the
amount — the same call ADR 0007 makes for trade direction, for the same reason.

A single real transfer between two of the user's own accounts is filed twice,
once in each account, and on a Fidelity export both rows carry the word
"CONTRIBUTION": one reads `TRANSFERRED TO VS ... CURRENT CONTRIBUTION` at a
negative amount and the other `CASH CONTRIBUTION CURRENT YEAR` at the matching
positive. Mapping on the word would book two deposits for one movement and
inflate net contribution by the size of every internal transfer ever made. The
sign is unambiguous on every row a history file writes and needs no account
matching to interpret.

This also means a movement between the user's own two accounts is a Distribution
from one and a Contribution to the other, which is *not* the CONTEXT.md Transfer
— that term is about units carrying their basis between wallets, and no lot is
created or destroyed here. Per account both figures are right, and across the
accounts they cancel, which is what they should do.

## Declining to classify a row is the safe direction here, unlike the ledger

ADR 0008 argued the opposite for the trade ledger: leaving a Coinbase type
unknown costs the whole holding's basis, because the replay then disagrees with
the reported balance and every derived figure is withheld. Nothing of the kind
happens here. Contributions are not reconciled against anything, so a missed row
costs exactly that row.

The asymmetry runs the other way instead. A dividend read as a deposit does not
just add a wrong number — it *erases real growth*, because return is value minus
net contribution, and that is the one error this feature exists to prevent. So
`_INTERNAL_PHRASES` is checked before anything else and a row matching no
external phrase is skipped rather than guessed. `DIVIDEND RECEIVED` is the case
that decided the ordering: it is growth, and it says "received".

The list is short on purpose. It only has to name the events that also contain
an external word, because a row matching no external phrase is already skipped —
`YOU BOUGHT TESLA INC COM` needs no entry.

## Two questions about a year, and they are allowed to disagree

Net Contribution excludes employer money that has not vested: it answers what
the account's own money amounts to, and unvested money is not the user's. The
per-year rows count employer money gross: they answer progress against an annual
limit, and a limit is measured on money paid in, whether or not it has vested
since.

So `years[].net` will not add up to `net` while any employer money is unvested.
That is two questions disagreeing, not an error, and it is the reason the two
figures are computed in one place with the difference stated rather than in two
places where they would drift apart.

`tax_year` is a column rather than `date.year` for the same kind of reason. An
IRA contribution made before April 15 may be designated for the year before, and
Fidelity's own export says which in the action text. It is also where a year
that predates provider coverage goes: one row, typed by hand, carrying the year
it counts against rather than the day it was entered.
