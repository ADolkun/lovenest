from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional


@dataclass
class AccountData:
    external_id: str
    name: str
    type: str  # checking, savings, credit_card
    balance: Decimal
    currency: str
    credit_limit: Optional[Decimal] = None
    statement_close_day: Optional[int] = None
    payment_due_day: Optional[int] = None
    minimum_payment: Optional[Decimal] = None
    card_brand: Optional[str] = None
    card_level: Optional[str] = None


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
    maturity_date: Optional[date] = None
    is_withdrawn: bool = False  # provider signaled the position was sold/transferred
    metadata: Optional[dict] = None


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

    async def create_connect_token(
        self, client_user_id: str, item_id: str | None = None
    ) -> ConnectTokenData:
        """Create a connect token for widget-based flows. Override in widget providers."""
        raise NotImplementedError(f"{self.name} does not support widget connect tokens")

    @abstractmethod
    def get_oauth_url(self, redirect_uri: str, state: str) -> str:
        """Generate OAuth URL for user to authorize."""
        ...

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

    async def get_holdings(self, credentials: dict) -> list[HoldingData]:
        """Fetch investment holdings for a connection.

        Providers that don't expose holdings (cash-only accounts, custom
        script providers without brokerage data, etc.) can rely on the
        default empty list.
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
