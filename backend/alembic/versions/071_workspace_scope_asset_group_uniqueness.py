"""workspace-scope the asset group uniqueness index (issue #75)

Revision ID: 071
Revises: 070
Create Date: 2026-08-12

Migration 034 made ``(user_id, source, external_id)`` unique back when a user
had exactly one workspace. That is no longer true: one user can link the same
provider item once per workspace, and #75 makes the sync build a wallet per
workspace for it. The old index forbids exactly that row, so it has to widen to
include ``workspace_id`` or the second workspace's first asset sync dies on an
IntegrityError.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "071"
down_revision: Union[str, None] = "070"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_INDEX = "ux_asset_groups_user_source_external"
_NEW_INDEX = "ux_asset_groups_workspace_source_external"


def upgrade() -> None:
    op.drop_index(_OLD_INDEX, table_name="asset_groups")
    op.create_index(
        _NEW_INDEX,
        "asset_groups",
        ["workspace_id", "user_id", "source", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_NEW_INDEX, table_name="asset_groups")
    op.create_index(
        _OLD_INDEX,
        "asset_groups",
        ["user_id", "source", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
