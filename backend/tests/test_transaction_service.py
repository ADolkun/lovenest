import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.rule import Rule
from app.models.transaction import Transaction
from app.schemas.transaction import TransactionCreate, TransactionUpdate, TransferCreate
from app.services.transaction_service import (
    _apply_fx_override,
    bulk_update_category,
    create_transaction,
    create_transfer,
    delete_transaction,
    get_transaction,
    get_transactions,
    update_transaction,
)


@pytest_asyncio.fixture
async def txn_account(session: AsyncSession, test_user) -> Account:
    account = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        name="TxnAcc",
        type="checking",
        balance=Decimal("10000"),
        currency="BRL",
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


# ---------------------------------------------------------------------------
# create_transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_transaction_manual(
    session: AsyncSession, test_user, test_categories, txn_account
):
    data = TransactionCreate(
        description="Lunch",
        amount=Decimal("35.00"),
        date=date(2025, 3, 10),
        type="debit",
        account_id=txn_account.id,
        category_id=test_categories[0].id,
    )
    txn = await create_transaction(session, test_user.id, data)

    assert txn.id is not None
    assert txn.description == "Lunch"
    assert txn.source == "manual"
    assert txn.category_id == test_categories[0].id


@pytest.mark.asyncio
async def test_create_transaction_applies_rules(
    session: AsyncSession, test_user, test_categories, txn_account
):
    # Create a rule
    rule = Rule(
        id=uuid.uuid4(),
        user_id=test_user.id,
        name="UBER auto",
        conditions_op="or",
        conditions=[{"field": "description", "op": "starts_with", "value": "UBER"}],
        actions=[{"op": "set_category", "value": str(test_categories[1].id)}],
        priority=10,
        is_active=True,
    )
    session.add(rule)
    await session.commit()

    # Create transaction without category — rule should apply
    data = TransactionCreate(
        description="UBER TRIP",
        amount=Decimal("25"),
        date=date(2025, 3, 10),
        type="debit",
        account_id=txn_account.id,
    )
    txn = await create_transaction(session, test_user.id, data)

    assert txn.category_id == test_categories[1].id


@pytest.mark.asyncio
async def test_create_transaction_with_category_skips_rules(
    session: AsyncSession, test_user, test_categories, txn_account
):
    rule = Rule(
        id=uuid.uuid4(),
        user_id=test_user.id,
        name="UBER skip",
        conditions_op="or",
        conditions=[{"field": "description", "op": "starts_with", "value": "UBER"}],
        actions=[{"op": "set_category", "value": str(test_categories[1].id)}],
        priority=10,
        is_active=True,
    )
    session.add(rule)
    await session.commit()

    # Explicitly provide a different category — rule should NOT override
    data = TransactionCreate(
        description="UBER TRIP",
        amount=Decimal("25"),
        date=date(2025, 3, 10),
        type="debit",
        account_id=txn_account.id,
        category_id=test_categories[0].id,
    )
    txn = await create_transaction(session, test_user.id, data)
    assert txn.category_id == test_categories[0].id


@pytest.mark.asyncio
async def test_create_transaction_invalid_account(session: AsyncSession, test_user):
    data = TransactionCreate(
        description="Orphan",
        amount=Decimal("10"),
        date=date(2025, 3, 10),
        type="debit",
        account_id=uuid.uuid4(),
    )
    with pytest.raises(ValueError, match="Account not found"):
        await create_transaction(session, test_user.id, data)


# ---------------------------------------------------------------------------
# get_transactions — pagination & filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_transactions_pagination(session: AsyncSession, test_user, txn_account):
    # Create 5 transactions
    for i in range(5):
        txn = Transaction(
            id=uuid.uuid4(),
            user_id=test_user.id,
            account_id=txn_account.id,
            description=f"Txn {i}",
            amount=Decimal("10"),
            date=date(2025, 3, i + 1),
            type="debit",
            source="manual",
            created_at=datetime.now(timezone.utc),
        )
        session.add(txn)
    await session.commit()

    page1, total = await get_transactions(session, test_user.id, limit=2, page=1)
    assert total >= 5
    assert len(page1) == 2

    page2, _ = await get_transactions(session, test_user.id, limit=2, page=2)
    assert len(page2) == 2

    # No overlap
    p1_ids = {t.id for t in page1}
    p2_ids = {t.id for t in page2}
    assert p1_ids.isdisjoint(p2_ids)


