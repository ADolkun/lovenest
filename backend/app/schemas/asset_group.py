import uuid
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

# Roth, traditional and HSA are the tax-advantaged treatments; `other` covers
# accounts whose character is neither (e.g. a foreign or trust account).
TaxTreatment = Literal["taxable", "roth", "traditional", "hsa", "other"]

# Wallets whose Realised Gain is also Reportable Gain (CONTEXT.md). An
# allowlist, deliberately: a treatment added later — and a holding with no
# wallet at all — is non-reportable until someone decides otherwise, where a
# blocklist would silently tax it.
REPORTABLE_TAX_TREATMENTS = frozenset({"taxable"})


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
    # The `type` of the provider account this wallet mirrors (#76: one wallet
    # per provider account) — what allocation by account type groups on. Null
    # for a manual wallet, or a synced one no account could be matched to.
    account_type: Optional[str] = None
    # Convenience rollup — filled by the service. Expressed in the group's
    # asset currencies without conversion; the frontend already handles
    # multi-currency totals.
    asset_count: int = 0
    current_value: float = 0.0
    current_value_primary: float = 0.0
    # The provider-reported balance of the account this wallet mirrors, in the
    # primary currency. Liquid Cash (CONTEXT.md) is what is left of it once the
    # wallet's holdings are subtracted. Null for a manual wallet, or a synced
    # one no account could be matched to — which is not the same as zero, and
    # the frontend must not derive cash from it.
    account_balance: Optional[float] = None
    #: What `current_value` is denominated in. Null where the wallet's holdings
    #: disagree, or it has none — then `current_value` is a mixed-unit sum and
    #: only `current_value_primary` means anything.
    currency: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)
