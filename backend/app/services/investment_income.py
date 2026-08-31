"""Investment Income: what a Holding paid out, how often, and what is taxable.

Income at Receipt (CONTEXT.md) reaches the database by two different roads,
because payouts arrive in two different forms:

- **Paid in the asset.** A staking or stablecoin reward grows a position, so
  the sync writes it to the trade ledger as a buy whose ``notes`` say it was
  income (``TradeData.notes``). The units are the payout.
- **Paid in cash.** A money-market or equity dividend lands in the account's
  cash, so it is an ordinary credit in the transaction feed, named only in
  prose: ``DIVIDEND RECEIVED FIDELITY GOVERNMENT MONEY MARKET (SPAXX)``.

Which Holding *earned* a payout is a different question from which one it was
*paid into*, and only the cash road answers it. A broker names the instrument
in the description, so a dividend attributes to a Holding on evidence. A
ledger payout carries no such statement — an exchange can pay the reward on
one asset in a different one, and this account does exactly that: the weekly
USDC reward arrives as BTC. Crediting it to the Holding it landed in would
report a stablecoin earning nothing beside a dust position yielding 109%.

So a ledger payout is the **Wallet's** income, not the Holding's. That is the
most the data supports, and it is enough: the question a reward answers is
whether the account is still being paid.

The two roads never carry the same payout twice: units that grew a position
were never cash, and cash that arrived never grew it.
"""

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as _date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.models.transaction import Transaction
from app.providers.base import INCOME_AT_RECEIPT_NOTE
from app.schemas.asset_group import REPORTABLE_TAX_TREATMENTS

#: What a payout is on a tax return, which is not the same as what the broker
#: calls it. A money-market fund pays *dividends* (1099-DIV box 1a) even though
#: it behaves like interest, and a staking reward is neither — it is ordinary
#: income with no information return of its own.
KIND_INTEREST = "interest"
KIND_DIVIDEND = "dividend"
KIND_REWARD = "reward"

#: Words that make a credit a payout rather than money arriving from outside.
#: Order matters: "DIVIDEND RECEIVED" is a dividend even where the fund behind
#: it is a money market. "distribution" is deliberately absent — it is a
#: withdrawal from a retirement account far more often than a fund payout, and
#: reading one as income would invent growth out of the user's own money.
_CASH_INCOME_PHRASES = ((KIND_DIVIDEND, "dividend"), (KIND_INTEREST, "interest"))

#: How brokers name the instrument inside an action line: trailing parentheses
#: around a bare ticker. "(Cash)" and "(Margin)" match the shape too, which is
#: why a candidate is only accepted when it equals a ticker actually held in
#: that account.
_PARENTHESISED = re.compile(r"\(([A-Za-z0-9.\-]{1,32})\)")

#: Median days between payouts, and what to call that spacing. Bands are wide
#: enough to absorb weekends and month lengths — a monthly payout on the last
#: business day swings between 28 and 33 days.
_CADENCE_BANDS: tuple[tuple[int, str, int], ...] = (
    (2, "daily", 365),
    (10, "weekly", 52),
    (20, "biweekly", 26),
    (45, "monthly", 12),
    (135, "quarterly", 4),
    (200, "semiannual", 2),
    (400, "annual", 1),
)

#: Cadence needs two gaps to be a pattern rather than a coincidence.
_MIN_PAYOUTS_FOR_CADENCE = 3

#: How many recent payouts the run rate averages. One is too easily an outlier;
#: twelve would average away the rate change the figure exists to show.
_RUN_RATE_SAMPLE = 3


@dataclass(frozen=True)
class Payout:
    date: _date
    amount: Decimal
    kind: str
    #: Set only where a description named the ticker — the one case in which
    #: the Holding that earned the payout is stated rather than guessed.
    asset_id: Optional[uuid.UUID] = None
    group_id: Optional[uuid.UUID] = None
    tax_treatment: Optional[str] = None


