"""Immutable recovery source annotations and append-only review assertions.

Revision ID: 103
Revises: 102
"""
import sqlalchemy as sa
from alembic import op

revision = '103'
down_revision = '102'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'investment_recovery_entries',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('workspace_id', sa.UUID(), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False),
        sa.Column('group_id', sa.UUID(), sa.ForeignKey('asset_groups.id'), nullable=False),
        sa.Column('observation_id', sa.UUID(), sa.ForeignKey('investment_observations.id'), nullable=False),
        sa.Column('leg_id', sa.UUID(), sa.ForeignKey('investment_legs.id'), nullable=False),
        sa.Column('entry_key', sa.String(64), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_by', sa.UUID(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('workspace_id', 'group_id', 'entry_key', 'fingerprint'),
    )
    op.create_table(
        'investment_recovery_reviews',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('workspace_id', sa.UUID(), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False),
        sa.Column('group_id', sa.UUID(), sa.ForeignKey('asset_groups.id'), nullable=False),
        sa.Column('anchor_entry_id', sa.UUID(), sa.ForeignKey('investment_recovery_entries.id'), nullable=False),
        sa.Column('supersedes_id', sa.UUID(), sa.ForeignKey('investment_recovery_reviews.id'), unique=True),
        sa.Column('review_key', sa.String(255), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_by', sa.UUID(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('workspace_id', 'group_id', 'review_key', 'fingerprint'),
    )
    for table, columns in {
        'investment_recovery_entries': ('workspace_id', 'group_id', 'observation_id', 'leg_id'),
        'investment_recovery_reviews': ('workspace_id', 'group_id', 'anchor_entry_id'),
    }.items():
        for column in columns:
            op.create_index(f'ix_{table}_{column}', table, [column])


def downgrade():
    op.drop_table('investment_recovery_reviews')
    op.drop_table('investment_recovery_entries')
