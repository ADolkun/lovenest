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
# The range `asset_transactions` can hold: `price` and `quantity` are both
# NUMERIC(38, 18). Past the top the write fails, and late — after this sync's
# accounts, balances and transactions are already staged. Below the bottom
# Postgres rounds to zero, which is worse than failing: a row worth nothing
# keeps the replay's quantity exact, and quantity is all `_ledger_reconciles`
# checks, so the basis is cached silently short by that row's whole value.
MAX_LEDGER_VALUE = Decimal(10) ** 20
MIN_LEDGER_PRICE = Decimal(10) ** -18
# Past this many historical prices in one walk, the backfill stops asking. It
# is a rate-limit guard: nothing caches a price across syncs, so a history
# that wants a thousand serial lookups wants them again on the next sync and
# every one after.
MAX_SPOT_LOOKUPS = 250
SPOT_PRICE_PATH = "/v2/prices/{pair}/spot"
COINBASE_HELP_URL = "https://portal.cdp.coinbase.com/access/api"

# What a transaction type means for a position, which is not the same as what
# it is called. Four classes, and only two of them ever reach a ledger:
#
#   trade       units move and the row states the money that moved for them.
#               Direction is the sign of the quantity, so a convert needs no
#               special case: Coinbase files it once in each wallet it
#               touched, and each leg's own sign says which way that asset
#               went.
#   income      units arrive as payment rather than as a purchase. Income at
#               receipt, opening a lot at that value — booking it as free
#               units understates basis and overstates the eventual gain.
#   unrecorded  the row moves units, or money, in a way this ledger cannot
#               state a basis for: a transfer between the user's own wallets,
#               which carries its basis with it; a send to someone else,
#               which Coinbase files under the same type and which no field
#               here distinguishes; a fiat movement, which is not a position
#               at all; a clawback, which rescinds income rather than selling
#               it.
#   unknown     a type this build has never classified, reported by name.
#
# Recognising a row and declining to record it is not free, and the cost is
# not one row: the replay then disagrees with the reported balance, and
# `_ledger_reconciles` withholds the derived basis of the *whole holding*.
# That is the price of every "unrecorded" and every "unknown" below, and it is
# why a type gets classified as a trade wherever its shape is plain, even when
# its tax treatment is arguable.
#
# The list is the vendor's own enumeration, plus the reward types a real
# account emits that the current reference does not list (`staking_reward`,
# `inflation_reward`, `interest`) and the pre-Advanced transfer names.
TX_TRADE = "trade"
TX_INCOME = "income"
TX_UNRECORDED = "unrecorded"
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
    # Stablecoin conversions at par: whatever their tax character, the gain
    # they can carry is rounding-sized, and leaving them unrecorded would
    # cost the USDC wallet its basis entirely.
    "fcm_futures_usdc_sell": TX_TRADE,
    "fcm_futures_usdc_sell_additional_encumberment_rollup": TX_TRADE,
    "retail_simple_dust": TX_TRADE,
    "earn_payout": TX_INCOME,
    "incentives_rewards_payout": TX_INCOME,
    "subscription_rebate": TX_INCOME,
    "staking_reward": TX_INCOME,
    "inflation_reward": TX_INCOME,
    "interest": TX_INCOME,
    "send": TX_UNRECORDED,
    "receive": TX_UNRECORDED,
    "request": TX_UNRECORDED,
    "transfer": TX_UNRECORDED,
    "staking_transfer": TX_UNRECORDED,
    "unstaking_transfer": TX_UNRECORDED,
    "intx_deposit": TX_UNRECORDED,
    "intx_withdrawal": TX_UNRECORDED,
    "exchange_deposit": TX_UNRECORDED,
    "exchange_withdrawal": TX_UNRECORDED,
    "pro_deposit": TX_UNRECORDED,
    "pro_withdrawal": TX_UNRECORDED,
    "vault_withdrawal": TX_UNRECORDED,
    "unsupported_asset_recovery": TX_UNRECORDED,
    # A clawback takes back units already paid out. Booked as a sell it would
    # be priced from its own row — the value on the day it landed, not the day
    # the payout did — and a clawback of a year-old reward would book that
    # year of appreciation as a realised gain on units nobody sold.
    "clawback": TX_UNRECORDED,
    "incentives_shared_clawback": TX_UNRECORDED,
    "fiat_deposit": TX_UNRECORDED,
    "fiat_withdrawal": TX_UNRECORDED,
    "subscription": TX_UNRECORDED,
    "derivatives_settlement": TX_UNRECORDED,
    # Coinbase's own name for "uncategorized", so the one thing it must not be
    # read as is a trade.
    "tx": TX_UNKNOWN,
}


