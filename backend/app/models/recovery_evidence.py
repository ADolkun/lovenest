"""Immutable recovery annotations and append-only review decisions."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InvestmentRecoveryEntry(Base):
    __tablename__ = 'investment_recovery_entries'
    __table_args__ = (UniqueConstraint('workspace_id', 'group_id', 'entry_key', 'fingerprint'),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('workspaces.id', ondelete='CASCADE'), index=True)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('asset_groups.id'), index=True)
    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('investment_observations.id'), index=True)
    leg_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('investment_legs.id'), index=True)
    entry_key: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InvestmentRecoveryReview(Base):
    __tablename__ = 'investment_recovery_reviews'
    __table_args__ = (UniqueConstraint('workspace_id', 'group_id', 'review_key', 'fingerprint'), UniqueConstraint('supersedes_id'))

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('workspaces.id', ondelete='CASCADE'), index=True)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('asset_groups.id'), index=True)
    anchor_entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('investment_recovery_entries.id'), index=True)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('investment_recovery_reviews.id'))
    review_key: Mapped[str] = mapped_column(String(255))
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
