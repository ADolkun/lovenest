"""Service-level tests for dashboard_service.

Tests: _month_range, get_summary, get_spending_by_category,
get_projected_transactions, _account_balance_at, _total_balance_by_currency,
_get_open_accounts.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.recurring_transaction import RecurringTransaction
from app.models.transaction import Transaction
from app.services.dashboard_service import (
    _month_range,
    _get_open_accounts,
    _account_balance_at,
    _total_balance_by_currency,
    get_summary,
    get_spending_by_category,
    get_projected_transactions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_account(
    session: AsyncSession, user_id: uuid.UUID,
    name: str = "Dash Test", acc_type: str = "checking",
    balance: str = "0.00", currency: str = "BRL",
    connection_id: uuid.UUID | None = None,
    is_closed: bool = False,
) -> Account:
    account = Account(
        id=uuid.uuid4(), user_id=user_id, name=name,
        type=acc_type, balance=Decimal(balance), currency=currency,
        connection_id=connection_id, is_closed=is_closed,
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


async def _add_txn(
    session: AsyncSession, user_id: uuid.UUID, account_id: uuid.UUID,
    amount: float, txn_type: str, txn_date: date,
    source: str = "manual", transfer_pair_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
) -> Transaction:
    from datetime import datetime, timezone
    txn = Transaction(
        id=uuid.uuid4(), user_id=user_id, account_id=account_id,
        description=f"Test {txn_type} {amount}", amount=Decimal(str(amount)),
        date=txn_date, type=txn_type, source=source, currency="BRL",
        transfer_pair_id=transfer_pair_id, category_id=category_id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(txn)
    await session.commit()
    await session.refresh(txn)
    return txn


async def _make_category(
    session: AsyncSession, user_id: uuid.UUID, name: str,
    icon: str = "tag", color: str = "#000",
) -> Category:
    cat = Category(
        id=uuid.uuid4(), user_id=user_id, name=name,
        icon=icon, color=color, is_system=False,
    )
    session.add(cat)
    await session.commit()
    await session.refresh(cat)
    return cat


# ---------------------------------------------------------------------------
# _month_range (pure function)
# ---------------------------------------------------------------------------


def test_month_range_normal():
    start, end = _month_range(date(2025, 6, 15))
    assert start == date(2025, 6, 1)
    assert end == date(2025, 7, 1)


def test_month_range_december():
    start, end = _month_range(date(2025, 12, 20))
    assert start == date(2025, 12, 1)
    assert end == date(2026, 1, 1)


def test_month_range_january():
    start, end = _month_range(date(2026, 1, 1))
    assert start == date(2026, 1, 1)
    assert end == date(2026, 2, 1)


# ---------------------------------------------------------------------------
# _get_open_accounts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_open_accounts(session: AsyncSession, test_user):
    """Returns only non-closed accounts."""
    open_acc = await _make_account(session, test_user.id, "Open")
    closed_acc = await _make_account(session, test_user.id, "Closed", is_closed=True)

    accounts = await _get_open_accounts(session, test_user.id)
    ids = [a.id for a in accounts]
    assert open_acc.id in ids
    assert closed_acc.id not in ids


# ---------------------------------------------------------------------------
# _account_balance_at (dashboard version — supports bank-connected)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_balance_at_manual(session: AsyncSession, test_user):
    """Manual account balance is sum of transactions up to cutoff."""
    account = await _make_account(session, test_user.id, "Manual Bal")
    today = date.today()

    await _add_txn(session, test_user.id, account.id, 1000, "credit", today - timedelta(days=10), source="opening_balance")
    await _add_txn(session, test_user.id, account.id, 200, "debit", today - timedelta(days=5))
    await _add_txn(session, test_user.id, account.id, 300, "debit", today)

    # Balance at 3 days ago should be 1000 - 200 = 800 (excludes today's 300 debit)
    bal = await _account_balance_at(session, account, today - timedelta(days=3))
    assert bal == pytest.approx(800.0)


@pytest.mark.asyncio
async def test_account_balance_at_manual_opening_fallback(session: AsyncSession, test_user):
    """Opening balance dated after cutoff is not carried back — it appears as a delta on its actual date."""
    account = await _make_account(session, test_user.id, "Fallback Bal")
    today = date.today()

    # Opening balance dated today, cutoff is yesterday
    await _add_txn(session, test_user.id, account.id, 5000, "credit", today, source="opening_balance")

    # Balance at yesterday is 0 — the opening balance hasn't happened yet
    bal = await _account_balance_at(session, account, today - timedelta(days=1))
    assert bal == pytest.approx(0.0)

    # Balance at today includes the opening balance
    bal_today = await _account_balance_at(session, account, today)
    assert bal_today == pytest.approx(5000.0)


@pytest.mark.asyncio
async def test_account_balance_at_bank_connected(session: AsyncSession, test_user, test_connection):
    """Bank-connected account backtracks from stored balance."""
    account = await _make_account(
        session, test_user.id, "Connected Bal", balance="5000.00",
        connection_id=test_connection.id,
    )
    today = date.today()

    # Add transactions after cutoff
    await _add_txn(session, test_user.id, account.id, 300, "credit", today)

    # Balance at yesterday = 5000 - 300 = 4700
    bal = await _account_balance_at(session, account, today - timedelta(days=1))
    assert bal == pytest.approx(4700.0)


@pytest.mark.asyncio
async def test_account_balance_at_credit_card_connected(session: AsyncSession, test_user, test_connection):
    """Bank-connected credit_card negates balance."""
    account = await _make_account(
        session, test_user.id, "CC Bal", acc_type="credit_card", balance="2000.00",
        connection_id=test_connection.id,
    )
    bal = await _account_balance_at(session, account, date.today())
    # Credit card: current_bal = -2000
    assert bal == pytest.approx(-2000.0)


# ---------------------------------------------------------------------------
# _total_balance_by_currency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_total_balance_by_currency(session: AsyncSession, test_user):
    """Total balance groups by currency."""
    brl_acc = await _make_account(session, test_user.id, "BRL", currency="BRL")
    usd_acc = await _make_account(session, test_user.id, "USD", currency="USD")
    today = date.today()

    await _add_txn(session, test_user.id, brl_acc.id, 1000, "credit", today, source="opening_balance")
    await _add_txn(session, test_user.id, usd_acc.id, 500, "credit", today, source="opening_balance")

    totals = await _total_balance_by_currency(session, test_user.id, today)
    assert totals.get("BRL", 0) == pytest.approx(1000.0)
    assert totals.get("USD", 0) == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# get_summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_summary_basic(session: AsyncSession, test_user):
    """Summary returns correct structure with balances and counts."""
    account = await _make_account(session, test_user.id, "Summary Acc")
    today = date.today()

    await _add_txn(session, test_user.id, account.id, 5000, "credit", today, source="opening_balance")
    await _add_txn(session, test_user.id, account.id, 200, "debit", today)
    await _add_txn(session, test_user.id, account.id, 100, "credit", today)

    summary = await get_summary(session, test_user.id)
    assert summary.monthly_income == pytest.approx(100.0)
    assert summary.monthly_expenses == pytest.approx(200.0)
    assert summary.accounts_count >= 1


@pytest.mark.asyncio
async def test_get_summary_excludes_opening_balance_from_income(session: AsyncSession, test_user):
    """Opening balance does not count as monthly income."""
    account = await _make_account(session, test_user.id, "No OB Income")
    await _add_txn(session, test_user.id, account.id, 10000, "credit", date.today(), source="opening_balance")

    summary = await get_summary(session, test_user.id)
    assert summary.monthly_income == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_get_summary_excludes_transfers(session: AsyncSession, test_user):
    """Transfer pair transactions excluded from income/expenses."""
    account = await _make_account(session, test_user.id, "Transfer Excl")
    today = date.today()
    pair_id = uuid.uuid4()

    await _add_txn(session, test_user.id, account.id, 500, "debit", today, transfer_pair_id=pair_id)
    await _add_txn(session, test_user.id, account.id, 100, "debit", today)

    summary = await get_summary(session, test_user.id)
    assert summary.monthly_expenses == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_get_summary_pending_categorization(session: AsyncSession, test_user):
    """Summary counts uncategorized transactions."""
    account = await _make_account(session, test_user.id, "Pending Cat")
    today = date.today()

    # 2 uncategorized, 1 opening_balance (excluded)
    await _add_txn(session, test_user.id, account.id, 100, "debit", today)
    await _add_txn(session, test_user.id, account.id, 200, "debit", today)
    await _add_txn(session, test_user.id, account.id, 5000, "credit", today, source="opening_balance")

    summary = await get_summary(session, test_user.id)
    assert summary.pending_categorization >= 2


@pytest.mark.asyncio
async def test_get_summary_with_specific_month(session: AsyncSession, test_user):
    """Summary uses the specified month."""
    account = await _make_account(session, test_user.id, "Month Test")
    today = date.today()
    past = (today.replace(day=1) - timedelta(days=1)).replace(day=15)

    await _add_txn(session, test_user.id, account.id, 300, "debit", past)

    # Current month - no transactions
    await get_summary(session, test_user.id, month=today.replace(day=1))
    # Past month - has 300 debit
    summary_past = await get_summary(session, test_user.id, month=past.replace(day=1))
    assert summary_past.monthly_expenses >= 300.0


@pytest.mark.asyncio
async def test_get_summary_with_balance_date(session: AsyncSession, test_user):
    """balance_date overrides the default cutoff for balance calculation."""
    account = await _make_account(session, test_user.id, "Balance Date")
    today = date.today()

    await _add_txn(session, test_user.id, account.id, 1000, "credit", today - timedelta(days=10))
    await _add_txn(session, test_user.id, account.id, 500, "debit", today - timedelta(days=3))

    # With cutoff 5 days ago, the 500 debit shouldn't be included
    summary = await get_summary(
        session, test_user.id, month=today.replace(day=1),
        balance_date=today - timedelta(days=5),
    )
    total = sum(summary.total_balance.values())
    assert total == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# get_spending_by_category
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spending_by_category_basic(session: AsyncSession, test_user):
    """Returns spending grouped by category."""
    cat = await _make_category(session, test_user.id, "Food", color="#F00")
    account = await _make_account(session, test_user.id, "Spend Test")
    today = date.today()

    await _add_txn(session, test_user.id, account.id, 100, "debit", today, category_id=cat.id)
    await _add_txn(session, test_user.id, account.id, 50, "debit", today, category_id=cat.id)

    spending = await get_spending_by_category(session, test_user.id)
    assert len(spending) > 0
    food = next((s for s in spending if s.category_id == str(cat.id)), None)
    assert food is not None
    assert food.total == pytest.approx(150.0)


@pytest.mark.asyncio
async def test_spending_by_category_uncategorized(session: AsyncSession, test_user):
    """Uncategorized transactions show as 'Sem categoria'."""
    account = await _make_account(session, test_user.id, "Uncat Spend")
    today = date.today()

    await _add_txn(session, test_user.id, account.id, 75, "debit", today)

    spending = await get_spending_by_category(session, test_user.id)
    uncat = next((s for s in spending if s.category_id is None), None)
    assert uncat is not None
    assert uncat.category_name == "Sem categoria"


@pytest.mark.asyncio
async def test_spending_excludes_credits(session: AsyncSession, test_user):
    """Spending by category only includes debit transactions."""
    cat = await _make_category(session, test_user.id, "Income Cat")
    account = await _make_account(session, test_user.id, "Credit Excl")
    today = date.today()

    await _add_txn(session, test_user.id, account.id, 1000, "credit", today, category_id=cat.id)

    spending = await get_spending_by_category(session, test_user.id)
    income = next((s for s in spending if s.category_id == str(cat.id)), None)
    assert income is None


@pytest.mark.asyncio
async def test_spending_excludes_transfers(session: AsyncSession, test_user):
    """Transfer pairs are excluded from spending."""
    account = await _make_account(session, test_user.id, "Transfer Spend")
    today = date.today()
    pair_id = uuid.uuid4()

    await _add_txn(session, test_user.id, account.id, 500, "debit", today, transfer_pair_id=pair_id)

    spending = await get_spending_by_category(session, test_user.id)
    # No spending should include the transfer
    total = sum(s.total for s in spending)
    assert 500 not in [s.total for s in spending] or total == 0


@pytest.mark.asyncio
async def test_spending_percentage(session: AsyncSession, test_user):
    """Percentages sum to approximately 100%."""
    cat1 = await _make_category(session, test_user.id, "Cat A")
    cat2 = await _make_category(session, test_user.id, "Cat B")
    account = await _make_account(session, test_user.id, "Pct Test")
    today = date.today()

    await _add_txn(session, test_user.id, account.id, 300, "debit", today, category_id=cat1.id)
    await _add_txn(session, test_user.id, account.id, 700, "debit", today, category_id=cat2.id)

    spending = await get_spending_by_category(session, test_user.id)
    total_pct = sum(s.percentage for s in spending)
    assert total_pct == pytest.approx(100.0, abs=0.1)


# ---------------------------------------------------------------------------
# get_projected_transactions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_projected_transactions(session: AsyncSession, test_user):
    """Projected transactions include recurring template details."""
    cat = await _make_category(session, test_user.id, "Recurring Cat")

    # Create a recurring transaction for next month
    next_month = date.today().replace(day=1)
    if next_month.month == 12:
        next_month = next_month.replace(year=next_month.year + 1, month=1)
    else:
        next_month = next_month.replace(month=next_month.month + 1)

    from datetime import datetime, timezone
    rec = RecurringTransaction(
        id=uuid.uuid4(), user_id=test_user.id,
        description="Weekly Coffee", amount=Decimal("25.00"),
        currency="BRL", type="debit", frequency="weekly",
        start_date=next_month, next_occurrence=next_month,
        is_active=True, category_id=cat.id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(rec)
    await session.commit()

    projections = await get_projected_transactions(session, test_user.id, month=next_month)
    assert len(projections) >= 4  # Weekly = at least 4 occurrences

    for proj in projections:
        assert proj.description == "Weekly Coffee"
        assert proj.amount == 25.0
        assert proj.category_name == "Recurring Cat"


@pytest.mark.asyncio
async def test_get_projected_transactions_no_category(session: AsyncSession, test_user):
    """Projected transactions work without category."""
    next_month = date.today().replace(day=1)
    if next_month.month == 12:
        next_month = next_month.replace(year=next_month.year + 1, month=1)
    else:
        next_month = next_month.replace(month=next_month.month + 1)

    from datetime import datetime, timezone
    rec = RecurringTransaction(
        id=uuid.uuid4(), user_id=test_user.id,
        description="Monthly Fee", amount=Decimal("50.00"),
        currency="BRL", type="debit", frequency="monthly",
        start_date=next_month, next_occurrence=next_month,
        is_active=True, category_id=None,
        created_at=datetime.now(timezone.utc),
    )
    session.add(rec)
    await session.commit()

    projections = await get_projected_transactions(session, test_user.id, month=next_month)
    assert len(projections) >= 1
    proj = projections[0]
    assert proj.category_name is None
    assert proj.category_id is None
