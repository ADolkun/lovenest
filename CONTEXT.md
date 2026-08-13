# Context

Glossary for lovenest. Domain terms only — no implementation detail.

## Portfolio

**Asset** — anything with a value that is not a cash Account: real estate, a vehicle,
a valuable, or an investment holding. Cash lives in Accounts; everything else is an Asset.

**Holding** — one investment Asset consolidated per ticker per Wallet. A Holding's
quantity and cost basis are *derived* by replaying its Trades, never entered directly.

**Wallet** — a grouping of Holdings, normally corresponding to one brokerage account.
A Wallet carries the tax character of the account it stands for — Taxable,
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

## Tax character

**Holding Period** — how long a quantity has been held, which decides the tax
character of a gain. **Long** at one year or more, **Short** below it. It is always
*derived* — from the buy date to the sell date, or to today when the position is
still open. It is never a stored flag, and it is never entered by hand.

**Taxable Account** — an account where a sell creates a reportable gain or loss.
Holding Period, Cost Basis, and Realised Gain matter here.

**Tax-Advantaged Account** — an account where trading creates no taxable event:
Roth IRA, Traditional 401(k), HSA. Holding Period is meaningless inside one, so
lot-level history is not tracked for these.

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
Period and any per-lot sale decision need them kept apart, so Lots are tracked
for Taxable Accounts only.

**Wash Sale** — selling at a loss and acquiring the same or a substantially
identical security within 30 days either side, which disallows the loss. The
disallowed amount normally moves into the basis of the replacement shares — but
when the replacement is bought inside an IRA, the loss is lost outright.
Crucially the rule spans *all* accounts a person holds, so detecting it is
never a single-account question.

**Liquid Cash** — settled, uninvested cash sitting inside a brokerage account.
Part of the account's total, but not part of any Holding.

**Cash Equivalent** — an instrument that is nominally a Holding but behaves as
Liquid Cash: a government money-market fund, or a fiat-pegged stablecoin. It has
a ticker and a share count, yet it carries no market risk and no meaningful gain.
Counted as Liquid Cash, never as an invested position — otherwise allocation and
return figures are quietly wrong.

**Dust** — a Holding whose value is negligible: left over from an old trade, a
staking reward, or an airdrop. Carried for completeness, excluded from allocation
views so it does not bury real positions.

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
Workspace, and no view, report, or total ever spans more than one. Membership is
what grants visibility: a Workspace you are not a member of is not merely filtered
out of your reports, it is absent from the application entirely.
