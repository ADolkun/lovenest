"""retain reversible pending transaction lifecycle evidence

Revision ID: 119
Revises: 118
"""
from alembic import op
import sqlalchemy as sa

revision = "119"
down_revision = "118"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transactions", sa.Column("sync_metadata", sa.JSON(), nullable=True))


def downgrade() -> None:
    # Do not strand an automatic ignore after its ownership/audit disappears.
    # User ignores have no ownership marker (or explicitly relinquished it).
    transactions = sa.table(
        "transactions", sa.column("is_ignored", sa.Boolean()),
        sa.column("sync_metadata", sa.JSON()),
    )
    op.execute(transactions.update().where(
        transactions.c.sync_metadata["pending_ignore_owned"].as_boolean().is_(True),
    ).values(is_ignored=False))
    op.drop_column("transactions", "sync_metadata")
