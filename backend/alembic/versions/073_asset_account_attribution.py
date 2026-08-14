"""record the provider account a holding sits in (issue #76)

Revision ID: 073
Revises: 072
Create Date: 2026-08-13

A Wallet stands for exactly one brokerage account and carries that account's
tax character, so sync needs the holding's account to build one wallet per
account. The attribution already reached the sync layer and was discarded;
this is where it lands.

Additive and nullable: existing rows backfill on their next sync, and NULL
keeps its meaning afterwards — the provider does not report the relationship
(Pluggy's investments are item-level), which is unattributable, not "none".
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "073"
down_revision: Union[str, None] = "072"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "assets", sa.Column("account_external_id", sa.String(255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("assets", "account_external_id")
