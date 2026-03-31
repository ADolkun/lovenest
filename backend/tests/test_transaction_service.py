import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.rule import Rule
from app.models.transaction import Transaction
from app.schemas.transaction import TransactionCreate, TransactionUpdate
from app.services.transaction_service import (
    bulk_update_category,
    create_transaction,
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
