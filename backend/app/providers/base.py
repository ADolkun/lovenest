from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Optional


# Outcome of asking a provider to pull fresh data from the underlying institution
# before we read accounts/transactions.
#
# - "refreshed":         provider successfully synced; safe to read.
# - "skipped":           provider has no on-demand refresh (default for most providers).
# - "needs_user_action": provider needs the user to act (MFA, expired credentials).
#                        Caller should mark the connection as needing reconnection
#                        and skip the read pass.
# - "failed":            transient error (timeout, rate limit). Caller may proceed
#                        with a stale read; we don't punish the user for it.
RefreshOutcome = Literal["refreshed", "skipped", "needs_user_action", "failed"]


def default_oauth_redirect_uri() -> str:
    """The OAuth callback URL implied by where the frontend is served.

    Providers fall back to this when their own ``*_OAUTH_REDIRECT_URI`` is
    unset, so a deployment on a custom port or domain works without setting one
    variable per provider. Deriving it here rather than in Compose also keeps
    the compose files free of nested ``${VAR:-${OTHER:-x}}`` interpolation,
    which podman-compose cannot parse (containers/podman-compose#1064).
    """
    from app.core.config import get_settings

    return f"{get_settings().frontend_url.rstrip('/')}/oauth/callback"


def mask_last4(value: Optional[str]) -> Optional[str]:
    """Reduce a bank-assigned account identifier to its last four characters.

    Providers expose different identifiers for the same purpose: an IBAN
    (Enable Banking), a branch/account number (Pluggy), a card number. They all
    serve one job here, telling two same-named accounts apart, so we normalize
    them to a single shape and deliberately keep only the tail. Storing the full
    identifier would put a payable bank reference in the database for no gain.

    Separators (spaces, dashes) are stripped first, since IBANs are commonly
    formatted in groups of four. Returns None when the source is missing or too
    short to mask, so callers never render a partial identifier.
    """
    if not value:
        return None
    cleaned = "".join(ch for ch in str(value) if ch.isalnum())
    if len(cleaned) < 4:
        return None
    return cleaned[-4:]


def to_decimal(value: Any) -> Optional[Decimal]:
    """Parse a provider-supplied number, returning None when it isn't one.

    Providers send amounts as strings, floats, or nulls interchangeably, and
    an unparseable one has to read as absent rather than take down the sync.
    """
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    # "NaN" and "Infinity" parse, and then raise on the first comparison a
    # caller makes — which for a provider is mid-sync, after the rest of the
    # pull is staged. Not a number reads as absent, like anything else that
    # isn't one.
    return parsed if parsed.is_finite() else None


def iso_currency(value: Any, fallback: Optional[str] = None) -> Optional[str]:
    """Normalize a connector-supplied currency, guarding against non-ISO values.

    Currency fields are populated by the connector, and for crypto/brokerage
    positions some put the asset's ticker there instead (e.g. ``"DOGE"``,
    ``"CORECHAIN"``) rather than an ISO 4217 code. Every column these land in
    — ``accounts.currency``, ``transactions.currency``, ``assets.currency`` —
    is ``VARCHAR(3)``, so anything longer overflows the write and takes down
    the whole connection's sync (issue #448).

    This is a shape check, not a validity check: a 3-letter ticker like
    ``BTC`` still passes. It exists to keep an oversized value from ever
    reaching the DB, so use it at every site that forwards a raw provider
    currency.
    """
    if isinstance(value, str) and len(value) == 3 and value.isalpha():
        return value.upper()
    return fallback


def normalize_ticker(value: Any) -> Optional[str]:
    """Normalize a holding's symbol for the ``assets.ticker`` column.

    Truncated to the column's 32 chars so an unexpectedly long symbol can't
    reproduce the overflow ``iso_currency`` already guards against.
    """
    if not isinstance(value, str):
        return None
    return value.strip().upper()[:32] or None


