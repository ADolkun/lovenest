import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, computed_field

from app.core.connection_settings import (
    AccountStatus,
    account_status,
    known_account_ids,
    seen_account_ids,
)


class BankConnectionBase(BaseModel):
    provider: str
    institution_name: str


class ConnectionInstitutionRead(BaseModel):
    """One institution reached through a connection (issue #345). Distinct
    from InstitutionRead below, which is a provider's connectable-bank
    catalog entry, not a linked institution."""

    name: str
    logo_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


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
    # Institutions this link spans. Empty for providers that are one
    # institution per connection — institution_name above covers those.
    institutions: list[ConnectionInstitutionRead] = []

    model_config = ConfigDict(from_attributes=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pending_account_count(self) -> int:
        """How many provider accounts appeared after the allowlist was configured.

        Derived from the connection's own settings so the connections list can
        show it without a provider request, over the same rule
        `list_provider_accounts` applies per account — the accounts here are the
        ones the last sync saw rather than a fresh provider read.
        """
        known = known_account_ids(self.settings)
        return sum(
            1
            for external_id in seen_account_ids(self.settings)
            if account_status(self.settings, external_id, known) == "pending"
        )


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
    # Empty is the review-first connect: create the connection, import nothing,
    # and let the user pick from the account picker before the first sync.
    account_allowlist: Optional[list[str]] = None
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
    # Provider account ids the picker had on screen when the allowlist was
    # saved. Only the client knows this: an account that turned up at the
    # provider since the last sync is in no set the server holds yet, so
    # without it an account the user deliberately unchecked comes back as
    # pending on the next sync. Read only alongside `account_allowlist`, and
    # never stored verbatim — it feeds the reviewed set.
    reviewed_account_ids: Optional[list[str]] = None


class ProviderAccountRead(BaseModel):
    """One account the provider currently exposes, and what the allowlist does to it.

    `status` is derived at read time, never stored, so it cannot drift from
    the allowlist it describes.
    """

    external_id: str
    name: str
    balance: Decimal
    currency: str
    # None where the provider does not say — an abstention, not "no holdings".
    has_holdings: Optional[bool]
    status: AccountStatus
