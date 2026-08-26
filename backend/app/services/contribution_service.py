"""Contributions and Distributions: money crossing a Wallet's boundary.

Pure functions first (no session, no I/O), then the session-backed reads and
writes. The pure half is what the tests pin: classification, Net Contribution,
and the employer/vesting split are arithmetic, and arithmetic does not need a
database to be wrong in an interesting way.
"""

import re
import uuid
from collections import defaultdict
from datetime import date as _date
from decimal import Decimal
from typing import Iterable, Optional, cast

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset_contribution import AssetContribution
from app.models.asset_group import AssetGroup
from app.schemas.asset_contribution import (
    AssetContributionCreate,
    AssetContributionRead,
    AssetContributionUpdate,
    ContributionKind,
    ContributionParty,
    ContributionSummaryRead,
    ContributionYearRead,
)
from app.services import asset_group_service
from app.services.fx_rate_service import convert

# ---------------------------------------------------------------------------
# Pure: classifying a broker's action text (no DB)
# ---------------------------------------------------------------------------
#
# A brokerage history files every event in one column of prose — the same
# column carries "YOU BOUGHT TESLA INC COM (TSLA)", "DIVIDEND RECEIVED FIDELITY
# GOVERNMENT MONEY MARKET (SPAXX)" and "CASH CONTRIBUTION CURRENT YEAR". Only
# the last of those crosses the account's boundary, and reading the other two
# as deposits is exactly the error this feature exists to stop.

#: Phrases that mean the money stayed inside the account. Checked first, and
#: deliberately short: it only has to name the events that *also* contain an
#: external word, because anything matching nothing at all is already skipped.
#: "DIVIDEND RECEIVED" is the case that matters — a dividend is growth, and it
#: says "received".
_INTERNAL_PHRASES = (
    "dividend",
    "reinvestment",
    "reinvested",
    "interest",
    "cap gain",
    "capital gain",
    "core account",
    "in lieu of",
    "fee",
)

#: Phrases that mean the money crossed the boundary. What they never decide is
#: *which way* — see `classify_flow`.
_EXTERNAL_PHRASES = (
    "contribution",
    "distribution",
    "rollover",
    "funds transfer",
    "transferred to",
    "transferred from",
    "transfer in",
    "transfer out",
    "deposit",
    "withdrawal",
    "withdraw",
    "wire",
    "ach",
    "rmd",
    "employer",
)

#: A contribution the employer made. The user's own money and the employer's
#: are held to different annual limits, so they cannot share a total.
_EMPLOYER_PHRASES = ("employer", "match")

_NORMALIZE = re.compile(r"[^a-z0-9]+")


def _normalize(action: str) -> str:
    return _NORMALIZE.sub(" ", (action or "").lower()).strip()


def classify_flow(action: str, amount: Optional[Decimal]) -> Optional[str]:
    """What a history row's action text and amount mean: `"contribution"`,
    `"distribution"`, or `None` for a row that does not cross the boundary.

    Direction is the **sign of the amount**, never the wording — the same
    decision ADR 0007 makes for trade direction, and for the same reason. A
    single real transfer is filed twice, once in each account, and both rows
    read "TRANSFERRED TO VS ... CURRENT CONTRIBUTION": the one that says
    -6,535.95 is the account paying out and the one that says +6,535.95 is the
    account receiving. Mapping on the word "contribution" would book both as
    deposits and double-count the money.

    `None` is a row this does not model, not a malformed one. Declining is the
    safe direction here, unlike the trade ledger (ADR 0008), because a missed
    contribution understates deposits and so *understates* return, while a
    dividend read as a deposit silently erases real growth.
    """
    text = _normalize(action)
    if not text or amount is None or amount == 0:
        return None
    if any(phrase in text for phrase in _INTERNAL_PHRASES):
        return None
    if not any(phrase in text for phrase in _EXTERNAL_PHRASES):
        return None
    return "contribution" if amount > 0 else "distribution"


def classify_party(action: str) -> str:
    """`"employer"` when the action text names one, else `"self"`."""
    text = _normalize(action)
    return "employer" if any(p in text for p in _EMPLOYER_PHRASES) else "self"


