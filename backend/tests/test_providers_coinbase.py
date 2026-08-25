"""Unit tests for the Coinbase provider.

Everything is fakeable: ``httpx.MockTransport`` serves inline payloads and the
signing key is generated per-run by ``cryptography``. No Coinbase credential
appears anywhere in this file and nothing touches the network.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jose import jwt

from app.agents.services.crypto import encrypt
from app.providers.base import (
    ProviderRateLimited,
    ProviderUserActionRequired,
    SessionExpiredError,
)
from app.providers.coinbase import CoinbaseProvider, _parse_api_key, _rows

KEY_NAME = "organizations/00000000-0000-0000-0000-000000000000/apiKeys/test-key"
PORTFOLIO_ID = "portfolio-1"


def _generate_key() -> tuple[str, str]:
    """A throwaway P-256 keypair as (private PEM, public PEM)."""
    private = ec.generate_private_key(ec.SECP256R1())
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _key_file(private_pem: str) -> str:
    return json.dumps({"name": KEY_NAME, "privateKey": private_pem})


def _credentials(private_pem: str) -> dict:
    """Credentials in the shape `handle_oauth_callback` actually stores."""
    return {"key_name": KEY_NAME, "private_key_enc": encrypt(private_pem)}


def _patched_client(handler):
    """Point CoinbaseProvider._client at a MockTransport."""

    transport = httpx.MockTransport(handler)

    async def fake_client(self):  # noqa: ANN001
        return httpx.AsyncClient(
            transport=transport, base_url="https://api.coinbase.com", timeout=30
        )

    return patch.object(CoinbaseProvider, "_client", fake_client)


def _account(
    account_id: str,
    code: str,
    amount: str,
    *,
    name: str | None = None,
    fiat: bool = False,
) -> dict:
    return {
        "id": account_id,
        "name": name or f"{code} Wallet",
        "type": "fiat" if fiat else "wallet",
        "balance": {"amount": amount, "currency": code},
        "currency": {"code": code, "type": "fiat" if fiat else "crypto"},
        "portfolio_id": PORTFOLIO_ID,
    }


def _page(accounts: list[dict], next_cursor: str | None = None) -> dict:
    return {"data": accounts, "pagination": {"next_starting_after": next_cursor}}


READ_ONLY_PERMISSIONS = {
    "can_view": True,
    "can_trade": False,
    "can_transfer": False,
    "portfolio_uuid": "portfolio-1",
}

RATES = {
    "data": {"currency": "USD", "rates": {"AMP": "2500", "XRP": "2", "ADA": "4", "USD": "1.0"}}
}


def _routing_handler(
    pages: list[dict],
    *,
    permissions: dict | None = None,
    rates: dict | None = None,
):
    """Serve /v2/accounts pages in order, plus permissions and rate lookups."""
    state = {"page": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/brokerage/key_permissions":
            return httpx.Response(200, json=permissions or READ_ONLY_PERMISSIONS)
        if path == "/v2/exchange-rates":
            return httpx.Response(200, json=rates if rates is not None else RATES)
        assert path == "/v2/accounts"
        index = state["page"]
        # A restart of the walk (get_accounts, then get_holdings) replays page 1.
        if request.url.params.get("starting_after") is None:
            index = 0
        state["page"] = index + 1
        return httpx.Response(200, json=pages[index])

    return handler


# ----- credential parsing -----------------------------------------------------


def test_parse_api_key_reads_portal_download():
    private_pem, _ = _generate_key()
    assert _parse_api_key(_key_file(private_pem)) == (KEY_NAME, private_pem.strip())


def test_parse_api_key_accepts_legacy_field_names():
    private_pem, _ = _generate_key()
    raw = json.dumps({"apiKeyName": KEY_NAME, "private_key": private_pem})
    assert _parse_api_key(raw)[0] == KEY_NAME


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not json at all",
        "[1, 2, 3]",
        json.dumps({"privateKey": "-----BEGIN EC PRIVATE KEY-----x"}),
        json.dumps({"name": KEY_NAME}),
        json.dumps({"name": KEY_NAME, "privateKey": "not-a-pem"}),
    ],
)
def test_parse_api_key_rejects_malformed_input(raw):
    with pytest.raises(ValueError):
        _parse_api_key(raw)


def test_rows_tolerates_malformed_payloads():
    assert _rows("nope") == []
    assert _rows({"data": "nope"}) == []
    assert _rows({"data": [{"id": "a"}, "junk", None]}) == [{"id": "a"}]


def test_missing_credentials_raise_session_expired():
    with pytest.raises(SessionExpiredError):
        CoinbaseProvider._key_pair({})
    with pytest.raises(SessionExpiredError):
        CoinbaseProvider._key_pair({"key_name": KEY_NAME})


def test_plaintext_key_is_still_readable():
    """Mirrors the write path: `encrypt` returns None without a usable secret
    key, and the connect flow then stores the PEM as-is."""
    private_pem, _ = _generate_key()
    assert CoinbaseProvider._key_pair(
        {"key_name": KEY_NAME, "private_key": private_pem}
    ) == (KEY_NAME, private_pem)


def test_parse_api_key_rejects_a_non_ecdsa_key():
    """ES256 needs an EC key; an RSA PEM parses but could never sign."""
    rsa_pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(ValueError, match="ECDSA"):
        _parse_api_key(json.dumps({"name": KEY_NAME, "privateKey": rsa_pem}))


# ----- request signing --------------------------------------------------------


@pytest.mark.asyncio
async def test_token_claim_covers_method_host_and_path_without_query():
    private_pem, public_pem = _generate_key()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers["Authorization"].removeprefix("Bearer ")
        captured["claims"] = jwt.decode(
            token,
            public_pem,
            algorithms=["ES256"],
            options={"verify_aud": False},
        )
        captured["header"] = jwt.get_unverified_header(token)
        return httpx.Response(200, json=_page([]))

    provider = CoinbaseProvider()
    with _patched_client(handler):
        await provider.get_accounts(_credentials(private_pem))

    assert captured["claims"]["uri"] == "GET api.coinbase.com/v2/accounts"
    assert captured["claims"]["sub"] == KEY_NAME
    assert captured["claims"]["iss"] == "cdp"
    assert captured["claims"]["exp"] > captured["claims"]["nbf"]
    assert captured["header"]["alg"] == "ES256"
    assert captured["header"]["kid"] == KEY_NAME
    assert captured["header"]["nonce"]


@pytest.mark.asyncio
async def test_signed_uri_stays_stable_across_paged_requests():
    """The query string is excluded, so every page signs the same claim."""
    private_pem, public_pem = _generate_key()
    uris: list[str] = []
    pages = [
        _page([_account("a1", "XRP", "1")], next_cursor="a1"),
        _page([_account("a2", "AMP", "2")]),
    ]
    inner = _routing_handler(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("Authorization")
        if authorization:
            claims = jwt.decode(
                authorization.removeprefix("Bearer "),
                public_pem,
                algorithms=["ES256"],
                options={"verify_aud": False},
            )
            uris.append(claims["uri"])
        return inner(request)

    provider = CoinbaseProvider()
    with _patched_client(handler):
        holdings = await provider.get_holdings(_credentials(private_pem))

    assert [h.external_id for h in holdings] == ["a1", "a2"]
    assert uris == ["GET api.coinbase.com/v2/accounts"] * 2


@pytest.mark.asyncio
async def test_public_price_request_is_unsigned():
    private_pem, _ = _generate_key()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/exchange-rates":
            seen["authorization"] = request.headers.get("Authorization")
            return httpx.Response(200, json=RATES)
        return httpx.Response(200, json=_page([_account("a1", "XRP", "1")]))

    provider = CoinbaseProvider()
    with _patched_client(handler):
        await provider.get_holdings(_credentials(private_pem))

    assert seen["authorization"] is None


# ----- read-only enforcement --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("permissions", "expected_fragment"),
    [
        ({"can_view": True, "can_trade": True, "can_transfer": False}, "trade"),
        ({"can_view": True, "can_trade": False, "can_transfer": True}, "transfer"),
        ({"can_view": True, "can_trade": True, "can_transfer": True}, "trade and transfer"),
        ({"can_view": False, "can_trade": False, "can_transfer": False}, "cannot view"),
    ],
)
async def test_key_without_read_only_scope_is_rejected(permissions, expected_fragment):
    private_pem, _ = _generate_key()
    handler = _routing_handler([_page([])], permissions=permissions)

    provider = CoinbaseProvider()
    with _patched_client(handler):
        with pytest.raises(ProviderUserActionRequired) as exc:
            await provider.handle_oauth_callback(_key_file(private_pem))

    assert exc.value.code == "credentials_not_read_only"
    assert expected_fragment in str(exc.value)


@pytest.mark.asyncio
async def test_handle_oauth_callback_stores_key_encrypted_and_lists_accounts():
    private_pem, _ = _generate_key()
    handler = _routing_handler([_page([_account("a1", "XRP", "77.5")])])

    provider = CoinbaseProvider()
    with _patched_client(handler):
        connection = await provider.handle_oauth_callback(_key_file(private_pem))

    assert connection.external_id == PORTFOLIO_ID
    assert connection.institution_name == "Coinbase"
    assert [a.external_id for a in connection.accounts] == [PORTFOLIO_ID]
    # The PEM is never stored in the clear, and never under a plaintext key.
    assert connection.credentials["key_name"] == KEY_NAME
    assert "private_key" not in connection.credentials
    assert private_pem not in connection.credentials["private_key_enc"]
    assert CoinbaseProvider._key_pair(connection.credentials)[1] == private_pem.strip()


# ----- accounts ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_account_list_yields_no_accounts_or_holdings():
    private_pem, _ = _generate_key()
    handler = _routing_handler([_page([])])

    provider = CoinbaseProvider()
    with _patched_client(handler):
        assert await provider.get_accounts(_credentials(private_pem)) == []
        assert await provider.get_holdings(_credentials(private_pem)) == []


@pytest.mark.asyncio
async def test_zero_balance_accounts_are_enumerated_not_skipped():
    private_pem, _ = _generate_key()
    pages = [_page([_account("a1", "XRP", "0.00000000"), _account("a2", "AMP", "5")])]

    provider = CoinbaseProvider()
    with _patched_client(_routing_handler(pages)):
        accounts = await provider.get_accounts(_credentials(private_pem))
        holdings = await provider.get_holdings(_credentials(private_pem))

    assert [a.external_id for a in accounts] == [PORTFOLIO_ID]
    assert [h.external_id for h in holdings] == ["a1", "a2"]
    empty = next(h for h in holdings if h.external_id == "a1")
    assert empty.quantity == Decimal("0.00000000")
    assert empty.current_value == Decimal("0")


@pytest.mark.asyncio
async def test_many_coins_collapse_into_one_exchange_account():
    """CONTEXT.md: crypto is an asset class, not a kind of Account.

    Tax character attaches to a Wallet and a Wallet is minted per provider
    account, so one row per coin would ask the user to classify the same
    exchange account once per token they have ever held.
    """
    private_pem, _ = _generate_key()
    pages = [
        _page(
            [
                _account("a1", "XRP", "77.5"),
                _account("a2", "ADA", "10"),
                _account("a3", "AMP", "0"),
                _account("a4", "USD", "12.34", name="Cash (USD)", fiat=True),
            ]
        )
    ]

    provider = CoinbaseProvider()
    with _patched_client(_routing_handler(pages)):
        accounts = await provider.get_accounts(_credentials(private_pem))
        holdings = await provider.get_holdings(_credentials(private_pem))

    exchange, fiat = accounts
    assert exchange.external_id == PORTFOLIO_ID
    assert exchange.name == "Coinbase"
    # The positions arrive as holdings; counting them here too would double them.
    assert (exchange.type, exchange.balance, exchange.currency) == (
        "investment",
        Decimal("0"),
        "USD",
    )
    assert exchange.has_holdings is True
    # A fiat wallet is genuinely its own cash account, and has no holding.
    assert (fiat.external_id, fiat.type, fiat.balance, fiat.currency) == (
        "a4",
        "cash",
        Decimal("12.34"),
        "USD",
    )
    assert fiat.has_holdings is False
    assert {h.external_id for h in holdings} == {"a1", "a2", "a3"}
    assert {h.account_external_id for h in holdings} == {PORTFOLIO_ID}


@pytest.mark.asyncio
async def test_every_holding_is_attributable_to_a_listed_account():
    """What the allowlist gate needs to be able to filter on.

    ``_sync_holdings`` keeps a holding only when its ``account_external_id``
    is in the set ``_syncable_accounts`` produced from ``get_accounts``, so an
    unattributed holding would be dropped for every connection with an
    allowlist configured.
    """
    private_pem, _ = _generate_key()
    pages = [
        _page([_account("a1", "XRP", "1"), _account("a2", "AMP", "0")], next_cursor="a2"),
        _page([_account("a3", "USD", "5", fiat=True)]),
    ]

    provider = CoinbaseProvider()
    with _patched_client(_routing_handler(pages)):
        accounts = await provider.get_accounts(_credentials(private_pem))
        holdings = await provider.get_holdings(_credentials(private_pem))

    listed = {a.external_id for a in accounts}
    # The exchange account, plus the one fiat wallet — not one row per coin.
    assert listed == {PORTFOLIO_ID, "a3"}
    assert {h.account_external_id for h in holdings} == {PORTFOLIO_ID}
    assert {h.external_id for h in holdings} == {"a1", "a2"}


@pytest.mark.asyncio
async def test_long_asset_code_does_not_leak_into_the_currency_column():
    private_pem, _ = _generate_key()
    pages = [_page([_account("a1", "CORECHAIN", "3.5")])]
    rates = {"data": {"rates": {"CORECHAIN": "50"}}}

    provider = CoinbaseProvider()
    with _patched_client(_routing_handler(pages, rates=rates)):
        accounts = await provider.get_accounts(_credentials(private_pem))
        holdings = await provider.get_holdings(_credentials(private_pem))

    assert accounts[0].currency == "USD"
    assert holdings[0].currency == "USD"
    assert holdings[0].ticker == "CORECHAIN"


@pytest.mark.asyncio
async def test_unusable_stored_key_reads_as_an_expired_session():
    """A rotated SECRET_KEY leaves an undecryptable PEM behind."""
    provider = CoinbaseProvider()
    with pytest.raises(SessionExpiredError):
        await provider.get_accounts(
            {"key_name": KEY_NAME, "private_key": "-----BEGIN EC PRIVATE KEY-----\ngarbage\n"}
        )


@pytest.mark.asyncio
async def test_account_walk_gives_up_at_the_page_cap():
    private_pem, _ = _generate_key()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/exchange-rates":
            return httpx.Response(200, json=RATES)
        calls["n"] += 1
        return httpx.Response(
            200,
            json=_page([_account(f"a{calls['n']}", "XRP", "1")], next_cursor=f"c{calls['n']}"),
        )

    provider = CoinbaseProvider()
    with patch("app.providers.coinbase.MAX_PAGES", 3), _patched_client(handler):
        holdings = await provider.get_holdings(_credentials(private_pem))

    assert calls["n"] == 3
    assert len(holdings) == 3


@pytest.mark.asyncio
async def test_account_walk_stops_on_a_repeated_cursor():
    private_pem, _ = _generate_key()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/exchange-rates":
            return httpx.Response(200, json=RATES)
        calls["n"] += 1
        return httpx.Response(
            200, json=_page([_account(f"a{calls['n']}", "XRP", "1")], next_cursor="stuck")
        )

    provider = CoinbaseProvider()
    with _patched_client(handler):
        holdings = await provider.get_holdings(_credentials(private_pem))

    assert calls["n"] == 2
    assert len(holdings) == 2


# ----- pricing ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_colliding_ticker_is_priced_from_the_crypto_source():
    """AMP is both a token and Ameriprise Financial (NYSE, ~$500/share).

    Coinbase's own rate table is the only source consulted, so the holding is
    worth its crypto price and cannot pick up the equity's.
    """
    private_pem, _ = _generate_key()
    pages = [_page([_account("a1", "AMP", "1000")])]
    # 2500 AMP per USD → $0.0004 each → 1000 units is $0.40, not $500,000.
    provider = CoinbaseProvider()
    with _patched_client(_routing_handler(pages)):
        holdings = await provider.get_holdings(_credentials(private_pem))

    assert holdings[0].ticker == "AMP"
    assert holdings[0].unit_price == Decimal("0.0004")
    assert holdings[0].current_value == Decimal("0.4")


@pytest.mark.asyncio
async def test_unpriced_asset_is_skipped_unless_the_wallet_is_empty():
    private_pem, _ = _generate_key()
    pages = [_page([_account("a1", "XRP", "10"), _account("a2", "NOPRICE", "0")])]
    rates = {"data": {"rates": {"XRP": "2"}}}

    provider = CoinbaseProvider()
    with _patched_client(_routing_handler(pages, rates=rates)):
        holdings = await provider.get_holdings(_credentials(private_pem))

    assert [h.external_id for h in holdings] == ["a1", "a2"]
    assert holdings[0].current_value == Decimal("5")
    assert holdings[1].current_value == Decimal("0")


@pytest.mark.asyncio
async def test_unpriced_asset_with_a_balance_is_dropped():
    private_pem, _ = _generate_key()
    pages = [_page([_account("a1", "NOPRICE", "10")])]

    provider = CoinbaseProvider()
    with _patched_client(_routing_handler(pages, rates={"data": {"rates": {}}})):
        assert await provider.get_holdings(_credentials(private_pem)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rates",
    [
        {"data": {"rates": {"XRP": "0"}}},
        {"data": {"rates": {"XRP": "not-a-number"}}},
        {"data": {"rates": "malformed"}},
        {"data": "malformed"},
        {},
    ],
)
async def test_malformed_rate_payloads_drop_the_holding_rather_than_raise(rates):
    private_pem, _ = _generate_key()
    pages = [_page([_account("a1", "XRP", "10")])]

    provider = CoinbaseProvider()
    with _patched_client(_routing_handler(pages, rates=rates)):
        assert await provider.get_holdings(_credentials(private_pem)) == []


# ----- error statuses ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_ask_the_user_to_reconnect(status):
    private_pem, _ = _generate_key()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"errors": [{"id": "unauthorized"}]})

    provider = CoinbaseProvider()
    with _patched_client(handler):
        with pytest.raises(ProviderUserActionRequired) as exc:
            await provider.get_accounts(_credentials(private_pem))

    assert exc.value.code == "credentials_invalid"


@pytest.mark.asyncio
async def test_rate_limit_surfaces_as_provider_rate_limited():
    private_pem, _ = _generate_key()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"errors": [{"id": "rate_limit_exceeded"}]})

    provider = CoinbaseProvider()
    with _patched_client(handler):
        with pytest.raises(ProviderRateLimited):
            await provider.get_accounts(_credentials(private_pem))


@pytest.mark.asyncio
async def test_server_error_propagates():
    private_pem, _ = _generate_key()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    provider = CoinbaseProvider()
    with _patched_client(handler):
        with pytest.raises(httpx.HTTPStatusError):
            await provider.get_accounts(_credentials(private_pem))


@pytest.mark.asyncio
async def test_non_json_success_response_is_reported():
    private_pem, _ = _generate_key()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    provider = CoinbaseProvider()
    with _patched_client(handler):
        with pytest.raises(RuntimeError, match="non-JSON"):
            await provider.get_accounts(_credentials(private_pem))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": {"not": "a list"}},
        {"data": [{"name": "no id"}], "pagination": "malformed"},
        {"data": [{"id": "a1", "balance": "malformed", "currency": "malformed"}]},
    ],
)
async def test_malformed_account_payloads_do_not_raise(payload):
    private_pem, _ = _generate_key()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/exchange-rates":
            return httpx.Response(200, json=RATES)
        return httpx.Response(200, json=payload)

    provider = CoinbaseProvider()
    with _patched_client(handler):
        await provider.get_accounts(_credentials(private_pem))
        await provider.get_holdings(_credentials(private_pem))


# ----- trade history ----------------------------------------------------------


def _transaction(
    tx_id: str,
    tx_type: str,
    quantity: str,
    fiat: str,
    *,
    code: str = "XRP",
    status: str = "completed",
    created_at: str = "2024-03-04T18:30:00Z",
    native_currency: str = "USD",
) -> dict:
    return {
        "id": tx_id,
        "type": tx_type,
        "status": status,
        "amount": {"amount": quantity, "currency": code},
        "native_amount": {"amount": fiat, "currency": native_currency},
        "created_at": created_at,
    }


def _history_handler(
    accounts: list[dict],
    history: dict[str, list[dict]],
    *,
    fail_after: int | None = None,
):
    """Serve one page of accounts, then each account's paged transactions.

    `history` maps an account id to the pages of its history, so a walk can be
    made to run one page or five. `fail_after` makes the Nth transaction
    request return 500, standing in for a network drop mid-pagination.
    """
    seen = {"tx_requests": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/exchange-rates":
            return httpx.Response(200, json=RATES)
        if path == "/v2/accounts":
            return httpx.Response(200, json=_page(accounts))
        account_id = path.split("/")[3]
        seen["tx_requests"] += 1
        if fail_after is not None and seen["tx_requests"] > fail_after:
            return httpx.Response(500, json={"errors": [{"id": "internal"}]})
        pages = history.get(account_id, [{"data": [], "pagination": {}}])
        cursor = request.url.params.get("starting_after")
        index = 0 if cursor is None else int(cursor)
        return httpx.Response(200, json=pages[index])

    return handler


def _tx_page(rows: list[dict], next_cursor: str | None = None) -> dict:
    return {"data": rows, "pagination": {"next_starting_after": next_cursor}}


@pytest.mark.asyncio
async def test_buys_and_sells_become_trades_with_a_derived_unit_price():
    private_pem, _ = _generate_key()
    accounts = [_account("a1", "XRP", "10")]
    history = {
        "a1": [
            _tx_page(
                [
                    _transaction("tx-buy", "buy", "20", "50.00"),
                    _transaction("tx-sell", "sell", "-5", "-20.00",
                                 created_at="2025-01-02T00:15:00Z"),
                ]
            )
        ]
    }

    provider = CoinbaseProvider()
    with _patched_client(_history_handler(accounts, history)):
        trades = await provider.get_trades(_credentials(private_pem))

    assert [(t.external_id, t.kind) for t in trades] == [("tx-buy", "buy"), ("tx-sell", "sell")]
    buy, sell = trades
    assert buy.holding_external_id == "a1"
    assert (buy.quantity, buy.price, buy.occurred_at) == (
        Decimal("20"), Decimal("2.5"),
        datetime(2024, 3, 4, 18, 30, tzinfo=timezone.utc),
    )
    # A sell reports both sides negative; the ledger stores magnitudes.
    assert (sell.quantity, sell.price, sell.occurred_at) == (
        Decimal("5"), Decimal("4"),
        datetime(2025, 1, 2, 0, 15, tzinfo=timezone.utc),
    )


def _fill(
    tx_id: str,
    quantity: str,
    fiat: str,
    *,
    code: str = "HBAR",
    product: str = "HBAR-USDC",
    order_side: str = "buy",
    commission: str = "0",
    created_at: str = "2024-12-19T19:51:18Z",
) -> dict:
    """One leg of an Advanced Trade fill, in the shape the v2 API returns."""
    return {
        "id": tx_id,
        "type": "advanced_trade_fill",
        "status": "completed",
        "amount": {"amount": quantity, "currency": code},
        "native_amount": {"amount": fiat, "currency": "USD"},
        "created_at": created_at,
        "advanced_trade_fill": {
            "commission": commission,
            "fill_price": "0.26",
            "order_id": "order-1",
            "order_side": order_side,
            "product_id": product,
        },
    }


@pytest.mark.asyncio
async def test_an_advanced_trade_fill_is_a_trade():
    """The surface an account that actually trades reports every disposal on."""
    private_pem, _ = _generate_key()
    accounts = [_account("a1", "HBAR", "1867.7")]
    history = {"a1": [_tx_page([_fill("f-1", "1867.7", "486.37")])]}

    provider = CoinbaseProvider()
    with _patched_client(_history_handler(accounts, history)):
        trades = await provider.get_trades(_credentials(private_pem))

    assert len(trades) == 1
    assert trades[0].kind == "buy"
    assert trades[0].quantity == Decimal("1867.7")


@pytest.mark.asyncio
async def test_both_legs_of_a_fill_are_read_by_sign_not_by_order_side():
    """A fill is filed in both wallets it moved, and both say `order_side: buy`.

    Buying HBAR with USDC writes +1867.7 to the HBAR wallet and -485.602 to the
    USDC wallet. Reading `order_side` would book the second as a *purchase* of
    USDC — inflating a stablecoin position by the size of every trade ever made
    through it.
    """
    private_pem, _ = _generate_key()
    accounts = [_account("a1", "HBAR", "1867.7"), _account("a2", "USDC", "500")]
    history = {
        "a1": [_tx_page([_fill("f-1", "1867.7", "486.37")])],
        "a2": [
            _tx_page(
                [_fill("f-2", "-485.602", "-485.60", code="USDC", order_side="buy")]
            )
        ],
    }

    provider = CoinbaseProvider()
    with _patched_client(_history_handler(accounts, history)):
        trades = await provider.get_trades(_credentials(private_pem))

    by_id = {t.external_id: t for t in trades}
    assert by_id["f-1"].kind == "buy"
    assert by_id["f-1"].holding_external_id == "a1"
    assert by_id["f-2"].kind == "sell"
    assert by_id["f-2"].holding_external_id == "a2"
    assert by_id["f-2"].quantity == Decimal("485.602")


@pytest.mark.asyncio
async def test_a_sell_side_fill_disposes_of_the_base_asset():
    private_pem, _ = _generate_key()
    accounts = [_account("a1", "ADA", "100")]
    history = {
        "a1": [
            _tx_page(
                [
                    _fill(
                        "f-1", "-505.21499099", "-388.89",
                        code="ADA", product="ADA-USD", order_side="sell",
                    )
                ]
            )
        ]
    }

    provider = CoinbaseProvider()
    with _patched_client(_history_handler(accounts, history)):
        trades = await provider.get_trades(_credentials(private_pem))

    assert trades[0].kind == "sell"
    assert trades[0].quantity == Decimal("505.21499099")


@pytest.mark.asyncio
async def test_a_fill_commission_is_not_added_to_the_basis():
    """The legs balance to the stablecoin conversion, so nothing was deducted."""
    private_pem, _ = _generate_key()
    accounts = [_account("a1", "HBAR", "1867.7")]
    history = {
        "a1": [_tx_page([_fill("f-1", "1867.7", "486.37", commission="13.9610575")])]
    }

    provider = CoinbaseProvider()
    with _patched_client(_history_handler(accounts, history)):
        trades = await provider.get_trades(_credentials(private_pem))

    assert trades[0].price == Decimal("486.37") / Decimal("1867.7")


@pytest.mark.asyncio
async def test_history_is_walked_to_cursor_exhaustion():
    private_pem, _ = _generate_key()
    accounts = [_account("a1", "XRP", "10")]
    history = {
        "a1": [
            _tx_page([_transaction("tx-1", "buy", "1", "2")], next_cursor="1"),
            _tx_page([_transaction("tx-2", "buy", "1", "3")], next_cursor="2"),
            _tx_page([_transaction("tx-3", "buy", "1", "4")]),
        ]
    }

    provider = CoinbaseProvider()
    with _patched_client(_history_handler(accounts, history)):
        trades = await provider.get_trades(_credentials(private_pem))

    assert [t.external_id for t in trades] == ["tx-1", "tx-2", "tx-3"]


@pytest.mark.asyncio
async def test_a_single_page_history_needs_no_second_request():
    private_pem, _ = _generate_key()
    accounts = [_account("a1", "XRP", "10")]
    handler = _history_handler(
        accounts, {"a1": [_tx_page([_transaction("tx-1", "buy", "1", "2")])]}
    )
    requests: list[str] = []

    def counting(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return handler(request)

    provider = CoinbaseProvider()
    with _patched_client(counting):
        trades = await provider.get_trades(_credentials(private_pem))

    assert [t.external_id for t in trades] == ["tx-1"]
    assert requests.count("/v2/accounts/a1/transactions") == 1


@pytest.mark.asyncio
async def test_an_empty_history_yields_no_trades():
    private_pem, _ = _generate_key()
    accounts = [_account("a1", "XRP", "0")]

    provider = CoinbaseProvider()
    with _patched_client(_history_handler(accounts, {"a1": [_tx_page([])]})):
        assert await provider.get_trades(_credentials(private_pem)) == []


@pytest.mark.asyncio
async def test_an_error_mid_pagination_raises_rather_than_truncating():
    """Half a history reads as a whole one, so the walk must not return it."""
    private_pem, _ = _generate_key()
    accounts = [_account("a1", "XRP", "10")]
    history = {
        "a1": [
            _tx_page([_transaction("tx-1", "buy", "1", "2")], next_cursor="1"),
            _tx_page([_transaction("tx-2", "buy", "1", "3")]),
        ]
    }

    provider = CoinbaseProvider()
    with _patched_client(_history_handler(accounts, history, fail_after=1)):
        with pytest.raises(httpx.HTTPStatusError):
            await provider.get_trades(_credentials(private_pem))


@pytest.mark.asyncio
async def test_a_history_longer_than_the_page_cap_raises_rather_than_truncating():
    """A prefix of someone's trading is not a smaller cost basis, it is a wrong one."""
    private_pem, _ = _generate_key()
    accounts = [_account("a1", "XRP", "10")]

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/accounts":
            return httpx.Response(200, json=_page(accounts))
        calls["n"] += 1
        return httpx.Response(
            200,
            json=_tx_page(
                [_transaction(f"tx-{calls['n']}", "buy", "1", "2")],
                next_cursor=f"c{calls['n']}",
            ),
        )

    provider = CoinbaseProvider()
    with patch("app.providers.coinbase.MAX_PAGES", 3), _patched_client(handler):
        with pytest.raises(RuntimeError, match="partial history"):
            await provider.get_trades(_credentials(private_pem))


