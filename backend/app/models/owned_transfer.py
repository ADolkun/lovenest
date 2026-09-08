"""Reviewed decisions, not a second inventory ledger."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InvestmentOwnership(Base):
    __tablename__ = "investment_ownership"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_groups.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    asserted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    asserted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InvestmentMovementApplication(Base):
    __tablename__ = "investment_movement_applications"
    __table_args__ = (UniqueConstraint("workspace_id", "application_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    application_key: Mapped[str] = mapped_column(String(64))
    leg_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_legs.id"), index=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id"), index=True)
    ownership_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_ownership.id"))
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset_transactions.id"), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InvestmentOwnedTransfer(Base):
    __tablename__ = "investment_owned_transfers"
    __table_args__ = (UniqueConstraint("workspace_id", "identity_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    identity_key: Mapped[str] = mapped_column(String(64))
    out_application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_movement_applications.id"), unique=True)
    in_application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_movement_applications.id"), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InvestmentIncident(Base):
    __tablename__ = "investment_incidents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    leg_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investment_legs.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
