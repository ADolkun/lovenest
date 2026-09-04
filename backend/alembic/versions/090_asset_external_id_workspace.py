"""scope asset external IDs to workspaces

Revision ID: 090
Revises: 089
Create Date: 2026-08-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "090"
down_revision: Union[str, None] = "089"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _duplicate_external_id():
    return (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT workspace_id AS scope_id, source, external_id, count(*) AS row_count
                FROM assets
                WHERE external_id IS NOT NULL
                GROUP BY workspace_id, source, external_id
                HAVING count(*) > 1
                LIMIT 1
                """
            )
        )
        .mappings()
        .first()
    )


def _require_unique_external_ids() -> None:
    duplicate = _duplicate_external_id()
    if duplicate is None:
        return
    raise RuntimeError(
        "Cannot change the asset external ID index because duplicate records "
        f"exist for workspace_id={duplicate['scope_id']}, "
        f"source={duplicate['source']!r}, external_id={duplicate['external_id']!r} "
        f"({duplicate['row_count']} rows). Reconcile those assets and rerun the migration."
    )


def upgrade() -> None:
    _require_unique_external_ids()
    op.drop_index("ux_assets_workspace_source_external", table_name="assets")
    op.create_index(
        "ux_assets_workspace_source_external",
        "assets",
        ["workspace_id", "source", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_assets_workspace_source_external", table_name="assets")
    op.create_index(
        "ux_assets_workspace_source_external",
        "assets",
        ["workspace_id", "user_id", "source", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
