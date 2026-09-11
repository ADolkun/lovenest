"""Retain source interpretations and explicit acquisition corrections.

Revision ID: 104
Revises: 103
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "104"
down_revision = "103"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "investment_source_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", sa.UUID(), sa.ForeignKey("asset_groups.id", ondelete="SET NULL")),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("supersedes_id", sa.UUID(), sa.ForeignKey("investment_source_reviews.id")),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "request_key"),
        sa.UniqueConstraint("supersedes_id"),
    )
    for column in ("workspace_id", "group_id"):
        op.create_index(f"ix_investment_source_reviews_{column}", "investment_source_reviews", [column])


def downgrade():
    op.drop_table("investment_source_reviews")
