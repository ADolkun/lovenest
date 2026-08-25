# An exchange transaction is classified before it is believed

Ticket #69 mapped `buy`, `sell` and `advanced_trade_fill` and left every other
type Coinbase reports on the floor, which is why a wallet fed by a transfer or
a staking payout replayed short of its balance and stayed a Snapshot. #70
classifies all of them. Four decisions follow, and each of them is about a
transaction that moves units without being a purchase.

## Every type the vendor enumerates has a class, and an unlisted one is reported

`TX_CLASSES` names all twenty-nine types Coinbase's reference lists, plus the
reward types a real account emits that the reference has never mentioned
(`staking_reward`, `inflation_reward`, `interest`) and the legacy Pro and
Exchange transfer names. Each lands in one of four classes: **trade** (units
moved and the row states the money that moved for them), **income** (units
arrived as payment), **transfer** (the same person's coins moving), **cash**
(fiat only). Only the first two reach a ledger.

A type absent from the table classifies as unknown, reaches no ledger, and is
logged by name with a count once per sync, alongside a breakdown of what the
history was made of — which is the only place that answers "why does this
holding still have no cost basis". Coinbase adds types over time, and
the two available failure modes are not symmetrical: a type wrongly assumed to
be a trade is a cost basis that is confidently wrong, while one that reaches
nothing is a holding that stays a Snapshot and says so. Three types are *listed* as unknown rather than left to fall through. `tx` is
the vendor's own word for uncategorized, so it is the one type guaranteed not
to mean anything in particular. `retail_simple_dust` and the two
`fcm_futures_usdc_sell*` margin conversions have never been seen on a real
account, and the asymmetry above applies to us as much as to Coinbase:
guessing "trade" for an unobserved type is the confidently wrong basis this
table exists to prevent. The warning names them the first time one arrives.

`wrap_asset` and `unwrap_asset` *are* classified as trades, and that is a
call worth stating. Wrapping is arguably a change of form rather than a
disposal, and booking it as one realises a gain and restarts the holding
period. But it also changes the ticker held, so it produces a different
Holding — and classified as anything else, that new Holding opens with no
basis at all. A disposal at the stated value is the only classification that
leaves both sides with a defensible number, and it is what the reconciled
tax-tool exports treat it as.

A convert needs no entry of its own beyond the class. Coinbase files a `trade`
once in each wallet it touched, and direction is the sign of the quantity
(ADR 0007), so the two sides land on two ledgers with two different bases
without the mapping ever having to know they were one order.

## A transfer is recognised precisely so that it is not recorded

Transfers are classified and then deliberately written nowhere. Basis travels
with the coins, so a `receive` states no acquisition price, and recording one
at the day's spot price would invent a lot the user never bought — the exact
error the crypto importer avoids for the same reason
(`asset_import_service._TRANSFER_WORDS`).

The consequence is that a wallet fed by a transfer still replays short of its
reported balance, and `_ledger_reconciles` still leaves it a Snapshot. That is
the honest outcome: the missing basis is not on the exchange to be had, and it
arrives, if at all, from the one-time cost-basis import of #67. Classifying
transfers buys the ability to say *which* rows are missing rather than a
number that looks complete.

A clawback is not filed here, and the reason is the section below. It takes
back units already paid out — and now that the payout is a lot opened at
receipt value, the reversal is a disposal at roughly that same price: about
zero gain, and a replay that lands back on the reported balance. Classified as
a transfer it would instead leave the paid-out units on the ledger for good,
costing that holding its derived basis permanently and silently.

## A reward opens a lot at what it was worth, and the row says so

A staking or interest payout is income at receipt, and the units then carry
that value as their basis. Treating them as free shares understates basis and
overstates the eventual gain by exactly the amount already taxed as income.

The ledger has two kinds, so the payout is written as a buy at the receipt
value — the vocabulary CONTEXT.md now records as Income at Receipt, and the
same concept the crypto importer calls an *acquire*. What a buy cannot say is *why*, so the row carries a note —
`Coinbase staking_reward — income at receipt` — which is also what tells a
convert leg apart from dollars actually spent. A `notes` string is a weaker
thing than a third ledger kind, and it is what the domain can absorb without a
migration touching every writer, reader and view of `asset_transactions`; a
kind earns its place when something computes on it.

## An absent value is backfilled; a stated one is never overridden

Where a row states no USD total at all — a reward Coinbase valued at nothing —
the day's price comes from `/v2/prices/{asset}-USD/spot?date=…`, which needs
no authentication. Answers are memoized per asset and day, because a daily
staking payout otherwise asks the same question once per row, and capped at
`MAX_SPOT_LOOKUPS`, past which the walk raises: nothing caches a price across
syncs, so a history needing a thousand serial lookups would earn a rate limit
that every subsequent sync walks straight back into.

A total denominated in another currency is *not* treated as absent. It is a
real number in the wrong unit, and the temptation is to reprice the row from
the spot table — which would discard the price the order actually filled at
for a daily average. On a day an asset moves twenty percent that is thousands
of dollars of realised gain, with the quantity untouched, so nothing
downstream could notice. Those rows are skipped, exactly as #69 skipped them,
and the holding stays a Snapshot until a source that states USD arrives.

That endpoint answers 404 both for an asset Coinbase never listed and for a
date outside the window it keeps — measured at roughly the last three years,
so a 2023 reward is already outside it. A 404 means the row reaches no ledger.

Writing it at zero basis instead was the first attempt, on the reasoning that
units which arrived free make the whole disposal gain. That is right for the
importer, where the file is an authoritative reconciled history the user
chose, and wrong here, for a reason specific to sync: a zero-priced row keeps
the replay's *quantity* exact, and quantity is the whole of what
`_ledger_reconciles` can check (ADR 0007). It is the one shape of error that
passes the safety net — the derived basis is cached, short by the reward's
entire value, with nothing anywhere saying so. Every other failure in this
provider perturbs quantity and is caught. So the rule is: a row this provider
cannot price reaches no ledger, the replay comes up short, and the holding
stays a Snapshot. A missing basis the user can see beats a wrong one that
reconciles.

The same rule bounds the price from below. `asset_transactions.price` is
`NUMERIC(38, 18)`; past the top the write fails loudly, but below the bottom
Postgres rounds to zero, which is the same silent-and-reconciling error
reached through a dust-priced trade rather than through income.

Any other failure — a rate limit, a 500 — propagates and costs the sync its
ledger, since a partly-priced history is the confidently wrong cost basis
`get_trades` promises never to return.
