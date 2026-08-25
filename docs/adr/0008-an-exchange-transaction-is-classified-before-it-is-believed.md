# An exchange transaction is classified before it is believed

Ticket #69 mapped `buy`, `sell` and `advanced_trade_fill` and left every other
type Coinbase reports on the floor, which is why a wallet fed by a transfer or
a staking payout replayed short of its balance and stayed a Snapshot. #70
classifies all of them. Four decisions follow, and each of them is about a
transaction that moves units without being a purchase.

## Every type has a class, and declining to record one is not free

`TX_CLASSES` names all twenty-nine types Coinbase's reference lists, plus the
reward types a real account emits that the current reference's type table does
not list (`staking_reward`, `inflation_reward`, `interest`) and the
pre-Advanced Pro and Exchange transfer names. Each lands in one of four classes: **trade**
(units moved and the row states the money that moved for them), **income**
(units arrived as payment), **unrecorded** (the row moves units, or money, in
a way this ledger cannot state a basis for), **unknown** (never classified
here, reported by name). Only the first two reach a ledger.

The reason unknown is not a comfortable default is that a dropped row does not
cost one row's basis. It costs the whole holding's: the replay then disagrees
with the reported balance and `_ledger_reconciles` withholds the derived
figures for every lot in that wallet. So the asymmetry runs both ways. A type
wrongly assumed to be a trade is a cost basis that is confidently wrong; a
type left unknown is a position with no cost basis at all, and a user who
traded through that wallet for a decade loses all of it.

The line drawn is between *shape* and *tax nuance*. Where the vendor's own
description says units left or arrived and the row states a fiat total, the
shape is not in doubt and the row is a trade, even where the tax treatment is
arguable: `wrap_asset` and `unwrap_asset` (described as conversions),
`retail_simple_dust` (a sweep, so a disposal however small), and the two
`fcm_futures_usdc_sell*` conversions, which move USDC to USD at par and so can
carry only a rounding-sized gain. An earlier draft filed those last three as
unknown on the grounds that nobody here has seen one; that weighed the risk of
a wrong basis without weighing the certainty of no basis for the entire
wallet.

`tx` is the one type listed as unknown deliberately. It is the vendor's own
word for uncategorized, so it is the only type guaranteed not to mean anything
in particular. Everything else absent from the table is unknown by fallback,
logged by name with a count once per sync — Coinbase adds types over time, and
a new one has to arrive as a question rather than as a number.

A convert needs no entry of its own beyond the class. Coinbase files a `trade`
once in each wallet it touched, and direction is the sign of the quantity
(ADR 0007), so the two sides land on two ledgers with two different bases
without the mapping ever having to know they were one order.

## Unrecorded is a decision, not a gap

A transfer between the user's own wallets carries its basis with it and states
no acquisition price, so recording one would invent a lot that never existed —
the error the crypto importer avoids for the same reason
(`asset_import_service._TRANSFER_WORDS`). The consequence is accepted: the
wallet replays short, `_ledger_reconciles` leaves it a Snapshot, and the
missing basis arrives from #67's one-time import or not at all.

Most of the class is not a transfer in that sense, though, and the rest of it
is here for three other reasons:

**A send may not be to yourself, and a receive may not be from yourself.**
Coinbase files a payment to another person under the same `send` type as a
move to your own wallet, and no field in the row distinguishes them; `receive`,
`request` and `unsupported_asset_recovery` are ambiguous in the same way. One
reading is a disposal at fair market value or ordinary income at receipt; the
other is a basis-preserving move. Unable to tell, this records neither — the
same call the importer makes, where `receive` and `send` both sit in
`_TRANSFER_WORDS`. **Do not promote `receive` to income on the strength of
CONTEXT.md's airdrop wording**: most of them are the user's own coins arriving,
and a spot-priced lot for each would be exactly the invented basis this
avoids.

**A fiat movement is not a position.** `fiat_deposit`, `fiat_withdrawal`,
`subscription` and `derivatives_settlement` move money, not units — and where
they land in fiat wallets, `get_trades` skips those before classification ever
runs.

