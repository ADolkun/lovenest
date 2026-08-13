"""clear aggregator-stamped acquisition dates (issue #61)

Revision ID: 069
Revises: 068
Create Date: 2026-08-12

SimpleFIN holdings were stamped with the aggregator's ``created`` timestamp —
when it first observed the holding, not when the position was acquired. The
mapping no longer sets it; this clears the values already written, along with
the historical AssetValue seeded at that same date. Holdings whose date came
from a real trade ledger are left alone. The observation date is not
recoverable, so the downgrade cannot restore it.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "069"
down_revision: Union[str, None] = "068"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Assets with a trade ledger are excluded: `recompute_and_cache` derives
    # their purchase_date from the first buy, so theirs is real and would be
    # re-derived anyway on the next ledger edit.
    stamped = (
        "SELECT id FROM assets a WHERE a.source = 'simplefin' "
        "AND a.purchase_date IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM asset_transactions t WHERE t.asset_id = a.id)"
    )
    # Drop the day-one chart point seeded from the same wrong date first —
    # it was written by _ensure_historical_seed and is unreachable once the
    # date is gone. Today's row is a real sync snapshot, not a seed.
    op.execute(
        "DELETE FROM asset_values WHERE source = 'sync' AND date <> CURRENT_DATE "
        "AND asset_id IN (" + stamped + ") "
        "AND date = (SELECT purchase_date FROM assets WHERE id = asset_values.asset_id)"
    )
    op.execute("UPDATE assets SET purchase_date = NULL WHERE id IN (" + stamped + ")")


def downgrade() -> None:
    """No-op — the observation dates are gone and were never the truth."""
