"""Investment Income: what a Holding paid out, and how often.

Income at Receipt (CONTEXT.md) reaches the database by two different roads,
because payouts arrive in two different forms:

- **Paid in the asset.** A staking or stablecoin reward grows the position
  itself, so the sync writes it to the trade ledger as a buy whose ``notes``
  say it was income (``TradeData.notes``). The units are the payout and the
  price is what they were worth on the day.
- **Paid in cash.** A money-market or equity dividend lands in the account's
  cash, so it is an ordinary credit in the transaction feed, named only in
  prose: ``DIVIDEND RECEIVED FIDELITY GOVERNMENT MONEY MARKET (SPAXX)``.

Nothing links the second kind to a Holding, so it is matched: the account is
the one the Holding sits in (``Asset.account_external_id``), and the ticker is
the one the description names in parentheses, which is how every US broker
writes it. A row naming no ticker — "Interest earned of $1.24." on a sweep
balance — belongs to the account rather than to any Holding, and is skipped
rather than attributed to an arbitrary one.

The two roads never carry the same payout twice: units that grew the position
were never cash, and cash that arrived never grew it.
"""

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as _date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_transaction import AssetTransaction
from app.models.transaction import Transaction
from app.providers.base import INCOME_AT_RECEIPT_NOTE

#: Words that make a credit a payout on a Holding rather than money arriving
#: from outside. Kept to the two that name income unambiguously: "distribution"
#: is a withdrawal from a retirement account far more often than a fund payout,
#: and reading one as income would invent growth out of the user's own money.
_CASH_INCOME_PHRASES = ("dividend", "interest")

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


async def workspace_income(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    as_of: Optional[_date] = None,
    months: int = 12,
) -> dict[str, dict]:
    """Income per Holding over the trailing window, keyed by asset id.

    Holdings that received nothing are absent rather than present with a zero:
    a zero here would claim the Holding pays nothing, when it may equally be
    one whose payouts this cannot see.
    """
    as_of = as_of or _date.today()
    since = as_of - timedelta(days=round(months * 30.44))

    assets = (
        await session.execute(
            select(Asset).where(
                Asset.workspace_id == workspace_id,
                Asset.is_archived.is_(False),
                Asset.sell_date.is_(None),
                Asset.ticker.isnot(None),
            )
        )
    ).scalars().all()
    if not assets:
        return {}

    payouts: dict[uuid.UUID, list[Payout]] = defaultdict(list)
    currencies = {a.id: a.currency for a in assets}

    # Paid in the asset: the ledger row is the payout.
    ledger = await session.execute(
        select(AssetTransaction.asset_id, AssetTransaction.date, AssetTransaction.quantity, AssetTransaction.price)
        .where(
            AssetTransaction.workspace_id == workspace_id,
            AssetTransaction.date >= since,
            AssetTransaction.date <= as_of,
            AssetTransaction.notes.ilike(f"%{INCOME_AT_RECEIPT_NOTE}%"),
        )
    )
    for asset_id, day, quantity, price in ledger:
        if asset_id in currencies:
            payouts[asset_id].append(Payout(day, Decimal(quantity) * Decimal(price)))

    # Paid in cash: match the credit's account and the ticker it names.
    by_account: dict[str, list[Asset]] = defaultdict(list)
    for asset in assets:
        if asset.account_external_id:
            by_account[asset.account_external_id].append(asset)
    cash = (
        await session.execute(
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
                Transaction.date <= as_of,
                Account.external_id.in_(by_account.keys()),
            )
        )
        if by_account
        else []
    )
    for account_external_id, day, amount, currency, description in cash:
        text = (description or "").lower()
        if not any(phrase in text for phrase in _CASH_INCOME_PHRASES):
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
            payouts[asset.id].append(Payout(day, Decimal(amount)))

    return {
        str(asset_id): summarise(rows) | {"currency": currencies[asset_id]}
        for asset_id, rows in payouts.items()
        if rows
    }