@pytest.mark.asyncio
async def test_get_transactions_filter_by_account(session: AsyncSession, test_user, txn_account):
    other_account = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        name="Other",
        type="savings",
        balance=Decimal("0"),
        currency="BRL",
    )
    session.add(other_account)
    await session.commit()

    txn1 = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=txn_account.id,
        description="In main",
        amount=Decimal("10"),
        date=date(2025, 3, 1),
        type="debit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    txn2 = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=other_account.id,
        description="In other",
        amount=Decimal("20"),
        date=date(2025, 3, 1),
        type="debit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    session.add_all([txn1, txn2])
    await session.commit()

    results, _ = await get_transactions(session, test_user.id, account_id=txn_account.id)
    descs = {t.description for t in results}
    assert "In main" in descs
    assert "In other" not in descs


@pytest.mark.asyncio
async def test_get_transactions_filter_by_category(
    session: AsyncSession, test_user, test_categories, txn_account
):
    txn1 = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=txn_account.id,
        category_id=test_categories[0].id,
        description="Cat A",
        amount=Decimal("10"),
        date=date(2025, 3, 1),
        type="debit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    txn2 = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=txn_account.id,
        category_id=test_categories[1].id,
        description="Cat B",
        amount=Decimal("20"),
        date=date(2025, 3, 1),
        type="debit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    session.add_all([txn1, txn2])
    await session.commit()

    results, _ = await get_transactions(session, test_user.id, category_id=test_categories[0].id)
    descs = {t.description for t in results}
    assert "Cat A" in descs
    assert "Cat B" not in descs


@pytest.mark.asyncio
async def test_get_transactions_filter_by_date_range(session: AsyncSession, test_user, txn_account):
    txn_jan = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=txn_account.id,
        description="Jan",
        amount=Decimal("10"),
        date=date(2025, 1, 15),
        type="debit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    txn_mar = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=txn_account.id,
        description="Mar",
        amount=Decimal("10"),
        date=date(2025, 3, 15),
        type="debit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    session.add_all([txn_jan, txn_mar])
    await session.commit()

    results, _ = await get_transactions(
        session,
        test_user.id,
        from_date=date(2025, 3, 1),
        to_date=date(2025, 3, 31),
    )
    descs = {t.description for t in results}
    assert "Mar" in descs
    assert "Jan" not in descs


@pytest.mark.asyncio
async def test_get_transactions_filter_by_search(session: AsyncSession, test_user, txn_account):
    txn = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=txn_account.id,
        description="NETFLIX SUBSCRIPTION",
        amount=Decimal("39.90"),
        date=date(2025, 3, 1),
        type="debit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    session.add(txn)
    await session.commit()

    results, _ = await get_transactions(session, test_user.id, search="netflix")
    descs = {t.description for t in results}
    assert "NETFLIX SUBSCRIPTION" in descs


@pytest.mark.asyncio
async def test_get_transactions_filter_by_type(session: AsyncSession, test_user, txn_account):
    txn_debit = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=txn_account.id,
        description="Expense",
        amount=Decimal("50"),
        date=date(2025, 3, 1),
        type="debit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    txn_credit = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=txn_account.id,
        description="Income",
        amount=Decimal("1000"),
        date=date(2025, 3, 1),
        type="credit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    session.add_all([txn_debit, txn_credit])
    await session.commit()

    results, _ = await get_transactions(session, test_user.id, txn_type="credit")
    types = {t.type for t in results}
    assert "credit" in types
    assert all(t.type == "credit" for t in results)


# ---------------------------------------------------------------------------
# get_transaction / update_transaction / delete_transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_transaction_by_id(session: AsyncSession, test_user, txn_account):
    txn = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=txn_account.id,
        description="Lookup",
        amount=Decimal("10"),
        date=date(2025, 3, 1),
        type="debit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    session.add(txn)
    await session.commit()

    fetched = await get_transaction(session, txn.id, test_user.id)
    assert fetched is not None
    assert fetched.id == txn.id


@pytest.mark.asyncio
async def test_get_transaction_not_found(session: AsyncSession, test_user):
    result = await get_transaction(session, uuid.uuid4(), test_user.id)
    assert result is None


@pytest.mark.asyncio
async def test_update_transaction(session: AsyncSession, test_user, txn_account):
    txn = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=txn_account.id,
        description="Old",
        amount=Decimal("10"),
        date=date(2025, 3, 1),
        type="debit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    session.add(txn)
    await session.commit()

    updated = await update_transaction(
        session,
        txn.id,
        test_user.id,
        TransactionUpdate(description="New", amount=Decimal("99")),
    )
    assert updated is not None
    assert updated.description == "New"
    assert updated.amount == Decimal("99")


