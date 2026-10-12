"""Reimbursement links: a credit booked against the expenses it repays.

ADR 0012. A link moves P&L between months and categories without changing
any total; reporting reads it through `reimbursement_unlinked_share` in
`_query_filters.py`. Everything here keeps one invariant that reporting
relies on: for every row, the links touching it add up to no more than its
amount, so the share it keeps is never negative and net cash flow is
conserved.
"""
import uuid
from collections import defaultdict
from datetime import timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Optional

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.app_clock import app_today
from app.models.category import Category
from app.models.payee import Payee
from app.models.transaction import Transaction
from app.models.transaction_attachment import TransactionAttachment
from app.models.transaction_reimbursement import TransactionReimbursement
from app.models.transaction_split import TransactionSplit
from app.services._query_filters import has_valid_transfer_pair
from app.services.transaction_semantics import credit_card_payment_filter

CENT = Decimal("0.01")
CANDIDATE_DAYS_BEFORE = 180
CANDIDATE_DAYS_AFTER = 14

# Fields whose change can leave a row's links pointing at something the
# eligibility rules would refuse. Everything else (category, description,
# flags) is safe to edit while linked.
_LINK_SENSITIVE_FIELDS = frozenset({"amount", "type", "currency", "status", "date"})


class ReimbursementError(ValueError):
    """A link the rules refuse. The message is shown to the user as is."""


def _money(amount: Decimal, currency: str) -> str:
    return f"{amount:,.2f} {currency}"


def _has_group_splits():
    return exists(select(1).where(TransactionSplit.transaction_id == Transaction.id))


def _eligible_row():
    """SQL counterpart of `_ineligibility`, for searching candidates."""
    return and_(
        Transaction.status == "posted",
        Transaction.date <= app_today(),
        Transaction.source.not_in(("settlement", "opening_balance")),
        ~has_valid_transfer_pair(),
        ~credit_card_payment_filter(),
        ~_has_group_splits(),
    )


async def _eligibility_flags(
    session: AsyncSession, transaction_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Any]:
    result = await session.execute(
        select(
            Transaction.id,
            has_valid_transfer_pair().label("paired"),
            credit_card_payment_filter().label("card_payment"),
            _has_group_splits().label("split"),
        ).where(Transaction.id.in_(transaction_ids))
    )
    return {row.id: row for row in result.all()}


def _ineligibility(tx: Transaction, flags: Any) -> Optional[str]:
    """Why this row can't take part in a link, phrased to follow "it"."""
    if tx.status != "posted":
        return "is still pending. Link it once it posts."
    if tx.date > app_today():
        return "is dated in the future."
    if tx.source == "settlement":
        return "is a group settlement."
    if tx.source == "opening_balance":
        return "is an opening balance."
    if flags.paired:
        return "is part of a transfer."
    if flags.card_payment:
        return "is a credit card payment."
    if flags.split:
        return (
            "is split with a group. Use a group settlement to record what "
            "others paid back."
        )
    return None


