import uuid
from datetime import date
from typing import Optional

from sqlalchemy import select, func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.transaction import Transaction
from app.models.transaction_attachment import TransactionAttachment
from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.schemas.transaction import TransactionCreate, TransactionUpdate
from app.services.rule_service import apply_rules_to_transaction
from app.services.fx_rate_service import stamp_primary_amount


def _apply_fx_override(transaction, amount, amount_primary=None, fx_rate_used=None):
    """Apply manual FX override values to a transaction.

    - Both provided → use as-is
    - Only amount_primary → derive rate = amount_primary / amount
    - Only fx_rate_used → derive amount_primary = amount * fx_rate_used
    """
    from decimal import Decimal, ROUND_HALF_UP

    amount = Decimal(str(amount))
    if amount_primary is not None and fx_rate_used is not None:
        transaction.amount_primary = Decimal(str(amount_primary))
        transaction.fx_rate_used = Decimal(str(fx_rate_used))
    elif amount_primary is not None:
        transaction.amount_primary = Decimal(str(amount_primary))
        if amount:
            transaction.fx_rate_used = (Decimal(str(amount_primary)) / amount)
        else:
            transaction.fx_rate_used = Decimal("1")
    elif fx_rate_used is not None:
        transaction.fx_rate_used = Decimal(str(fx_rate_used))
        transaction.amount_primary = (amount * Decimal(str(fx_rate_used))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )


async def get_transactions(
    session: AsyncSession,
    user_id: uuid.UUID,
    account_id: Optional[uuid.UUID] = None,
    category_id: Optional[uuid.UUID] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    page: int = 1,
    limit: int = 50,
    include_opening_balance: bool = False,
    search: Optional[str] = None,
    uncategorized: bool = False,
    txn_type: Optional[str] = None,
    skip_pagination: bool = False,
) -> tuple[list[Transaction], int]:
    # Base query: user's own transactions (manual or via account)
    base_query = (
        select(Transaction)
        .outerjoin(Account)
        .outerjoin(BankConnection)
        .where(
            or_(
                Transaction.user_id == user_id,
                BankConnection.user_id == user_id,
            )
        )
        .options(selectinload(Transaction.category), selectinload(Transaction.account))
    )

    # Exclude opening_balance transactions from the normal list unless explicitly requested
    if not include_opening_balance:
        base_query = base_query.where(Transaction.source != "opening_balance")

    # Apply filters
    if account_id:
        base_query = base_query.where(Transaction.account_id == account_id)
    if category_id:
        base_query = base_query.where(Transaction.category_id == category_id)
    if uncategorized:
        base_query = base_query.where(
            Transaction.category_id == None,
            Transaction.transfer_pair_id.is_(None),
        )
    if txn_type:
        base_query = base_query.where(Transaction.type == txn_type)
    if from_date:
        base_query = base_query.where(Transaction.date >= from_date)
    if to_date:
        base_query = base_query.where(Transaction.date <= to_date)
    if search:
        term = f"%{search}%"
        base_query = base_query.where(
            or_(
                Transaction.description.ilike(term),
                Transaction.payee.ilike(term),
                Transaction.notes.ilike(term),
            )
        )

    # Get total count
    count_query = select(func.count()).select_from(base_query.subquery())
    total = await session.scalar(count_query)

    # Apply ordering (and pagination unless skipped)
    query = base_query.order_by(Transaction.date.desc(), Transaction.created_at.desc())
    if not skip_pagination:
        query = query.offset((page - 1) * limit).limit(limit)

    result = await session.execute(query)
    transactions = list(result.scalars().all())

    # Batch-load attachment counts in a single query
    if transactions:
        tx_ids = [tx.id for tx in transactions]
        count_rows = await session.execute(
            select(
                TransactionAttachment.transaction_id,
                func.count(TransactionAttachment.id),
            )
            .where(TransactionAttachment.transaction_id.in_(tx_ids))
            .group_by(TransactionAttachment.transaction_id)
        )
        counts = dict(count_rows.all())
        for tx in transactions:
            tx.attachment_count = counts.get(tx.id, 0)

    return transactions, total or 0


