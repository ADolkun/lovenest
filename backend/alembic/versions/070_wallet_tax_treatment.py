"""add wallet tax treatment (issue #61)

Revision ID: 070
Revises: 069
Create Date: 2026-08-12

Existing wallets become ``taxable`` through the server-side default, so nothing
needs a manual pass to keep working. The check constraint keeps persisted
values in sync with the API's validated set.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "070"
down_revision: Union[str, None] = "069"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CONSTRAINT_NAME = "ck_asset_groups_tax_treatment"


def upgrade() -> None:
    op.add_column(
        "asset_groups",
        sa.Column(
            "tax_treatment",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'taxable'"),
        ),
    )
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        "asset_groups",
        "tax_treatment IN ('taxable', 'roth', 'traditional', 'hsa', 'other')",
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "asset_groups", type_="check")
    op.drop_column("asset_groups", "tax_treatment")
