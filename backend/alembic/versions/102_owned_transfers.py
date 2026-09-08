"""Reviewed movement applications, ownership and carried acquisition lineage.

Revision ID: 102
Revises: 101
"""
import sqlalchemy as sa
from alembic import op

revision = "102"
down_revision = "101"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("asset_transactions", "price", existing_type=sa.Numeric(38, 18), nullable=True)
    op.add_column("asset_transactions", sa.Column("movement", sa.JSON(), nullable=True))
    op.create_table(
        "investment_ownership",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", sa.UUID(), sa.ForeignKey("asset_groups.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("asserted_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("asserted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "investment_movement_applications",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("application_key", sa.String(64), nullable=False),
        sa.Column("leg_id", sa.UUID(), sa.ForeignKey("investment_legs.id"), nullable=False),
        sa.Column("asset_id", sa.UUID(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("ownership_id", sa.UUID(), sa.ForeignKey("investment_ownership.id"), nullable=False),
        sa.Column("transaction_id", sa.UUID(), sa.ForeignKey("asset_transactions.id"), unique=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("workspace_id", "application_key"),
    )
    op.create_table(
        "investment_owned_transfers",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("out_application_id", sa.UUID(), sa.ForeignKey("investment_movement_applications.id"), nullable=False, unique=True),
        sa.Column("in_application_id", sa.UUID(), sa.ForeignKey("investment_movement_applications.id"), nullable=False, unique=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("workspace_id", "identity_key"),
    )
    op.create_table(
        "investment_incidents",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("leg_id", sa.UUID(), sa.ForeignKey("investment_legs.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for table, columns in {
        "investment_ownership": ("workspace_id", "group_id"),
        "investment_movement_applications": ("workspace_id", "leg_id", "asset_id"),
        "investment_owned_transfers": ("workspace_id",),
        "investment_incidents": ("workspace_id", "leg_id"),
    }.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    # A downgrade cannot reinterpret movement rows as zero-priced purchases.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM asset_transactions WHERE movement IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("Reverse reviewed movement applications before downgrading")
    for table in ("investment_incidents", "investment_owned_transfers", "investment_movement_applications", "investment_ownership"):
        op.drop_table(table)
    op.drop_column("asset_transactions", "movement")
    op.alter_column("asset_transactions", "price", existing_type=sa.Numeric(38, 18), nullable=False)