def _median(values: list[int]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def cadence_of(payouts: list[Payout]) -> tuple[Optional[str], Optional[int]]:
    """How often the payouts arrive, and how many of them a year holds.

    ``(None, None)`` when there are too few to see a pattern, or when the
    spacing is too uneven to name — an irregular series still has a real
    twelve-month total, it just has no run rate to project.
    """
    if len(payouts) < _MIN_PAYOUTS_FOR_CADENCE:
        return None, None
    ordered = sorted(payouts, key=lambda p: p.date)
    gaps = [
        (b.date - a.date).days
        for a, b in zip(ordered, ordered[1:])
        if (b.date - a.date).days > 0
    ]
    if len(gaps) < _MIN_PAYOUTS_FOR_CADENCE - 1:
        return None, None
    median = _median(gaps)
    for limit, name, per_year in _CADENCE_BANDS:
        if median <= limit:
            return name, per_year
    return None, None


def summarise(payouts: list[Payout]) -> dict:
    """The twelve-month total, the cadence, and the rate it is paying *now*.

    Yield is deliberately not computed here. Income over a period divided by
    today's balance is only a yield when the balance held still, and the case
    this feature exists for — cash swept into a money market last month — is
    exactly the case where it did not. ``run_rate`` answers the same question
    honestly: what the recent payouts annualise to, which a caller can compare
    against the balance those payouts were actually earned on.
    """
    total = sum((p.amount for p in payouts), Decimal(0))
    cadence, per_year = cadence_of(payouts)
    ordered = sorted(payouts, key=lambda p: p.date)
    recent = ordered[-_RUN_RATE_SAMPLE:]
    run_rate = (
        sum((p.amount for p in recent), Decimal(0)) / len(recent) * per_year
        if per_year and recent
        else None
    )
    last = ordered[-1] if ordered else None
    return {
        "total": float(total),
        "payouts": len(payouts),
        "cadence": cadence,
        "run_rate": float(run_rate) if run_rate is not None else None,
        "last_date": last.date.isoformat() if last else None,
        "last_amount": float(last.amount) if last else None,
    }


async def collect_payouts(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    since: _date,
    until: _date,
) -> list[Payout]:
    """Every payout in the window, down both roads, with its tax character.

    ``until`` is inclusive: it names the last day a payout may have landed on,
    not the boundary of a period.
    """
    assets = (
        await session.execute(
            select(Asset, AssetGroup.tax_treatment)
            .outerjoin(
                AssetGroup,
                (Asset.group_id == AssetGroup.id) & (AssetGroup.workspace_id == workspace_id),
            )
            .where(
                Asset.workspace_id == workspace_id,
                Asset.is_archived.is_(False),
                Asset.sell_date.is_(None),
                Asset.ticker.isnot(None),
            )
        )
    ).all()
    if not assets:
        return []

    treatments = {asset.id: treatment for asset, treatment in assets}
    payouts: list[Payout] = []

    # Paid in the asset. The Wallet earned it; which Holding did is unstated.
    ledger = await session.execute(
        select(
            AssetTransaction.asset_id,
            AssetTransaction.date,
            AssetTransaction.quantity,
            AssetTransaction.price,
        ).where(
            AssetTransaction.workspace_id == workspace_id,
            AssetTransaction.date >= since,
            AssetTransaction.date <= until,
            AssetTransaction.notes.ilike(f"%{INCOME_AT_RECEIPT_NOTE}%"),
        )
    )
    groups = {asset.id: asset.group_id for asset, _ in assets}
    for asset_id, day, quantity, price in ledger:
        if asset_id not in groups:
            continue
        payouts.append(
            Payout(
                date=day,
                amount=Decimal(quantity) * Decimal(price),
                kind=KIND_REWARD,
                group_id=groups[asset_id],
                tax_treatment=treatments[asset_id],
            )
        )

    # Paid in cash. The description names the Holding that earned it.
    by_account: dict[str, list[Asset]] = defaultdict(list)
    for asset, _ in assets:
        if asset.account_external_id:
            by_account[asset.account_external_id].append(asset)
    if not by_account:
        return payouts

    cash = await session.execute(
        select(
            Account.external_id,
            Transaction.date,
            Transaction.amount,
            Transaction.currency,
            Transaction.description,
        )
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.workspace_id == workspace_id,
            Transaction.type == "credit",
            Transaction.date >= since,
            Transaction.date <= until,
            Account.external_id.in_(by_account.keys()),
        )
    )
    for account_external_id, day, amount, currency, description in cash:
        text = (description or "").lower()
        kind = next((k for k, phrase in _CASH_INCOME_PHRASES if phrase in text), None)
        if kind is None:
            continue
        named = {m.group(1).upper() for m in _PARENTHESISED.finditer(description or "")}
        for asset in by_account[account_external_id]:
            if (asset.ticker or "").strip().upper() not in named:
                continue
            # A payout denominated in something other than the Holding is not
            # summable with the rest of its income, and converting it here
            # would report a figure at a rate no statement shows.
            if currency and asset.currency and currency != asset.currency:
                continue
            payouts.append(
                Payout(
                    date=day,
                    amount=Decimal(amount),
                    kind=kind,
                    asset_id=asset.id,
                    group_id=asset.group_id,
                    tax_treatment=treatments[asset.id],
                )
            )
    return payouts