def _classify_transaction(raw_type: Any) -> str:
    """What one transaction type means for a position.

    A type absent from the table is ``TX_UNKNOWN``, which reaches no ledger
    and is reported by name: Coinbase adds types over time, and a new one
    assumed to be a trade is a wrong cost basis rather than a missing one.
    """
    return TX_CLASSES.get(str(raw_type or "").strip().lower(), TX_UNKNOWN)


def _trade_notes(tx_type: str, tx_class: str) -> Optional[str]:
    """The vendor's own word for the row, where "buy" or "sell" loses it.

    See ``TradeData.notes``. A plain buy or sell needs none: the kind already
    says everything the type does.
    """
    if tx_class == TX_INCOME:
        return f"Coinbase {tx_type} — income at receipt"
    if tx_type in ("buy", "sell", "advanced_trade_fill"):
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
        """What one unit of ``code`` was worth in USD on ``day``, or None.

        The spot table is public, so a backfill costs a request and no key.

        Coinbase answers 404 both for an asset it never listed and for a date
        outside the window it keeps: checked on 2026-08-24, no asset priced
        before mid-2023. Past ``MAX_SPOT_LOOKUPS`` the answer is None without
        asking. Both are the same None as any other — the row it belongs to
        reaches no ledger — because refusing the whole history instead would
        throw away every correctly-priced row with it, and skip the recompute
        `_sync_trades` runs on every sync, leaving a holding whose quantity is
        today's and whose basis is the last sync's.
        """
        key = (code, day)
        if key in cache:
            return cache[key]
        if len(cache) >= MAX_SPOT_LOOKUPS:
            return None
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

    async def _unit_price(
        self,
        raw: dict,
        quantity: Decimal,
        code: Optional[str],
        when: datetime,
        tx_class: str,
        spot: dict[tuple[str, date], Optional[Decimal]],
    ) -> Optional[Decimal]:
        """What one unit cost in USD on this row, or None if nothing can say.

        ``native_amount`` is the fiat total including the fee, which is what
        cost basis means. The docs only call it "the value in the user's
        native currency", so this was checked against a real account: on every
        buy carrying a fee it equalled the linked buy resource's ``total``,
        never its ``subtotal``. A fill's ``commission`` is deliberately not
        added on top — it reads like an uncharged notional, since the two legs
        of a fill balance to the stablecoin conversion alone, which they could
        not do if a commission that size had really been deducted.

        A total in another currency is not a USD basis, and the spot table is
        not a substitute for one: repricing a stated total at the day's price
        would throw away the price the order actually filled at, and on a day
        an asset moved twenty percent that is thousands of dollars of realised
        gain — with the quantity untouched, so nothing downstream would
        notice. The backfill is therefore for income only, where there is no
        execution price to lose: a reward is worth what the asset was worth
        when it landed, which is exactly what the spot table answers.
        """
        native = raw.get("native_amount")
        native = native if isinstance(native, dict) else {}
        fiat = _to_decimal(native.get("amount"))
        # A stated zero states nothing, whatever currency it is denominated
        # in, so it reads as absent rather than as a free acquisition.
        if fiat == 0:
            fiat = None
        if fiat is not None:
            if _iso_currency(native.get("currency")) != "USD":
                logger.info(
                    "Coinbase transaction %s is priced in %s, not the holding's "
                    "currency; skipping",
                    raw.get("id"),
                    native.get("currency"),
                )
                return None
            return abs(fiat) / quantity
        if tx_class == TX_INCOME and code:
            return await self._spot_price(code, when.date(), spot)
        return None

    async def _trade(
        self,
        raw: dict,
        holding_external_id: str,
        tx_type: str,
        tx_class: str,
        spot: dict[tuple[str, date], Optional[Decimal]],
    ) -> Optional[TradeData]:
        """Map one classified transaction to a ledger entry, or None.

        Direction is the sign of the quantity, never the type and never
        ``order_side``. An ``advanced_trade_fill`` is filed twice, once in each
        wallet the order moved, and both rows carry the *order's* side: buying
        HBAR with USDC writes +1867.7 to one wallet and -485.602 to the other,
        and both say ``order_side: buy``. Reading the side would book the
        funding leg as a purchase of the stablecoin it was paid in (ADR 0007).
        The same rule is what makes a convert two sides with no special case.

        None is the ordinary answer for much of a history, and it is always
        the safe one: a row that cannot be stated in USD reaches no ledger,
        which leaves the replay short and the whole Holding a Snapshot — a
        missing basis, which the user can see, rather than a wrong one that
        reconciles (ADR 0008).
        """
        # Anything unsettled is not yet a fact about the position — a pending
        # buy can still fail, and a canceled one never happened.
        if str(raw.get("status") or "").lower() != "completed":
            return None
        external_id = str(raw.get("id") or "")
        amount = raw.get("amount")
        if not external_id or not isinstance(amount, dict):
            return None
        quantity = _to_decimal(amount.get("amount"))
        if quantity is None or quantity == 0:
            return None
        # Income arriving negative is a payout being reversed, not a disposal.
        # Booking it as a sell would realise a gain on units the user was never
        # paid for, on a row stamped "income at receipt" while it did so.
        if tx_class == TX_INCOME and quantity < 0:
            logger.info(
                "Coinbase %s %s reverses a payout rather than paying one; skipping",
                tx_type,
                external_id,
            )
            return None
        when = _utc_timestamp(raw.get("created_at"))
        if when is None:
            return None
        kind = "buy" if quantity > 0 else "sell"
        quantity = abs(quantity)
        code = self._asset_code(amount)
        price = await self._unit_price(raw, quantity, code, when, tx_class, spot)
        if (
            price is None
            or not MIN_LEDGER_PRICE <= price < MAX_LEDGER_VALUE
            or quantity >= MAX_LEDGER_VALUE
        ):
            logger.info(
                "Coinbase %s %s states %s %s at %s, which the ledger cannot hold; "
                "skipping",
                tx_type,
                external_id,
                quantity,
                code or "units",
                price,
            )
            return None
        return TradeData(
            external_id=external_id,
            holding_external_id=holding_external_id,
            kind=kind,
            price=price,
            quantity=quantity,
            occurred_at=when,
            notes=_trade_notes(tx_type, tx_class),
        )

    async def get_trades(self, credentials: dict) -> list[TradeData]:
        """Everything that moved a holding's basis, one wallet's history at a time.

        Coinbase files a transaction under the wallet it moved, and that
        wallet's id is the holding's ``external_id`` (see ``get_holdings``),
        so each walk lands on exactly one ledger with no matching to do. A
        convert therefore arrives as two rows in two walks, and each is
        recorded against its own holding — one asset sold, the other bought —
        rather than as a single trade of the pair.

        Zero-balance wallets are walked like any other: a position sold down
        to nothing still has the buys and the sell that got it there, and
        those are the whole cost-basis story for a realised gain.

        Fiat wallets carry no holding to write to, so they are skipped.
        """
        trades: list[TradeData] = []
        unknown_types: Counter[str] = Counter()
        spot: dict[tuple[str, date], Optional[Decimal]] = {}
        left_off = 0
        for raw_account in await self._walk_accounts(credentials):
            account_id = str(raw_account.get("id") or "")
            if not account_id or self._is_fiat(raw_account):
                continue
            rows = await self._walk(
                TRANSACTIONS_PATH.format(account_id=account_id), credentials
            )
            for row in rows:
                tx_type = str(row.get("type") or "").strip().lower()
                tx_class = _classify_transaction(tx_type)
                if tx_class == TX_UNKNOWN:
                    unknown_types[tx_type or "(none)"] += 1
                if tx_class not in (TX_TRADE, TX_INCOME):
                    continue
                trade = await self._trade(row, account_id, tx_type, tx_class, spot)
                if trade is None:
                    left_off += 1
                    continue
                trades.append(trade)
        if unknown_types:
            logger.warning(
                "Coinbase reported transaction types this build does not classify, "
                "so they reached no ledger: %s",
                ", ".join(f"{name} x{count}" for name, count in sorted(unknown_types.items())),
            )
        if left_off:
            # Once, with a count: the per-row INFO lines are where *which* rows
            # live. This is the number that explains a holding still carrying no
            # derived basis after a sync that reported no error at all.
            logger.warning(
                "Coinbase left %d ledgerable transactions off the ledger — "
                "unsettled, or with no USD value this build could state; the "
                "holdings they belong to keep the reported quantity and no "
                "derived basis",
                left_off,
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
