import re
import uuid
from collections.abc import Collection
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.services import reimbursement_service


def _normalize_description(value: str | None) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (value or "").casefold()).split())


def descriptions_match(a: str | None, b: str | None) -> bool:
    """Match only normalized merchant text; never infer from shared fragments."""
    normalized = [_normalize_description(value) for value in (a, b)]
    return bool(normalized[0]) and normalized[0] == normalized[1]


async def find_unique_transaction_match(
    session: AsyncSession,
    account_id: uuid.UUID,
    txn_data,
    sources: Collection[str],
    *,
    date_tolerance_days: int = 3,
    unclaimed_only: bool = False,
    exclude_ids: Collection[uuid.UUID] = (),
    exclude_external_ids: Collection[str] = (),
) -> Optional[Transaction]:
    """Return one unambiguous imported/provider counterpart, never a guess."""
    date_lo = txn_data.date - timedelta(days=date_tolerance_days)
    date_hi = txn_data.date + timedelta(days=date_tolerance_days)
    statement = select(Transaction).where(
        Transaction.account_id == account_id,
        Transaction.source.in_(sources),
        Transaction.amount == txn_data.amount,
        Transaction.type == txn_data.type,
        Transaction.date >= date_lo,
        Transaction.date <= date_hi,
    )
    if unclaimed_only:
        statement = statement.where(Transaction.raw_data.is_(None))
    if exclude_ids:
        statement = statement.where(Transaction.id.not_in(exclude_ids))
    if exclude_external_ids:
        statement = statement.where(
            or_(
                Transaction.external_id.is_(None),
                Transaction.external_id.not_in(exclude_external_ids),
            )
        )
    result = await session.execute(statement)

    incoming_descriptions = (
        txn_data.description,
        getattr(txn_data, "payee", None),
        getattr(txn_data, "payee_raw", None),
    )
    matches = [
        candidate
        for candidate in result.scalars().all()
        if any(
            descriptions_match(candidate_description, incoming_description)
            for candidate_description in (
                candidate.description,
                candidate.original_description,
                candidate.payee,
            )
            for incoming_description in incoming_descriptions
        )
    ]
    if len(matches) <= 1:
        return matches[0] if matches else None

    incoming_exact = {
        normalized
        for value in incoming_descriptions
        if (normalized := _normalize_description(value))
    }
    exact_matches = [
        candidate
        for candidate in matches
        if incoming_exact
        & {
            normalized
            for value in (
                candidate.description,
                candidate.original_description,
                candidate.payee,
            )
            if (normalized := _normalize_description(value))
        }
    ]
    if exact_matches:
        # Indistinguishable exact candidates form a multiset. Consume one
        # deterministically; the caller's exclusions prevent reusing it.
        return min(exact_matches, key=lambda candidate: str(candidate.id))
    return None


async def take_incoming_amount(
    session: AsyncSession, transaction: Transaction, amount: Decimal
) -> None:
    """Give a matched row the amount its bank or file reported.

    What was sized to the old amount follows it: group-split shares are
    rescaled to the new total, reimbursement links shrink to fit (ADR 0012),
    and a transfer pair is unlinked, since its other leg still carries the
    old amount. A pair whose amounts the user typed on both legs stays
    linked: neither leg was derived from the other, so the other leg's
    amount is still what landed there.
    """
    if Decimal(transaction.amount) == amount:
        return
    transaction.amount = amount

    splits = (await session.execute(
        select(TransactionSplit)
        .where(TransactionSplit.transaction_id == transaction.id)
        .order_by(TransactionSplit.created_at, TransactionSplit.id)
    )).scalars().all()
    if splits:
        new_total = abs(Decimal(amount)).quantize(Decimal("0.01"))
        old_total = sum((split.share_amount for split in splits), Decimal("0"))
        allocated = Decimal("0")
        for split in splits[:-1]:
            split.share_amount = (
                split.share_amount * new_total / old_total
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            allocated += split.share_amount
        splits[-1].share_amount = new_total - allocated

    await reimbursement_service.fit_links_to_amount(session, transaction)

    pair_id = transaction.transfer_pair_id
    if pair_id is not None and not transaction.transfer_amount_explicit:
        await session.execute(
            update(Transaction)
            .where(Transaction.transfer_pair_id == pair_id)
            .values(transfer_pair_id=None)
        )
        transaction.transfer_pair_id = None