def tax_year_for(action: str, when: _date) -> int:
    """The year a contribution counts against.

    An IRA contribution made before April 15 may be designated for the prior
    year, and a broker that lets you choose says which you chose — Fidelity
    writes "CASH CONTRIBUTION PRIOR YEAR" in the action text. Absent that, the
    year it was paid in is the year it counts against.
    """
    return when.year - 1 if "prior year" in _normalize(action) else when.year


# ---------------------------------------------------------------------------
# Pure: Net Contribution (no DB)
# ---------------------------------------------------------------------------


def is_vested(vested_on: Optional[_date], as_of: _date) -> bool:
    """Whether employer money has become the user's.

    A null `vested_on` is vested: an absent restriction is no restriction, and
    an immediately-vested safe-harbour match has no date to record.
    """
    return vested_on is None or vested_on <= as_of


def _d(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def cents(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def summarise(
    rows: Iterable,
    *,
    as_of: _date,
    current_value: Optional[Decimal] = None,
) -> dict:
    """Net Contribution for one wallet, and the parts it is made of.

    `net` is CONTEXT.md's Net Contribution — what the account's own money
    amounts to — and it counts the user's own contributions plus the employer
    money that has vested, minus every distribution. Unvested employer money is
    in the account and growing, but it is not the user's yet, so it is reported
    beside the figure rather than inside it.

    `return_net_of_contributions` is the wallet's value now minus every dollar
    paid in, vested or not, less what was taken out: a balance that rose only
    because money was deposited shows no gain. It cannot use `net` — unvested
    employer money sits in the balance and would otherwise be counted as return.

    The per-year rows answer a different question — progress against an annual
    limit — and a limit is measured on money paid in, whether or not it has
    vested since. So `years[].employer` is gross, and `years[].net` will not
    add up to `net` while any employer money is still unvested. That is the
    two questions disagreeing, not an error.
    """
    own = employer = employer_vested = distributions = Decimal("0")
    per_year: dict[int, dict[str, Decimal]] = defaultdict(
        lambda: {"own": Decimal("0"), "employer": Decimal("0"), "distributions": Decimal("0")}
    )

    for row in rows:
        amount = _d(row.amount)
        year = per_year[int(row.tax_year)]
        if row.kind == "distribution":
            distributions += amount
            year["distributions"] += amount
        elif row.party == "employer":
            employer += amount
            year["employer"] += amount
            if is_vested(row.vested_on, as_of):
                employer_vested += amount
        else:
            own += amount
            year["own"] += amount

    net = own + employer_vested - distributions
    paid_in = own + employer - distributions
    return {
        "own_contributions": cents(own),
        "employer_contributions": cents(employer),
        "employer_vested": cents(employer_vested),
        "employer_unvested": cents(employer - employer_vested),
        "distributions": cents(distributions),
        "net": cents(net),
        "current_value": None if current_value is None else cents(_d(current_value)),
        "return_net_of_contributions": (
            None if current_value is None else cents(_d(current_value) - paid_in)
        ),
        "years": [
            {
                "tax_year": year,
                "own": cents(totals["own"]),
                "employer": cents(totals["employer"]),
                "distributions": cents(totals["distributions"]),
                "net": cents(totals["own"] + totals["employer"] - totals["distributions"]),
            }
            for year, totals in sorted(per_year.items(), reverse=True)
        ],
    }


def withdrawable_basis(rows: Iterable, *, as_of: _date) -> Decimal:
    """What a Roth IRA can pay out before retirement age without penalty.

    Net Contribution floored at zero: distributions can only take back money
    that went in, so the figure describes an empty basis, never a debt.
    """
    return max(Decimal(str(summarise(rows, as_of=as_of)["net"])), Decimal("0"))


# ---------------------------------------------------------------------------
# Session-backed
# ---------------------------------------------------------------------------


async def _treatments_by_group(
    session: AsyncSession, workspace_id: uuid.UUID
) -> dict[uuid.UUID, Optional[str]]:
    return {
        group_id: treatment
        for group_id, treatment in (
            await session.execute(
                select(AssetGroup.id, AssetGroup.tax_treatment).where(
                    AssetGroup.workspace_id == workspace_id
                )
            )
        ).all()
    }


def _to_read(row: AssetContribution, as_of: _date) -> AssetContributionRead:
    return AssetContributionRead(
        id=row.id,
        group_id=row.group_id,
        kind=cast(ContributionKind, row.kind),
        party=cast(ContributionParty, row.party),
        amount=float(_d(row.amount)),
        date=row.date,
        tax_year=row.tax_year,
        vested_on=row.vested_on,
        is_vested=row.party != "employer" or is_vested(row.vested_on, as_of),
        source=row.source,
        notes=row.notes,
    )


async def _load(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    group_id: Optional[uuid.UUID] = None,
    tax_year: Optional[int] = None,
) -> list[AssetContribution]:
    query = select(AssetContribution).where(AssetContribution.workspace_id == workspace_id)
    if group_id is not None:
        query = query.where(AssetContribution.group_id == group_id)
    if tax_year is not None:
        query = query.where(AssetContribution.tax_year == tax_year)
    result = await session.execute(
        query.order_by(AssetContribution.date.desc(), AssetContribution.created_at.desc())
    )
    return list(result.scalars().all())


async def list_contributions(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    group_id: Optional[uuid.UUID] = None,
    tax_year: Optional[int] = None,
    as_of: _date,
) -> list[AssetContributionRead]:
    rows = await _load(session, workspace_id, group_id=group_id, tax_year=tax_year)
    return [_to_read(row, as_of) for row in rows]


async def _require_group(
    session: AsyncSession, group_id: uuid.UUID, workspace_id: uuid.UUID
) -> AssetGroup:
    """The wallet, or the same 404 a wallet outside the workspace gets.

    `group_id` arrives from a request and is a bare foreign key, so checking
    the workspace here is what stops a contribution being written against
    another tenant's wallet.
    """
    group = await session.get(AssetGroup, group_id)
    if group is None or group.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return group


async def create_contribution(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    data: AssetContributionCreate,
    *,
    as_of: _date,
) -> AssetContributionRead:
    await _require_group(session, data.group_id, workspace_id)
    row = AssetContribution(
        workspace_id=workspace_id,
        group_id=data.group_id,
        kind=data.kind,
        party=data.party,
        amount=data.amount,
        date=data.date,
        tax_year=data.tax_year if data.tax_year is not None else data.date.year,
        vested_on=data.vested_on,
        notes=data.notes,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _to_read(row, as_of)


async def update_contribution(
    session: AsyncSession,
    contribution_id: uuid.UUID,
    workspace_id: uuid.UUID,
    data: AssetContributionUpdate,
    *,
    as_of: _date,
) -> Optional[AssetContributionRead]:
    row = await session.get(AssetContribution, contribution_id)
    if row is None or row.workspace_id != workspace_id:
        return None

    fields = data.model_dump(exclude_unset=True)
    if fields.get("group_id") is not None:
        await _require_group(session, fields["group_id"], workspace_id)
    for field, value in fields.items():
        setattr(row, field, value)
    # A date moved without a tax year moves the year with it; a tax year the
    # caller states outranks the date, which is the whole point of the column.
    if "date" in fields and "tax_year" not in fields:
        row.tax_year = row.date.year

    if row.party == "employer" and row.kind != "contribution":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only a contribution can come from an employer",
        )
    if row.vested_on is not None and row.party != "employer":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only employer money vests",
        )

    await session.commit()
    await session.refresh(row)
    return _to_read(row, as_of)


async def delete_contribution(
    session: AsyncSession, contribution_id: uuid.UUID, workspace_id: uuid.UUID
) -> bool:
    row = await session.get(AssetContribution, contribution_id)
    if row is None or row.workspace_id != workspace_id:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def summaries(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    as_of: _date,
) -> list[ContributionSummaryRead]:
    """One summary per wallet that has any contribution history.

    A wallet with no rows is left out rather than reported as zero: a Net
    Contribution of nothing recorded and a Net Contribution of zero are
    different claims, and only the second is a figure.
    """
    rows = await _load(session, workspace_id)
    if not rows:
        return []

    by_group: dict[uuid.UUID, list[AssetContribution]] = defaultdict(list)
    for row in rows:
        by_group[row.group_id].append(row)

    # The wallet's own currency, not the reader's. A contribution carries no
    # currency of its own — it is recorded in the currency of the wallet it
    # was paid into — so comparing it against a balance converted to whatever
    # the reader displays would subtract euros from dollars, and two members
    # of one workspace would read different returns off identical rows.
    wallets = {
        g.id: g for g in await asset_group_service.get_groups(session, workspace_id, user_id)
    }

    out = []
    for group_id, group_rows in by_group.items():
        wallet = wallets.get(group_id)
        comparable = wallet is not None and wallet.currency is not None
        summary = summarise(
            group_rows,
            as_of=as_of,
            current_value=Decimal(str(wallet.current_value)) if comparable else None,
        )
        out.append(
            ContributionSummaryRead(
                group_id=group_id,
                currency=wallet.currency if wallet is not None else None,
                **{k: v for k, v in summary.items() if k != "years"},
                years=[ContributionYearRead(**y) for y in summary["years"]],
            )
        )
    return sorted(out, key=lambda s: str(s.group_id))


async def _currencies_by_group(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> dict[uuid.UUID, Optional[str]]:
    return {
        g.id: g.currency
        for g in await asset_group_service.get_groups(session, workspace_id, user_id)
    }


async def _to_primary(
    session: AsyncSession,
    amount: Decimal,
    currency: Optional[str],
    primary_currency: str,
) -> Decimal:
    """A wallet's figure in the currency the projection runs in.

    A contribution is recorded in the currency of the wallet it was paid into,
    and the engine adds the four buckets together — so they have to arrive in
    one currency, the same one `current_value_primary` is already converted to.
    A wallet with no single currency is taken at face value rather than
    guessed at; that is the same assumption its balance already makes.
    """
    if not amount or currency is None or currency == primary_currency:
        return amount
    converted, _rate = await convert(session, amount, currency, primary_currency)
    return converted


async def basis_by_tax_treatment(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    as_of: _date,
    primary_currency: str,
) -> dict[str, Decimal]:
    """Withdrawable basis per wallet tax character, for the projection feed.

    Summed per wallet and floored per wallet, not once at the end: an
    over-distributed wallet must not eat into another wallet's basis, because
    the penalty rule is applied account by account.
    """
    rows = await _load(session, workspace_id)
    treatments = await _treatments_by_group(session, workspace_id)
    currencies = await _currencies_by_group(session, workspace_id, user_id)

    by_group: dict[uuid.UUID, list[AssetContribution]] = defaultdict(list)
    for row in rows:
        by_group[row.group_id].append(row)

    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for group_id, group_rows in by_group.items():
        treatment = treatments.get(group_id)
        if treatment is None:
            continue
        totals[treatment] += await _to_primary(
            session,
            withdrawable_basis(group_rows, as_of=as_of),
            currencies.get(group_id),
            primary_currency,
        )
    return dict(totals)


async def annual_by_tax_treatment(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    tax_year: int,
    primary_currency: str,
) -> dict[str, Decimal]:
    """Gross contributions paid in for one tax year, per wallet tax character.

    Gross, and including employer money: the projection is asking what goes
    into each bucket in a year, and unvested money still lands in the account
    and still compounds.
    """
    rows = await _load(session, workspace_id, tax_year=tax_year)
    treatments = await _treatments_by_group(session, workspace_id)
    currencies = await _currencies_by_group(session, workspace_id, user_id)

    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        treatment = treatments.get(row.group_id)
        if treatment is None or row.kind != "contribution":
            continue
        totals[treatment] += await _to_primary(
            session, _d(row.amount), currencies.get(row.group_id), primary_currency
        )
    return dict(totals)


async def delete_by_import(
    session: AsyncSession, import_id: uuid.UUID, workspace_id: uuid.UUID
) -> int:
    """Take back every contribution one import wrote."""
    rows = (
        await session.execute(
            select(AssetContribution).where(
                AssetContribution.import_id == import_id,
                AssetContribution.workspace_id == workspace_id,
            )
        )
    ).scalars().all()
    for row in rows:
        await session.delete(row)
    return len(rows)
