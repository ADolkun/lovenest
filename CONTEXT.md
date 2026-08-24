# Context

Glossary for lovenest. Domain terms only — no implementation detail.

## Portfolio

**Account** — a place money sits, in one of five kinds: Checking, Savings, Credit
Card, Investment, Cash. Cash is pocket money — notes in a drawer, an unbanked
balance — and is not a Wallet: a Wallet is the asset-side grouping below. Crypto is
an asset class, not a kind of Account, because one exchange account can hold spot,
stablecoins and staking positions at once.

**Asset** — anything with a value that is not a cash Account: real estate, a vehicle,
a valuable, or an investment holding. Cash lives in Accounts; everything else is an Asset.

**Holding** — one investment Asset consolidated per ticker per Wallet. The same
ticker in two Wallets is two Holdings, deliberately: they have different basis and
different tax character. A Holding's quantity and cost basis are *derived* by
replaying its Trades — except for a Snapshot Holding, which has none yet.

**Snapshot Holding** — a Holding imported from a provider that reports a position
but not the Trades behind it. Quantity and unit basis come straight from the
provider, and there is no ledger to replay, so Holding Period and Tax Lots are
unknown rather than zero. It stops being a Snapshot once real Trades are imported
for it, after which the ledger is authoritative.

**Hand-Valued Holding** — a Holding whose worth is whatever the user last typed:
a bankruptcy claim, a stake in something unlisted, a position at an institution
no aggregator reaches. Nothing automatic revalues it — not a price refresh, not
a provider sync. It may carry a symbol as a *label*, the way an exchange named
it, but never one a price provider answers: symbols collide across asset
classes, so a resolvable one would eventually be read as a lookup key and quote
an unrelated security over the user's figure.

**Hand-Set Value** — one figure the user recorded for a Holding on a day, and
the authority for that day. A sync writes nothing for a day already valued by
hand. It is stamped with when it was *entered*, which is a different thing from
the day it is *about*: a balance typed today for last month is fresh, and the
whole point of the stamp is telling that apart from a figure a year stale.

**Wallet** — a grouping of Holdings corresponding to exactly one brokerage account.
One account, one Wallet: a Wallet spanning two accounts cannot carry a truthful tax
character, and a Wallet is the unit that character attaches to — Taxable,
Tax-Advantaged (Roth, Traditional, HSA), or other. No provider reports it, so it
is always set by the user; wallets default to Taxable.

**Trade** — a single buy or sell of a quantity of a ticker at a price on a date.
Trades are the source of truth for a Holding; the Holding's cached figures are an
optimisation, not the record.

**Average Price** — weighted-average cost per unit across all open quantity
(*preço médio*). Deliberately **not** FIFO. Buys move it; sells do not.

**Cost Basis** — total money currently at risk in a Holding: Average Price × quantity.

**Realised Gain** — profit or loss booked at the moment of a sell. Distinct from
unrealised gain, which is the paper difference between market value and Cost Basis.
A measure of performance, computed for every Wallet: a Roth IRA trade makes real
money and the figure says so.

**Reportable Gain** — the part of Realised Gain that a tax return has to account
for: the part arising in a Taxable Wallet. It is the *only* figure any tax
calculation may consume. A gain realised in a Tax-Advantaged Wallet — Roth,
Traditional, HSA — is never Reportable, and neither is one in a Wallet marked
`other`, nor one in a Holding sitting in no Wallet at all, since only a Wallet
explicitly known to be Taxable can produce one.

## Tax character

**Holding Period** — how long a quantity has been held, which decides the tax
character of a gain. **Long** at one year or more, **Short** below it. It is always
*derived* — from the buy date to the sell date, or to today when the position is
still open. It is never a stored flag, and it is never entered by hand.

**Taxable Account** — an account where a sell creates a reportable gain or loss.
Holding Period, Cost Basis, and Realised Gain matter here.

**Tax-Advantaged Account** — an account where trading creates no taxable event:
Roth IRA, Traditional 401(k), HSA. A gain here has no tax character, so Holding
Period is not shown for its Holdings. It is still *computed*: the Wash Sale rule
reaches into these accounts, and it is the case where the loss is worst.

