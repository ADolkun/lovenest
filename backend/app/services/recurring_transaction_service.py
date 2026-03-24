import calendar
import uuid
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.recurring_transaction import RecurringTransaction
from app.models.transaction import Transaction
from app.schemas.recurring_transaction import RecurringTransactionCreate, RecurringTransactionUpdate
from app.services.fx_rate_service import stamp_primary_amount


async def get_recurring_transactions(
    session: AsyncSession, user_id: uuid.UUID
) -> list[RecurringTransaction]:
    result = await session.execute(
        select(RecurringTransaction)
        .where(RecurringTransaction.user_id == user_id)
        .order_by(RecurringTransaction.next_occurrence)
    )
    return list(result.scalars().all())


async def get_recurring_transaction(
    session: AsyncSession, recurring_id: uuid.UUID, user_id: uuid.UUID
) -> Optional[RecurringTransaction]:
    result = await session.execute(
        select(RecurringTransaction)
        .where(RecurringTransaction.id == recurring_id, RecurringTransaction.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def create_recurring_transaction(
    session: AsyncSession, user_id: uuid.UUID, data: RecurringTransactionCreate
) -> RecurringTransaction:
    next_occ = data.start_date
    if data.skip_first:
        next_occ = _advance_date(data.start_date, data.frequency)
    recurring = RecurringTransaction(
        user_id=user_id,
        account_id=data.account_id,
        category_id=data.category_id,
        description=data.description,
        amount=data.amount,
        currency=data.currency,
        type=data.type,
        frequency=data.frequency,
        day_of_month=data.day_of_month,
        start_date=data.start_date,
        end_date=data.end_date,
        next_occurrence=next_occ,
    )
    session.add(recurring)
    await session.flush()
    await stamp_primary_amount(
        session, user_id, recurring,
        date_field="start_date",
    )
    await session.commit()
    await session.refresh(recurring)
    return recurring


async def update_recurring_transaction(
    session: AsyncSession, recurring_id: uuid.UUID, user_id: uuid.UUID, data: RecurringTransactionUpdate
) -> Optional[RecurringTransaction]:
    recurring = await get_recurring_transaction(session, recurring_id, user_id)
    if not recurring:
        return None

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(recurring, key, value)

    await session.commit()
    await session.refresh(recurring)
    return recurring


async def delete_recurring_transaction(
    session: AsyncSession, recurring_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    recurring = await get_recurring_transaction(session, recurring_id, user_id)
    if not recurring:
        return False

    await session.delete(recurring)
    await session.commit()
    return True


def _advance_date(current: date, frequency: str) -> date:
    """Advance a date by the given frequency."""
    if frequency == "weekly":
        return current + timedelta(weeks=1)
    elif frequency == "monthly":
        month = current.month + 1
        year = current.year
        if month > 12:
            month = 1
            year += 1
        day = min(current.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)
    elif frequency == "yearly":
        year = current.year + 1
        day = min(current.day, calendar.monthrange(year, current.month)[1])
        return date(year, current.month, day)
    # Default: monthly
    month = current.month + 1
    year = current.year
    if month > 12:
        month = 1
        year += 1
    day = min(current.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def get_occurrences_in_range(
    start: date, frequency: str, end_date: Optional[date],
    range_start: date, range_end: date,
) -> list[date]:
    """Compute all occurrence dates for a recurring pattern within [range_start, range_end).
    Pure date math — no DB writes. Used by dashboard for virtual projections."""
    occurrences: list[date] = []
    current = start
    # Advance to range_start without collecting
    while current < range_start:
        if end_date and current > end_date:
            return occurrences
        current = _advance_date(current, frequency)
    # Collect occurrences within range
    while current < range_end:
        if end_date and current > end_date:
            break
        occurrences.append(current)
        current = _advance_date(current, frequency)
        if len(occurrences) > 200:  # safety limit
            break
    return occurrences


async def generate_pending(
    session: AsyncSession, user_id: uuid.UUID, up_to: Optional[date] = None
) -> int:
    """Generate transactions for all pending recurring transactions up to a given date.
    If up_to is None, defaults to today. This allows the dashboard to pre-generate
    transactions for future months when the user navigates ahead.
    Returns the count of transactions generated."""
    cutoff = up_to or date.today()

    result = await session.execute(
        select(RecurringTransaction)
        .where(
            RecurringTransaction.user_id == user_id,
            RecurringTransaction.is_active == True,
            RecurringTransaction.next_occurrence <= cutoff,
        )
    )
    recurring_list = list(result.scalars().all())

    count = 0
    for recurring in recurring_list:
        # Generate transactions until next_occurrence is past the cutoff
        while recurring.next_occurrence <= cutoff:
            # Check if past end_date
            if recurring.end_date and recurring.next_occurrence > recurring.end_date:
                recurring.is_active = False
                break

            transaction = Transaction(
                user_id=user_id,
                account_id=recurring.account_id,
                category_id=recurring.category_id,
                description=recurring.description,
                amount=recurring.amount,
                currency=recurring.currency,
                date=recurring.next_occurrence,
                type=recurring.type,
                source="recurring",
            )
            session.add(transaction)
            await session.flush()
            await stamp_primary_amount(session, user_id, transaction)
            count += 1

            # Advance to next occurrence
            recurring.next_occurrence = _advance_date(recurring.next_occurrence, recurring.frequency)

            # Check again if past end_date after advancing
            if recurring.end_date and recurring.next_occurrence > recurring.end_date:
                recurring.is_active = False

    await session.commit()
    return count