@dataclass
class AccountData:
    external_id: str
    name: str
    type: str  # checking, savings, credit_card, investment, cash
    balance: Decimal
    currency: str
    credit_limit: Optional[Decimal] = None
    statement_close_day: Optional[int] = None
    payment_due_day: Optional[int] = None
    minimum_payment: Optional[Decimal] = None
    card_brand: Optional[str] = None
    card_level: Optional[str] = None
    # Last 4 chars of the bank's own identifier for the account, when the
    # provider exposes one. Disambiguates accounts a bank reports under an
    # identical name (issue #408). Never the full identifier — see mask_last4.
    masked_number: Optional[str] = None
    # Whether the provider lists investment holdings under this account.
    # Only a full account fetch can answer this — SimpleFIN's balances-only
    # mode strips holdings, so it must not be used on a path that sets this.
    # None means the provider does not report the relationship at all (Pluggy's
    # investments are item-level; Enable Banking has none), which is an
    # abstention: answering False there would tell the user an investment
    # account holds nothing.
    has_holdings: Optional[bool] = None
    # Per-account institution override (SimpleFIN — issue #345). None = same
    # as connection. The external id is the provider's stable org id
    # (SimpleFIN conn_id) so a renamed bank updates its row instead of
    # minting a new one; name-only hints fall back to name identity.
    institution_external_id: Optional[str] = None
    institution_name: Optional[str] = None
    institution_logo_url: Optional[str] = None


@dataclass
class TransactionData:
    external_id: str
    description: str
    amount: Decimal
    date: date
    type: str  # debit, credit
    currency: Optional[str] = None  # ISO currency code (e.g. BRL, USD)
    amount_in_account_currency: Optional[Decimal] = None  # Bank-provided conversion for intl txns
    pluggy_category: Optional[str] = None
    status: str = "posted"  # posted, pending
    payee: Optional[str] = None
    raw_data: Optional[dict] = None
    # Installment metadata (parcelamento) — populated by CC providers that expose it.
    installment_number: Optional[int] = None
    total_installments: Optional[int] = None
    installment_total_amount: Optional[Decimal] = None
    installment_purchase_date: Optional[date] = None
    # Provider-side identifier of the bill this transaction belongs to.
    # Resolved to a credit_card_bills.id FK at sync time (issue #92).
    bill_external_id: Optional[str] = None


@dataclass
class ConnectionData:
    external_id: str
    institution_name: str
    credentials: dict
    accounts: list[AccountData]
    # Fully-formed institution logo URL when the provider exposes one
    # (Pluggy connector.imageUrl, Enable Banking ASPSP logo). None for
    # providers/institutions without a logo (e.g. SimpleFin).
    logo_url: Optional[str] = None


@dataclass
class ConnectTokenData:
    access_token: str


@dataclass
class BillData:
    """A normalized credit-card bill (fatura), provider-agnostic.

    Only the fields universal to a credit-card statement are promoted. Anything
    a specific integration provides on top (finance charges, recorded payments,
    installment options, etc.) is preserved verbatim in `raw_data` so we can
    pull more out later without forcing a schema-shaped opinion now.
    """

    external_id: str
    due_date: date
    total_amount: Decimal
    currency: str = "BRL"
    minimum_payment: Optional[Decimal] = None
    raw_data: Optional[dict] = None


@dataclass
class HoldingData:
    """A normalized investment holding, provider-agnostic.

    Providers that don't expose holdings-style data return an empty list
    from `get_holdings`. Provider-specific fields that don't fit the common
    shape (rate, profit, issuer, status, type/subtype labels, etc.) go in
    `metadata` as-is — the sync layer stores it on Asset.external_metadata
    without trying to normalize it.
    """

    external_id: str
    name: str
    currency: str
    current_value: Decimal
    quantity: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    purchase_price: Optional[Decimal] = None
    purchase_date: Optional[date] = None
    isin: Optional[str] = None
    # Exchange/asset symbol (e.g. "AAPL", "DOGE") for the dedicated
    # `assets.ticker` column. Kept separate from `currency`: connectors that
    # report a crypto position put the ticker in both, and only one of them
    # belongs in a 3-char ISO currency field.
    ticker: Optional[str] = None
    maturity_date: Optional[date] = None
    is_withdrawn: bool = False  # provider signaled the position was sold/transferred
    metadata: Optional[dict] = None
    # The provider account this holding sits in, when the provider exposes the
    # relationship. Stays None for providers whose holdings are connection-level
    # (Pluggy investments), so an unset value means "unattributable", not "none".
    account_external_id: Optional[str] = None

    # Provider display name for the owning account, used as the synced wallet's
    # default name. Users can still rename the wallet after it is created.
    account_name: Optional[str] = None


