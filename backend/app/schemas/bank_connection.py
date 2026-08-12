import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


class BankConnectionBase(BaseModel):
    provider: str
    institution_name: str


class BankConnectionRead(BankConnectionBase):
    id: uuid.UUID
    user_id: uuid.UUID
    external_id: str
    display_name: Optional[str] = None
    logo_url: Optional[str] = None
    settings: Optional[dict] = None
    status: str
    last_sync_at: Optional[datetime] = None
    last_sync_error_account_id: Optional[uuid.UUID] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OAuthUrlRequest(BaseModel):
    provider: str = "pluggy"
    flow_params: Optional[dict] = None


class OAuthUrlResponse(BaseModel):
    url: str


class OAuthCallbackRequest(BaseModel):
    code: str
    state: Optional[str] = None
    provider: Optional[str] = None
    sync_assets: Optional[bool] = None
    reconnect_connection_id: Optional[uuid.UUID] = None


class ReauthUrlResponse(BaseModel):
    url: str


class InstitutionRead(BaseModel):
    name: str
    display_name: str
    country: str
    logo: Optional[str] = None
    bic: Optional[str] = None
    psu_types: list[str] = []
    max_consent_days: Optional[int] = None
    max_history_days: Optional[int] = None


class InstitutionListResponse(BaseModel):
    countries: list[str]
    institutions: list[InstitutionRead]


class ConnectTokenRequest(BaseModel):
    provider: str = "pluggy"


class ConnectTokenResponse(BaseModel):
    access_token: str


class ReconnectTokenResponse(BaseModel):
    access_token: str


class ConnectionSettingsUpdate(BaseModel):
    display_name: Optional[str] = None
    payee_source: Optional[Literal["auto", "merchant", "payment_data", "description", "none"]] = None
    import_pending: Optional[bool] = None
    sync_assets: Optional[bool] = None
    # Provider account ids this connection may sync. Omitted leaves the
    # connection on legacy behaviour (sync everything); an empty list is a
    # valid selection meaning "sync nothing", not a reset.
    account_allowlist: Optional[list[str]] = None


class ProviderAccountRead(BaseModel):
    """One account the provider currently exposes, and what the allowlist does to it.

    `status` is derived at read time, never stored, so it cannot drift from
    the allowlist it describes.
    """

    external_id: str
    name: str
    balance: Decimal
    currency: str
    has_holdings: bool
    status: Literal["included", "excluded", "pending"]
