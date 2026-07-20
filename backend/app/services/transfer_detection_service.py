import uuid
from collections import defaultdict
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.transaction import Transaction
from app.services.transaction_semantics import (
    is_credit_card_payment,
    is_credit_card_payment_pair,
    is_compatible_non_card_transfer,
)


def _compatible_transfer(
    debit: Transaction,
    credit: Transaction,
    account_info: dict[uuid.UUID, tuple[str, str]],
) -> bool:
    debit_type, debit_name = account_info[debit.account_id]
    credit_type, credit_name = account_info[credit.account_id]
    debit_payment = is_credit_card_payment(
        debit.description, debit.type, debit_type
    )
    credit_payment = is_credit_card_payment(
        credit.description, credit.type, credit_type
    )
    if debit_payment or credit_payment:
        return is_credit_card_payment_pair(
            debit.description,
            debit_type,
            credit.description,
            credit_type,
            debit_name,
            credit_name,
        )

    return is_compatible_non_card_transfer(
        debit.description,
        credit.description,
        debit_name,
        credit_name,
    )


async def detect_transfer_pairs(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    candidate_ids: Optional[list[uuid.UUID]] = None,
    date_tolerance_days: int = 2,
) -> int:
    """Detect inter-account transfer pairs and link them with a shared UUID.

    Algorithm:
    1. When candidate_ids is given, load candidate debits AND candidate credits
       so that detection works regardless of which side was just imported.
    2. For each debit, find an unpaired credit with: same user/currency,
       different account, same absolute amount, date within ±tolerance days,
       and compatible transfer evidence in both descriptions
    3. Greedy closest-date-first matching; each tx can only pair once

    Returns the number of pairs created.
    """
    if candidate_ids == []:
        return 0

    # Load candidate debits — filtered to candidate_ids when provided
    debit_query = select(Transaction).where(
        Transaction.workspace_id == workspace_id,
        Transaction.type == "debit",
        Transaction.transfer_pair_id.is_(None),
        Transaction.source != "opening_balance",
    )
    if candidate_ids:
        debit_query = debit_query.where(Transaction.id.in_(candidate_ids))

    debit_result = await session.execute(debit_query)
    debits = list(debit_result.scalars().all())

    # When candidate_ids is given, also load ALL unpaired debits that could
    # match new credits (the reverse direction).
    if candidate_ids:
        reverse_debit_query = select(Transaction).where(
            Transaction.workspace_id == workspace_id,
            Transaction.type == "debit",
            Transaction.transfer_pair_id.is_(None),
            Transaction.source != "opening_balance",
            Transaction.id.not_in(candidate_ids),
        )
        reverse_result = await session.execute(reverse_debit_query)
        reverse_debits = list(reverse_result.scalars().all())
    else:
        reverse_debits = []

    all_debits = debits + reverse_debits

    if not all_debits:
        return 0

    # Load all unpaired credits for the user (potential partners)
    credit_query = select(Transaction).where(
        Transaction.workspace_id == workspace_id,
        Transaction.type == "credit",
        Transaction.transfer_pair_id.is_(None),
        Transaction.source != "opening_balance",
    )
    credit_result = await session.execute(credit_query)
    credits = list(credit_result.scalars().all())

    if not credits:
        return 0

    account_ids = {tx.account_id for tx in (*all_debits, *credits)}
    account_rows = await session.execute(
        select(Account.id, Account.type, Account.name).where(
            Account.id.in_(account_ids)
        )
    )
    account_info = {
        account_id: (account_type, account_name)
        for account_id, account_type, account_name in account_rows.all()
    }

    # When candidate_ids is given, restrict reverse debits to only match
    # credits that are in candidate_ids (avoid pairing two old transactions).
    candidate_id_set = set(candidate_ids) if candidate_ids else None

    # Build a lookup: amount -> list of credits
    credit_by_amount: dict[float, list[Transaction]] = defaultdict(list)
    for c in credits:
        credit_by_amount[abs(float(c.amount))].append(c)

    paired_credit_ids: set[uuid.UUID] = set()
    paired_debit_ids: set[uuid.UUID] = set()
    pairs_created = 0

    for debit in all_debits:
        debit_amount = abs(float(debit.amount))
        amount_candidates = credit_by_amount.get(debit_amount, [])

        is_reverse_debit = candidate_id_set is not None and debit.id not in candidate_id_set

        # Find closest-date match in a different account
        best_match: Optional[Transaction] = None
        best_delta: Optional[int] = None

        for credit in amount_candidates:
            if credit.id in paired_credit_ids:
                continue
            if credit.account_id == debit.account_id:
                continue
            if credit.currency != debit.currency:
                continue
            if not _compatible_transfer(debit, credit, account_info):
                continue
            # Reverse debits may only match credits from candidate_ids
            if is_reverse_debit and candidate_id_set and credit.id not in candidate_id_set:
                continue

            delta = abs((credit.date - debit.date).days)
            if delta > date_tolerance_days:
                continue

            if best_delta is None or delta < best_delta:
                best_match = credit
                best_delta = delta

        if best_match:
            pair_id = uuid.uuid4()
            debit.transfer_pair_id = pair_id
            best_match.transfer_pair_id = pair_id
            paired_credit_ids.add(best_match.id)
            paired_debit_ids.add(debit.id)
            pairs_created += 1

    return pairs_created


async def unlink_transfer_pair(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    pair_id: uuid.UUID,
) -> int:
    """Remove a transfer pair link. Returns number of transactions unlinked."""
    result = await session.execute(
        select(Transaction).where(
            Transaction.workspace_id == workspace_id,
            Transaction.transfer_pair_id == pair_id,
        )
    )
    transactions = list(result.scalars().all())

    for tx in transactions:
        tx.transfer_pair_id = None

    return len(transactions)
