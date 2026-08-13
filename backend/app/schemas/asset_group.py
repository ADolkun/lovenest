import uuid
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

# Roth, traditional and HSA are the tax-advantaged treatments; `other` covers
# accounts whose character is neither (e.g. a foreign or trust account).
TaxTreatment = Literal["taxable", "roth", "traditional", "hsa", "other"]


class AssetGroupBase(BaseModel):
    name: str
    icon: str = "wallet"
    color: str = "#0EA5E9"
    position: int = 0
    tax_treatment: TaxTreatment = "taxable"


class AssetGroupCreate(AssetGroupBase):
    pass


class AssetGroupUpdate(BaseModel):
    name: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    position: Optional[int] = None
    tax_treatment: Optional[TaxTreatment] = None


class AssetGroupRead(AssetGroupBase):
    id: uuid.UUID
    user_id: uuid.UUID
    source: str = "manual"
    connection_id: Optional[uuid.UUID] = None
    # The originating institution — preserved as context even if the user
    # renames the wallet to something like "Renda Fixa Longo Prazo".
    # Null for manual wallets, a bank/broker name for synced ones.
    institution_name: Optional[str] = None
    # Convenience rollup — filled by the service. Expressed in the group's
    # asset currencies without conversion; the frontend already handles
    # multi-currency totals.
    asset_count: int = 0
    current_value: float = 0.0
    current_value_primary: float = 0.0

    model_config = ConfigDict(from_attributes=True)