@pytest.mark.asyncio
async def test_update_transaction_not_found(session: AsyncSession, test_user):
    result = await update_transaction(
        session,
        uuid.uuid4(),
        test_user.id,
        TransactionUpdate(description="Ghost"),
    )
    assert result is None


@pytest.mark.asyncio
async def test_delete_transaction(session: AsyncSession, test_user, txn_account):
    txn = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=txn_account.id,
        description="ToDelete",
        amount=Decimal("5"),
        date=date(2025, 3, 1),
        type="debit",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    session.add(txn)
    await session.commit()

    assert await delete_transaction(session, txn.id, test_user.id) is True
    assert await get_transaction(session, txn.id, test_user.id) is None


@pytest.mark.asyncio
async def test_delete_transaction_not_found(session: AsyncSession, test_user):
    assert await delete_transaction(session, uuid.uuid4(), test_user.id) is False


# ---------------------------------------------------------------------------
# bulk_update_category
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_update_category(session: AsyncSession, test_user, test_categories, txn_account):
    txns = []
    for i in range(3):
        txn = Transaction(
            id=uuid.uuid4(),
            user_id=test_user.id,
            account_id=txn_account.id,
            description=f"Bulk {i}",
            amount=Decimal("10"),
            date=date(2025, 3, i + 1),
            type="debit",
            source="manual",
            created_at=datetime.now(timezone.utc),
        )
        session.add(txn)
        txns.append(txn)
    await session.commit()

    ids = [t.id for t in txns]
    count = await bulk_update_category(session, test_user.id, ids, test_categories[0].id)
    assert count == 3

    for txn in txns:
        await session.refresh(txn)
        assert txn.category_id == test_categories[0].id