**Contribution** — money moved into an account from outside it. The counterpart to
market growth: an account's balance is Contributions plus gains. Tracked so that
true return can be separated from deposits.

**Distribution** — money moved out of an account to somewhere outside it. The
mirror of a Contribution, and not the same thing as a sell: selling converts a
Holding to Liquid Cash inside the account, while a Distribution removes money
from the account entirely.

**Net Contribution** — Contributions minus Distributions. This, not gross
Contributions, is what an account's own money amounts to. It matters most for a
Roth IRA, where the figure governs how much can be withdrawn before retirement
age without penalty — a Distribution permanently lowers it.

**Tax Lot** — one acquisition of a quantity of a ticker on one date at one price.
A Holding is made of Lots. Average Price deliberately blends them, but Holding
Period and any per-lot sale decision need them kept apart. A Lot is always
*derived* by replaying Trades and is never stored as a record of its own — so a
Snapshot Holding has no Lots at all, which is a different thing from having one
Lot of unknown date.

**Lot Matching** — deciding which Lots a sell consumed, which is what gives the
Realised Gain its holding-period character. Oldest first (FIFO), the broker
default. It settles *which* units left, never *what* they cost: the amount stays
the Average Price figure, so one sell has one Realised Gain and the long and
short parts always add back up to it.

**Wash Sale** — selling at a loss and acquiring the same or a substantially
identical security within 30 days either side, which disallows the loss. The
disallowed amount normally moves into the basis of the replacement shares — but
when the replacement is bought inside an IRA, the loss is lost outright.
Crucially the rule spans *all* accounts a person holds, so detecting it is
never a single-account question. It does not reach every asset class: crypto is
property rather than a security, so no wash sale arises there.

**Liquid Cash** — settled, uninvested cash sitting inside a brokerage account.
Part of the account's total, but not part of any Holding.

**Cash Equivalent** — an instrument that is nominally a Holding but behaves as
Liquid Cash: a government money-market fund, or a fiat-pegged stablecoin. It has
a ticker and a share count, yet it carries no market risk and no meaningful gain.
Counted as Liquid Cash, never as an invested position — otherwise allocation and
return figures are quietly wrong. Whether a ticker is one is the user's call; a
list of the well-known ones supplies the opening guess and nothing more.

**Dust** — a Holding worth under one dollar: left over from an old trade, a
staking reward, or an airdrop. Carried for completeness, excluded from allocation
views so it does not bury real positions. The test is the absolute amount, not a
share of the portfolio — dust should not reclassify itself every time the market
moves.

## Connections

**Account Allowlist** — the set of provider accounts a Connection is permitted to
sync. Three states, not two: absent means sync everything the provider returns,
present means sync only what it lists, and present-but-empty means sync nothing.
Excluding an account stops future syncs of it; it never deletes what was already
imported.

**Pending Account** — a provider account that appeared after the Account Allowlist
was configured, so the user has never had the chance to include or exclude it. It
does not sync while it waits, and it is distinct from an account deliberately
excluded.

**Review-First Connect** — connecting an aggregator with an empty Account
Allowlist, so the first import creates nothing and every provider account starts
as a Pending Account for the user to choose from. Only available where the
provider enumerates accounts at connect time; elsewhere the connection falls back
to importing everything.

## Tenancy

**Workspace** — the tenant boundary. Every financial record belongs to exactly one
Workspace, and membership is what grants visibility: a Workspace you are not a
member of is not merely filtered out of your reports, it is absent from the
application entirely.

The boundary is absolute for *records* and has exactly one exception for *totals*
— the Split Projection. No other view, report, search result, or figure may span
Workspaces, and a total that does so without being a Split Projection is a bug.

**Split Projection** — your share of a Transaction that lives in someone else's
Workspace, appearing in your own totals because you share a Group with its owner.
It is the sole legitimate crossing of the Workspace boundary, and it carries a
share of an amount, never the record: the underlying Transaction stays where it
is, and its Workspace is not thereby made visible to you.
