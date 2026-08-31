"""Tests for Investment Income — the trailing payout total and its cadence.

The pure section pins the cadence bands and the run rate, which is where a
wrong answer is quietly wrong: a monthly payout misread as weekly overstates
what a Holding earns by four. The service section pins the two roads a payout
travels, and that a cash credit reaches the right Holding.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_transaction import AssetTransaction
from app.models.transaction import Transaction
from app.models.user import User
from app.providers.base import INCOME_AT_RECEIPT_NOTE
from app.services.investment_income import (
    Payout,
    cadence_of,
    summarise,
    workspace_income,
)


def _series(start: date, every: int, count: int, amount: str) -> list[Payout]:
    return [Payout(start + timedelta(days=every * i), Decimal(amount)) for i in range(count)]


# ---------------------------------------------------------------------------
# Pure: cadence and run rate (no DB)
# ---------------------------------------------------------------------------

def test_a_weekly_series_is_weekly():
    assert cadence_of(_series(date(2026, 1, 1), 7, 20, "1.5")) == ("weekly", 52)


def test_a_month_end_series_is_monthly_despite_uneven_months():
    # Last business day of each month: 28 to 33 days apart, never exactly 30.
    days = [date(2026, m, 1) - timedelta(days=1) for m in range(2, 13)]
    assert cadence_of([Payout(d, Decimal("60")) for d in days]) == ("monthly", 12)


def test_a_quarterly_series_is_quarterly():
    assert cadence_of(_series(date(2025, 3, 1), 91, 5, "5")) == ("quarterly", 4)


def test_two_payouts_are_not_yet_a_pattern():
    # Two points make one gap, and one gap is a coincidence, not a cadence.
    assert cadence_of(_series(date(2026, 1, 1), 7, 2, "1")) == (None, None)


def test_a_series_spread_over_years_names_no_cadence():
    payouts = [Payout(date(2020, 1, 1), Decimal("1")), Payout(date(2023, 1, 1), Decimal("1")),
               Payout(date(2026, 1, 1), Decimal("1"))]
    assert cadence_of(payouts) == (None, None)


def test_run_rate_annualises_the_recent_payouts_not_the_old_ones():
    # A rate cut: the twelve-month total still carries the old payouts, the
    # run rate must not. This is the whole point of the figure.
    old = _series(date(2026, 1, 1), 7, 10, "10")
    new = _series(date(2026, 3, 19), 7, 3, "1")
    result = summarise(old + new)

    assert result["total"] == pytest.approx(103.0)
    assert result["cadence"] == "weekly"
    assert result["run_rate"] == pytest.approx(52.0)
    assert result["last_amount"] == pytest.approx(1.0)


def test_an_irregular_series_still_reports_its_total():
    payouts = [Payout(date(2026, 1, 1), Decimal("7")), Payout(date(2026, 6, 1), Decimal("3"))]
    result = summarise(payouts)

    assert result["total"] == pytest.approx(10.0)
    assert result["cadence"] is None
    assert result["run_rate"] is None


# ---------------------------------------------------------------------------
# Service: the two roads a payout travels
# ---------------------------------------------------------------------------

async def _holding(session, user, workspace, *, ticker: str, account_external_id=None) -> Asset:
    asset = Asset(
        id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, name=ticker,
        type="cash_equivalent", currency="USD", valuation_method="manual", ticker=ticker,
        units=Decimal("1000"), position=0, account_external_id=account_external_id,
    )
    session.add(asset)
    await session.commit()
    return asset


async def _account(session, user, workspace, *, external_id: str) -> Account:
    account = Account(
        id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, name="Brokerage",
        type="investment", currency="USD", external_id=external_id,
    )
    session.add(account)
    await session.commit()
    return account


def _credit(user, workspace, account, *, description: str, amount: str, when: date) -> Transaction:
    return Transaction(
        id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, account_id=account.id,
        description=description, amount=Decimal(amount), currency="USD", date=when,
        effective_date=when, type="credit", source="sync",
    )


@pytest.mark.asyncio
async def test_a_payout_paid_in_the_asset_is_read_off_the_ledger(
    session: AsyncSession, test_workspace, test_user: User
):
    asset = await _holding(session, test_user, test_workspace, ticker="USDC")
    for i in range(4):
        session.add(
            AssetTransaction(
                id=uuid.uuid4(), asset_id=asset.id, workspace_id=test_workspace.id,
                kind="buy", quantity=Decimal("2"), price=Decimal("1"), fee=Decimal("0"),
                date=date(2026, 1, 1) + timedelta(days=7 * i), source="coinbase",
                notes=f"Coinbase interest — {INCOME_AT_RECEIPT_NOTE}",
            )
        )
    # A plain purchase on the same Holding is not income and must not be summed.
    session.add(
        AssetTransaction(
            id=uuid.uuid4(), asset_id=asset.id, workspace_id=test_workspace.id,
            kind="buy", quantity=Decimal("5000"), price=Decimal("1"), fee=Decimal("0"),
            date=date(2026, 2, 1), source="coinbase",
        )
    )
    await session.commit()

    result = await workspace_income(session, test_workspace.id, as_of=date(2026, 3, 1))

    assert result[str(asset.id)]["total"] == pytest.approx(8.0)
    assert result[str(asset.id)]["cadence"] == "weekly"


@pytest.mark.asyncio
async def test_a_dividend_credit_reaches_the_holding_its_description_names(
    session: AsyncSession, test_workspace, test_user: User
):
    account = await _account(session, test_user, test_workspace, external_id="ACT-1")
    spaxx = await _holding(session, test_user, test_workspace, ticker="SPAXX", account_external_id="ACT-1")
    nvda = await _holding(session, test_user, test_workspace, ticker="NVDA", account_external_id="ACT-1")
    session.add_all([
        _credit(test_user, test_workspace, account,
                description="DIVIDEND RECEIVED FIDELITY GOVERNMENT MONEY MARKET (SPAXX) (Cash)",
                amount="60", when=date(2026, 1, 31)),
        _credit(test_user, test_workspace, account,
                description="DIVIDEND RECEIVED NVIDIA CORPORATION COM (NVDA) (Cash)",
                amount="5", when=date(2026, 1, 31)),
    ])
    await session.commit()

    result = await workspace_income(session, test_workspace.id, as_of=date(2026, 3, 1))

    assert result[str(spaxx.id)]["total"] == pytest.approx(60.0)
    assert result[str(nvda.id)]["total"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_income_naming_no_ticker_belongs_to_the_account_not_a_holding(
    session: AsyncSession, test_workspace, test_user: User
):
    account = await _account(session, test_user, test_workspace, external_id="ACT-1")
    asset = await _holding(session, test_user, test_workspace, ticker="SPAXX", account_external_id="ACT-1")
    session.add(
        _credit(test_user, test_workspace, account, description="Interest earned of $1.24.",
                amount="1.24", when=date(2026, 1, 31))
    )
    await session.commit()

    result = await workspace_income(session, test_workspace.id, as_of=date(2026, 3, 1))

    # Sweep interest on the cash balance is not the money-market fund's payout,
    # and attributing it to the only Holding in the account would invent yield.
    assert str(asset.id) not in result


@pytest.mark.asyncio
async def test_a_purchase_credit_is_not_income(
    session: AsyncSession, test_workspace, test_user: User
):
    account = await _account(session, test_user, test_workspace, external_id="ACT-1")
    asset = await _holding(session, test_user, test_workspace, ticker="SPAXX", account_external_id="ACT-1")
    session.add(
        _credit(test_user, test_workspace, account,
                description="REDEMPTION FROM CORE ACCOUNT (SPAXX)", amount="500",
                when=date(2026, 1, 31))
    )
    await session.commit()

    assert str(asset.id) not in await workspace_income(
        session, test_workspace.id, as_of=date(2026, 3, 1)
    )


@pytest.mark.asyncio
async def test_a_holding_that_received_nothing_is_absent_rather_than_zero(
    session: AsyncSession, test_workspace, test_user: User
):
    asset = await _holding(session, test_user, test_workspace, ticker="VOO")

    # Absent, not `{"total": 0}` — a zero would claim the Holding pays nothing,
    # when it may equally be one whose payouts this cannot see.
    assert str(asset.id) not in await workspace_income(
        session, test_workspace.id, as_of=date(2026, 3, 1)
    )
