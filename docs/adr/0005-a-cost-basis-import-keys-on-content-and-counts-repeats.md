# A cost-basis import keys on content, and counts repeats

Ticket #67 seeds historical cost basis from a file, because no aggregator
supplies acquisition dates or tax lots. Re-uploading is the normal way people
fix a mapping mistake, so the import has to be idempotent, and the three
questions the ticket left open all follow from *what makes a row the same row*.

## The idempotency key is the content fingerprint, counted

A ledger row is recognised by `(asset_id, date, kind, quantity, price)`, or by
`(asset_id, external_id)` where the file gives the row a stable identifier of
its own.

The alternative on the table was the file's hash plus the row number, which
matches "re-importing the *same file*" most literally. It was rejected because
almost nobody re-imports the same file: they re-import a *later export of the
same account*, which carries the earlier rows plus new ones at shifted line
numbers, and every earlier row would import a second time.

The fingerprint's own weakness — two genuinely distinct buys of the same size on
the same day at the same price collide — is answered by counting rather than by
changing the key. `_already_imported` returns a multiset, and a matched row
consumes one occurrence. A file holding that pair twice therefore imports both
the first time and neither the second, which is what both halves of the problem
ask for. A plain set could not import the second copy at all.

Two consequences worth naming. Quantities and prices are rounded to the ledger's
scale *before* the fingerprint is taken, because the database would otherwise
round them on the way in and the next import would compute a different key
against the same row — which is also why migration 082 rebuilds that column
as `NUMERIC(38, 18)`. Both halves had to grow: a crypto lot report routinely
carries sixteen decimals, and a meme-coin lot runs to twelve digits before the
point, so widening only the fractional half would have traded one silent
rounding for a refusal on the ticket's own case. And a
row that a lot report expresses as an acquisition and a disposal on one line
becomes two orders, so a file-supplied identifier is suffixed with the side it
belongs to; one id cannot key both.

## A crypto seed reuses `import_logs.entity = 'asset_orders'`

Undo routes off that column (`api/import_logs.py`), and a crypto history that
has been reconciled into lots produces exactly the same thing an order file
does: `AssetTransaction` rows on holdings. A third entity value would duplicate
the undo path to describe the same rows differently.

The distinction that does matter — which rows a given upload wrote — is already
carried by `asset_transactions.import_id`, per import rather than per file
shape.

## Types that are neither a buy nor a sell

A broker file says buy or sell. A crypto history also says transfer, staking
reward, airdrop, fork, insolvency distribution, and a dozen vendor-specific
spellings of each. They land in three places:

- **Transfers are skipped**, with the reason reported. The same person's coins
  moving between their own wallets carry their basis with them; importing one
  would open a Lot that never existed and close one that never did either.
- **Rewards, airdrops and distributions open a Lot**, at whatever the row says
  the units were worth — zero when it says nothing, which is the honest answer:
  units that cost nothing make the whole eventual disposal a gain.
- **A type this module does not model is a skip, not an error.** The row is
  well-formed; the gap is ours, and reporting it as malformed blames the file.

That distinction is why `AssetImportResult` grew a `skips` list beside its
`errors` list rather than a larger integer: "42 rows skipped" and "42 rows are
broken" are very different things to read before pressing Import.

## Consequences

A holding no price provider lists — a delisted stock, a token from an
insolvency estate — can now be created, behind an explicit opt-in, as a
`manual` holding with basis and no market price. `_existing_holdings` therefore
matches those too, or the next import of the same ticker would build a second
holding beside the first — but only the ones this importer created
(`source = 'import'`). A hand-made manual asset that happens to carry a ticker
is the user's own record, and rewriting its units from a file it never came
from would be the importer taking something that is not its.

Such a holding has no quote and so writes no `AssetValue`. It is not therefore
worth nothing: `_compute_current_value` falls back to `purchase_price` for a
non-market holding, and `recompute_and_cache` sets that from the ledger, so it
reads at its cost basis with no gain — the only honest figure available.

Seeding a Snapshot Holding (CONTEXT.md) with a partial history silently
replaced the provider's quantity with the file's, because the position is
recomputed from the ledger. The discrepancy is now warned about and not
resolved: which of the two figures is right is not something the importer can
know.
