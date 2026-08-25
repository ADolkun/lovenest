"""Coinbase provider — crypto balances from the retail v2 API.

Connects with the paste-a-token flow: the user downloads a CDP API key from
https://portal.cdp.coinbase.com/access/api and pastes the JSON file in. The
key name and its EC private key are stored encrypted; nothing else is kept.

Auth is a fresh short-lived JWT per request, signed with that private key.
Two details are easy to get wrong:

  * **ES256, not EdDSA.** Coinbase's general CDP guidance names Ed25519 for
    newer secret keys, but ``api.coinbase.com``'s retail v2 surface only
    accepts ES256 over the ECDSA P-256 key the portal hands out.
  * **The signed ``uri`` claim excludes the query string** — it is
    ``"<METHOD> <host><path>"`` and nothing more. Signing the query too would
    invalidate the token on every page of a cursor walk.

Only read-only keys are accepted: ``/api/v3/brokerage/key_permissions``
reports what the key can do, and one carrying trade or transfer permission is
refused at connect time rather than stored.

Prices come from Coinbase's own public exchange-rate table, never from the
equity quote source. Crypto tickers collide with listed companies — AMP, ACH,
PRO and VET are all real NYSE symbols as well as tokens — so asking a stock
API for "AMP" returns a confidently wrong number. Holdings are therefore
valued here, at sync time, and carry a price the equity source never sees.

No new dependency: ``jose`` signs the token, ``cryptography`` backs it, and
``httpx`` makes the calls — all three already ship with the app.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jose import jwt

from app.agents.services.crypto import decrypt, encrypt
from app.core.config import get_settings
from app.providers.base import (
    AccountData,
    BankProvider,
    ConnectionData,
    HoldingData,
    ProviderRateLimited,
    ProviderUserActionRequired,
    SessionExpiredError,
    TradeData,
    TransactionData,
    iso_currency as _iso_currency,
    normalize_ticker as _ticker,
    to_decimal as _to_decimal,
)
from app.providers.favicon import favicon_url_for

logger = logging.getLogger(__name__)

COINBASE_HTTP_TIMEOUT = 30.0
# Coinbase rejects tokens older than two minutes; mint one per request well
# inside that so clock skew on a self-hosted box can't expire it in flight.
TOKEN_LIFETIME_SECONDS = 110
ACCOUNTS_PATH = "/v2/accounts"
KEY_PERMISSIONS_PATH = "/api/v3/brokerage/key_permissions"
EXCHANGE_RATES_PATH = "/v2/exchange-rates"
TRANSACTIONS_PATH = "/v2/accounts/{account_id}/transactions"
# A cursor walk over a personal account is a few pages; the cap only exists so
# a server that keeps handing out fresh cursors can't spin forever.
MAX_PAGES = 100
# Coinbase's v2 maximum. The default is 25, and a long trading history walked
# 25 rows at a time is four times the requests for the same answer.
PAGE_LIMIT = 100
# One past the largest per-unit price `asset_transactions.price` — a
# NUMERIC(38, 18) — can hold.
MAX_LEDGER_PRICE = Decimal(10) ** 20
SPOT_PRICE_PATH = "/v2/prices/{pair}/spot"
COINBASE_HELP_URL = "https://portal.cdp.coinbase.com/access/api"

# What a transaction type means for a position, which is not the same as what
# it is called. Four classes, and only two of them ever reach a ledger:
#
#   trade     units move and the row states the money that moved for them.
#             Direction is the sign of the quantity, so a convert needs no
#             special case: Coinbase files it once in each wallet it touched,
#             and each leg's own sign says which way that asset went.
#   income    units arrive as payment rather than as a purchase. Income at
#             receipt, opening a lot at that value — booking it as free units
#             understates basis and overstates the eventual gain.
#   transfer  the same person's coins moving between their own wallets or
#             chains. Basis travels with them, so recording one would invent a
#             lot that never existed (the importer skips them for the same
#             reason, `asset_import_service._TRANSFER_WORDS`).
#   cash      fiat moving; no position changes.
#
# The list is the vendor's own enumeration, plus the reward types a real
# account emits that their table has never listed (`staking_reward`,
# `inflation_reward`, `interest`).
TX_TRADE = "trade"
TX_INCOME = "income"
TX_TRANSFER = "transfer"
TX_CASH = "cash"
TX_UNKNOWN = "unknown"

TX_CLASSES: dict[str, str] = {
    "buy": TX_TRADE,
    "sell": TX_TRADE,
    # On a real account this is the *common* disposal, not an exotic one: an
    # account that trades on the Advanced surface reports no `sell` at all.
    "advanced_trade_fill": TX_TRADE,
    "trade": TX_TRADE,
    "wrap_asset": TX_TRADE,
    "unwrap_asset": TX_TRADE,
    "retail_simple_dust": TX_TRADE,
    "fcm_futures_usdc_sell": TX_TRADE,
    "fcm_futures_usdc_sell_additional_encumberment_rollup": TX_TRADE,
    "earn_payout": TX_INCOME,
    "incentives_rewards_payout": TX_INCOME,
    "subscription_rebate": TX_INCOME,
    "staking_reward": TX_INCOME,
    "inflation_reward": TX_INCOME,
    "interest": TX_INCOME,
    "send": TX_TRANSFER,
    "receive": TX_TRANSFER,
    "request": TX_TRANSFER,
    "transfer": TX_TRANSFER,
    "staking_transfer": TX_TRANSFER,
    "unstaking_transfer": TX_TRANSFER,
    "intx_deposit": TX_TRANSFER,
    "intx_withdrawal": TX_TRANSFER,
    "exchange_deposit": TX_TRANSFER,
    "exchange_withdrawal": TX_TRANSFER,
    "pro_deposit": TX_TRANSFER,
    "pro_withdrawal": TX_TRANSFER,
    "vault_withdrawal": TX_TRANSFER,
    "unsupported_asset_recovery": TX_TRANSFER,
    # A clawback takes back units already paid out. It reverses an acquisition
    # rather than disposing of one, so booking it as a sell would realise a
    # gain on money the user never received.
    "clawback": TX_TRANSFER,
    "incentives_shared_clawback": TX_TRANSFER,
    "fiat_deposit": TX_CASH,
    "fiat_withdrawal": TX_CASH,
    "subscription": TX_CASH,
    "derivatives_settlement": TX_CASH,
    # Coinbase's own name for "uncategorized", so the one thing it must not be
    # read as is a trade.
    "tx": TX_UNKNOWN,
}
# The classes whose rows carry a basis, and so belong on the trade ledger.
LEDGER_TX_CLASSES = (TX_TRADE, TX_INCOME)
# Types for which "buy" or "sell" is already the whole truth, and a note on
# the ledger row would only repeat the kind.
PLAIN_TX_TYPES = ("buy", "sell", "advanced_trade_fill")


def classify_transaction(raw_type: Any) -> str:
    """What one transaction type means for a position.

    A type absent from the table is ``TX_UNKNOWN`` and reaches no ledger:
    Coinbase adds types over time, and the failure mode of assuming a new one
    is a trade is a wrong cost basis rather than a missing one.
    """
    return TX_CLASSES.get(str(raw_type or "").strip().lower(), TX_UNKNOWN)


@dataclass(frozen=True)
class _Movement:
    """One settled transaction, parsed but not yet valued."""

    external_id: str
    kind: str  # buy, sell
    quantity: Decimal
    asset_code: Optional[str]
    # USD total the row states for the movement, or None when it states none
    # this provider can use — which is what the spot backfill is for.
    fiat: Optional[Decimal]
    occurred_at: datetime


def _movement(raw: dict) -> Optional[_Movement]:
    """Parse one transaction into the movement it describes, or None.

    Direction is the sign of the quantity, never the type and never
    ``order_side``. An ``advanced_trade_fill`` is filed twice, once in each
    wallet the order moved, and both rows carry the *order's* side: buying
    HBAR with USDC writes +1867.7 to one wallet and -485.602 to the other, and
    both say ``order_side: buy``. Reading the side would book the funding leg
    as a purchase of the stablecoin it was paid in. The same rule is what
    makes a convert two sides without a special case.

    None is the ordinary answer for much of a history: anything unsettled is
    not yet a fact about the position — a pending buy can still fail, and a
    canceled one never happened — and a row with no id, no quantity or no
    timestamp states nothing that can be recorded.
    """
    if str(raw.get("status") or "").lower() != "completed":
        return None
    external_id = str(raw.get("id") or "")
    amount = raw.get("amount")
    if not external_id or not isinstance(amount, dict):
        return None
    quantity = _to_decimal(amount.get("amount"))
    if quantity is None or quantity == 0:
        return None
    when = _utc_timestamp(raw.get("created_at"))
    if when is None:
        return None
    native = raw.get("native_amount")
    native = native if isinstance(native, dict) else {}
    fiat = _to_decimal(native.get("amount"))
    # Holdings from this provider are recorded in USD, so a total in another
    # currency is a number that means something else. It reads as absent, and
    # the spot table supplies the USD value instead of the row being dropped.
    if fiat is not None and _iso_currency(native.get("currency")) != "USD":
        fiat = None
    return _Movement(
        external_id=external_id,
        kind="buy" if quantity > 0 else "sell",
        quantity=abs(quantity),
        asset_code=_ticker(amount.get("currency")),
        fiat=abs(fiat) if fiat is not None else None,
        occurred_at=when,
    )


def _trade_notes(tx_type: str, tx_class: str) -> Optional[str]:
    """What to record when "buy" or "sell" is not the whole truth.

    The ledger has two kinds, so a convert leg and a staking payout both land
    as one of them. The note is what keeps the difference legible — income at
    receipt is a tax event a purchase is not, and neither is a conversion the
    same thing as spending dollars.
    """
    if tx_class == TX_INCOME:
        return f"Coinbase {tx_type} — income at receipt"
    if tx_type in PLAIN_TX_TYPES:
        return None
    return f"Coinbase {tx_type}"


def _parse_api_key(raw: str) -> tuple[str, str]:
    """Pull the key name and PEM private key out of a pasted CDP key file.

    The portal downloads ``{"name": "organizations/../apiKeys/..",
    "privateKey": "-----BEGIN EC PRIVATE KEY-----.."}``; older exports use
    ``apiKeyName``/``privateKey``. Both are accepted, nothing else is.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        raise ValueError("Coinbase API key is empty")
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Coinbase API key must be the JSON file downloaded from the CDP portal"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError("Coinbase API key JSON must be an object")
    key_name = parsed.get("name") or parsed.get("apiKeyName")
    private_key = parsed.get("privateKey") or parsed.get("private_key")
    if not isinstance(key_name, str) or not key_name.strip():
        raise ValueError("Coinbase API key JSON is missing 'name'")
    if not isinstance(private_key, str) or "-----BEGIN" not in private_key:
        raise ValueError("Coinbase API key JSON is missing a PEM 'privateKey'")
    private_key = private_key.strip()
    # Prove the PEM is the ECDSA key ES256 needs while we can still answer
    # "bad key". Past this point a signing failure surfaces as an expired
    # session, which is the wrong thing to tell someone connecting for the
    # first time.
    try:
        loaded = serialization.load_pem_private_key(private_key.encode(), password=None)
    except (ValueError, TypeError) as exc:
        raise ValueError("Coinbase private key is not a readable PEM private key") from exc
    if not isinstance(loaded, ec.EllipticCurvePrivateKey):
        raise ValueError("Coinbase private key must be an ECDSA key, not RSA or Ed25519")
    return key_name.strip(), private_key


