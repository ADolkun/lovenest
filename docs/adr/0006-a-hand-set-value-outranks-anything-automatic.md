# A hand-set value outranks anything automatic

Ticket #68 makes holdings at institutions with no API first-class. Half the
portfolio that matters here has no quote behind it — a Celsius claim paid out
in instalments, a BlockFi distribution, a token whose only market is a DEX — so
the number on screen is whatever the user last typed. Two decisions follow from
taking that seriously, and both are about what the machine is *not* allowed to
do to that number.

## A Hand-Valued Holding carries no *resolvable* ticker

A holding on the manual valuation method may not be given a symbol a price
provider answers. Attempting it is a 422, on create and on edit alike; the
provider is asked, and a symbol it does not know is allowed through.

The first cut of this rule refused every ticker, and was wrong. Two paths in
this repo already mint manual-method holdings that carry one:
`connection_service._upsert_asset_from_holding`, where the ticker is the
provider's statement of what the position *is*, and
`asset_import_service._new_holding`, which deliberately keeps an order's
symbol on a holding no quote answers rather than mint a market-priced holding
with a permanently broken feed. A rule the API enforced and the importer
quietly broke would not be an invariant, just a rejection.

Filtering the refresh by `valuation_method` would have been enough to stop a
price from being written today, and that filter does exist. Resolvability is
the sharper line because the danger is not the refresh as it is now — it is
that a stored symbol reads as a lookup key to any future path that decides a
ticker is reason enough to fetch a price. `USA` is a Solana memecoin in the
user's own 2025 positions and also a New York closed-end equity fund; that
path would overwrite the user's figure with the fund's, silently and
plausibly. A symbol nothing quotes cannot do this, so it stays a label.

A provider that errors counts as "does not resolve". Refusing a user's holding
because Yahoo is down would be the wrong way to fail.

## A day the user valued is a day sync skips

`_upsert_asset_value_for_today` returns early when a hand-set row already
exists for that date, rather than appending its own beside it.

The protection is day-scoped, and deliberately so: it settles who owns *that
day*, not who owns the holding forever. A correction made yesterday does not
stop today's sync from valuing today, because today is a day the user has not
spoken about.

Appending was the smaller change and was rejected. The hand-set row would
survive untouched in the literal sense while ceasing to be the value anything
reads — every "latest value" query resolves by date first, so a sync row
written seconds later shadows it in the holding total, the wallet total, the
net-worth series and the chart. Leaving a row intact that nothing reads is the
same loss with extra steps. This matches what `_ensure_historical_seed` already
did for the purchase-date seed, so the two halves of sync now decline the same
way for the same reason.

The market-price path declines the same way: a refresh will not overwrite a
`manual` row for today and relabel it `sync`, it writes its own row instead.

## Which row wins is settled by when it was written

`asset_values` gains `recorded_at`, and every "latest value" query orders by
`(date, recorded_at)`.

The tiebreak before this was `ORDER BY id DESC` on a UUID4 primary key — random
bytes. Two rows sharing a date resolved arbitrarily, and could resolve
differently on the next request, which made "the user's correction wins" a coin
flip rather than a rule. The column is not only a tiebreak: it is what lets a
figure state its own age, which `date` cannot, since correcting last month's
balance today writes a row dated last month.

One hand-set figure per holding per day follows from the same reasoning.
Re-entering a day's balance is a correction, not a second opinion, so it
updates the row and moves its stamp forward instead of leaving two rows dated
alike for a reader to choose between.

## What this does not change

An over-sell is still refused outright rather than warned about, as it was
before this ticket: a position cannot go negative, and the 422 names the
attempted and available quantities. The ticket's wording asked for a warning;
the stronger answer was already built, already tested, and already surfaced to
the user as a toast on the form.

What was *not* already built is the same check on deletion. `add` and `update`
both validated the prospective ledger; `delete` did not, so removing a buy a
later sell depended on left `_recompute` to clamp the shortfall silently and
book a realized gain against units that never left. Deletion now refuses on
the same terms as the other two.
