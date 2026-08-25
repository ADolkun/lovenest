# A synced ledger is believed only when it reconciles

> Amended by [ADR 0008](0008-an-exchange-transaction-is-classified-before-it-is-believed.md):
> the passages below saying the other transaction types "wait for #70" are history.

Ticket #69 makes an exchange the first thing other than a person to write the
trade ledger. Cost basis and acquisition dates stop coming from a file and
start coming from Coinbase. Three decisions follow, and all three are about
what to do when the history the exchange hands back is not the whole story.

## Direction is the sign of the quantity, not the type and not the side

`_trade` reads `buy` or `sell` off the sign of `amount`, having used the
transaction's `type` only to decide the row is eligible at all.

`advanced_trade_fill` is why. A fill is filed twice, once in each wallet the
order moved, and both rows carry the same `advanced_trade_fill` block —
including `order_side`, which describes the *order* rather than the leg.
Buying HBAR with USDC writes `+1867.7 HBAR` to one wallet and `-485.602 USDC`
to the other, and both say `order_side: buy`. Mapping on the side would book
the second as a purchase of USDC, inflating a stablecoin position by the size
of every trade ever routed through it. The sign is unambiguous on every type
here and needs no product-id parsing to interpret.

The type list includes `advanced_trade_fill` because on a real account it is
the common case, not an exotic one: an account that trades on the Advanced
surface reports no `sell` transactions at all — 185 fills against 100 buys and
zero sells — so without it the ledger is buys-only, reconciles against nothing,
and the feature derives no basis for anybody who actually trades.

A fill's `commission` is not added to the basis. The two legs of a fill balance
to the stablecoin conversion alone, which they could not do if a commission of
that size had really been deducted, so it reads as an uncharged notional
already inside `fill_price`.

## The provider's transaction id is the whole dedup key

`_sync_trades` skips a trade whose `(asset_id, external_id)` is already on the
ledger, and nothing else.

The importer next door keys differently, and on purpose. `import_orders` also
fingerprints on content — `(date, kind, quantity, price)` — and counts repeats,
because a CSV row may carry no id at all, and two genuinely distinct buys of
the same size on the same day at the same price are a thing a crypto history
does several times a page (ADR 0005). Neither problem exists here: the API
states an id for every transaction, and it is stable across walks. Adding the
content fingerprint would import strictly less — two real trades that happen to
match would collapse into one — for a robustness this source does not need.

The trade-off is that a provider which re-issues ids would double-write. That
is a thing to notice in a log, not a thing to design against before it happens.

## A ledger that disagrees with the balance is not the authority

CONTEXT.md says a Holding's quantity and cost basis are derived by replaying
its Trades. `_sync_trades` derives them only when the replay reproduces the
quantity the provider reports, within a dust tolerance.

This looks like hedging on the domain model and is the opposite. The model's
claim holds for a *complete* ledger. #69 maps `buy` and `sell`; the two dozen
other types Coinbase enumerates — `send`, `receive`, `trade`, `staking_reward`,
`earn_payout` — wait for #70. So a coin that arrived by transfer and was later
partly sold produces a ledger of sells with no buys, and replaying that is not
a smaller answer, it is a different one: `_recompute` clamps the oversell to
zero units, and `recompute_and_cache` reads zero units as a full exit and
stamps a sell date. `sell_date IS NULL` is what puts a holding in the
portfolio, so believing that replay would delete a position the exchange still
reports a balance for, and the next sync would not put it back.

An over-sell fails the same test for a different reason. A replay that sells
more than it bought is proof of a missing buy whatever the balance says, and
it slips past the quantity check exactly when the position was closed out:
`_recompute` clamps the over-sell to zero and the exchange reports zero, so the
two agree by coincidence while the realised gain counts only the units that
happen to have a traceable buy. Every other writer of this ledger refuses an
over-sell outright — `_raise_if_oversell` on the API path, `reason='oversell'`
in the importer. Sync cannot refuse, because it does not get to choose what the
exchange reports; it declines to derive instead.

Reconciling against the balance is the one completeness check available
without the missing types. Fail it and the holding stays exactly what it was
before the trades arrived — a Snapshot, provider quantity, basis underived —
while the trades stay on the ledger, because they are real and they start
counting the day #70 makes the history whole. The tolerance is relative and
small: exchanges settle fees in kind and round at the eighteenth decimal, so a
complete history reproduces a balance to within a rounding error rather than
exactly.

The check also runs on a sync that appended nothing, which is why
`_sync_trades` no longer returns early on an empty history. `_sync_holdings`
writes the reported balance onto `units` immediately before, every time; if the
replay only ran when something new arrived, the position would flip between the
two figures on alternate syncs.

## The trade sync derives quantity and basis, and nothing else

`_sync_trades` restores `sell_date` and `sell_price` to whatever they were
before the replay ran.

`recompute_and_cache` treats a full exit as a closure and a re-opened position
as a resurrection, which is right when a person adds a trade and wrong when a
sync does. `_sync_holdings` deliberately stops valuing a holding the user
marked sold, a hundred lines earlier in the same file; a replay that cleared
that date on the next sync would undo the marking and put the position back in
the portfolio. Whether a Holding is closed stays the user's statement, or the
provider's via `is_withdrawn` — the trade ledger's job is what the position is
and what it cost.

## Two trades on one day replay in the order they happened

The ledger stores a date, but `TradeData` carries the full instant and
`_sync_trades` stamps it on `created_at`, which is the tiebreaker `_recompute`
sorts by.

Without it the tiebreak falls through to insertion order, because rows flushed
together share a server-side `created_at` to the microsecond. Coinbase lists
transactions newest-first, so a buy and a sell of one asset on one day would
replay backwards: the sell finds nothing to sell, gets clamped to zero, and a
real round-trip books no gain at all. That is a taxable event silently missing
from `reportable_gain`, and no field in the ledger would show anything wrong.

## A partial walk is refused, not returned

`CoinbaseProvider._walk` raises when it runs out of pages, unless the caller
says a prefix is usable. The accounts walk says so; the history walk does not.

The asymmetry is the point. A truncated account list costs a wallet that shows
up next sync. A truncated history is a cost basis computed from an arbitrary
slice of someone's trading, and nothing downstream can tell it apart from a
correct one — not the user, not the reconciliation check above, which a
prefix ending at the right quantity would pass. The page cap exists so a
server handing out fresh cursors forever cannot spin; reaching it means the
assumption behind that cap was wrong, and the honest report is an error, not a
number.