async def workspace_income(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    as_of: Optional[_date] = None,
    months: int = 12,
) -> dict:
    """Trailing income per Holding and per Wallet.

    ``holdings`` carries only what a description attributed on evidence.
    ``wallets`` carries everything the Wallet received, so a reward paid in an
    asset other than the one that earned it is reported where it is certainly
    true rather than where it merely landed.

    A Holding or Wallet that received nothing is absent rather than present
    with a zero: a zero would claim it pays nothing, when it may equally be one
    whose payouts this cannot see.
    """
    as_of = as_of or _date.today()
    payouts = await collect_payouts(
        session, workspace_id, since=as_of - timedelta(days=round(months * 30.44)), until=as_of
    )

    by_holding: dict[uuid.UUID, list[Payout]] = defaultdict(list)
    by_wallet: dict[uuid.UUID, list[Payout]] = defaultdict(list)
    for payout in payouts:
        if payout.asset_id is not None:
            by_holding[payout.asset_id].append(payout)
        if payout.group_id is not None:
            by_wallet[payout.group_id].append(payout)

    return {
        "holdings": {str(key): summarise(rows) for key, rows in by_holding.items() if rows},
        "wallets": {str(key): summarise(rows) for key, rows in by_wallet.items() if rows},
    }


async def reportable_income(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    start: _date,
    end: _date,
) -> dict:
    """Investment income arising in Taxable Wallets, split the way a return is.

    The same allowlist that gates Reportable Gain (``REPORTABLE_TAX_TREATMENTS``)
    gates this: a payout inside a Roth or a Traditional IRA is not income this
    year, and a feed that summed it would tax money the user is not taxed on.

    ``end`` is exclusive, so a tax year is ``start=1 Jan, end=1 Jan`` and no
    payout is counted in two years.

    Qualified dividends are **not** derived. Whether a dividend is qualified
    turns on the holding period behind it and on the payer, neither of which a
    description states — and a money-market dividend, the largest figure here,
    never is. The caller keeps its own qualified figure.

    ``non_reportable_income`` is everything else, carried so a caller can say
    what it left out instead of silently dropping it. It must never reach a
    tax figure.
    """
    payouts = await collect_payouts(
        session, workspace_id, since=start, until=end - timedelta(days=1)
    )
    buckets = {KIND_INTEREST: Decimal(0), KIND_DIVIDEND: Decimal(0), KIND_REWARD: Decimal(0)}
    non_reportable = Decimal(0)
    for payout in payouts:
        if payout.tax_treatment in REPORTABLE_TAX_TREATMENTS:
            buckets[payout.kind] += payout.amount
        else:
            non_reportable += payout.amount

    # Rounded to cents: these go on a tax form, and a reward priced to six
    # decimals of a coin is not a figure anyone files.
    def money(amount: Decimal) -> float:
        return float(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "interest_income": money(buckets[KIND_INTEREST]),
        "ordinary_dividends": money(buckets[KIND_DIVIDEND]),
        "other_ordinary_income": money(buckets[KIND_REWARD]),
        "non_reportable_income": money(non_reportable),
    }
