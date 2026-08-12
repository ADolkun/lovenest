import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, computed_field


def allowlist_ids(settings: Optional[dict]) -> Optional[set[str]]:
    """Read the tri-state `account_allowlist` out of a connection's settings.

    The difference between the first two states is the compatibility contract:

    - absent: sync every account the provider returns (what every connection
      did before this setting existed), signalled by None;
    - present: sync only the listed provider account ids;
    - present and empty: sync nothing. A valid state, not an error.

    A non-list value reads as absent — a malformed setting must not silently
    stop a connection from syncing.

    Lives in the schema layer, not the service, so the connection read can
    derive allowlist state without the service importing it back.
    """
    raw = (settings or {}).get("account_allowlist")
    if not isinstance(raw, list):
        return None
    return {str(item) for item in raw}


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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pending_account_count(self) -> int:
        """How many provider accounts appeared after the allowlist was configured.

        Derived from the connection's own settings so the connections list can
        show it without a provider request, and matches the `pending` status
        `list_provider_accounts` derives per account.

        Without a pinned reviewed set the answer is 0, not an approximation:
        there `list_provider_accounts` treats every seen id as known, so no
        seen account can come out pending.
        """
        conn_settings = self.settings or {}
        allowlist = allowlist_ids(conn_settings)
        reviewed = conn_settings.get("reviewed_account_ids")
        if allowlist is None or not isinstance(reviewed, list):
            return 0
        known = allowlist | {str(item) for item in reviewed}
        seen = {str(item) for item in conn_settings.get("seen_account_ids") or []}
        return len(seen - known)


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