@pytest.mark.asyncio
async def test_bulk_update_category_clear(
    session: AsyncSession, test_user, test_categories, txn_account
):
    txn = Transaction(
        id=uuid.uuid4(),
        user_id=test_user.id,
        account_id=txn_account.id,
        description="ClearCat",
        amount=Decimal("10"),
        date=date(2025, 3, 1),
        type="debit",
        source="manual",
        category_id=test_categories[0].id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(txn)
    await session.commit()

    count = await bulk_update_category(session, test_user.id, [txn.id], category_id=None)
    assert count == 1

    await session.refresh(txn)
    assert txn.category_id is None


# ---------------------------------------------------------------------------
# _apply_fx_override
# ---------------------------------------------------------------------------


def test_apply_fx_override_both():
    txn = Transaction(
        id=uuid.uuid4(), user_id=uuid.uuid4(), account_id=uuid.uuid4(),
        description="T", amount=Decimal("100"), date=date.today(),
        type="debit", source="manual",
    )
    _apply_fx_override(txn, 100, amount_primary=500.0, fx_rate_used=5.0)
    assert txn.amount_primary == Decimal("500.0")
    assert txn.fx_rate_used == Decimal("5.0")


def test_apply_fx_override_only_amount_primary():
    txn = Transaction(
        id=uuid.uuid4(), user_id=uuid.uuid4(), account_id=uuid.uuid4(),
        description="T", amount=Decimal("100"), date=date.today(),
        type="debit", source="manual",
    )
    _apply_fx_override(txn, 100, amount_primary=250.0)
    assert txn.amount_primary == Decimal("250.0")
    assert txn.fx_rate_used == Decimal("2.5")


def test_apply_fx_override_only_fx_rate():
    txn = Transaction(
        id=uuid.uuid4(), user_id=uuid.uuid4(), account_id=uuid.uuid4(),
        description="T", amount=Decimal("100"), date=date.today(),
        type="debit", source="manual",
    )
    _apply_fx_override(txn, 100, fx_rate_used=3.0)
    assert txn.fx_rate_used == Decimal("3.0")
    assert txn.amount_primary == Decimal("300.00")


def test_apply_fx_override_zero_amount():
    txn = Transaction(
        id=uuid.uuid4(), user_id=uuid.uuid4(), account_id=uuid.uuid4(),
        description="T", amount=Decimal("0"), date=date.today(),
        type="debit", source="manual",
    )
    _apply_fx_override(txn, 0, amount_primary=0.0)
    assert txn.amount_primary == Decimal("0.0")
    assert txn.fx_rate_used == Decimal("1")


# ---------------------------------------------------------------------------
# create_transaction — FX and category
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_transaction_with_fx_override(session: AsyncSession, test_user, txn_account):
    data = TransactionCreate(
        account_id=txn_account.id, description="USD Purchase",
        amount=Decimal("100"), date=date.today(), type="debit",
        amount_primary=Decimal("500"), fx_rate_used=Decimal("5"),
    )
    txn = await create_transaction(session, test_user.id, data)
    assert txn.amount_primary == Decimal("500")
    assert txn.fx_rate_used == Decimal("5")


# ---------------------------------------------------------------------------
# create_transfer
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def txn_account_usd(session: AsyncSession, test_user) -> Account:
    acct = Account(
        id=uuid.uuid4(), user_id=test_user.id, name="USD Acct",
        type="checking", balance=Decimal("5000"), currency="USD",
    )
    session.add(acct)
    await session.commit()
    await session.refresh(acct)
    return acct


@pytest.mark.asyncio
async def test_create_transfer_same_currency(session: AsyncSession, test_user, txn_account):
    acct2 = Account(
        id=uuid.uuid4(), user_id=test_user.id, name="Savings",
        type="savings", balance=Decimal("0"), currency="BRL",
    )
    session.add(acct2)
    await session.commit()

    data = TransferCreate(
        from_account_id=txn_account.id, to_account_id=acct2.id,
        description="Transfer", amount=Decimal("1000"), date=date.today(),
    )
    debit_tx, credit_tx = await create_transfer(session, test_user.id, data)
    assert debit_tx.type == "debit"
    assert credit_tx.type == "credit"
    assert debit_tx.transfer_pair_id == credit_tx.transfer_pair_id
    assert debit_tx.amount == credit_tx.amount


@pytest.mark.asyncio
async def test_create_transfer_same_account(session: AsyncSession, test_user, txn_account):
    data = TransferCreate(
        from_account_id=txn_account.id, to_account_id=txn_account.id,
        description="Self", amount=Decimal("100"), date=date.today(),
    )
    with pytest.raises(ValueError, match="same account"):
        await create_transfer(session, test_user.id, data)


@pytest.mark.asyncio
async def test_create_transfer_cross_currency(session: AsyncSession, test_user, txn_account, txn_account_usd):
    data = TransferCreate(
        from_account_id=txn_account.id, to_account_id=txn_account_usd.id,
        description="Cross-currency", amount=Decimal("500"), date=date.today(),
        fx_rate=Decimal("0.2"),
    )
    debit_tx, credit_tx = await create_transfer(session, test_user.id, data)
    assert debit_tx.currency == "BRL"
    assert credit_tx.currency == "USD"
    assert credit_tx.amount == Decimal("100.00")


@pytest.mark.asyncio
async def test_create_transfer_cross_currency_auto_fx(session: AsyncSession, test_user, txn_account, txn_account_usd):
    data = TransferCreate(
        from_account_id=txn_account.id, to_account_id=txn_account_usd.id,
        description="Auto FX", amount=Decimal("500"), date=date.today(),
    )
    debit_tx, credit_tx = await create_transfer(session, test_user.id, data)
    assert debit_tx.type == "debit"
    assert credit_tx.type == "credit"


@pytest.mark.asyncio
async def test_create_transfer_invalid_from_account(session: AsyncSession, test_user, txn_account):
    data = TransferCreate(
        from_account_id=uuid.uuid4(), to_account_id=txn_account.id,
        description="Bad from", amount=Decimal("100"), date=date.today(),
    )
    with pytest.raises(ValueError, match="Source account not found"):
        await create_transfer(session, test_user.id, data)


@pytest.mark.asyncio
async def test_create_transfer_invalid_to_account(session: AsyncSession, test_user, txn_account):
    data = TransferCreate(
        from_account_id=txn_account.id, to_account_id=uuid.uuid4(),
        description="Bad to", amount=Decimal("100"), date=date.today(),
    )
    with pytest.raises(ValueError, match="Destination account not found"):
        await create_transfer(session, test_user.id, data)


# ---------------------------------------------------------------------------
# get_transactions — additional filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_transactions_uncategorized(session: AsyncSession, test_user, txn_account, test_categories):
    await create_transaction(session, test_user.id, TransactionCreate(
        account_id=txn_account.id, description="Uncategorized",
        amount=Decimal("50"), date=date.today(), type="debit",
    ))
    await create_transaction(session, test_user.id, TransactionCreate(
        account_id=txn_account.id, description="Categorized",
        amount=Decimal("50"), date=date.today(), type="debit",
        category_id=test_categories[0].id,
    ))
    txns, _ = await get_transactions(session, test_user.id, uncategorized=True)
    descs = [t.description for t in txns]
    assert "Uncategorized" in descs
    assert "Categorized" not in descs


@pytest.mark.asyncio
async def test_get_transactions_date_filter(session: AsyncSession, test_user, txn_account):
    from datetime import timedelta
    today = date.today()
    yesterday = today - timedelta(days=1)
    await create_transaction(session, test_user.id, TransactionCreate(
        account_id=txn_account.id, description="Today",
        amount=Decimal("10"), date=today, type="debit",
    ))
    await create_transaction(session, test_user.id, TransactionCreate(
        account_id=txn_account.id, description="Yesterday",
        amount=Decimal("10"), date=yesterday, type="debit",
    ))
    txns, _ = await get_transactions(session, test_user.id, from_date=today, to_date=today)
    descs = [t.description for t in txns]
    assert "Today" in descs
    assert "Yesterday" not in descs


@pytest.mark.asyncio
async def test_get_transactions_exclude_transfers(session: AsyncSession, test_user, txn_account):
    acct2 = Account(
        id=uuid.uuid4(), user_id=test_user.id, name="Sav",
        type="savings", balance=Decimal("0"), currency="BRL",
    )
    session.add(acct2)
    await session.commit()

    await create_transfer(session, test_user.id, TransferCreate(
        from_account_id=txn_account.id, to_account_id=acct2.id,
        description="Xfer", amount=Decimal("100"), date=date.today(),
    ))
    await create_transaction(session, test_user.id, TransactionCreate(
        account_id=txn_account.id, description="Regular",
        amount=Decimal("50"), date=date.today(), type="debit",
    ))
    txns, _ = await get_transactions(session, test_user.id, exclude_transfers=True)
    descs = [t.description for t in txns]
    assert "Regular" in descs


@pytest.mark.asyncio
async def test_get_transactions_skip_pagination(session: AsyncSession, test_user, txn_account):
    for i in range(5):
        await create_transaction(session, test_user.id, TransactionCreate(
            account_id=txn_account.id, description=f"Txn{i}",
            amount=Decimal("10"), date=date.today(), type="debit",
        ))
    txns, total = await get_transactions(session, test_user.id, skip_pagination=True, limit=2)
    assert len(txns) == total


# ---------------------------------------------------------------------------
# update_transaction — FX and cascade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_transaction_fx_override(session: AsyncSession, test_user, txn_account):
    txn = await create_transaction(session, test_user.id, TransactionCreate(
        account_id=txn_account.id, description="FX Test",
        amount=Decimal("100"), date=date.today(), type="debit",
    ))
    data = TransactionUpdate(amount_primary=Decimal("500"), fx_rate_used=Decimal("5"))
    updated = await update_transaction(session, txn.id, test_user.id, data)
    assert updated.amount_primary == Decimal("500")


@pytest.mark.asyncio
async def test_update_transaction_restamp_on_amount_change(session: AsyncSession, test_user, txn_account):
    txn = await create_transaction(session, test_user.id, TransactionCreate(
        account_id=txn_account.id, description="Restamp",
        amount=Decimal("100"), date=date.today(), type="debit",
    ))
    data = TransactionUpdate(amount=Decimal("200"))
    updated = await update_transaction(session, txn.id, test_user.id, data)
    assert updated.amount == Decimal("200")


@pytest.mark.asyncio
async def test_update_transfer_cascades(session: AsyncSession, test_user, txn_account):
    acct2 = Account(
        id=uuid.uuid4(), user_id=test_user.id, name="CascSav",
        type="savings", balance=Decimal("0"), currency="BRL",
    )
    session.add(acct2)
    await session.commit()

    debit_tx, credit_tx = await create_transfer(session, test_user.id, TransferCreate(
        from_account_id=txn_account.id, to_account_id=acct2.id,
        description="Cascade Xfer", amount=Decimal("200"), date=date.today(),
    ))
    data = TransactionUpdate(description="Updated Xfer")
    updated = await update_transaction(session, debit_tx.id, test_user.id, data)
    assert updated.description == "Updated Xfer"

    paired = await get_transaction(session, credit_tx.id, test_user.id)
    assert paired.description == "Updated Xfer"


# ---------------------------------------------------------------------------
# delete_transaction — transfer cascade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_transfer_cascades(session: AsyncSession, test_user, txn_account):
    acct2 = Account(
        id=uuid.uuid4(), user_id=test_user.id, name="DelSav",
        type="savings", balance=Decimal("0"), currency="BRL",
    )
    session.add(acct2)
    await session.commit()

    debit_tx, credit_tx = await create_transfer(session, test_user.id, TransferCreate(
        from_account_id=txn_account.id, to_account_id=acct2.id,
        description="Del Xfer", amount=Decimal("300"), date=date.today(),
    ))
    assert await delete_transaction(session, debit_tx.id, test_user.id) is True
    assert await get_transaction(session, credit_tx.id, test_user.id) is None
