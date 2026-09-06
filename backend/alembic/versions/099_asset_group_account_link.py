"""link a manual portfolio explicitly to one account

Revision ID: 099
Revises: 098
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "099"
down_revision: Union[str, None] = "098"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("asset_groups", sa.Column("account_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_asset_groups_account_id", "asset_groups", "accounts",
        ["account_id"], ["id"], ondelete="SET NULL",
    )
    op.create_unique_constraint("uq_asset_groups_account_id", "asset_groups", ["account_id"])


def downgrade() -> None:
    op.drop_constraint("uq_asset_groups_account_id", "asset_groups", type_="unique")
    op.drop_constraint("fk_asset_groups_account_id", "asset_groups", type_="foreignkey")
    op.drop_column("asset_groups", "account_id")