@dataclass
class TradeData:
    """One buy or sell behind a holding, provider-agnostic.

    ``external_id`` is the provider's own identifier for the transaction, and
    the only thing a re-sync deduplicates on — so it has to name the same
    trade on every call, not a position in a page.

    ``holding_external_id`` is the ``HoldingData.external_id`` of the position
    the trade moves, which is how the sync layer finds the ledger to write to.

    ``price`` is per unit in the holding's currency, and carries any fee the
    provider folded into the fiat total: cost basis is what left the bank, not
    the sticker price.

    ``occurred_at`` is the full instant, not the day. The ledger stores a
    date, but replaying it has to put two trades of the same asset on the same
    day in the order they happened — buy-then-sell books a gain, sell-then-buy
    books nothing — and the day alone cannot say which came first.

    ``notes`` is what the two kinds cannot say on their own: that a buy is a
    staking payout taxed as income at receipt, or that a sell is the provider
    calling it something other than a sale. None when "buy" or "sell" is
    already the whole truth.
    """

    external_id: str
    holding_external_id: str
    kind: str  # buy, sell
    quantity: Decimal
    price: Decimal
    occurred_at: datetime
    notes: Optional[str] = None


@dataclass
class InstitutionData:
    """One ASPSP/bank offered by an OAuth provider."""

    name: str  # canonical identifier the provider expects in subsequent calls
    display_name: str
    country: str  # ISO 3166-1 alpha-2
    logo: Optional[str] = None
    bic: Optional[str] = None
    psu_types: list[str] = field(default_factory=list)  # e.g. ["personal", "business"]
    max_consent_days: Optional[int] = None
    max_history_days: Optional[int] = None


@dataclass
class InstitutionListData:
    """List of supported institutions (banks) for a provider, optionally for one country."""

    countries: list[str]
    institutions: list[InstitutionData]


class SessionExpiredError(Exception):
    """Raised when a provider session/consent has expired and reauth is required."""


