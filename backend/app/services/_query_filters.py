"""Shared SQLAlchemy filter fragments for report/dashboard queries.

Centralizes the "what counts as real income/expense" definition so every
aggregation site agrees. Changes to the rule (e.g. adding a new exclusion
signal) only need to be made here.
"""
from sqlalchemy import and_, or_, select

from app.models.category import Category
from app.models.transaction import Transaction


def counts_as_pnl():
    """SQL filter: True when a transaction should contribute to income/expense totals.

    Excludes:
      - paired transfers (both legs were matched; already cancel out),
      - transactions in categories flagged `treat_as_transfer` (one-sided
        movements like investment applications where the counterpart is
        an Asset/Holding, not another Account).

    Does NOT exclude `source='opening_balance'` — callers that already
    filter those keep doing so; this helper only handles the transfer-like
    exclusion family so both rules stay visible at each call site.
    """
    return and_(
        Transaction.transfer_pair_id.is_(None),
        or_(
            Transaction.category_id.is_(None),
            Transaction.category_id.not_in(
                select(Category.id).where(Category.treat_as_transfer.is_(True))
            ),
        ),
    )
