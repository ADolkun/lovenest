# Tax Lots are derived from the trade ledger, never stored

Holding Period and Wash Sale detection both need lot-level granularity, which the
app has never had. We derive Lots by replaying `asset_transactions` rather than
adding a lots table.

Buy-side trade rows already carry date, quantity and price — they *are* lots,
just unconsumed. The ledger is already declared the source of truth for a
position, with `recompute_position` replaying it to produce `average_price`. A
lots table would be a second source of truth for data that falls out of a pass
the code already makes, and the two would drift the first time a trade was
corrected.

## Consequences

A Holding with no trades has no Lots. This is the common case today: provider
sync writes positions as snapshots, `asset_transactions` is empty, and SimpleFIN
deliberately supplies no acquisition date because the only date it offers is when
the aggregator first saw the position. Such a Snapshot Holding reports Holding
Period as *unknown*, not as short-term — a fabricated acquisition date produces a
confidently wrong long-versus-short answer, which is worse than no answer on the
figure that most changes what a sale costs.

Tax character therefore does not become available by building this. It becomes
available when real trades are imported for a Holding, at which point the ledger
takes over for that Holding and Lots appear.

Lots are computed for every Wallet, including Tax-Advantaged ones. The Holding
Period is merely *hidden* there, because the Wash Sale rule reaches into IRAs and
that is the case where the disallowed loss is lost outright rather than moved
into the replacement basis.

If replaying the ledger ever measurably hurts, cache the derived figures on the
Holding the way `average_price` already is. Do not promote the cache to a record.
