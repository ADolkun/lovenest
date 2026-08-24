import uuid
from datetime import date as _date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.asset import Asset


class AssetValue(Base):
    __tablename__ = "asset_values"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("assets.id", ondelete="CASCADE"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=15, scale=6))
    # Per-share price on `date` for market-priced holdings (quantity-independent).
    # The value chart is rebuilt as ledger_quantity(date) × price(date) so that
    # entering past buys/sells correctly reshapes the whole history (issue:
    # backdated trades didn't update the baked `amount`). Null for manual/growth
    # assets, where `amount` is the value directly.
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=18, scale=6), nullable=True)
    date: Mapped[_date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual, rule, sync
    # When this figure was entered, as opposed to the day it is *about*.
    # A hand-set value is only trustworthy if the user can see how stale it
    # is, and `date` cannot say: correcting last month's figure today writes
    # a row dated last month. Also the tiebreak when two rows share a date.
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    asset: Mapped["Asset"] = relationship(back_populates="values")
