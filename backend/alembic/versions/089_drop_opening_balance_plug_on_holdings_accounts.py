"""drop the opening_balance plug on accounts whose balance carries Holdings

Revision ID: 089
Revises: 088
Create Date: 2026-08-30

Migration 029 (and the sync path it seeded) closed the gap between
`accounts.balance` and SUM(transactions) with a synthetic `opening_balance`
row, on the assumption that the gap is history the provider did not return.
That holds for a cash account. For an account carrying Holdings the two sides
are not the same quantity: the balance is the account's *total* — Liquid Cash
plus Holdings (CONTEXT.md) — while the ledger only ever holds the cash side,
because Holdings live in `assets`/`asset_values`. The "missing history" it
plugs is therefore market value plus unrealised gain, back-dated a day before
the oldest transaction — a day on which none of it existed. On a brokerage
with no cash ledger at all the plug comes out as the entire portfolio.

Scoped by the same join `_query_filters.holding_inside_account_balance` uses to
keep net worth from double-counting those Holdings, so exactly the accounts net
worth treats as already carrying their Holdings lose the plug. An account whose
Holdings the provider could not attribute (Pluggy files investments at the item,
not the account) matches nothing here and keeps its plug — the same limitation
net worth already lives with, not a new one.
"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "089"
down_revision: Union[str, None] = "088"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def strip_plugs(bind) -> int:
    """Delete every opening_balance row on a Holdings-carrying account."""
    result = bind.execute(
        sa.text(
            """
            DELETE FROM transactions
            WHERE source = 'opening_balance'
              AND account_id IN (
                  SELECT a.id FROM accounts a
                  WHERE a.connection_id IS NOT NULL
                    AND a.is_closed = false
                    AND EXISTS (
                        SELECT 1 FROM assets s
                        WHERE s.connection_id = a.connection_id
                          AND s.account_external_id = a.external_id
                          AND s.workspace_id = a.workspace_id
                          AND s.is_archived = false
                          AND s.sell_date IS NULL
                    )
              )
            """
        )
    )
    return result.rowcount or 0


def upgrade() -> None:
    removed = strip_plugs(op.get_bind())
    logging.getLogger("alembic.runtime.migration").info(
        "089: removed %d opening balance plug(s)", removed
    )


def downgrade() -> None:
    # Deliberately empty. The rows this deletes state that a portfolio was
    # deposited on a day it did not exist; re-creating them would re-create the
    # fiction, and the sync no longer writes one to reconcile against anyway.
    pass
