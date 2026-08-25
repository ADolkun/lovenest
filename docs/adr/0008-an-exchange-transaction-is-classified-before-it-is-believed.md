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
logged by name with a count once per sync. Coinbase adds types over time, and
the two available failure modes are not symmetrical: a type wrongly assumed to
be a trade is a cost basis that is confidently wrong, while one that reaches
nothing is a holding that stays a Snapshot and says so. `tx` — the vendor's own
word for uncategorized — is listed as unknown deliberately rather than left to
fall through, because it is the one type guaranteed not to mean anything in
particular.

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

A clawback is filed here rather than as a disposal. It takes back units already
paid out, reversing an acquisition; booking it as a sell would realise a gain
on money the user never received.

## A reward opens a lot at what it was worth, and the row says so

A staking or interest payout is income at receipt, and the units then carry
that value as their basis. Treating them as free shares understates basis and
overstates the eventual gain by exactly the amount already taxed as income.

The ledger has two kinds, so the payout is written as a buy at the receipt
value. What a buy cannot say is *why*, so the row carries a note —
`Coinbase staking_reward — income at receipt` — which is also what tells a
convert leg apart from dollars actually spent. A `notes` string is a weaker
thing than a third ledger kind, and it is what the domain can absorb without a
migration touching every writer, reader and view of `asset_transactions`; a
kind earns its place when something computes on it.

## A missing value is backfilled from the public spot table, and a 404 is an answer

Where a row states no usable USD total — a reward Coinbase valued at nothing,
or a total denominated in a currency that is not the holding's — the day's
price comes from `/v2/prices/{asset}-USD/spot?date=…`, which needs no
authentication. Answers are memoized per asset and day, because a daily
staking payout otherwise asks the same question once per row.

That endpoint answers 404 both for an asset Coinbase never listed and for a
date outside the window it keeps — measured at roughly the last three years,
so a 2023 reward is already outside it. A 404 is therefore "no price", not an
error: for income the lot opens at zero basis, which is the honest figure for
units that arrived free and makes the whole eventual disposal gain; for a
trade the row is skipped, because a purchase recorded at zero is a basis the
user did not have. Any other failure — a rate limit, a 500 — propagates and
costs the sync its ledger, since a partly-priced history is the confidently
wrong cost basis `get_trades` promises never to return.
