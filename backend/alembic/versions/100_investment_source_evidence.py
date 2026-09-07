"""retain investment observations and reviewed links without duplicating the ledger

Revision ID: 100
Revises: 099
"""
import sqlalchemy as sa
from alembic import op

revision = "100"
down_revision = "099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "investment_observations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", sa.UUID(), sa.ForeignKey("asset_groups.id", ondelete="SET NULL")),
        sa.Column("connection_id", sa.UUID(), sa.ForeignKey("bank_connections.id", ondelete="SET NULL")),
        sa.Column("import_id", sa.UUID(), sa.ForeignKey("import_logs.id", ondelete="SET NULL")),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "identity_key", "fingerprint"),
    )
    op.create_table(
        "investment_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", sa.UUID(), sa.ForeignKey("asset_groups.id", ondelete="SET NULL")),
        sa.Column("event_key", sa.String(64), nullable=False),
        sa.Column("opening_boundary", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "group_id", "event_key"),
    )
    op.create_table(
        "investment_legs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.UUID(), sa.ForeignKey("investment_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("observation_id", sa.UUID(), sa.ForeignKey("investment_observations.id"), nullable=False),
        sa.Column("source_leg_key", sa.String(255), nullable=False),
        sa.Column("asset_id", sa.UUID(), sa.ForeignKey("assets.id", ondelete="SET NULL")),
        sa.Column("asset_transaction_id", sa.UUID(), sa.ForeignKey("asset_transactions.id", ondelete="SET NULL"), unique=True),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("observation_id", "source_leg_key"),
    )
    op.create_table(
        "investment_observation_links",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("observation_id", sa.UUID(), sa.ForeignKey("investment_observations.id"), nullable=False),
        sa.Column("source_leg_key", sa.String(255), nullable=False),
        sa.Column("leg_id", sa.UUID(), sa.ForeignKey("investment_legs.id"), nullable=False),
        sa.Column("import_id", sa.UUID(), sa.ForeignKey("import_logs.id", ondelete="SET NULL")),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("quantity", sa.String(128)),
        sa.Column("reason", sa.String(500)),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("conflicting_fields", sa.JSON(), nullable=False),
        sa.Column("reviewed_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("observation_id", "source_leg_key", "leg_id"),
    )
    for table, columns in {
        "investment_observations": ("workspace_id", "group_id", "import_id"),
        "investment_events": ("workspace_id", "group_id"),
        "investment_legs": ("workspace_id", "event_id", "observation_id", "asset_id"),
        "investment_observation_links": ("workspace_id", "observation_id", "leg_id", "import_id"),
    }.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    for table in ("investment_observation_links", "investment_legs", "investment_events", "investment_observations"):
        op.drop_table(table)
