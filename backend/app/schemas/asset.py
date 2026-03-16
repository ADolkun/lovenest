import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict


class AssetCreate(BaseModel):
    name: str
    type: str
    currency: str = "BRL"
    units: Optional[Decimal] = None
    valuation_method: str = "manual"
    purchase_date: Optional[date] = None
    purchase_price: Optional[Decimal] = None
    sell_date: Optional[date] = None
    sell_price: Optional[Decimal] = None
    current_value: Optional[Decimal] = None  # convenience: creates initial AssetValue
    growth_type: Optional[str] = None
    growth_rate: Optional[Decimal] = None
    growth_frequency: Optional[str] = None
    growth_start_date: Optional[date] = None
    is_archived: bool = False
    position: int = 0


class AssetUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    currency: Optional[str] = None
    units: Optional[Decimal] = None
    valuation_method: Optional[str] = None
    purchase_date: Optional[date] = None
    purchase_price: Optional[Decimal] = None
    sell_date: Optional[date] = None
    sell_price: Optional[Decimal] = None
    growth_type: Optional[str] = None
    growth_rate: Optional[Decimal] = None
    growth_frequency: Optional[str] = None
    growth_start_date: Optional[date] = None
    is_archived: Optional[bool] = None
    position: Optional[int] = None


class AssetRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    type: str
    currency: str
    units: Optional[float] = None
    valuation_method: str
    purchase_date: Optional[date] = None
    purchase_price: Optional[float] = None
    sell_date: Optional[date] = None
    sell_price: Optional[float] = None
    growth_type: Optional[str] = None
    growth_rate: Optional[float] = None
    growth_frequency: Optional[str] = None
    growth_start_date: Optional[date] = None
    is_archived: bool
    position: int
    current_value: Optional[float] = None
    gain_loss: Optional[float] = None
    value_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class AssetValueCreate(BaseModel):
    amount: Decimal
    date: date


class AssetValueRead(BaseModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    amount: float
    date: date
    source: str

    model_config = ConfigDict(from_attributes=True)
