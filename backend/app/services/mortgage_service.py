import uuid
from datetime import date
from typing import Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.mortgage_payment_allocation import (
    MORTGAGE_PRINCIPAL_SOURCE,
    MortgagePaymentAllocation,
)
from app.models.transaction import Transaction
from app.services.credit_card_service import apply_effective_date


async def opening_balance_date(session: AsyncSession, loan: Account) -> Optional[date]:
    return await session.scalar(
        select(Transaction.date)
        .where(
            Transaction.account_id == loan.id,
            Transaction.workspace_id == loan.workspace_id,
            Transaction.source == "opening_balance",
        )
        .order_by(Transaction.date.desc())
        .limit(1)
    )


def posts_principal(
    loan: Account, principal: object, payment_date: date, opening_date: Optional[date]
) -> bool:
    """Whether a breakdown's principal belongs in the loan's own ledger.

    A bank-connected loan's balance comes from the bank, which also books the
    repayment itself; posting principal here would count it twice. An opening
    balance is a snapshot of the loan at the end of that date, so a payment on
    or before it is already reflected in the balance.
    """
    if not principal or loan.connection_id is not None:
        return False
    return opening_date is None or payment_date > opening_date


def principal_entry(
    loan: Account, user_id: uuid.UUID, payment: Transaction, principal
) -> Transaction:
    entry = Transaction(
        user_id=user_id,
        workspace_id=loan.workspace_id,
        account_id=loan.id,
        description=f"Principal repayment: {payment.description}",
        amount=principal,
        currency=loan.currency,
        date=payment.date,
        type="credit",
        status="posted",
        source=MORTGAGE_PRINCIPAL_SOURCE,
        exclude_from_pnl=True,
    )
    apply_effective_date(entry, loan)
    return entry


async def has_breakdown(
    session: AsyncSession, workspace_id: uuid.UUID, payment_id: uuid.UUID
) -> bool:
    linked = await session.scalar(
        select(MortgagePaymentAllocation.id)
        .where(
            MortgagePaymentAllocation.payment_transaction_id == payment_id,
            MortgagePaymentAllocation.workspace_id == workspace_id,
        )
        .limit(1)
    )
    return linked is not None


async def is_mortgage_linked(
    session: AsyncSession, workspace_id: uuid.UUID, tx: Transaction
) -> bool:
    """A payment with a breakdown already moves its principal to the loan,
    and the principal entry is that movement; neither can be a transfer too."""
    return tx.source == MORTGAGE_PRINCIPAL_SOURCE or await has_breakdown(
        session, workspace_id, tx.id
    )


async def delete_breakdown(
    session: AsyncSession, payment_id: uuid.UUID, workspace_id: uuid.UUID
) -> bool:
    """Remove a payment's breakdown and the principal entries it posted.

    Returns whether there was one. Does not commit.
    """
    existing = await session.execute(
        select(MortgagePaymentAllocation.id, MortgagePaymentAllocation.loan_transaction_id).where(
            MortgagePaymentAllocation.payment_transaction_id == payment_id,
            MortgagePaymentAllocation.workspace_id == workspace_id,
        )
    )
    rows = existing.all()
    if not rows:
        return False
    loan_transaction_ids = [entry_id for _, entry_id in rows if entry_id is not None]
    await session.execute(
        delete(MortgagePaymentAllocation).where(
            MortgagePaymentAllocation.payment_transaction_id == payment_id,
            MortgagePaymentAllocation.workspace_id == workspace_id,
        )
    )
    if loan_transaction_ids:
        await session.execute(
            delete(Transaction).where(
                Transaction.id.in_(loan_transaction_ids),
                Transaction.source == MORTGAGE_PRINCIPAL_SOURCE,
            )
        )
    await session.flush()
    return True


async def resync_principal_entries(session: AsyncSession, loan: Account) -> None:
    """Re-apply the posting rule to every breakdown on this loan.

    Called when the loan's opening balance moves: a later snapshot already
    includes repayments made before it, and an earlier one no longer does.
    Does not commit; the caller owns the transaction boundary.
    """
    opening_date = await opening_balance_date(session, loan)
    rows = await session.execute(
        select(MortgagePaymentAllocation, Transaction)
        .join(Transaction, Transaction.id == MortgagePaymentAllocation.payment_transaction_id)
        .where(MortgagePaymentAllocation.loan_account_id == loan.id)
    )
    for allocation, payment in rows.all():
        wanted = posts_principal(loan, allocation.principal_amount, payment.date, opening_date)
        if wanted and allocation.loan_transaction_id is None:
            entry = principal_entry(loan, payment.user_id, payment, allocation.principal_amount)
            session.add(entry)
            await session.flush()
            allocation.loan_transaction_id = entry.id
        elif not wanted and allocation.loan_transaction_id is not None:
            entry_id = allocation.loan_transaction_id
            # Detach first: the principal entry's foreign key cascades to the
            # breakdown row, which has to survive.
            await session.execute(
                update(MortgagePaymentAllocation)
                .where(MortgagePaymentAllocation.id == allocation.id)
                .values(loan_transaction_id=None)
            )
            allocation.loan_transaction_id = None
            entry = await session.get(Transaction, entry_id)
            if entry is not None:
                await session.delete(entry)
    await session.flush()
