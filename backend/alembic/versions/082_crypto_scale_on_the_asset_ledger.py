"""give the asset ledger enough decimals to hold a crypto quantity

Revision ID: 082
Revises: 081
Create Date: 2026-08-23

`asset_transactions.quantity` and `.price` were `NUMERIC(18, 6)`, which is
right for shares and wrong for coins: 0.00014359 BTC rounds to 0.000144 on the
way in, and a satoshi-scale dust row rounds to zero outright. A cost-basis
import reconciled from a crypto tax tool is exactly the file that carries
sixteen decimals, so the ledger has to carry them too.

Widening is lossless for every existing row — `NUMERIC(28, 18)` holds ten
integer digits, more than any share count in the table — so there is no data
migration, only the type change. `assets.units` and `assets.average_price` are
the cached replay of the same ledger and move with it; `purchase_price` stays
at two decimals because it is money, not a quantity.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "082"
down_revision: Union[str, None] = "081"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column, widened, original)
_COLUMNS = (
    ("asset_transactions", "quantity", (28, 18), (18, 6)),
    ("asset_transactions", "price", (28, 18), (18, 6)),
    ("assets", "units", (28, 18), (15, 6)),
    ("assets", "average_price", (28, 18), (18, 6)),
)


def _alter(index: int, existing: int) -> None:
    for table, column, *scales in _COLUMNS:
        op.alter_column(
            table,
            column,
            type_=sa.Numeric(*scales[index]),
            existing_type=sa.Numeric(*scales[existing]),
            existing_nullable=table == "assets",
        )


def upgrade() -> None:
    _alter(0, 1)


def downgrade() -> None:
    # Rounds any row that used the new decimals — the price of going back.
    _alter(1, 0)
