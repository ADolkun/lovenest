"""The aggregate the retirement projection runs on.

The projection itself lives in the tax sidecar (`extras/tax/tax_engine.py`) and
is not reimplemented, moved or retested here. This module only assembles the
figures it already asks for, under the exact key names it already reads, so the
sidecar can merge the response into its form and calculate as before.

A figure names itself in `live` only where this application actually knows it,
and everything not in that list is still whatever the user typed. A bucket the
user keeps no wallet for is therefore absent even though its key is sent as
zero. `hsa_receipts` never appears either: banked medical receipts are paper in
a drawer, and nothing here knows about them.
"""

import uuid
from datetime import date as _date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import asset_group_service, contribution_service

#: Wallet tax character → the projection's bucket. `other` is deliberately
#: absent: the projection has four buckets and each carries its own withdrawal
#: rules, so a wallet whose character is "none of these" cannot be filed under
#: one of them without inventing the rules that would then apply to it.
_BUCKETS = {
    "traditional": "trad_401k",
    "roth": "roth_ira",
    "hsa": "hsa",
    "taxable": "taxable",
}

#: The same four buckets on the contribution side. Not derivable from
#: `_BUCKETS` — the engine's balance key is `roth_ira` but its contribution key
#: is `annual_roth`, and a feed that guessed one from the other would send a
#: key the engine silently reads as zero.
_ANNUAL_KEYS = {
    "traditional": "annual_trad_401k",
    "roth": "annual_roth",
    "hsa": "annual_hsa",
    "taxable": "annual_taxable",
}


async def projection_feed(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    as_of: _date,
) -> dict:
    """Balances by bucket, withdrawable basis, and this year's contributions."""
    balances: dict[str, Decimal] = {bucket: Decimal("0") for bucket in _BUCKETS.values()}
    other = Decimal("0")
    tracked: set[str] = set()

    for group in await asset_group_service.get_groups(session, workspace_id, user_id):
        value = Decimal(str(group.current_value_primary))
        bucket = _BUCKETS.get(group.tax_treatment)
        if bucket is None:
            other += value
        else:
            balances[bucket] += value
            tracked.add(group.tax_treatment)

    # The engine adds the four buckets together, so everything it reads has to
    # be in one currency — the same one the balances are already converted to.
    primary_currency = await asset_group_service._primary_currency_for(session, user_id)
    basis = await contribution_service.basis_by_tax_treatment(
        session, workspace_id, user_id, as_of=as_of, primary_currency=primary_currency
    )
    annual = await contribution_service.annual_by_tax_treatment(
        session, workspace_id, user_id,
        tax_year=as_of.year, primary_currency=primary_currency,
    )

    feed = {
        "as_of": as_of.isoformat(),
        "tax_year": as_of.year,
        "currency": primary_currency,
        **{bucket: contribution_service.cents(total) for bucket, total in balances.items()},
        # The projection's "withdrawable basis": what a Roth IRA can pay out
        # before retirement age without penalty, which is its Net Contribution.
        "roth_basis": contribution_service.cents(basis.get("roth", Decimal("0"))),
        **{
            key: contribution_service.cents(annual.get(treatment, Decimal("0")))
            for treatment, key in _ANNUAL_KEYS.items()
        },
        # Year to date, not a full year: eight months of contributions is not
        # an annual rate, and a projection that treated it as one would
        # under-project every year of the plan. The caller has to say so.
        "annual_is_year_to_date": True,
        "excluded": {
            "other": contribution_service.cents(other),
            "ungrouped": contribution_service.cents(
                await asset_group_service.ungrouped_value(session, workspace_id, user_id)
            ),
        },
    }
    # Only a bucket the user actually keeps a wallet for is live. Every key is
    # still sent, but an untracked one is a zero this application invented, and
    # claiming it would overwrite the real figure the user typed with 0 — and
    # then colour it green. A tracked bucket holding nothing is a different
    # thing: that zero is an answer, and it is claimed.
    feed["live"] = sorted(
        [_BUCKETS[treatment] for treatment in tracked]
        + [_ANNUAL_KEYS[treatment] for treatment in tracked]
        + (["roth_basis"] if "roth" in tracked else [])
    )
    return feed
