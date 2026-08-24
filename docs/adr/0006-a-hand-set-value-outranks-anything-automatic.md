# A hand-set value outranks anything automatic

Ticket #68 makes holdings at institutions with no API first-class. Half the
portfolio that matters here has no quote behind it — a Celsius claim paid out
in instalments, a BlockFi distribution, a token whose only market is a DEX — so
the number on screen is whatever the user last typed. Two decisions follow from
taking that seriously, and both are about what the machine is *not* allowed to
do to that number.

## A Hand-Valued Holding carries no ticker

A holding on the manual valuation method may not be given a ticker. Attempting
it is a 422, on create and on edit alike.

Filtering the refresh by `valuation_method` would have been enough to stop the
price from being written, and that filter does exist. It was not enough on its
own, because the ticker is the part that is wrong: symbols collide across asset
classes and venues. `USA` is a Solana memecoin in the user's own 2025 positions
and also a New York closed-end equity fund; `ALEO` is a layer-1 token and a
symbol Yahoo will happily resolve to something else. Storing one on a holding
nobody quotes leaves a loaded gun for any future code path that decides a
ticker is reason enough to fetch a price — and that path would overwrite the
user's figure with an unrelated security's, silently and plausibly.

Provider-synced holdings are untouched by this rule. They sit on the manual
method too, but their ticker is the provider's statement of what the position
*is*, not a lookup key the user chose, and sync owns their value anyway.

## A day the user valued is a day sync skips

`_upsert_asset_value_for_today` returns early when a hand-set row already
exists for that date, rather than appending its own beside it.

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
the user as a message on the form.