@pytest.mark.asyncio
async def test_zero_balance_wallets_still_give_up_their_history():
    """A position sold to nothing is exactly where realised gain comes from."""
    private_pem, _ = _generate_key()
    accounts = [_account("a1", "XRP", "0")]
    history = {"a1": [_tx_page([_transaction("tx-1", "buy", "1", "2")])]}

    provider = CoinbaseProvider()
    with _patched_client(_history_handler(accounts, history)):
        trades = await provider.get_trades(_credentials(private_pem))

    assert [t.external_id for t in trades] == ["tx-1"]


@pytest.mark.asyncio
async def test_fiat_wallet_history_is_not_a_trade():
    private_pem, _ = _generate_key()
    accounts = [_account("cash", "USD", "500", fiat=True)]
    history = {"cash": [_tx_page([_transaction("tx-1", "buy", "1", "2", code="USD")])]}

    provider = CoinbaseProvider()
    with _patched_client(_history_handler(accounts, history)):
        assert await provider.get_trades(_credentials(private_pem)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        # issue #70's types, not this ticket's.
        _transaction("tx", "send", "1", "2"),
        _transaction("tx", "interest", "1", "2"),
        _transaction("tx", "earn_payout", "1", "2"),
        _transaction("tx", "receive", "1", "2"),
        _transaction("tx", "trade", "1", "2"),
        _transaction("tx", "staking_reward", "1", "2"),
        # Not settled: a pending buy can still fail, a canceled one never was.
        _transaction("tx", "buy", "1", "2", status="pending"),
        _transaction("tx", "buy", "1", "2", status="canceled"),
        # A basis in another currency would be a number written into a column
        # that means something else.
        _transaction("tx", "buy", "1", "2", native_currency="EUR"),
        # Nothing to divide by, nothing to identify it, nothing to date it.
        _transaction("tx", "buy", "0", "2"),
        _transaction("", "buy", "1", "2"),
        _transaction("tx", "buy", "1", "2", created_at="not a timestamp"),
        # A dust quantity against a whole-dollar total prices past what
        # NUMERIC(38, 18) holds; the write would fail after the rest of the
        # sync was already staged.
        _transaction("tx", "buy", "0.00000000000000000001", "1000"),
        {"id": "tx", "type": "buy", "status": "completed"},
        {"id": "tx", "type": "buy", "status": "completed",
         "amount": "nope", "native_amount": "nope"},
        _transaction("tx", "buy", "not a number", "2"),
        _transaction("tx", "buy", "1", "not a number"),
    ],
)
async def test_rows_that_are_not_a_priced_buy_or_sell_are_skipped(row):
    private_pem, _ = _generate_key()
    accounts = [_account("a1", "XRP", "10")]

    provider = CoinbaseProvider()
    with _patched_client(_history_handler(accounts, {"a1": [_tx_page([row])]})):
        assert await provider.get_trades(_credentials(private_pem)) == []