def _api_host() -> str:
    """Host the JWT claim is bound to, derived from the configured base URL."""
    return urlsplit(get_settings().coinbase_api_url).netloc or "api.coinbase.com"


def _sign_request(key_name: str, private_key: str, method: str, path: str) -> str:
    """Mint a request-scoped ES256 bearer token.

    The ``uri`` claim pins the token to one method, host and path. The query
    string is deliberately left out — Coinbase does not sign it, and folding
    it in would break paging, where only ``starting_after`` changes.
    """
    now = int(time.time())
    try:
        return jwt.encode(
            {
                "sub": key_name,
                "iss": "cdp",
                "nbf": now,
                "exp": now + TOKEN_LIFETIME_SECONDS,
                "uri": f"{method.upper()} {_api_host()}{path}",
            },
            private_key,
            algorithm="ES256",
            headers={"kid": key_name, "nonce": secrets.token_hex(16)},
        )
    except Exception as exc:  # noqa: BLE001 — jose raises a wide family here
        raise SessionExpiredError(
            f"Coinbase API key could not sign a request: {exc}"
        ) from exc


def _rows(payload: Any, key: str = "data") -> list[dict]:
    """Read a list of objects out of a payload, tolerating a malformed shape.

    Coinbase wraps everything in ``{"data": [...]}``, but an error page or a
    proxy can put a string or an object there instead. Non-dict entries are
    dropped rather than allowed to raise mid-walk.
    """
    if not isinstance(payload, dict):
        return []
    rows = payload.get(key)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _utc_timestamp(value: Any) -> Optional[datetime]:
    """A v2 timestamp in UTC, or None when it isn't one.

    UTC rather than the user's own zone: it is what Coinbase states, and an
    instant shifted into a zone we would be guessing at dates a trade to the
    wrong day on the few hours a year the two disagree.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class CoinbaseProvider(BankProvider):
    """Coinbase retail connector — one exchange account holding many positions."""

    @property
    def name(self) -> str:
        return "coinbase"

    @property
    def flow_type(self) -> str:
        return "token"

    # ----- credentials -------------------------------------------------------

    @staticmethod
    def _key_pair(credentials: dict) -> tuple[str, str]:
        creds = credentials or {}
        key_name = creds.get("key_name") or ""
        private_key = decrypt(creds.get("private_key_enc")) or creds.get("private_key") or ""
        if not key_name or not private_key:
            raise SessionExpiredError("Coinbase API key is missing")
        return key_name, private_key

    # ----- HTTP --------------------------------------------------------------

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=get_settings().coinbase_api_url.rstrip("/"),
            timeout=COINBASE_HTTP_TIMEOUT,
            headers={
                "Accept": "application/json",
                "User-Agent": "Securo/0.1 (+https://usesecuro.com)",
            },
        )

    async def _get(
        self,
        path: str,
        *,
        credentials: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """One signed (or public) GET, with Coinbase's failure modes mapped.

        ``credentials=None`` is the public path — the exchange-rate table
        needs no key, so we don't mint a token for it.
        """
        headers: dict[str, str] = {}
        if credentials is not None:
            key_name, private_key = self._key_pair(credentials)
            headers["Authorization"] = f"Bearer {_sign_request(key_name, private_key, 'GET', path)}"
        async with await self._client() as client:
            resp = await client.get(path, params=params, headers=headers)
        if resp.status_code in (401, 403):
            raise ProviderUserActionRequired(
                f"Coinbase refused the request ({resp.status_code}). The API key may have "
                "been revoked — create a new read-only key and reconnect.",
                code="credentials_invalid",
                help_url=COINBASE_HELP_URL,
            )
        if resp.status_code == 429:
            raise ProviderRateLimited("Coinbase rate-limited the request")
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"Coinbase returned a non-JSON response for {path}") from exc
        return payload if isinstance(payload, dict) else {}

    # ----- connection flow ---------------------------------------------------

    def get_oauth_url(self, *args, **kwargs):  # type: ignore[override]
        raise NotImplementedError("Coinbase uses paste-a-key flow, not OAuth redirect")

    async def _assert_read_only(self, credentials: dict) -> str:
        """Reject a key that can do anything but look, and return its portfolio id.

        A connection exists to read balances. A key that can trade or move
        funds hands the app authority it never needs, and the user cannot tell
        from the pasted file which kind they downloaded — so we ask Coinbase
        and say plainly what is wrong when the answer is the wrong one.
        """
        payload = await self._get(KEY_PERMISSIONS_PATH, credentials=credentials)
        granted = [
            label
            for label, key in (("trade", "can_trade"), ("transfer", "can_transfer"))
            if payload.get(key)
        ]
        if granted:
            raise ProviderUserActionRequired(
                "This Coinbase API key grants "
                + " and ".join(granted)
                + " permission. Securo only reads balances — create a View-only key "
                "and paste that one instead.",
                code="credentials_not_read_only",
                help_url=COINBASE_HELP_URL,
            )
        if not payload.get("can_view"):
            raise ProviderUserActionRequired(
                "This Coinbase API key cannot view balances. Create a key with View "
                "permission and paste that one instead.",
                code="credentials_not_read_only",
                help_url=COINBASE_HELP_URL,
            )
        return str(payload.get("portfolio_uuid") or "")

    async def handle_oauth_callback(self, code: str) -> ConnectionData:
        """Turn a pasted CDP key file into a connection.

        Named for the OAuth flow it shares an endpoint with; the contract
        ("given an opaque code, produce a ConnectionData") fits the
        paste-a-token providers unchanged.
        """
        key_name, private_key = _parse_api_key(code)
        credentials: dict[str, Any] = {
            "key_name": key_name,
            "private_key_enc": encrypt(private_key) or private_key,
            "connected_at": datetime.now(timezone.utc).isoformat(),
        }
        portfolio_uuid = await self._assert_read_only(credentials)
        accounts = await self.get_accounts(credentials)
        return ConnectionData(
            external_id=portfolio_uuid or f"coinbase-{key_name.rsplit('/', 1)[-1]}",
            institution_name="Coinbase",
            credentials=credentials,
            accounts=accounts,
            logo_url=favicon_url_for("https://www.coinbase.com"),
        )

    async def refresh_credentials(self, credentials: dict) -> dict:
        self._key_pair(credentials)
        return credentials

    # ----- reads -------------------------------------------------------------

    async def _walk(
        self, path: str, credentials: dict, *, partial_ok: bool = False
    ) -> list[dict]:
        """Every row of a cursor-paged v2 collection, to exhaustion.

        An error part-way through propagates rather than returning what
        arrived first, and so does running out of pages unless ``partial_ok``
        says a prefix is usable — a truncated account list costs a wallet that
        reappears next sync, a truncated history costs a wrong cost basis.

        The cursor is tracked so a server that keeps handing back one it
        already gave stops the walk instead of looping on it.
        """
        rows: list[dict] = []
        seen_cursors: set[str] = set()
        params: dict[str, str] = {"limit": str(PAGE_LIMIT)}
        for _ in range(MAX_PAGES):
            payload = await self._get(path, credentials=credentials, params=params)
            rows.extend(_rows(payload))
            pagination = payload.get("pagination")
            cursor = pagination.get("next_starting_after") if isinstance(pagination, dict) else None
            cursor = str(cursor) if cursor else ""
            if not cursor or cursor in seen_cursors:
                return rows
            seen_cursors.add(cursor)
            params = {"limit": str(PAGE_LIMIT), "starting_after": cursor}
        if partial_ok:
            logger.warning("Coinbase walk of %s hit the %d-page cap", path, MAX_PAGES)
            return rows
        raise RuntimeError(
            f"Coinbase still had pages of {path} after {MAX_PAGES}; refusing to "
            "report a partial history"
        )

    async def _walk_accounts(self, credentials: dict) -> list[dict]:
        """Every Coinbase account, following the cursor to exhaustion.

        Zero-balance wallets are kept: Coinbase mints one per asset the user
        has ever touched, and ``get_trades`` walks each one's history — a
        position sold to nothing still has the trades that got it there.

        A cap hit here yields the accounts that did arrive: a missing wallet
        is a holding that shows up next sync, not a wrong number.
        """
        return await self._walk(ACCOUNTS_PATH, credentials, partial_ok=True)

    @staticmethod
    def _balance(raw: dict) -> dict:
        balance = raw.get("balance")
        return balance if isinstance(balance, dict) else {}

    @staticmethod
    def _is_fiat(raw: dict) -> bool:
        currency = raw.get("currency")
        return isinstance(currency, dict) and currency.get("type") == "fiat"

    @staticmethod
    def _asset_code(raw: dict) -> Optional[str]:
        currency = raw.get("currency")
        if isinstance(currency, dict):
            return _ticker(currency.get("code"))
        return _ticker(currency)

    @staticmethod
    def _portfolio_id(raw_accounts: list[dict]) -> str:
        for raw in raw_accounts:
            portfolio = raw.get("portfolio_id")
            if isinstance(portfolio, str) and portfolio:
                return portfolio
        return "portfolio"

    async def get_accounts(self, credentials: dict) -> list[AccountData]:
        """The exchange account, plus one account per fiat wallet.

        Coinbase mints a "wallet" per asset the user has ever touched, but
        those are positions, not accounts — "Crypto is an asset class, not a
        kind of Account, because one exchange account can hold spot,
        stablecoins and staking positions at once" (CONTEXT.md). Mapping each
        coin to its own Account would mint a Wallet per coin, and tax
        character attaches to a Wallet, so the user would be asked to classify
        the same exchange account dozens of times.

        So every crypto wallet folds into one investment account carrying no
        cash balance — its value arrives as holdings, and counting it here too
        would double it in net worth. A fiat wallet is the opposite: it is
        cash, it is genuinely its own account, and it has no holding.
        """
        raw_accounts = await self._walk_accounts(credentials)
        identified = [raw for raw in raw_accounts if str(raw.get("id") or "")]
        accounts: list[AccountData] = []
        if any(not self._is_fiat(raw) for raw in identified):
            accounts.append(
                AccountData(
                    external_id=self._portfolio_id(identified),
                    name="Coinbase",
                    type="investment",
                    balance=Decimal("0"),
                    currency="USD",
                    has_holdings=True,
                )
            )
        for raw in identified:
            if not self._is_fiat(raw):
                continue
            balance = self._balance(raw)
            accounts.append(
                AccountData(
                    external_id=str(raw["id"]),
                    name=raw.get("name") or self._asset_code(raw) or "Coinbase Wallet",
                    type="cash",
                    balance=_to_decimal(balance.get("amount")) or Decimal("0"),
                    currency=_iso_currency(balance.get("currency")) or "USD",
                    has_holdings=False,
                )
            )
        return accounts

    async def _usd_prices(self) -> dict[str, Decimal]:
        """USD spot price per asset code, from Coinbase's own rate table.

        One unauthenticated request covers every asset Coinbase lists, which
        is both cheaper than a spot call per wallet and the reason this
        provider never has to ask an equity API what "AMP" is worth. The
        endpoint reports how much of each asset one USD buys, so the price is
        its reciprocal.
        """
        payload = await self._get(EXCHANGE_RATES_PATH, params={"currency": "USD"})
        data = payload.get("data")
        rates = data.get("rates") if isinstance(data, dict) else None
        if not isinstance(rates, dict):
            logger.warning("Coinbase exchange-rate payload carried no rates")
            return {}
        prices: dict[str, Decimal] = {}
        for code, raw_rate in rates.items():
            rate = _to_decimal(raw_rate)
            if rate is None or not isinstance(code, str):
                continue
            try:
                prices[code.upper()] = Decimal(1) / rate
            except (DivisionByZero, InvalidOperation):
                continue
        return prices

    async def get_holdings(self, credentials: dict) -> list[HoldingData]:
        """One holding per crypto wallet, valued in USD at sync time.

        All of them are attributed to the single exchange account
        ``get_accounts`` reports, so they land in one Wallet with one tax
        character. The wallet's own id stays the holding's ``external_id``:
        it is the stable upsert key, and the handle ``get_trades`` fetches
        per-account transaction history by.

        A wallet whose asset Coinbase cannot price is skipped rather than
        recorded at an unknown value — except an empty one, which is worth
        zero whatever the price turns out to be, and is kept so a
        fully-sold position stays on the books instead of being archived.
        """
        raw_accounts = await self._walk_accounts(credentials)
        prices = await self._usd_prices()
        portfolio_id = self._portfolio_id(
            [raw for raw in raw_accounts if str(raw.get("id") or "")]
        )
        holdings: list[HoldingData] = []
        for raw in raw_accounts:
            account_id = str(raw.get("id") or "")
            if not account_id or self._is_fiat(raw):
                continue
            code = self._asset_code(raw)
            quantity = _to_decimal(self._balance(raw).get("amount"))
            if code is None or quantity is None:
                continue
            price = prices.get(code)
            if price is None and quantity != 0:
                logger.warning("Coinbase has no USD price for %s; skipping holding", code)
                continue
            holdings.append(
                HoldingData(
                    external_id=account_id,
                    name=raw.get("name") or code,
                    currency="USD",
                    ticker=code,
                    quantity=quantity,
                    unit_price=price,
                    current_value=quantity * price if price is not None else Decimal("0"),
                    # No purchase_price or purchase_date: the balance endpoint
                    # reports neither. Real cost basis reaches the ledger via
                    # `get_trades`, not from a guess made here.
                    account_external_id=portfolio_id,
                    metadata={"asset_code": code, "account_type": raw.get("type")},
                )
            )
        return holdings

    async def _spot_price(
        self,
        code: str,
        day: date,
        cache: dict[tuple[str, date], Optional[Decimal]],
    ) -> Optional[Decimal]:
        """USD price of one unit of ``code`` on ``day``, or None.

        The spot table is public, so a backfill costs a request and no key.
        Answers are memoized for the walk because a daily staking payout asks
        the same question once per row.

        Coinbase answers 404 both for an asset it never listed and for a date
        outside the window it keeps — measured at roughly the last three years,
        so a 2023 reward is already outside it. That is None rather than an
        error: one unvalued row must not take down a sync that is otherwise
        fine.
        """
        key = (code, day)
        if key in cache:
            return cache[key]
        try:
            payload = await self._get(
                SPOT_PRICE_PATH.format(pair=f"{code}-USD"),
                params={"date": day.isoformat()},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            logger.info("Coinbase has no %s spot price for %s", code, day)
            payload = {}
        data = payload.get("data")
        price = _to_decimal(data.get("amount")) if isinstance(data, dict) else None
        cache[key] = price if price is not None and price > 0 else None
        return cache[key]

    async def _to_trade(
        self,
        movement: _Movement,
        holding_external_id: str,
        tx_type: str,
        tx_class: str,
        spot: dict[tuple[str, date], Optional[Decimal]],
    ) -> Optional[TradeData]:
        """Value one movement and turn it into a ledger entry, or None.

        The stated fiat total is the basis wherever there is one: it includes
        the fee, which is what cost basis means. Where there is not — a reward
        Coinbase valued at nothing, a row denominated in a currency this
        provider cannot write into a USD basis — the day's spot price stands
        in, which is the vendor's own number for that date rather than a guess
        made here.
        """
        price = (
            movement.fiat / movement.quantity
            if movement.fiat is not None and movement.fiat > 0
            else None
        )
        if price is None and movement.asset_code:
            price = await self._spot_price(
                movement.asset_code, movement.occurred_at.date(), spot
            )
        if price is None and tx_class == TX_INCOME:
            # Units that arrived as payment and that nothing could value cost
            # nothing, so the whole eventual disposal is gain. Zero is the
            # honest basis; dropping the row would lose the units themselves.
            price = Decimal("0")
        if price is None:
            logger.info(
                "Coinbase transaction %s (%s) states no usable USD value and none "
                "could be backfilled; skipping",
                movement.external_id,
                tx_type,
            )
            return None
        # `asset_transactions.price` is NUMERIC(38, 18). A dust quantity
        # against a whole-dollar total can produce more integer digits than
        # that holds, and the write would fail late — after the accounts,
        # balances and transactions of this sync were already staged.
        if price >= MAX_LEDGER_PRICE:
            logger.warning(
                "Coinbase transaction %s prices %s at %s, past what the ledger "
                "stores; skipping",
                movement.external_id,
                movement.asset_code,
                price,
            )
            return None
        return TradeData(
            external_id=movement.external_id,
            holding_external_id=holding_external_id,
            kind=movement.kind,
            price=price,
            quantity=movement.quantity,
            occurred_at=movement.occurred_at,
            notes=_trade_notes(tx_type, tx_class),
        )

    async def get_trades(self, credentials: dict) -> list[TradeData]:
        """Everything that moved a holding's basis, one wallet's history at a time.

        Coinbase files a transaction under the wallet it moved, and that
        wallet's id is the holding's ``external_id`` (see ``get_holdings``),
        so each walk lands on exactly one ledger with no matching to do. A
        convert therefore arrives as two rows in two walks, and each is
        recorded against its own holding — the one asset sold, the other
        bought — rather than as a single trade of the pair.

        Zero-balance wallets are walked like any other: a position sold down
        to nothing still has the buys and the sell that got it there, and
        those are the whole cost-basis story for a realised gain.

        Fiat wallets carry no holding to write to, so they are skipped.

        Transfers reach no ledger by design (see ``TX_CLASSES``), so a wallet
        fed by one still replays short of its balance and stays a Snapshot —
        `_ledger_reconciles` is what keeps that from being read as a sale.
        """
        trades: list[TradeData] = []
        unknown_types: Counter[str] = Counter()
        spot: dict[tuple[str, date], Optional[Decimal]] = {}
        unvalued = 0
        for raw_account in await self._walk_accounts(credentials):
            account_id = str(raw_account.get("id") or "")
            if not account_id or self._is_fiat(raw_account):
                continue
            rows = await self._walk(
                TRANSACTIONS_PATH.format(account_id=account_id), credentials
            )
            for row in rows:
                tx_type = str(row.get("type") or "").strip().lower()
                tx_class = classify_transaction(tx_type)
                if tx_class == TX_UNKNOWN:
                    unknown_types[tx_type or "(none)"] += 1
                if tx_class not in LEDGER_TX_CLASSES:
                    continue
                movement = _movement(row)
                if movement is None:
                    continue
                trade = await self._to_trade(
                    movement, account_id, tx_type, tx_class, spot
                )
                if trade is None:
                    unvalued += 1
                    continue
                trades.append(trade)
        if unknown_types:
            # Reported rather than guessed at: Coinbase adds types over time,
            # and one this build has never seen is a gap to close, not a trade.
            logger.warning(
                "Coinbase reported transaction types this build does not classify, "
                "so they reached no ledger: %s",
                ", ".join(f"{name} x{count}" for name, count in sorted(unknown_types.items())),
            )
        if unvalued:
            logger.warning(
                "Coinbase reported %d transactions that could not be valued in USD; "
                "the holdings they belong to keep the reported quantity and no "
                "derived basis",
                unvalued,
            )
        return trades

    async def get_transactions(
        self,
        credentials: dict,
        account_external_id: str,
        since: Optional[date] = None,
        payee_source: str = "auto",
    ) -> list[TransactionData]:
        """No cash transactions.

        Coinbase's per-account transactions are buys, sells and transfers of
        an asset, not movements of money in a cash account. They belong in the
        trade ledger, which ``get_trades`` fills; importing them here would
        double-count every position against its own holding.
        """
        return []
