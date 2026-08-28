"""workspace-scope the asset uniqueness index

Revision ID: 088
Revises: 087
Create Date: 2026-08-28

The same widening migration 071 did for `asset_groups`, for the table that
holds the actual positions. Migration 032 made `(user_id, source, external_id)`
unique when a user had one workspace; since #75 the sync builds a wallet — and
therefore an asset — per workspace for the same provider item, so the second
workspace's first holdings sync dies on an IntegrityError against an index
nothing in the application believes in.

The suite could not see it: the index lived only in the migration, never in the
model, and the tests run on SQLite built from `Base.metadata`. Widening a unique
index only ever permits more rows, so there is nothing to de-duplicate first.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "088"
down_revision: Union[str, None] = "087"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_INDEX = "ux_assets_user_source_external"
_NEW_INDEX = "ux_assets_workspace_source_external"


def upgrade() -> None:
    op.drop_index(_OLD_INDEX, table_name="assets")
    op.create_index(
        _NEW_INDEX,
        "assets",
        ["workspace_id", "user_id", "source", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_NEW_INDEX, table_name="assets")
    op.create_index(
        _OLD_INDEX,
        "assets",
        ["user_id", "source", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
