# The Wash Sale warning fires only where a deduction is actually at stake

A warning the user learns to dismiss is worse than no warning, because it also
costs them the ones that matter. Two gates keep this one rare enough to read.

**The asset class must be one the rule reaches.** `COVERED_ASSET_TYPES` is an
allowlist — stocks, ETFs, funds — mirroring `REPORTABLE_TAX_TREATMENTS`. Crypto
is deliberately outside it: it is property rather than a security, so the rule
does not apply, and this portfolio holds enough of it that warning there would
train the user to ignore every warning. A class nobody has ruled on stays silent
rather than guessing.

`investment` is covered too, which is the uncomfortable part. It is not a class
at all but the generic bucket every provider-synced holding is created in
(`connection_service._upsert_asset_from_holding`), so excluding it would leave
the check inert on precisely the accounts #47 gap 3 describes — a taxable
brokerage and a Roth holding one ticker, both synced. Covering it means the
bucket must not also contain crypto, so a crypto exchange's holdings are now
seeded as `crypto` on create, alongside the existing Cash Equivalent seed. Both
are create-only guesses the user can overrule from the asset editor, and neither
is re-applied by a later sync.

This leaves one known hole: holdings synced *before* that seed existed are still
typed `investment`, so a pre-existing crypto position warns until it is
reclassified by hand. A backfill migration would close it, and is the right
follow-up if reclassifying proves annoying rather than a one-afternoon chore.

**The selling wallet must be Taxable.** A loss realised inside a Roth or an HSA
is no deduction to begin with, so there is nothing a wash sale could disallow.
This is the same gate Reportable Gain applies (CONTEXT.md): the direction of the
trade is what decides, not merely that two accounts are involved.

Note the asymmetry, because it is the whole point of the feature: the *selling*
side must be taxable, while the *replacing* side is worst when it is not. A
replacement bought inside an IRA has no basis for the disallowed loss to move
into, so the deduction is forfeited rather than deferred — those accounts are
flagged `unrecoverable`.

A candidate sale counts as a loss when the price lands below Average Price, the
same basis the ledger books a Realised Gain against (ADR 0003), so the warning
and the figure the sale would actually produce cannot disagree.

## Consequences

The warning names a wallet when it bought inside the window or holds the
instrument now — the selling wallet included, since a repurchase there is a wash
sale too, and a warning that fired with nothing to point at would be worse than
useless. Wallets are deduplicated on identity rather than on the name the user
typed, so two wallets sharing a label stay two warnings, and two Holdings in no
wallet stay two entries instead of collapsing into one "no wallet".

The check spans every wallet in the workspace, matching the instrument by
ticker. "Substantially identical" is not attempted — no data here could support
that judgement — so a warning is evidence of exposure, never proof of its
absence. The Workspace boundary is the outer limit: investments live in one
Workspace (ADR 0001), and a check that crossed it would be the leak the boundary
exists to prevent.

An acquisition is matched from the ledger, which means a wallet that holds the
instrument without a recorded trade — a Snapshot Holding — contributes no
match. It is still named among the accounts, since it is where a replacement
would come from.

Exposure is reported and nothing more: no disallowed amount is rolled into a
replacement's basis and no adjusted basis is computed anywhere. A test asserts
the check leaves every basis untouched. Doing the accounting would need the
replacement Lots to carry an adjustment that the derived-not-stored Lot model
(ADR 0002) has nowhere to put — that is the decision to revisit first if full
wash-sale accounting is ever wanted.