class ProviderUserActionRequired(Exception):
    """Raised when a provider needs the user to take an action outside the app.

    Example: Enable Banking restricted mode requires the user to pre-link
    accounts in the EB portal before sessions return any accounts.
    """

    def __init__(self, message: str, *, code: str, help_url: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.help_url = help_url


class ProviderRateLimited(Exception):
    """Raised when the upstream bank/aggregator is throttling data requests.

    Transient and outside the user's control — PSD2 caps unattended account
    access (commonly ~4/day per resource), so a burst of syncs returns HTTP
    429. The connection is healthy; callers should skip this run and retry
    later rather than flag it as errored.
    """


class ProviderNotConfiguredError(Exception):
    """Raised when a connection references a provider missing from the registry.

    This is a server configuration problem, not a bank problem — typically one
    process (e.g. the Celery worker) is not loading the environment that
    enables the provider while the API process is. The credentials are fine,
    so callers must not flip the connection to "error": the reconnect banner
    would send the user chasing the wrong fix (and burning setup tokens).
    """


class FxRateProvider(ABC):
    """Abstract interface for FX rate providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider identifier (e.g. 'openexchangerates')."""
        ...

    @abstractmethod
    async def fetch_latest(self) -> dict[str, Decimal]:
        """Return {currency_code: rate_vs_USD} for latest rates."""
        ...

    @abstractmethod
    async def fetch_historical(self, target_date: date) -> dict[str, Decimal]:
        """Return rates for a specific date."""
        ...


class BankProvider(ABC):
    """Abstract interface for open finance integrations.

    Implement this for each provider (Pluggy, Belvo, etc.)
    to enable bank account syncing via OAuth or widget flow.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider identifier (e.g. 'pluggy', 'belvo')."""
        ...

    @property
    def flow_type(self) -> str:
        """Connection flow type: 'oauth' for redirect-based, 'widget' for embedded widget."""
        return "oauth"

    @property
    def redirect_uri(self) -> str:
        """Provider-specific OAuth redirect URI.

        Each provider reads its own env var (e.g.
        ``PLUGGY_OAUTH_REDIRECT_URI``, ``ENABLE_BANKING_OAUTH_REDIRECT_URI``)
        so different providers can register different URLs in their
        respective dashboards. When unset the URL is derived from
        ``FRONTEND_URL`` so a non-default port or domain works out of the box.
        """
        from app.core.config import get_settings

        settings = get_settings()
        return settings.pluggy_oauth_redirect_uri or default_oauth_redirect_uri()

    async def create_connect_token(
        self, client_user_id: str, item_id: str | None = None
    ) -> ConnectTokenData:
        """Create a connect token for widget-based flows. Override in widget providers."""
        raise NotImplementedError(f"{self.name} does not support widget connect tokens")

    async def list_institutions(
        self, country: Optional[str] = None
    ) -> "InstitutionListData":
        """List supported institutions (banks). Empty by default for providers
        that don't surface a selection step (Pluggy uses its own widget).
        """
        return InstitutionListData(countries=[], institutions=[])

    @abstractmethod
    async def get_oauth_url(
        self,
        redirect_uri: str,
        state: str,
        flow_params: Optional[dict] = None,
    ) -> str:
        """Generate OAuth URL for user to authorize.

        ``flow_params`` carries provider-specific options gathered up-front
        (e.g. EB needs ``{"country": "DE", "institution_name": "Revolut"}``).
        """
        ...

    async def reauth_url(
        self,
        credentials: dict,
        settings: dict,
        redirect_uri: str,
        state: str,
    ) -> str:
        """Build a re-authorization URL for an existing connection whose
        session/consent expired. Default raises — only providers with
        renewable consent (OAuth-redirect) need to override.
        """
        raise NotImplementedError(f"{self.name} does not support reauth_url")

    @abstractmethod
    async def handle_oauth_callback(self, code: str) -> ConnectionData:
        """Exchange OAuth code for access token and fetch initial data."""
        ...

    @abstractmethod
    async def get_accounts(self, credentials: dict) -> list[AccountData]:
        """Fetch accounts for a connection."""
        ...

    @abstractmethod
    async def get_transactions(
        self, credentials: dict, account_external_id: str,
        since: Optional[date] = None, payee_source: str = "auto",
    ) -> list[TransactionData]:
        """Fetch transactions for an account."""
        ...

    @abstractmethod
    async def refresh_credentials(self, credentials: dict) -> dict:
        """Refresh access token if needed."""
        ...

    async def trigger_refresh(self, credentials: dict | None) -> RefreshOutcome:
        """Ask the provider to pull fresh data from the underlying institution.

        Some aggregator providers cache the bank's data on their own side and
        only re-fetch on their own schedule. For those, the value we read on a
        sync may be older than what the bank actually has. When the user asks
        for fresh data, providers that expose an on-demand refresh should
        override this method to trigger it and poll until it completes.

        The default is a no-op (returns ``"skipped"``) — providers whose APIs
        already serve live data on every read don't need to override.
        """
        return "skipped"

    async def get_institution_logo(self, credentials: dict) -> Optional[str]:
        """Return the institution logo URL for an existing connection, or None.

        Used to backfill connections that were linked before logo capture
        existed (the sync layer calls this only when a connection has no
        stored logo). Default is no-op so providers without institution
        logos (e.g. SimpleFin) don't need to implement it.
        """
        return None

    async def get_holdings(self, credentials: dict) -> list[HoldingData]:
        """Fetch investment holdings for a connection.

        Providers that don't expose holdings (cash-only accounts, custom
        script providers without brokerage data, etc.) can rely on the
        default empty list.
        """
        return []

    async def get_trades(self, credentials: dict) -> list[TradeData]:
        """Fetch the buy/sell history behind this connection's holdings.

        Default empty: most providers report a position without the trades
        that built it, and a holding with no ledger is a Snapshot — quantity
        from the provider, holding period unknown — rather than a wrong one.

        Implementations must fetch a holding's history *completely* or raise.
        Half a history is not half a cost basis, it is a confidently wrong
        one, and the sync layer would have no way to tell the two apart.
        """
        return []

    async def get_bills(self, credentials: dict, account_external_id: str) -> list[BillData]:
        """Fetch credit-card bills (faturas) for an account.

        Providers without a bills endpoint, or non-regulated Pluggy
        connections that don't expose /bills, return the default empty list.
        The sync layer falls back to locally-computed cycle math in that case
        (see app.services.credit_card_service).
        """
        return []