async def get_transaction(
    session: AsyncSession, transaction_id: uuid.UUID, user_id: uuid.UUID
) -> Optional[Transaction]:
    result = await session.execute(
        select(Transaction)
        .outerjoin(Account)
        .outerjoin(BankConnection)
        .where(
            Transaction.id == transaction_id,
            or_(
                Transaction.user_id == user_id,
                BankConnection.user_id == user_id,
            ),
        )
        .options(selectinload(Transaction.category))
    )
    transaction = result.scalar_one_or_none()
    if transaction:
        count_result = await session.execute(
            select(func.count(TransactionAttachment.id)).where(
                TransactionAttachment.transaction_id == transaction.id
            )
        )
        transaction.attachment_count = count_result.scalar_one()
    return transaction


async def create_transaction(
    session: AsyncSession, user_id: uuid.UUID, data: TransactionCreate
) -> Transaction:
    # Verify account belongs to user
    account_result = await session.execute(
        select(Account)
        .outerjoin(BankConnection)
        .where(
            Account.id == data.account_id,
            or_(
                Account.user_id == user_id,
                BankConnection.user_id == user_id,
            ),
        )
    )
    account = account_result.scalar_one_or_none()
    if not account:
        raise ValueError("Account not found")

    # Resolve currency: explicit value > account currency
    currency = data.currency or account.currency

    transaction = Transaction(
        user_id=user_id,
        account_id=data.account_id,
        category_id=data.category_id,  # use provided category if given
        description=data.description,
        amount=data.amount,
        currency=currency,
        date=data.date,
        type=data.type,
        source="manual",
        notes=data.notes,
    )
    session.add(transaction)
    await session.flush()  # get ID without committing

    # Apply rules only if no explicit category provided
    if not data.category_id:
        await apply_rules_to_transaction(session, user_id, transaction)

    # Stamp primary currency amount (manual override or auto)
    if data.amount_primary is not None or data.fx_rate_used is not None:
        _apply_fx_override(transaction, data.amount, data.amount_primary, data.fx_rate_used)
    else:
        await stamp_primary_amount(session, user_id, transaction)

    await session.commit()
    await session.refresh(transaction, ["category"])
    return transaction


async def update_transaction(
    session: AsyncSession, transaction_id: uuid.UUID, user_id: uuid.UUID, data: TransactionUpdate
) -> Optional[Transaction]:
    transaction = await get_transaction(session, transaction_id, user_id)
    if not transaction:
        return None

    update_data = data.model_dump(exclude_unset=True)

    # Pop FX override fields before generic setattr loop
    override_amount_primary = update_data.pop("amount_primary", None)
    override_fx_rate = update_data.pop("fx_rate_used", None)
    has_fx_override = override_amount_primary is not None or override_fx_rate is not None

    restamp_fields = {"amount", "currency", "date"}
    needs_restamp = bool(restamp_fields & update_data.keys())

    for key, value in update_data.items():
        setattr(transaction, key, value)

    if has_fx_override:
        _apply_fx_override(
            transaction,
            transaction.amount,
            override_amount_primary,
            override_fx_rate,
        )
    elif needs_restamp:
        await stamp_primary_amount(session, user_id, transaction)

    await session.commit()
    await session.refresh(transaction)
    return transaction


async def bulk_update_category(
    session: AsyncSession,
    user_id: uuid.UUID,
    transaction_ids: list[uuid.UUID],
    category_id: Optional[uuid.UUID] = None,
) -> int:
    result = await session.execute(
        update(Transaction)
        .where(
            Transaction.id.in_(transaction_ids),
            Transaction.user_id == user_id,
        )
        .values(category_id=category_id)
    )
    await session.commit()
    return result.rowcount


async def delete_transaction(
    session: AsyncSession, transaction_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    transaction = await get_transaction(session, transaction_id, user_id)
    if not transaction:
        return False

    # Clean up attachment files from storage before ORM cascade deletes DB records
    from app.services.attachment_service import cleanup_attachment_files
    await cleanup_attachment_files(session, [transaction_id])

    await session.delete(transaction)
    await session.commit()
    return True
