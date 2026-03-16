import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, Date, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.asset_value import AssetValue


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(50))  # real_estate, vehicle, valuable, investment, other
    currency: Mapped[str] = mapped_column(String(3), default="BRL")
    units: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=15, scale=6), nullable=True)
    valuation_method: Mapped[str] = mapped_column(String(20), default="manual")  # manual, growth_rule
    purchase_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    purchase_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=15, scale=2), nullable=True)
    sell_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    sell_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=15, scale=2), nullable=True)
    growth_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # percentage, absolute
    growth_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=15, scale=6), nullable=True)
    growth_frequency: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # daily, weekly, monthly, yearly
    growth_start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)

    values: Mapped[list["AssetValue"]] = relationship(back_populates="asset", cascade="all, delete-orphan")
