"""record when an asset value was entered, not just what day it is for

Revision ID: 083
Revises: 082
Create Date: 2026-08-23

`asset_values` carried only `date` — the day the figure is *about*. A holding
at an institution with no API is worth whatever the user last typed, and the
whole point of a hand-set value is knowing whether it is fresh or a year stale,
which `date` cannot say: a user correcting last month's figure today writes a
row dated last month.

It also settles which row wins when two share a date. The tiebreak was
`ORDER BY id DESC` on a random UUID4 — so a hand-set value and a sync-written
value on the same day resolved arbitrarily, differently on each read.

Existing rows are backfilled to midnight on their own `date`: the best the old
schema can say about when they were written, and it keeps the ordering stable
for history that predates the column.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "083"
down_revision: Union[str, None] = "082"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "asset_values",
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute('UPDATE asset_values SET recorded_at = "date"::timestamptz')


def downgrade() -> None:
    op.drop_column("asset_values", "recorded_at")
