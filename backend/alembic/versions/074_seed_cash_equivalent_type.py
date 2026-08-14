"""classify already-imported money-market and stablecoin holdings (issue #64)

Revision ID: 074
Revises: 073
Create Date: 2026-08-14

Cash Equivalents are excluded from allocation (CONTEXT.md), and the seeded
ticker guess only runs when a holding is created — so every holding imported
before it existed still reads as an invested position. Two SPAXX rows alone
account for over 49k of what the allocation views would call equity.

Only rows still sitting on the import default (`type = 'investment'`) are
touched: any other value is a classification someone chose, and the guess
never overrides the user.
"""
from typing import Sequence, Union

from alembic import op

from app.services.cash_equivalent import CASH_EQUIVALENT_TICKERS, CASH_EQUIVALENT_TYPE

revision: str = "074"
down_revision: Union[str, None] = "073"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TICKERS = ", ".join(f"'{t}'" for t in sorted(CASH_EQUIVALENT_TICKERS))


def upgrade() -> None:
    op.execute(
        f"UPDATE assets SET type = '{CASH_EQUIVALENT_TYPE}' "
        f"WHERE type = 'investment' AND upper(ticker) IN ({_TICKERS})"
    )


def downgrade() -> None:
    # Scoped to the seeded tickers, so it undoes this migration rather than
    # every classification anyone has made since.
    op.execute(
        f"UPDATE assets SET type = 'investment' "
        f"WHERE type = '{CASH_EQUIVALENT_TYPE}' AND upper(ticker) IN ({_TICKERS})"
    )