@pytest.mark.asyncio
async def test_a_local_timestamp_is_converted_to_utc():
    """19:30 in Los Angeles is the next day in UTC, and Coinbase says so."""
    private_pem, _ = _generate_key()
    accounts = [_account("a1", "XRP", "10")]
    history = {
        "a1": [
            _tx_page(
                [_transaction("tx-1", "buy", "1", "2", created_at="2024-03-04T19:30:00-08:00")]
            )
        ]
    }

    provider = CoinbaseProvider()
    with _patched_client(_history_handler(accounts, history)):
        trades = await provider.get_trades(_credentials(private_pem))

    assert trades[0].occurred_at == datetime(2024, 3, 5, 3, 30, tzinfo=timezone.utc)


# ----- misc contract ----------------------------------------------------------


@pytest.mark.asyncio
async def test_transactions_are_left_to_the_trade_ledger():
    private_pem, _ = _generate_key()
    assert await CoinbaseProvider().get_transactions(_credentials(private_pem), "a1") == []


@pytest.mark.asyncio
async def test_refresh_credentials_validates_and_passes_through():
    private_pem, _ = _generate_key()
    credentials = _credentials(private_pem)
    assert await CoinbaseProvider().refresh_credentials(credentials) is credentials
    with pytest.raises(SessionExpiredError):
        await CoinbaseProvider().refresh_credentials({"key_name": KEY_NAME})


def test_provider_identity():
    provider = CoinbaseProvider()
    assert (provider.name, provider.flow_type) == ("coinbase", "token")
    with pytest.raises(NotImplementedError):
        provider.get_oauth_url("https://example.test", "state")
