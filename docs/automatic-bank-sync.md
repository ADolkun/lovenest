# Automatic bank synchronization

`BANK_SYNC_MIN_INTERVAL_HOURS` controls how old the last successful bank sync
must be before a connection is eligible for automatic synchronization.
The default is **4 hours**, preserving the previous behavior. Any finite,
positive number of hours within Python's supported date range is accepted;
zero, negative/non-finite values and intervals that overflow the sync cutoff
fail startup validation.

Celery beat still checks hourly. Connections with no previous successful sync
remain eligible, and manual/on-demand synchronization is unchanged. A connection
is selected only when `last_sync_at` is strictly older than the cutoff, as before;
therefore the actual period can be longer than the configured minimum by up to
one polling tick (plus execution time). Startup-triggered automatic syncs use
the same eligibility filter.

## PSD2 unattended access

Banks may limit unattended account-information access to **four times per day**
under [Article 36(5)(b) of Delegated Regulation (EU) 2018/389](https://eur-lex.europa.eu/eli/reg_del/2018/389/oj).
For those banks, set `BANK_SYNC_MIN_INTERVAL_HOURS=6` in the worker environment.
The supplied development and production Compose files pass it through the shared
backend environment to API/worker/beat processes. Restart processes after changing
configuration. This setting applies to all bank providers, not just Enable Banking.

This is a spacing policy, **not a strict rolling request quota**: failures and
rate-limited runs do not advance `last_sync_at`, so they remain eligible on later
hourly checks. Manual syncs, multiple connections to the same bank and paginated
or retried provider requests may also consume access. A six-hour interval alone
does not guarantee compliance with every bank's per-resource accounting policy.
There is no change to provider refresh strategy or transaction fetch windows.

## SimpleFIN pending authorizations

A pending authorization is a provider observation, not a settled purchase.
SimpleFIN can remove it after cancellation or replace it with one or several
posted transactions with different IDs and amounts. Date-only timestamps and
merchant text cannot safely identify which posted row replaced a hold.
Differently keyed SimpleFIN pending and posted rows therefore remain separate;
exact-ID posting still upgrades the existing row and preserves its annotations.
Same-ID pending adjustments refresh untouched bank-owned amounts and dates.

The sync rechecks active pending rows within a bounded one-year history horizon,
using 45-day requests. Older or undated holds require manual review. A hold is
retired only inside a successfully read date window, after two source observations
at least two days apart report it absent and its source date is at least seven
days old. Coverage requires the account and transaction list to be present, every
row to parse, no provider warnings/errors, and a fresh source balance timestamp.
Repeated reads of the same cached observation do not advance this lifecycle.
A balance timestamp is only a freshness guard, not proof of cancellation.
Missing/closed/excluded accounts and incomplete reads never establish absence.

Retirement sets the existing ignore flag, retaining the row, amount, pending
status, bank payload, notes, category and attachments. App-owned `sync_metadata`
records why and when it happened, separately from bank `raw_data`. The row remains
available in ledger views and no longer counts in monetary totals. An exact-ID
posted return or a newer complete feed containing the hold restores only an
ignore owned by sync. A manual financial edit, ignore toggle or ignore rule takes
ownership away from automatic reconciliation. Legacy rows whose bank evidence
disagrees with their stored financial fields are left for review. Downgrading
restores sync-owned ignores before removing audit storage; user ignores remain.
Legitimate long holds that remain in the feed are never retired merely because of age.

The migration only adds nullable audit storage; it does not repair historical
transactions. Existing stale rows need subsequent qualifying observations, or a
separately reviewed manual ignore. Posted purchases and refunds are not expired.