**A clawback rescinds income.** It takes back units already paid out. Booked
as a sell it would be priced from its own row — the value on the day it
landed, not the day the payout did — so a clawback of a year-old reward would
book a year of appreciation as realised gain on units nobody sold. That is the
error this whole ADR exists to avoid, and an earlier draft of #70 shipped it,
reasoning that the reversal must price out near the payout. It does not: the
code never looks at the payout. The right treatment reduces the year's income
and closes the lot, which this ledger's two kinds cannot express, so it
records nothing and the holding falls back to a Snapshot. A negative `income`
row — Coinbase also reverses rewards that way — is dropped for the same
reason.

## A reward opens a lot at what it was worth, and the row says so

A staking or interest payout is income at receipt, and the units then carry
that value as their basis. Treating them as free shares understates basis and
overstates the eventual gain by exactly the amount already taxed as income.

The ledger has two kinds, so the payout is written as a buy at the receipt
value — the vocabulary CONTEXT.md records as Income at Receipt, and the same
concept the crypto importer calls an *acquire*. What a buy cannot say is
*why*, so the row carries a note: `Coinbase staking_reward — income at
receipt`. A `notes` string is a weaker thing than a third ledger kind, and it
is what the domain can absorb without a migration touching every writer,
reader and view of `asset_transactions`; a kind earns its place when something
computes on it, and nothing does yet.

## The backfill is for income, and a price nobody can state is not invented

Where an income row states no usable USD total — Coinbase valued it at
nothing, or valued it in another currency, and a stated zero states nothing in
any currency — the day's price comes from `/v2/prices/{asset}-USD/spot?date=…`,
which needs no authentication. A reward is worth what the asset was worth when
it landed, which is exactly what that endpoint answers, so there is no better
number being displaced.

A trade is different, and gets no backfill. Its price is the price the order
filled at, and substituting the day's spot for it moves realised gain by
whatever the asset did that day — with the quantity untouched, so nothing
downstream could notice. The same reasoning refuses to reprice a total stated
in another currency: it is a real number in the wrong unit, not an absent one.
Those rows are left off, exactly as #69 left them off.

Lookups are memoized per asset and day, for the length of one walk rather
than one wallet, and capped at `MAX_SPOT_LOOKUPS` over that same span — so on
a history large enough to reach the cap, wallet order decides which rows get
priced, and the log says the cap was reached.
Nothing caches a price across syncs, so a history wanting a thousand serial
lookups wants them again on every sync after, which is how a connection earns
a rate limit it never gets out of. Past the cap the answer is None, the same
as any other unpriceable row. An earlier draft raised instead; that discarded
every correctly-priced row collected so far, and — because `_sync_trades`
swallows the exception and returns — skipped the recompute that runs on every
sync, leaving a holding whose quantity was today's and whose basis was the
previous sync's.

The endpoint answers 404 both for an asset Coinbase never listed and for a
date outside the window it keeps — a rolling one of roughly three years:
measured on 2026-08-24, nothing priced before about 2023-09. A 404 means the
row reaches no ledger.

Writing it at zero basis instead was the first attempt, on the reasoning that
units which arrived free make the whole disposal gain. That is right for the
importer, where the file is an authoritative reconciled history the user
chose, and wrong here, for a reason specific to sync: a zero-priced row keeps
the replay's *quantity* exact, and quantity is the whole of what
`_ledger_reconciles` can check (ADR 0007). It is the one shape of error that
passes the safety net — the basis cached short by the reward's entire value,
with nothing anywhere saying so. So the rule the rest of this file follows: a
row that cannot be stated in USD reaches no ledger, and a missing basis the
user can see beats a wrong one that reconciles.

The same rule bounds both numbers the ledger stores, in both directions.
`price` and `quantity` are `NUMERIC(38, 18)`: above the top the write fails
late, after the rest of the sync is staged, and below the bottom Postgres
rounds to zero, which is the silent-and-reconciling error again — reached
through a dust-priced trade rather than through income.

A rate limit or a 500 still propagates and costs the sync its ledger. Not
knowing a price is different from not knowing whether the history is complete,
and only the second one makes a partial walk indistinguishable from a whole
one.