async def linked_totals(
    session: AsyncSession, transaction_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """Sum of link amounts per row, whichever side of the link it is on.
    Rows without links are absent."""
    if not transaction_ids:
        return {}
    wanted = set(transaction_ids)
    result = await session.execute(
        select(
            TransactionReimbursement.reimbursement_id,
            TransactionReimbursement.expense_id,
            TransactionReimbursement.amount,
        ).where(
            or_(
                TransactionReimbursement.reimbursement_id.in_(transaction_ids),
                TransactionReimbursement.expense_id.in_(transaction_ids),
            )
        )
    )
    totals: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    for reimbursement_id, expense_id, amount in result.all():
        for side in (reimbursement_id, expense_id):
            if side in wanted:
                totals[side] += amount
    return dict(totals)


async def ensure_unlinked(
    session: AsyncSession, transaction_ids: list[uuid.UUID], message: str
) -> None:
    if await linked_totals(session, transaction_ids):
        raise ReimbursementError(message)


async def links_for_transactions(
    session: AsyncSession, workspace_id: uuid.UUID, transaction_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Each row's links, read from that row's side. One query per side for a
    whole page, so the transaction list never turns into an N+1."""
    if not transaction_ids:
        return {}
    links: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    sides = (
        (TransactionReimbursement.reimbursement_id, TransactionReimbursement.expense_id),
        (TransactionReimbursement.expense_id, TransactionReimbursement.reimbursement_id),
    )
    for own_side, counterpart_side in sides:
        result = await session.execute(
            select(
                own_side,
                TransactionReimbursement.id,
                TransactionReimbursement.amount,
                Transaction.id,
                Transaction.description,
                Transaction.date,
                Transaction.currency,
                Category.name,
            )
            .select_from(TransactionReimbursement)
            .join(Transaction, Transaction.id == counterpart_side)
            .outerjoin(Category, Category.id == Transaction.category_id)
            .where(
                TransactionReimbursement.workspace_id == workspace_id,
                own_side.in_(transaction_ids),
            )
            .order_by(Transaction.date, Transaction.created_at)
        )
        for (
            own_id, link_id, amount, counterpart_id, description, on, currency, category_name,
        ) in result.all():
            links[own_id].append(
                {
                    "id": link_id,
                    "counterpart_id": counterpart_id,
                    "counterpart_description": description,
                    "counterpart_date": on,
                    "counterpart_category_name": category_name,
                    "amount": float(amount),
                    "currency": currency,
                }
            )
    return dict(links)


async def get_reimbursements(
    session: AsyncSession, workspace_id: uuid.UUID, transaction_id: uuid.UUID
) -> list[dict[str, Any]]:
    found = await session.scalar(
        select(Transaction.id).where(
            Transaction.id == transaction_id, Transaction.workspace_id == workspace_id
        )
    )
    if found is None:
        raise LookupError("Transaction not found")
    links = await links_for_transactions(session, workspace_id, [transaction_id])
    return links.get(transaction_id, [])


async def replace_reimbursements(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    credit_id: uuid.UUID,
    allocations: list[tuple[uuid.UUID, Decimal]],
) -> list[dict[str, Any]]:
    """Make `allocations` the whole set of expenses this credit repays."""
    requested: dict[uuid.UUID, Decimal] = {}
    for expense_id, raw_amount in allocations:
        if expense_id in requested:
            raise ReimbursementError("Each expense can appear only once in a reimbursement.")
        amount = Decimal(str(raw_amount)).quantize(CENT, rounding=ROUND_HALF_UP)
        if amount <= 0:
            raise ReimbursementError("Each reimbursed amount must be greater than zero.")
        requested[expense_id] = amount
    if credit_id in requested:
        raise ReimbursementError("A transaction can't reimburse itself.")

    # Locked in id order so two edits touching the same expense queue up
    # instead of both reading its remaining amount before either writes.
    locked_result = await session.execute(
        select(Transaction)
        .where(
            Transaction.id.in_([credit_id, *requested]),
            Transaction.workspace_id == workspace_id,
        )
        .order_by(Transaction.id)
        .with_for_update(of=Transaction)
    )
    locked = {tx.id: tx for tx in locked_result.scalars().all()}
    credit = locked.get(credit_id)
    if credit is None:
        raise LookupError("Transaction not found")
    if credit.type != "credit":
        raise ReimbursementError(
            "Only a credit can reimburse expenses. This transaction is a debit."
        )
    if any(expense_id not in locked for expense_id in requested):
        raise ReimbursementError("One of the expenses no longer exists in this workspace.")

    if requested:
        flags = await _eligibility_flags(session, list(locked))
        reason = _ineligibility(credit, flags[credit.id])
        if reason:
            raise ReimbursementError(
                f"This credit can't be linked as a reimbursement: it {reason}"
            )
        for expense_id in requested:
            expense = locked[expense_id]
            if expense.type != "debit":
                raise ReimbursementError(
                    f'"{expense.description}" is not an expense, so a credit can\'t reimburse it.'
                )
            if expense.currency != credit.currency:
                raise ReimbursementError(
                    f'"{expense.description}" is in {expense.currency} and this credit is '
                    f"in {credit.currency}. A reimbursement can only link transactions "
                    "in the same currency."
                )
            reason = _ineligibility(expense, flags[expense_id])
            if reason:
                raise ReimbursementError(
                    f'"{expense.description}" can\'t be reimbursed: it {reason}'
                )

        credit_amount = abs(Decimal(credit.amount))
        total = sum(requested.values(), Decimal("0"))
        if total > credit_amount:
            raise ReimbursementError(
                f"These amounts add up to {_money(total, credit.currency)}, more than "
                f"this credit's {_money(credit_amount, credit.currency)}."
            )

        claimed_elsewhere: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
        others = await session.execute(
            select(TransactionReimbursement.expense_id, TransactionReimbursement.amount).where(
                TransactionReimbursement.expense_id.in_(list(requested)),
                TransactionReimbursement.reimbursement_id != credit.id,
            )
        )
        for expense_id, amount in others.all():
            claimed_elsewhere[expense_id] += amount
        for expense_id, amount in requested.items():
            expense = locked[expense_id]
            available = abs(Decimal(expense.amount)) - claimed_elsewhere[expense_id]
            if amount > available:
                if available <= 0:
                    raise ReimbursementError(
                        f'"{expense.description}" is already fully reimbursed by other credits.'
                    )
                raise ReimbursementError(
                    f'"{expense.description}" has {_money(available, expense.currency)} left '
                    f"to reimburse, less than the {_money(amount, expense.currency)} asked for."
                )

    existing_result = await session.execute(
        select(TransactionReimbursement).where(
            TransactionReimbursement.reimbursement_id == credit.id
        )
    )
    existing = {link.expense_id: link for link in existing_result.scalars().all()}
    for expense_id, link in existing.items():
        if expense_id not in requested:
            await session.delete(link)
    for expense_id, amount in requested.items():
        link = existing.get(expense_id)
        if link is None:
            session.add(
                TransactionReimbursement(
                    workspace_id=workspace_id,
                    reimbursement_id=credit.id,
                    expense_id=expense_id,
                    amount=amount,
                )
            )
        else:
            link.amount = amount
    await session.commit()
    links = await links_for_transactions(session, workspace_id, [credit.id])
    return links.get(credit.id, [])


async def delete_reimbursement(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
    link_id: uuid.UUID,
) -> None:
    link = await session.scalar(
        select(TransactionReimbursement).where(
            TransactionReimbursement.id == link_id,
            TransactionReimbursement.workspace_id == workspace_id,
            or_(
                TransactionReimbursement.reimbursement_id == transaction_id,
                TransactionReimbursement.expense_id == transaction_id,
            ),
        )
    )
    if link is None:
        raise LookupError("Reimbursement link not found")
    await session.delete(link)
    await session.commit()


async def reimbursement_candidates(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    credit_id: uuid.UUID,
    q: Optional[str] = None,
    limit: int = 20,
) -> list[tuple[Transaction, Decimal, Decimal]]:
    """Expenses this credit could repay, best guesses first, each with what is
    still unclaimed by other credits and what this credit already links."""
    credit = await session.scalar(
        select(Transaction).where(
            Transaction.id == credit_id, Transaction.workspace_id == workspace_id
        )
    )
    if credit is None:
        raise LookupError("Transaction not found")
    if credit.type != "credit":
        raise ReimbursementError(
            "Only a credit can reimburse expenses. This transaction is a debit."
        )

    def linked(*conditions):
        return (
            select(func.coalesce(func.sum(TransactionReimbursement.amount), 0))
            .where(TransactionReimbursement.expense_id == Transaction.id, *conditions)
            .correlate(Transaction)
            .scalar_subquery()
        )

    claimed_elsewhere = linked(TransactionReimbursement.reimbursement_id != credit.id)
    allocated_here = linked(TransactionReimbursement.reimbursement_id == credit.id)

    stmt = (
        select(Transaction, claimed_elsewhere, allocated_here)
        .outerjoin(Payee, Transaction.payee_id == Payee.id)
        .where(
            Transaction.workspace_id == workspace_id,
            Transaction.type == "debit",
            Transaction.currency == credit.currency,
            _eligible_row(),
            func.abs(Transaction.amount) > claimed_elsewhere,
        )
        .options(
            selectinload(Transaction.category),
            selectinload(Transaction.account),
            selectinload(Transaction.payee_entity),
            selectinload(Transaction.splits),
        )
    )
    if q and q.strip():
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Transaction.description.ilike(term),
                Transaction.payee.ilike(term),
                Payee.name.ilike(term),
            )
        )
    else:
        stmt = stmt.where(
            Transaction.date >= credit.date - timedelta(days=CANDIDATE_DAYS_BEFORE),
            Transaction.date <= credit.date + timedelta(days=CANDIDATE_DAYS_AFTER),
        )

    # Expenses repaid later are the common case, so the ones on or before
    # the credit's date come first, nearest first; the few days after follow.
    on_or_before = Transaction.date <= credit.date
    same_category = (
        case((Transaction.category_id == credit.category_id, 0), else_=1)
        if credit.category_id is not None
        else None
    )
    ordering = [
        *([same_category] if same_category is not None else []),
        case((on_or_before, 0), else_=1),
        case((on_or_before, Transaction.date)).desc(),
        case((~on_or_before, Transaction.date)).asc(),
        Transaction.created_at.desc(),
        Transaction.id,
    ]
    result = await session.execute(stmt.order_by(*ordering).limit(limit))
    rows = [
        (tx, abs(Decimal(tx.amount)) - Decimal(str(elsewhere)), Decimal(str(here)))
        for tx, elsewhere, here in result.all()
    ]

    if rows:
        count_rows = await session.execute(
            select(TransactionAttachment.transaction_id, func.count(TransactionAttachment.id))
            .where(TransactionAttachment.transaction_id.in_([tx.id for tx, _, _ in rows]))
            .group_by(TransactionAttachment.transaction_id)
        )
        counts = {transaction_id: count for transaction_id, count in count_rows.all()}
        for tx, _, _ in rows:
            tx.attachment_count = counts.get(tx.id, 0)
            tx.payee_name = tx.payee_entity.name if tx.payee_entity else None
    return rows


async def ensure_update_keeps_links(
    session: AsyncSession, tx: Transaction, update_data: dict
) -> None:
    """Refuse an edit that would leave this row's links invalid."""
    if not _LINK_SENSITIVE_FIELDS & update_data.keys():
        return
    # Locks out a concurrent link edit between reading the total and writing
    # the new amount.
    await session.execute(
        select(Transaction.id).where(Transaction.id == tx.id).with_for_update()
    )
    linked = (await linked_totals(session, [tx.id])).get(tx.id)
    if not linked:
        return
    first = "Remove its reimbursement links first."
    if "type" in update_data and update_data["type"] != tx.type:
        raise ReimbursementError(
            f"This transaction is linked as a reimbursement, so its type can't change. {first}"
        )
    if "currency" in update_data and update_data["currency"] != tx.currency:
        raise ReimbursementError(
            "This transaction is linked as a reimbursement, so its currency can't "
            f"change. {first}"
        )
    if update_data.get("status", "posted") != "posted":
        raise ReimbursementError(
            "This transaction is linked as a reimbursement, so it can't go back to "
            f"pending. {first}"
        )
    if "date" in update_data and update_data["date"] > app_today():
        raise ReimbursementError(
            "This transaction is linked as a reimbursement, so it can't move to a "
            f"future date. {first}"
        )
    if "amount" in update_data and abs(Decimal(str(update_data["amount"]))) < linked:
        raise ReimbursementError(
            f"Reimbursement links on this transaction add up to {_money(linked, tx.currency)}, "
            "so its amount can't go below that. Reduce or remove the links first."
        )


async def fit_links_to_amount(session: AsyncSession, transaction: Transaction) -> None:
    """Shrink a row's links to fit an amount a provider changed under them.

    Only ever shrinks, proportionally, and only when the links no longer fit:
    growing a link could push the row on its other side past its own amount,
    and the money that changed hands is what the user linked, not what the
    bank re-reported. Rounded down so no link grows and the total never
    exceeds the new amount; a link that rounds to nothing is dropped.
    """
    result = await session.execute(
        select(TransactionReimbursement).where(
            or_(
                TransactionReimbursement.reimbursement_id == transaction.id,
                TransactionReimbursement.expense_id == transaction.id,
            )
        )
    )
    links = list(result.scalars().all())
    capacity = abs(Decimal(transaction.amount)).quantize(CENT)
    total = sum((link.amount for link in links), Decimal("0"))
    if total <= capacity:
        return
    for link in links:
        fitted = (link.amount * capacity / total).quantize(CENT, rounding=ROUND_DOWN)
        if fitted > 0:
            link.amount = fitted
        else:
            await session.delete(link)
