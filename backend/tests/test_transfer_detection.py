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
    acc_type: str = "checking",
) -> Account:
    account = Account(
        id=uuid.uuid4(), user_id=user_id, name=name,
        type=acc_type, balance=Decimal("0.00"), currency="BRL",
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


async def _add_txn(
    session: AsyncSession, user_id: uuid.UUID, account_id: uuid.UUID,
    amount: float, txn_type: str, txn_date: date,
    source: str = "manual", description: str | None = None,
    currency: str = "BRL",
) -> Transaction:
    from datetime import datetime, timezone
    txn = Transaction(
        id=uuid.uuid4(), user_id=user_id, account_id=account_id,
        description=description or f"Transfer {txn_type} {amount} REF{int(amount * 100)}",
        amount=Decimal(str(amount)), date=txn_date, type=txn_type,
        source=source, currency=currency,
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
async def test_detect_basic_pair(session: AsyncSession, test_user, test_workspace):
    """Detects a simple debit-credit pair across two accounts."""
    acc1 = await _make_account(session, test_user.id, "Account A")
    acc2 = await _make_account(session, test_user.id, "Account B")
    today = date.today()

    debit = await _add_txn(session, test_user.id, acc1.id, 500, "debit", today)
    credit = await _add_txn(session, test_user.id, acc2.id, 500, "credit", today)

    pairs_created = await detect_transfer_pairs(session, test_workspace.id)
    await session.commit()
    assert pairs_created == 1

    # Reload and verify
    await session.refresh(debit)
    await session.refresh(credit)
    assert debit.transfer_pair_id is not None
    assert debit.transfer_pair_id == credit.transfer_pair_id


@pytest.mark.asyncio
async def test_detect_with_candidate_ids(session: AsyncSession, test_user, test_workspace):
    """Only considers candidate debits when candidate_ids is provided."""
    acc1 = await _make_account(session, test_user.id, "Cand A")
    acc2 = await _make_account(session, test_user.id, "Cand B")
    today = date.today()

    debit1 = await _add_txn(session, test_user.id, acc1.id, 100, "debit", today)
    debit2 = await _add_txn(session, test_user.id, acc1.id, 200, "debit", today)
    await _add_txn(session, test_user.id, acc2.id, 100, "credit", today)
    await _add_txn(session, test_user.id, acc2.id, 200, "credit", today)

    # Only consider debit1 as candidate
    pairs = await detect_transfer_pairs(session, test_workspace.id, candidate_ids=[debit1.id])
    await session.commit()
    assert pairs == 1

    await session.refresh(debit1)
    await session.refresh(debit2)
    assert debit1.transfer_pair_id is not None
    assert debit2.transfer_pair_id is None


@pytest.mark.asyncio
async def test_detect_no_debits(session: AsyncSession, test_user, test_workspace):
    """Returns 0 when there are no debits."""
    acc = await _make_account(session, test_user.id, "No Debits")
    await _add_txn(session, test_user.id, acc.id, 100, "credit", date.today())

    pairs = await detect_transfer_pairs(session, test_workspace.id)
    assert pairs == 0


@pytest.mark.asyncio
async def test_detect_no_credits(session: AsyncSession, test_user, test_workspace):
    """Returns 0 when there are no credits."""
    acc = await _make_account(session, test_user.id, "No Credits")
    await _add_txn(session, test_user.id, acc.id, 100, "debit", date.today())

    pairs = await detect_transfer_pairs(session, test_workspace.id)
    assert pairs == 0


@pytest.mark.asyncio
async def test_detect_respects_date_tolerance(session: AsyncSession, test_user, test_workspace):
    """Only pairs transactions within date_tolerance_days."""
    acc1 = await _make_account(session, test_user.id, "Tol A")
    acc2 = await _make_account(session, test_user.id, "Tol B")
    today = date.today()

    await _add_txn(session, test_user.id, acc1.id, 300, "debit", today)
    # Credit too far away (5 days)
    await _add_txn(session, test_user.id, acc2.id, 300, "credit", today + timedelta(days=5))

    pairs = await detect_transfer_pairs(session, test_workspace.id, date_tolerance_days=2)
    assert pairs == 0


@pytest.mark.asyncio
async def test_detect_within_tolerance(session: AsyncSession, test_user, test_workspace):
    """Pairs transactions within tolerance."""
    acc1 = await _make_account(session, test_user.id, "In Tol A")
    acc2 = await _make_account(session, test_user.id, "In Tol B")
    today = date.today()

    debit = await _add_txn(session, test_user.id, acc1.id, 400, "debit", today)
    credit = await _add_txn(session, test_user.id, acc2.id, 400, "credit", today + timedelta(days=1))

    pairs = await detect_transfer_pairs(session, test_workspace.id, date_tolerance_days=2)
    await session.commit()
    assert pairs == 1

    await session.refresh(debit)
    await session.refresh(credit)
    assert debit.transfer_pair_id == credit.transfer_pair_id


@pytest.mark.asyncio
async def test_detect_ignores_same_account(session: AsyncSession, test_user, test_workspace):
    """Does not pair debit and credit in the same account."""
    acc = await _make_account(session, test_user.id, "Same Acc")
    today = date.today()

    await _add_txn(session, test_user.id, acc.id, 100, "debit", today)
    await _add_txn(session, test_user.id, acc.id, 100, "credit", today)

    pairs = await detect_transfer_pairs(session, test_workspace.id)
    assert pairs == 0


@pytest.mark.asyncio
async def test_detect_different_amounts_no_pair(session: AsyncSession, test_user, test_workspace):
    """Does not pair transactions with different amounts."""
    acc1 = await _make_account(session, test_user.id, "Diff A")
    acc2 = await _make_account(session, test_user.id, "Diff B")
    today = date.today()

    await _add_txn(session, test_user.id, acc1.id, 100, "debit", today)
    await _add_txn(session, test_user.id, acc2.id, 200, "credit", today)

    pairs = await detect_transfer_pairs(session, test_workspace.id)
    assert pairs == 0


@pytest.mark.asyncio
async def test_detect_does_not_pair_unrelated_equal_amounts(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _make_account(session, test_user.id, "Checking")
    card = await _make_account(session, test_user.id, "Card", acc_type="credit_card")
    today = date.today()
    debit = await _add_txn(
        session, test_user.id, card.id, 15, "debit", today,
        description="LINK.COM* SIMPLEFIN BR",
    )
    credit = await _add_txn(
        session, test_user.id, checking.id, 15, "credit", today,
        description="Zelle payment from OMKAR",
    )

    assert await detect_transfer_pairs(session, test_workspace.id) == 0
    assert debit.transfer_pair_id is None
    assert credit.transfer_pair_id is None


@pytest.mark.asyncio
async def test_detect_requires_compatible_transfer_families(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _make_account(session, test_user.id, "Checking")
    card = await _make_account(session, test_user.id, "Card", acc_type="credit_card")
    today = date.today()
    debit = await _add_txn(
        session, test_user.id, checking.id, 5, "debit", today,
        description="Acorns Invest Transfer",
    )
    credit = await _add_txn(
        session, test_user.id, card.id, 5, "credit", today,
        description="AUTOMATIC PAYMENT - THANK",
    )

    assert await detect_transfer_pairs(session, test_workspace.id) == 0
    assert debit.transfer_pair_id is None
    assert credit.transfer_pair_id is None


@pytest.mark.asyncio
async def test_detect_requires_same_reference_within_transfer_family(
    session: AsyncSession, test_user, test_workspace,
):
    first = await _make_account(session, test_user.id, "First Checking")
    second = await _make_account(session, test_user.id, "Second Checking")
    today = date.today()
    debit = await _add_txn(
        session, test_user.id, first.id, 25, "debit", today,
        description="Zelle payment JPM11112222 to Alex",
    )
    credit = await _add_txn(
        session, test_user.id, second.id, 25, "credit", today,
        description="Zelle payment Conf# 99990000 from Sam",
    )

    assert await detect_transfer_pairs(session, test_workspace.id) == 0
    assert debit.transfer_pair_id is None
    assert credit.transfer_pair_id is None


@pytest.mark.asyncio
async def test_detect_does_not_treat_counterparty_name_as_reference(
    session: AsyncSession, test_user, test_workspace,
):
    first = await _make_account(session, test_user.id, "First Checking")
    second = await _make_account(session, test_user.id, "Second Checking")
    today = date.today()
    await _add_txn(
        session, test_user.id, first.id, 25, "debit", today,
        description="Zelle JPM11112222 to ALEXANDER",
    )
    await _add_txn(
        session, test_user.id, second.id, 25, "credit", today,
        description="Zelle Conf# 99990000 from ALEXANDER",
    )

    assert await detect_transfer_pairs(session, test_workspace.id) == 0


@pytest.mark.asyncio
async def test_detect_zelle_bac_and_confirmation_reference(
    session: AsyncSession, test_user, test_workspace,
):
    first = await _make_account(session, test_user.id, "Shaire")
    second = await _make_account(session, test_user.id, "Chase College")
    today = date.today()
    debit = await _add_txn(
        session, test_user.id, first.id, 50, "debit", today,
        description="Zelle payment to XIAYIRE Conf# uoza6w78x",
    )
    credit = await _add_txn(
        session, test_user.id, second.id, 50, "credit", today,
        description="Zelle payment from MAIMAITIAIZEZI XIAYIRE BACuoza6w78x",
    )

    assert await detect_transfer_pairs(session, test_workspace.id) == 1
    assert debit.transfer_pair_id == credit.transfer_pair_id


@pytest.mark.asyncio
async def test_detect_generic_deposits_need_apple_cash_account(
    session: AsyncSession, test_user, test_workspace,
):
    first = await _make_account(session, test_user.id, "First Checking")
    second = await _make_account(session, test_user.id, "Second Checking")
    today = date.today()
    await _add_txn(
        session, test_user.id, first.id, 2.64, "debit", today,
        description="Deposit",
    )
    await _add_txn(
        session, test_user.id, second.id, 2.64, "credit", today,
        description="Deposit",
    )

    assert await detect_transfer_pairs(session, test_workspace.id) == 0


@pytest.mark.asyncio
async def test_detect_apple_cash_deposit_pair(
    session: AsyncSession, test_user, test_workspace,
):
    card = await _make_account(session, test_user.id, "Card")
    apple_cash = await _make_account(session, test_user.id, "Apple Cash")
    today = date.today()
    debit = await _add_txn(
        session, test_user.id, card.id, 2.64, "debit", today,
        description="Deposit",
    )
    credit = await _add_txn(
        session, test_user.id, apple_cash.id, 2.64, "credit", today,
        description="Deposit",
    )

    assert await detect_transfer_pairs(session, test_workspace.id) == 1
    assert debit.transfer_pair_id == credit.transfer_pair_id


@pytest.mark.asyncio
async def test_detect_card_payment_with_matching_evidence(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _make_account(session, test_user.id, "Checking")
    card = await _make_account(session, test_user.id, "Card", acc_type="credit_card")
    today = date.today()
    debit = await _add_txn(
        session, test_user.id, checking.id, 100, "debit", today,
        description="CHASE CREDIT CRD AUTOPAY PPD ID: 123",
    )
    credit = await _add_txn(
        session, test_user.id, card.id, 100, "credit", today,
        description="AUTOMATIC PAYMENT - THANK",
    )

    assert await detect_transfer_pairs(session, test_workspace.id) == 1
    assert debit.transfer_pair_id == credit.transfer_pair_id


@pytest.mark.asyncio
async def test_detect_card_payment_with_asymmetric_capital_one_description(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _make_account(session, test_user.id, "Checking")
    card = await _make_account(session, test_user.id, "Card", acc_type="credit_card")
    today = date.today()
    debit = await _add_txn(
        session, test_user.id, checking.id, 1500, "debit", today,
        description="CAPITAL ONE",
    )
    credit = await _add_txn(
        session, test_user.id, card.id, 1500, "credit", today,
        description="CAPITAL ONE MOBILE PYMT",
    )

    assert await detect_transfer_pairs(session, test_workspace.id) == 1
    assert debit.transfer_pair_id == credit.transfer_pair_id


@pytest.mark.asyncio
async def test_detect_card_payments_do_not_cross_issuers(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _make_account(session, test_user.id, "Checking")
    chase_card = await _make_account(
        session, test_user.id, "Chase Card", acc_type="credit_card"
    )
    capital_one_card = await _make_account(
        session, test_user.id, "Venture X", acc_type="credit_card"
    )
    today = date.today()
    chase_debit = await _add_txn(
        session, test_user.id, checking.id, 200, "debit", today,
        description="CHASE CREDIT CRD AUTOPAY PPD ID: 123",
    )
    capital_one_debit = await _add_txn(
        session, test_user.id, checking.id, 200, "debit", today,
        description="CAPITAL ONE CRCARDPMT",
    )
    capital_one_credit = await _add_txn(
        session, test_user.id, capital_one_card.id, 200, "credit", today,
        description="AUTOMATIC PAYMENT - THANK",
    )
    chase_credit = await _add_txn(
        session, test_user.id, chase_card.id, 200, "credit", today,
        description="AUTOMATIC PAYMENT - THANK",
    )

    assert await detect_transfer_pairs(session, test_workspace.id) == 2
    assert chase_debit.transfer_pair_id == chase_credit.transfer_pair_id
    assert capital_one_debit.transfer_pair_id == capital_one_credit.transfer_pair_id
    assert chase_debit.transfer_pair_id != capital_one_debit.transfer_pair_id


@pytest.mark.asyncio
async def test_detect_wells_fargo_auto_pay(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _make_account(session, test_user.id, "Checking")
    card = await _make_account(
        session, test_user.id, "Wells Fargo Card", acc_type="credit_card"
    )
    today = date.today()
    debit = await _add_txn(
        session, test_user.id, checking.id, 90, "debit", today,
        description="WF CREDIT CARD AUTO PAY 1234",
    )
    credit = await _add_txn(
        session, test_user.id, card.id, 90, "credit", today,
        description="AUTOMATIC PAYMENT - THANK",
    )

    assert await detect_transfer_pairs(session, test_workspace.id) == 1
    assert debit.transfer_pair_id == credit.transfer_pair_id


@pytest.mark.asyncio
async def test_empty_candidate_ids_do_not_scan_history(
    session: AsyncSession, test_user, test_workspace,
):
    first = await _make_account(session, test_user.id, "First Checking")
    second = await _make_account(session, test_user.id, "Second Checking")
    today = date.today()
    debit = await _add_txn(session, test_user.id, first.id, 100, "debit", today)
    credit = await _add_txn(session, test_user.id, second.id, 100, "credit", today)

    assert await detect_transfer_pairs(
        session, test_workspace.id, candidate_ids=[]
    ) == 0
    assert debit.transfer_pair_id is None
    assert credit.transfer_pair_id is None


@pytest.mark.asyncio
async def test_detect_does_not_pair_different_currencies(
    session: AsyncSession, test_user, test_workspace,
):
    acc1 = await _make_account(session, test_user.id, "USD")
    acc2 = await _make_account(session, test_user.id, "EUR")
    today = date.today()
    debit = await _add_txn(
        session, test_user.id, acc1.id, 100, "debit", today, currency="USD"
    )
    credit = await _add_txn(
        session, test_user.id, acc2.id, 100, "credit", today, currency="EUR"
    )

    assert await detect_transfer_pairs(session, test_workspace.id) == 0
    assert debit.transfer_pair_id is None
    assert credit.transfer_pair_id is None


@pytest.mark.asyncio
async def test_detect_excludes_opening_balance(session: AsyncSession, test_user, test_workspace):
    """Opening balance transactions are excluded from pairing."""
    acc1 = await _make_account(session, test_user.id, "OB A")
    acc2 = await _make_account(session, test_user.id, "OB B")
    today = date.today()

    await _add_txn(session, test_user.id, acc1.id, 1000, "debit", today, source="opening_balance")
    await _add_txn(session, test_user.id, acc2.id, 1000, "credit", today)

    pairs = await detect_transfer_pairs(session, test_workspace.id)
    assert pairs == 0


@pytest.mark.asyncio
async def test_detect_greedy_closest_date(session: AsyncSession, test_user, test_workspace):
    """Greedy matching picks the closest date first."""
    acc1 = await _make_account(session, test_user.id, "Greedy A")
    acc2 = await _make_account(session, test_user.id, "Greedy B")
    today = date.today()

    debit = await _add_txn(session, test_user.id, acc1.id, 500, "debit", today)
    await _add_txn(session, test_user.id, acc2.id, 500, "credit", today + timedelta(days=2))
    close_credit = await _add_txn(session, test_user.id, acc2.id, 500, "credit", today)

    pairs = await detect_transfer_pairs(session, test_workspace.id)
    await session.commit()
    assert pairs == 1

    await session.refresh(debit)
    await session.refresh(close_credit)
    assert debit.transfer_pair_id == close_credit.transfer_pair_id


# ---------------------------------------------------------------------------
# unlink_transfer_pair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unlink_transfer_pair(session: AsyncSession, test_user, test_workspace):
    """Unlinks a transfer pair, clearing transfer_pair_id on both."""
    acc1 = await _make_account(session, test_user.id, "Unlink A")
    acc2 = await _make_account(session, test_user.id, "Unlink B")
    today = date.today()

    debit = await _add_txn(session, test_user.id, acc1.id, 250, "debit", today)
    credit = await _add_txn(session, test_user.id, acc2.id, 250, "credit", today)

    await detect_transfer_pairs(session, test_workspace.id)
    await session.commit()
    await session.refresh(debit)
    pair_id = debit.transfer_pair_id
    assert pair_id is not None

    unlinked = await unlink_transfer_pair(session, test_workspace.id,pair_id)
    await session.commit()
    assert unlinked == 2

    await session.refresh(debit)
    await session.refresh(credit)
    assert debit.transfer_pair_id is None
    assert credit.transfer_pair_id is None


@pytest.mark.asyncio
async def test_unlink_nonexistent_pair(session: AsyncSession, test_user, test_workspace):
    """Unlinking a nonexistent pair returns 0."""
    unlinked = await unlink_transfer_pair(session, test_workspace.id,uuid.uuid4())
    assert unlinked == 0


# ---------------------------------------------------------------------------
# Reverse-direction candidate_ids detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidate_credit_matches_existing_debit(session: AsyncSession, test_user, test_workspace):
    """When candidate_ids contains only a credit, it pairs with an existing debit.

    Simulates: Account A synced first (debit already exists), then Account B
    syncs and imports the matching credit.
    """
    acc1 = await _make_account(session, test_user.id, "Rev A")
    acc2 = await _make_account(session, test_user.id, "Rev B")
    today = date.today()

    # Existing debit (from a previous sync)
    debit = await _add_txn(session, test_user.id, acc1.id, 750, "debit", today)

    # New credit just imported
    credit = await _add_txn(session, test_user.id, acc2.id, 750, "credit", today)

    # Only the credit is in candidate_ids (second sync)
    pairs = await detect_transfer_pairs(session, test_workspace.id, candidate_ids=[credit.id])
    await session.commit()
    assert pairs == 1

    await session.refresh(debit)
    await session.refresh(credit)
    assert debit.transfer_pair_id is not None
    assert debit.transfer_pair_id == credit.transfer_pair_id


@pytest.mark.asyncio
async def test_candidate_debit_matches_existing_credit(session: AsyncSession, test_user, test_workspace):
    """When candidate_ids contains only a debit, it pairs with an existing credit.

    Simulates: Account B synced first (credit already exists), then Account A
    syncs and imports the matching debit.
    """
    acc1 = await _make_account(session, test_user.id, "Fwd A")
    acc2 = await _make_account(session, test_user.id, "Fwd B")
    today = date.today()

    # Existing credit (from a previous sync)
    credit = await _add_txn(session, test_user.id, acc2.id, 600, "credit", today)

    # New debit just imported
    debit = await _add_txn(session, test_user.id, acc1.id, 600, "debit", today)

    # Only the debit is in candidate_ids (second sync)
    pairs = await detect_transfer_pairs(session, test_workspace.id, candidate_ids=[debit.id])
    await session.commit()
    assert pairs == 1

    await session.refresh(debit)
    await session.refresh(credit)
    assert debit.transfer_pair_id is not None
    assert debit.transfer_pair_id == credit.transfer_pair_id


@pytest.mark.asyncio
async def test_reverse_detection_does_not_pair_two_old_transactions(session: AsyncSession, test_user, test_workspace):
    """Reverse detection must not pair two transactions that are both outside candidate_ids."""
    acc1 = await _make_account(session, test_user.id, "Guard A")
    acc2 = await _make_account(session, test_user.id, "Guard B")
    acc3 = await _make_account(session, test_user.id, "Guard C")
    today = date.today()

    # Two old unpaired transactions that could match each other
    old_debit = await _add_txn(session, test_user.id, acc1.id, 300, "debit", today)
    old_credit = await _add_txn(session, test_user.id, acc2.id, 300, "credit", today)

    # A new unrelated credit (different amount) triggers detection
    new_credit = await _add_txn(session, test_user.id, acc3.id, 999, "credit", today)

    pairs = await detect_transfer_pairs(session, test_workspace.id, candidate_ids=[new_credit.id])
    await session.commit()
    assert pairs == 0

    # Old transactions must remain unpaired
    await session.refresh(old_debit)
    await session.refresh(old_credit)
    assert old_debit.transfer_pair_id is None
    assert old_credit.transfer_pair_id is None


@pytest.mark.asyncio
async def test_both_sides_imported_together(session: AsyncSession, test_user, test_workspace):
    """When both debit and credit are new (same sync), both are in candidate_ids."""
    acc1 = await _make_account(session, test_user.id, "Both A")
    acc2 = await _make_account(session, test_user.id, "Both B")
    today = date.today()

    debit = await _add_txn(session, test_user.id, acc1.id, 200, "debit", today)
    credit = await _add_txn(session, test_user.id, acc2.id, 200, "credit", today)

    pairs = await detect_transfer_pairs(
        session, test_workspace.id, candidate_ids=[debit.id, credit.id],
    )
    await session.commit()
    assert pairs == 1

    await session.refresh(debit)
    await session.refresh(credit)
    assert debit.transfer_pair_id == credit.transfer_pair_id
