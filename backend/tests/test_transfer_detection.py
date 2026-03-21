"""Service-level tests for transfer_detection_service.

Tests: detect_transfer_pairs, unlink_transfer_pair.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.transaction import Transaction
from app.services.transfer_detection_service import (
    detect_transfer_pairs,
    unlink_transfer_pair,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_account(
    session: AsyncSession, user_id: uuid.UUID, name: str,
) -> Account:
    account = Account(
        id=uuid.uuid4(), user_id=user_id, name=name,
        type="checking", balance=Decimal("0.00"), currency="BRL",
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


async def _add_txn(
    session: AsyncSession, user_id: uuid.UUID, account_id: uuid.UUID,
    amount: float, txn_type: str, txn_date: date,
    source: str = "manual",
) -> Transaction:
    from datetime import datetime, timezone
    txn = Transaction(
        id=uuid.uuid4(), user_id=user_id, account_id=account_id,
        description=f"Transfer {txn_type} {amount}",
        amount=Decimal(str(amount)), date=txn_date, type=txn_type,
        source=source, currency="BRL",
        created_at=datetime.now(timezone.utc),
    )
    session.add(txn)
    await session.commit()
    await session.refresh(txn)
    return txn


# ---------------------------------------------------------------------------
# detect_transfer_pairs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_basic_pair(session: AsyncSession, test_user):
    """Detects a simple debit-credit pair across two accounts."""
    acc1 = await _make_account(session, test_user.id, "Account A")
    acc2 = await _make_account(session, test_user.id, "Account B")
    today = date.today()

    debit = await _add_txn(session, test_user.id, acc1.id, 500, "debit", today)
    credit = await _add_txn(session, test_user.id, acc2.id, 500, "credit", today)

    pairs_created = await detect_transfer_pairs(session, test_user.id)
    await session.commit()
    assert pairs_created == 1

    # Reload and verify
    await session.refresh(debit)
    await session.refresh(credit)
    assert debit.transfer_pair_id is not None
    assert debit.transfer_pair_id == credit.transfer_pair_id


@pytest.mark.asyncio
async def test_detect_with_candidate_ids(session: AsyncSession, test_user):
    """Only considers candidate debits when candidate_ids is provided."""
    acc1 = await _make_account(session, test_user.id, "Cand A")
    acc2 = await _make_account(session, test_user.id, "Cand B")
    today = date.today()

    debit1 = await _add_txn(session, test_user.id, acc1.id, 100, "debit", today)
    debit2 = await _add_txn(session, test_user.id, acc1.id, 200, "debit", today)
    await _add_txn(session, test_user.id, acc2.id, 100, "credit", today)
    await _add_txn(session, test_user.id, acc2.id, 200, "credit", today)

    # Only consider debit1 as candidate
    pairs = await detect_transfer_pairs(session, test_user.id, candidate_ids=[debit1.id])
    await session.commit()
    assert pairs == 1

    await session.refresh(debit1)
    await session.refresh(debit2)
    assert debit1.transfer_pair_id is not None
    assert debit2.transfer_pair_id is None


@pytest.mark.asyncio
async def test_detect_no_debits(session: AsyncSession, test_user):
    """Returns 0 when there are no debits."""
    acc = await _make_account(session, test_user.id, "No Debits")
    await _add_txn(session, test_user.id, acc.id, 100, "credit", date.today())

    pairs = await detect_transfer_pairs(session, test_user.id)
    assert pairs == 0


@pytest.mark.asyncio
async def test_detect_no_credits(session: AsyncSession, test_user):
    """Returns 0 when there are no credits."""
    acc = await _make_account(session, test_user.id, "No Credits")
    await _add_txn(session, test_user.id, acc.id, 100, "debit", date.today())

    pairs = await detect_transfer_pairs(session, test_user.id)
    assert pairs == 0


@pytest.mark.asyncio
async def test_detect_respects_date_tolerance(session: AsyncSession, test_user):
    """Only pairs transactions within date_tolerance_days."""
    acc1 = await _make_account(session, test_user.id, "Tol A")
    acc2 = await _make_account(session, test_user.id, "Tol B")
    today = date.today()

    await _add_txn(session, test_user.id, acc1.id, 300, "debit", today)
    # Credit too far away (5 days)
    await _add_txn(session, test_user.id, acc2.id, 300, "credit", today + timedelta(days=5))

    pairs = await detect_transfer_pairs(session, test_user.id, date_tolerance_days=2)
    assert pairs == 0


@pytest.mark.asyncio
async def test_detect_within_tolerance(session: AsyncSession, test_user):
    """Pairs transactions within tolerance."""
    acc1 = await _make_account(session, test_user.id, "In Tol A")
    acc2 = await _make_account(session, test_user.id, "In Tol B")
    today = date.today()

    debit = await _add_txn(session, test_user.id, acc1.id, 400, "debit", today)
    credit = await _add_txn(session, test_user.id, acc2.id, 400, "credit", today + timedelta(days=1))

    pairs = await detect_transfer_pairs(session, test_user.id, date_tolerance_days=2)
    await session.commit()
    assert pairs == 1

    await session.refresh(debit)
    await session.refresh(credit)
    assert debit.transfer_pair_id == credit.transfer_pair_id


@pytest.mark.asyncio
async def test_detect_ignores_same_account(session: AsyncSession, test_user):
    """Does not pair debit and credit in the same account."""
    acc = await _make_account(session, test_user.id, "Same Acc")
    today = date.today()

    await _add_txn(session, test_user.id, acc.id, 100, "debit", today)
    await _add_txn(session, test_user.id, acc.id, 100, "credit", today)

    pairs = await detect_transfer_pairs(session, test_user.id)
    assert pairs == 0


@pytest.mark.asyncio
async def test_detect_different_amounts_no_pair(session: AsyncSession, test_user):
    """Does not pair transactions with different amounts."""
    acc1 = await _make_account(session, test_user.id, "Diff A")
    acc2 = await _make_account(session, test_user.id, "Diff B")
    today = date.today()

    await _add_txn(session, test_user.id, acc1.id, 100, "debit", today)
    await _add_txn(session, test_user.id, acc2.id, 200, "credit", today)

    pairs = await detect_transfer_pairs(session, test_user.id)
    assert pairs == 0


@pytest.mark.asyncio
async def test_detect_excludes_opening_balance(session: AsyncSession, test_user):
    """Opening balance transactions are excluded from pairing."""
    acc1 = await _make_account(session, test_user.id, "OB A")
    acc2 = await _make_account(session, test_user.id, "OB B")
    today = date.today()

    await _add_txn(session, test_user.id, acc1.id, 1000, "debit", today, source="opening_balance")
    await _add_txn(session, test_user.id, acc2.id, 1000, "credit", today)

    pairs = await detect_transfer_pairs(session, test_user.id)
    assert pairs == 0


@pytest.mark.asyncio
async def test_detect_greedy_closest_date(session: AsyncSession, test_user):
    """Greedy matching picks the closest date first."""
    acc1 = await _make_account(session, test_user.id, "Greedy A")
    acc2 = await _make_account(session, test_user.id, "Greedy B")
    today = date.today()

    debit = await _add_txn(session, test_user.id, acc1.id, 500, "debit", today)
    await _add_txn(session, test_user.id, acc2.id, 500, "credit", today + timedelta(days=2))
    close_credit = await _add_txn(session, test_user.id, acc2.id, 500, "credit", today)

    pairs = await detect_transfer_pairs(session, test_user.id)
    await session.commit()
    assert pairs == 1

    await session.refresh(debit)
    await session.refresh(close_credit)
    assert debit.transfer_pair_id == close_credit.transfer_pair_id


# ---------------------------------------------------------------------------
# unlink_transfer_pair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unlink_transfer_pair(session: AsyncSession, test_user):
    """Unlinks a transfer pair, clearing transfer_pair_id on both."""
    acc1 = await _make_account(session, test_user.id, "Unlink A")
    acc2 = await _make_account(session, test_user.id, "Unlink B")
    today = date.today()

    debit = await _add_txn(session, test_user.id, acc1.id, 250, "debit", today)
    credit = await _add_txn(session, test_user.id, acc2.id, 250, "credit", today)

    await detect_transfer_pairs(session, test_user.id)
    await session.commit()
    await session.refresh(debit)
    pair_id = debit.transfer_pair_id
    assert pair_id is not None

    unlinked = await unlink_transfer_pair(session, test_user.id, pair_id)
    await session.commit()
    assert unlinked == 2

    await session.refresh(debit)
    await session.refresh(credit)
    assert debit.transfer_pair_id is None
    assert credit.transfer_pair_id is None


@pytest.mark.asyncio
async def test_unlink_nonexistent_pair(session: AsyncSession, test_user):
    """Unlinking a nonexistent pair returns 0."""
    unlinked = await unlink_transfer_pair(session, test_user.id, uuid.uuid4())
    assert unlinked == 0
