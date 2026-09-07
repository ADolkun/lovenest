"""durable owned history archives and source interpretation qualification

Revision ID: 101
Revises: 100
"""
import sqlalchemy as sa
from alembic import op

revision = "101"
down_revision = "100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("investment_observations", sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_table(
        "investment_history_collections",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", sa.UUID(), sa.ForeignKey("bank_connections.id", ondelete="SET NULL")),
        sa.Column("group_id", sa.UUID(), sa.ForeignKey("asset_groups.id", ondelete="SET NULL")),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for column in ("workspace_id", "connection_id"):
        op.create_index(f"ix_investment_history_collections_{column}", "investment_history_collections", [column])


def downgrade() -> None:
    op.drop_table("investment_history_collections")
    op.drop_column("investment_observations", "is_current")
