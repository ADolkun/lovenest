"""Retained observations support canonical legs; only the existing ledger applies money."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, func, true
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InvestmentObservation(Base):
    __tablename__ = "investment_observations"
    __table_args__ = (UniqueConstraint("workspace_id", "identity_key", "fingerprint"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset_groups.id", ondelete="SET NULL"), index=True)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bank_connections.id", ondelete="SET NULL"))
    import_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("import_logs.id", ondelete="SET NULL"), index=True)
    identity_key: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    # Producer-owned qualification; the retained source payload is immutable.
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Validated EvidenceObservationInput; amounts stay decimal strings even
    # on SQLite. Original values are not rounded to the application ledger.
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InvestmentHistoryCollection(Base):
    """Durable producer archive, independent of short-lived research checkpoints."""
    __tablename__ = "investment_history_collections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bank_connections.id", ondelete="SET NULL"), index=True)
    group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset_groups.id", ondelete="SET NULL"))
    revision: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSON)
    payload: Mapped[dict] = mapped_column(JSON)
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InvestmentEvent(Base):
    __tablename__ = "investment_events"
    __table_args__ = (UniqueConstraint("workspace_id", "group_id", "event_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset_groups.id", ondelete="SET NULL"), index=True)
    event_key: Mapped[str] = mapped_column(String(64))
    opening_boundary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InvestmentLeg(Base):
    __tablename__ = "investment_legs"
    __table_args__ = (UniqueConstraint("observation_id", "source_leg_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_events.id", ondelete="CASCADE"), index=True)
    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_observations.id"), index=True)
    source_leg_key: Mapped[str] = mapped_column(String(255))
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), index=True)
    asset_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("asset_transactions.id", ondelete="SET NULL"), unique=True
    )
    # Set once, and never cleared by unlink: replay must not resurrect an
    # application whose evidence relationship was reversed.
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)


class InvestmentObservationLink(Base):
    __tablename__ = "investment_observation_links"
    __table_args__ = (UniqueConstraint("observation_id", "source_leg_key", "leg_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_observations.id"), index=True)
    source_leg_key: Mapped[str] = mapped_column(String(255))
    leg_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_legs.id"), index=True)
    import_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("import_logs.id", ondelete="SET NULL"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reason_codes: Mapped[list] = mapped_column(JSON, default=list)
    conflicting_fields: Mapped[list] = mapped_column(JSON, default=list)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InvestmentSourceReview(Base):
    """Append-only source interpretations and corrections of an existing entry."""
    __tablename__ = "investment_source_reviews"
    __table_args__ = (UniqueConstraint("workspace_id", "request_key"), UniqueConstraint("supersedes_id"))

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset_groups.id", ondelete="SET NULL"), index=True)
    request_key: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64))
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("investment_source_reviews.id"))
    # Source, transaction and wallet IDs plus both images remain in the payload
    # if a later ordinary deletion removes their live database rows.
    payload: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
