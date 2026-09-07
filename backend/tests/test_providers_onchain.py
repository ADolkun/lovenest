"""On-chain watch-only wallets and the transfer trace.

Every chain node is faked through httpx.MockTransport, so nothing here
touches the network and no address in this file is anybody's wallet.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.providers import onchain
from app.providers.base import (
    ProviderNotConfiguredError,
    ProviderRateLimited,
    ProviderUserActionRequired,
)
from app.providers.onchain import CHAINS, OnChainProvider, parse_addresses
from app.services import onchain_trace

SOL = CHAINS["solana"]
BASE = CHAINS["base"]

# Base58, 32-44 chars — shaped like real Solana addresses, owned by nobody.
A = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
B = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
C = "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
D = "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"
EVM = "0x" + "ab" * 20

JAN23 = 1737673208  # 2025-01-23T23:00:08Z


@contextmanager
def _patched_client(handler):
    """Point the module's shared client factory at a MockTransport."""
    from app.providers import onchain_transport

    transport = httpx.MockTransport(handler)
    coordination = AsyncMock()
    coordination.eval.side_effect = lambda script, *args: (
        [1, 0] if script == onchain_transport._ACQUIRE_SCRIPT else 0
    )

    def fake_client():
        return httpx.AsyncClient(transport=transport, timeout=5)

    # These existing tests exercise payloads, not distributed coordination.
    # Real Redis/cross-process behavior is covered in test_onchain_rpc.py.
    with (
        patch.object(onchain, "_client", fake_client),
        patch.object(onchain_transport, "_redis_client", lambda: coordination),
    ):
        yield


def _settings(**overrides):
    base = {
        "onchain_rpc_urls": {},
        "etherscan_api_key": "",
        "coinbase_api_url": "https://api.coinbase.com",
    }
    base.update(overrides)
    return patch.object(onchain, "get_settings", lambda: SimpleNamespace(**base))


def _sig(signature: str, block_time: int | None, err=None) -> dict:
    return {"signature": signature, "blockTime": block_time, "err": err, "slot": 1}


def _tx(block_time: int, deltas: dict[str, int]) -> dict:
    """A Solana transaction whose only evidence is its balance deltas."""
    keys = list(deltas)
    return {
        "blockTime": block_time,
        "meta": {
            "err": None,
            "preBalances": [10**12] * len(keys),
            "postBalances": [10**12 + deltas[k] for k in keys],
        },
        "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in keys]}},
    }


def _solana_handler(*, balances=None, signatures=None, txs=None, token_accounts=None):
    """Serve getBalance / getSignaturesForAddress / getTransaction from dicts.

    Token reads answer empty by default — a wallet holding no SPL tokens — so a
    test about native balances does not have to describe a token index too.
    """
    balances = balances or {}
    signatures = signatures or {}
    txs = txs or {}
    token_accounts = token_accounts or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "jup.ag" in request.url.host:
            return httpx.Response(200, json=[])
        body = json.loads(request.content)
        if body["method"] == "getTokenAccountsByOwner":
            return httpx.Response(
                200, json={"result": {"value": token_accounts.get(body["params"][0], [])}, "id": 1}
            )
        method, params = body["method"], body["params"]
        if method == "getBalance":
            return httpx.Response(
                200, json={"result": {"value": balances.get(params[0], 0)}, "id": 1}
            )
        if method == "getSignaturesForAddress":
            rows = sorted(signatures.get(params[0], []), key=lambda row: row["blockTime"], reverse=True)
            cursor = params[1].get("before")
            start = next((i + 1 for i, row in enumerate(rows) if row["signature"] == cursor), 0)
            return httpx.Response(200, json={"result": rows[start:start + params[1]["limit"]], "id": 1})
        if method == "getTransaction":
            return httpx.Response(200, json={"result": txs.get(params[0]), "id": 1})
        raise AssertionError(f"unexpected method {method}")

    return handler


# ----- credential parsing ---------------------------------------------------


def test_a_bare_solana_address_infers_its_chain():
    [watched] = parse_addresses(A)
    assert watched.chain.key == "solana"
    assert watched.address == A


def test_a_bare_evm_address_defaults_to_ethereum_because_the_bytes_are_ambiguous():
    [watched] = parse_addresses(EVM)
    assert watched.chain.key == "ethereum"


def test_a_chain_prefix_overrides_the_guess():
    [watched] = parse_addresses(f"base:{EVM}")
    assert watched.chain.key == "base"


def test_evm_addresses_are_lowercased_so_checksum_casing_is_not_a_second_wallet():
    [lower] = parse_addresses(EVM.lower())
    [upper] = parse_addresses("0x" + "AB" * 20)
    assert lower.external_id == upper.external_id


def test_repeating_an_address_is_a_typo_not_a_second_wallet():
    assert len(parse_addresses(f"{A}\n{A}\nsolana:{A}")) == 1


def test_comments_and_blank_lines_are_ignored():
    watched = parse_addresses(f"# my wallets\n\n{A}  # phantom\n")
    assert [w.address for w in watched] == [A]


@pytest.mark.parametrize(
    ("blob", "code"),
    [
        ("", "onchain_no_addresses"),
        ("   \n\n", "onchain_no_addresses"),
        ("not-an-address", "onchain_bad_address"),
        (f"dogecoin:{A}", "onchain_unknown_chain"),
        (f"solana:{EVM}", "onchain_bad_address"),
    ],
)
def test_an_unusable_blob_names_what_is_wrong_with_it(blob, code):
    with pytest.raises(ProviderUserActionRequired) as exc:
        parse_addresses(blob)
    assert exc.value.code == code


BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def test_a_connection_is_capped_at_a_reviewable_number_of_addresses():
    blob = "\n".join(
        f"{BASE58[i]}{A[1:]}" for i in range(onchain.MAX_WATCHED_ADDRESSES + 5)
    )
    with pytest.raises(ProviderUserActionRequired) as exc:
        parse_addresses(blob)
    assert exc.value.code == "onchain_too_many_addresses"


# ----- balances -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_solana_balance_is_reported_in_whole_sol_not_lamports():
    with _settings(), _patched_client(_solana_handler(balances={A: 7_045_392})):
        assert await onchain.native_balance(SOL, A) == Decimal("0.007045392")


@pytest.mark.asyncio
async def test_an_evm_balance_is_decoded_from_hex_wei():
    def handler(request):
        assert json.loads(request.content)["method"] == "eth_getBalance"
        return httpx.Response(200, json={"result": "0xde0b6b3a7640000", "id": 1})

    with _settings(), _patched_client(handler):
        assert await onchain.native_balance(BASE, EVM) == Decimal("1")


@pytest.fixture
def _no_backoff():
    """Keep the retry logic, drop the wall-clock it would otherwise spend."""
    with patch.object(onchain, "RPC_RETRY_BACKOFF_SECONDS", 0):
        yield


@pytest.mark.asyncio
async def test_a_momentary_throttle_is_ridden_out_rather_than_failing_the_trace(_no_backoff):
    attempts: list[int] = []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"result": {"value": 1_000_000_000}, "id": 1})

    with _settings(), _patched_client(handler):
        assert await onchain.native_balance(SOL, A) == Decimal("1")
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_a_node_that_stays_throttled_raises_rather_than_reading_as_an_empty_wallet(
    _no_backoff,
):
    with _settings(), _patched_client(lambda request: httpx.Response(429)):
        with pytest.raises(ProviderRateLimited):
            await onchain.native_balance(SOL, A)


@pytest.mark.asyncio
async def test_an_rpc_error_payload_is_not_mistaken_for_a_result():
    def handler(request):
        return httpx.Response(200, json={"error": {"code": -32602, "message": "bad"}, "id": 1})

    with _settings(), _patched_client(handler):
        with pytest.raises(RuntimeError):
            await onchain.native_balance(SOL, A)


# ----- Solana transfers -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_transfer_is_attributed_to_the_counterparty_whose_movement_matches():
    handler = _solana_handler(
        signatures={A: [_sig("sig1", JAN23)]},
        # A loses 10 SOL; B gains it; C also loses (a fee payer). Only B is
        # a plausible recipient — pairing A with C would invent a transfer.
        txs={"sig1": _tx(JAN23, {A: -10_000_000_000, B: 9_999_995_000, C: -5_000})},
    )
    with _settings(), _patched_client(handler):
        page = await onchain.transfers(SOL, A, limit=25)
    [transfer] = page.items
    assert (transfer.sender, transfer.recipient) == (A, B)
    assert transfer.amount == Decimal("10")
    assert transfer.direction(A) == "out"
    assert transfer.counterparty(A) == B


@pytest.mark.asyncio
async def test_a_failed_transaction_never_moved_anything_and_is_skipped():
    handler = _solana_handler(
        signatures={A: [_sig("bad", JAN23, err={"InsufficientFundsForRent": {}})]},
        txs={},
    )
    with _settings(), _patched_client(handler):
        assert (await onchain.transfers(SOL, A, limit=25)).items == []


@pytest.mark.asyncio
async def test_a_transaction_that_left_the_address_untouched_yields_no_transfer():
    handler = _solana_handler(
        signatures={A: [_sig("sig1", JAN23)]},
        txs={"sig1": _tx(JAN23, {A: 0, B: -100, C: 100})},
    )
    with _settings(), _patched_client(handler):
        assert (await onchain.transfers(SOL, A, limit=25)).items == []


@pytest.mark.asyncio
async def test_the_window_is_applied_before_the_per_transaction_fetch():
    fetched: list[str] = []

    def handler(request):
        body = json.loads(request.content)
        if body["method"] == "getSignaturesForAddress":
            return httpx.Response(
                200,
                json={"result": [_sig("old", JAN23 - 86400), _sig("new", JAN23)], "id": 1},
            )
        fetched.append(body["params"][0])
        return httpx.Response(
            200, json={"result": _tx(JAN23, {A: -1_000_000_000, B: 1_000_000_000}), "id": 1}
        )

    since = datetime.fromtimestamp(JAN23 - 60, tz=timezone.utc)
    with _settings(), _patched_client(handler):
        await onchain.transfers(SOL, A, limit=25, since=since)
    assert fetched == ["new"]


def _full_page(span_seconds: int, newest: int = JAN23):
    """A page that came back at the cap, spread evenly over ``span_seconds``."""
    step = span_seconds / onchain.SOLANA_SIGNATURE_PAGE
    return [
        _sig(f"s{i}", int(newest - i * step)) for i in range(onchain.SOLANA_SIGNATURE_PAGE)
    ]


@pytest.mark.asyncio
async def test_a_pooled_address_is_recognized_by_its_rate_not_its_volume():
    """A thousand transactions in an hour is an exchange; nobody types that fast."""
    calls: list[str] = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "getSignaturesForAddress":
            return httpx.Response(200, json={"result": _full_page(3600), "id": 1})
        return httpx.Response(200, json={"result": None, "id": 1})

    with _settings(), _patched_client(handler):
        page = await onchain.transfers(SOL, A, limit=25)
    assert page.saturated == onchain.SATURATED_POOLED
    assert page.items == []
    # The verdict came from the signature list alone — the per-transaction
    # walk it would have made is the whole point of asking first.
    assert calls == ["getSignaturesForAddress"]


@pytest.mark.asyncio
async def test_a_busy_wallet_spread_over_months_is_still_a_wallet_and_gets_followed():
    """The scam-aggregator shape: heavy volume, but months of it, so it is traceable."""
    rows = _full_page(120 * 86400)
    handler = _solana_handler(
        signatures={A: rows},
        txs={row["signature"]: _tx(row["blockTime"], {A: -10**9, B: 10**9}) for row in rows},
    )
    since = datetime.fromtimestamp(JAN23 - 3600, tz=timezone.utc)
    with _settings(), _patched_client(handler):
        page = await onchain.transfers(SOL, A, limit=25, since=since)
    assert page.saturated is None
    assert page.items


@pytest.mark.asyncio
async def test_a_page_that_never_reached_the_window_says_so_instead_of_reporting_nothing():
    # Every allowed page is newer than the requested ceiling. A bounded scan
    # must retain the unread window and cursor instead of declaring it empty.
    count = onchain.SOLANA_SIGNATURE_PAGE * (onchain.SOLANA_HISTORY_MAX_PAGES + 1)
    rows = [_sig(f"s{i}", JAN23 + (count - i) * 86400) for i in range(count)]
    handler = _solana_handler(signatures={A: rows})
    until = datetime.fromtimestamp(JAN23, tz=timezone.utc)
    with _settings(), _patched_client(handler):
        page = await onchain.transfers(SOL, A, limit=25, until=until)
    assert not page.complete and page.items == []
    assert page.coverage is not None
    assert page.coverage.pages_read == onchain.SOLANA_HISTORY_MAX_PAGES
    assert page.coverage.next_cursor is not None
    assert set(page.coverage.stop_reasons) == {"provider_page_limit", "window_not_reached"}


@pytest.mark.asyncio
async def test_a_short_page_is_the_whole_history_and_is_never_saturated():
    rows = [_sig(f"s{i}", JAN23 - i) for i in range(5)]
    handler = _solana_handler(
        signatures={A: rows},
        txs={row["signature"]: _tx(row["blockTime"], {A: -10**9, B: 10**9}) for row in rows},
    )
    with _settings(), _patched_client(handler):
        assert (await onchain.transfers(SOL, A, limit=25)).saturated is None


# ----- EVM transfers --------------------------------------------------------


def _blockscout_item(*, sender: str, recipient: str, value: str, at: int, **extra) -> dict:
    """One Blockscout row: parties are objects and the instant is ISO-8601."""
    return {
        "hash": "0xdead",
        "timestamp": datetime.fromtimestamp(at, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "from": {"hash": sender},
        "to": {"hash": recipient},
        "value": value,
        **extra,
    }


@pytest.mark.asyncio
async def test_an_evm_chain_traces_without_a_key_because_blockscout_is_keyless():
    """Both lists, and the drain is in the second one.

    Blockscout answers the same two questions Etherscan does, so a deployment
    that never signed up for a key still traces. `internal-transactions` is
    where the dominant EVM drain lives: the victim's own call carries no value
    and the sweep happens inside the contract.
    """
    drainer = "0x" + "cd" * 20
    asked: list[str] = []

    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        asked.append(path)
        assert "etherscan" not in request.url.host
        item = (
            _blockscout_item(sender=EVM.upper(), recipient=drainer, value="0", at=JAN23)
            if path == "transactions"
            else _blockscout_item(
                sender=EVM.upper(),
                recipient=drainer,
                value="4000000000000000000",
                at=JAN23 + 1,
                success=True,
            )
        )
        return httpx.Response(200, json={"items": [item], "next_page_params": None})

    with _settings(), _patched_client(handler):
        page = await onchain.transfers(BASE, EVM, limit=25)
    assert asked == ["transactions", "internal-transactions"]
    [transfer] = page.items
    assert transfer.amount == Decimal("4")
    assert transfer.sender == EVM.lower() and transfer.recipient == drainer


@pytest.mark.asyncio
async def test_a_failed_or_pending_blockscout_row_is_not_a_transfer():
    """Neither moved anything: one reverted, and one has not been mined."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "items": [
                    _blockscout_item(
                        sender=EVM,
                        recipient="0x" + "cd" * 20,
                        value="1000000000000000000",
                        at=JAN23,
                        status="error",
                    ),
                    {
                        "hash": "0xpending",
                        "timestamp": None,
                        "from": {"hash": EVM},
                        "to": {"hash": "0x" + "cd" * 20},
                        "value": "9000000000000000000",
                    },
                ]
            },
        )

    with _settings(), _patched_client(handler):
        page = await onchain.transfers(BASE, EVM, limit=25)
    assert page.items == []


@pytest.mark.asyncio
async def test_blockscout_history_follows_its_pages_because_one_is_fifty_rows():
    pages: list[dict] = []

    def handler(request):
        pages.append(dict(request.url.params))
        first = "block_number" not in request.url.params
        return httpx.Response(
            200,
            json={
                "items": [
                    _blockscout_item(
                        sender=EVM,
                        recipient="0x" + ("cd" if first else "ef") * 20,
                        value="1000000000000000000",
                        at=JAN23 - (0 if first else 60),
                    )
                ],
                "next_page_params": {"block_number": 21, "index": None} if first else None,
            },
        )

    with _settings(), _patched_client(handler):
        page = await onchain.transfers(BASE, EVM, limit=25)
    # Four requests: two pages of `transactions`, two of `internal-transactions`.
    assert len(pages) == 4
    # A null in `next_page_params` is Blockscout saying "no cursor here", not a
    # parameter to send back — httpx would serialise it as the string "None".
    assert pages[1] == {"block_number": "21"}
    assert len(page.items) == 4


@pytest.mark.asyncio
async def test_a_pooled_first_page_stops_the_paging_instead_of_walking_into_a_timeout():
    """The rate test has to fire before the next request, not after the last.

    Blockscout answers an exchange hot wallet's first page in a second and then
    times out paging deeper into it. Judging only once the paging finished
    would therefore fail on the very addresses the test exists to recognise.
    """
    asked: list[str] = []

    def handler(request):
        asked.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            200,
            json={
                # A full page of transactions inside a minute: an operator, not
                # a person.
                "items": [
                    _blockscout_item(
                        sender=EVM,
                        recipient="0x" + "cd" * 20,
                        value="1000000000000000000",
                        at=JAN23 - index,
                    )
                    for index in range(onchain.BLOCKSCOUT_PAGE)
                ],
                "next_page_params": {"block_number": 21},
            },
        )

    with _settings(), _patched_client(handler):
        page = await onchain.transfers(BASE, EVM, limit=25)
    assert page.saturated == onchain.SATURATED_POOLED
    assert asked == ["transactions"]


@pytest.mark.asyncio
async def test_a_page_that_will_not_load_shortens_the_history_rather_than_losing_it():
    """Half a list is evidence; it just is not evidence of absence."""
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        if "block_number" in request.url.params:
            raise httpx.ReadTimeout("too deep")
        return httpx.Response(
            200,
            json={
                "items": [
                    _blockscout_item(
                        sender=EVM,
                        recipient="0x" + "cd" * 20,
                        value="1000000000000000000",
                        at=JAN23 - index * 86_400,
                    )
                    for index in range(onchain.BLOCKSCOUT_PAGE)
                ],
                "next_page_params": {"block_number": 21},
            },
        )

    with _settings(), _patched_client(handler):
        page = await onchain.transfers(BASE, EVM, limit=25)
    assert page.items and not page.complete
    assert page.saturated is None


@pytest.mark.asyncio
async def test_a_first_page_that_will_not_load_is_a_failure_not_an_empty_history():
    def handler(request):
        raise httpx.ReadTimeout("https://synthetic.invalid/secret-sentinel")

    with _settings(), _patched_client(handler):
        with pytest.raises(RuntimeError, match="request timed out") as error:
            await onchain.transfers(BASE, EVM, limit=25)
    assert "sentinel" not in str(error.value)


@pytest.mark.asyncio
async def test_a_chain_with_neither_a_key_nor_an_index_says_so_rather_than_returning_nothing():
    bare = replace(BASE, token_index_url=None)
    with _settings(), _patched_client(lambda request: httpx.Response(200, json={})):
        with pytest.raises(ProviderNotConfiguredError) as exc:
            await onchain.transfers(bare, EVM, limit=25)
    assert "ETHERSCAN_API_KEY" in str(exc.value)


@pytest.mark.asyncio
async def test_an_explorer_key_is_preferred_over_blockscout_for_its_deeper_page():
    def handler(request):
        assert "blockscout" not in request.url.host
        return httpx.Response(200, json={"status": "0", "message": "No transactions found", "result": []})

    with _settings(etherscan_api_key="k"), _patched_client(handler):
        assert (await onchain.transfers(BASE, EVM, limit=25)).items == []


@pytest.mark.asyncio
async def test_an_explorer_reporting_no_transactions_is_an_empty_history_not_a_failure():
    def handler(request):
        return httpx.Response(200, json={"status": "0", "message": "No transactions found"})

    with _settings(etherscan_api_key="k"), _patched_client(handler):
        page = await onchain.transfers(BASE, EVM, limit=25)
    assert page.items == [] and page.saturated is None


@pytest.mark.asyncio
async def test_an_evm_transfer_is_decoded_from_wei_and_lowercased():
    other = "0x" + "CD" * 20

    def handler(request):
        assert request.url.params["chainid"] == "8453"
        if request.url.params["action"] != "txlist":
            return httpx.Response(200, json={"status": "0", "message": "No transactions found", "result": []})
        return httpx.Response(
            200,
            json={
                "status": "1",
                "result": [
                    {
                        "hash": "0xdead",
                        "timeStamp": str(JAN23),
                        "from": EVM.upper(),
                        "to": other,
                        "value": "1500000000000000000",
                        "isError": "0",
                    },
                    {"hash": "0xfail", "timeStamp": str(JAN23), "value": "1", "isError": "1"},
                    # A contract creation has no recipient to follow.
                    {
                        "hash": "0xcreate",
                        "timeStamp": str(JAN23),
                        "from": EVM.lower(),
                        "to": "",
                        "value": "5000000000000000000",
                        "isError": "0",
                    },
                ],
            },
        )

    with _settings(etherscan_api_key="k"), _patched_client(handler):
        page = await onchain.transfers(BASE, EVM, limit=25)
    [transfer] = page.items
    assert transfer.amount == Decimal("1.5")
    assert transfer.sender == EVM.lower()
    assert transfer.recipient == other.lower()


@pytest.mark.asyncio
async def test_an_evm_drain_is_found_even_though_it_is_an_internal_transaction():
    """The dominant EVM drain: the victim's own call carries no value.

    Reading only `txlist` sees a zero-value contract call and reports the
    drained wallet as untouched. The sweep is in `txlistinternal`.
    """
    drainer = "0x" + "cd" * 20
    actions: list[str] = []

    def handler(request):
        action = request.url.params["action"]
        actions.append(action)
        if action == "txlist":
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "result": [
                        {
                            "hash": "0xcall",
                            "timeStamp": str(JAN23),
                            "from": EVM.lower(),
                            "to": drainer,
                            "value": "0",
                            "isError": "0",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "1",
                "result": [
                    {
                        "hash": "0xcall",
                        "timeStamp": str(JAN23 + 1),
                        "from": EVM.lower(),
                        "to": drainer,
                        "value": "4000000000000000000",
                        "isError": "0",
                    }
                ],
            },
        )

    with _settings(etherscan_api_key="k"), _patched_client(handler):
        page = await onchain.transfers(BASE, EVM, limit=25)
    assert actions == ["txlist", "txlistinternal"]
    [transfer] = page.items
    assert transfer.amount == Decimal("4")
    assert transfer.recipient == drainer


# ----- Bitcoin --------------------------------------------------------------

# Minted for these tests: valid base58check over sha256("btc-a") and friends,
# so the checksums hold and no private key exists for any of them.
BTC_A = "1QJXx5X8qZS75DUP4csav8ALaWxELKSzHr"
BTC_B = "1GmLnpNR4V2vuU98ne23bfsPKfgdRMqYVN"
BTC_C = "1QH7FDZrm3kRUkP6oDYU65iKHDFPQhGdnz"
BTC_CHANGE = "1M6m1SHMKdrUa5in4J1pSWJStaXX5g8zp6"
BTC = CHAINS["bitcoin"]
COIN = 100_000_000


def _btc_tx(txid: str, block_time: int, vin, vout, confirmed: bool = True) -> dict:
    return {
        "txid": txid,
        "status": ({"confirmed": True, "block_time": block_time} if confirmed
                   else {"confirmed": False}),
        "vin": [{"prevout": {"scriptpubkey_address": a, "value": v}} for a, v in vin],
        "vout": [{"scriptpubkey_address": a, "value": v} for a, v in vout],
    }


def _esplora_handler(*, stats=None, history=None):
    """Serve Esplora's /address/{a} and /address/{a}/txs[/chain/{txid}] routes."""
    stats = stats or {}
    history = history or {}

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        index = parts.index("address")
        address, rest = parts[index + 1], parts[index + 2:]
        if not rest:
            funded, spent = stats.get(address, (0, 0))
            return httpx.Response(200, json={
                "chain_stats": {"funded_txo_sum": funded, "spent_txo_sum": spent},
                "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0},
            })
        rows = list(history.get(address, []))
        if len(rest) >= 3 and rest[1] == "chain":
            ids = [row["txid"] for row in rows]
            rows = rows[ids.index(rest[2]) + 1:] if rest[2] in ids else []
        return httpx.Response(200, json=rows[: onchain.BITCOIN_HISTORY_PAGE])

    return handler


@pytest.mark.parametrize(
    "address,valid,why",
    [
        (BTC_A, True, "P2PKH"),
        ("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", True, "P2SH"),
        ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", True, "BIP-173 v0 P2WPKH"),
        ("bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3", True, "v0 P2WSH"),
        ("BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4", True, "upper case is legal"),
        ("bc1p5d7rjq7g6rdk2yhzks9smlaqtedr4dekq08ge8ztwac72sfr9rusxg3297", True, "v1 taproot"),
        ("BC1SW50QGDZ25J", True, "BIP-350 v16, bech32m"),
        (BTC_A[:-1] + "s", False, "base58 checksum typo"),
        ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5", False, "bech32 checksum typo"),
        ("bc1p5d7rjq7g6rdk2yhzks9smlaqtedr4dekq08ge8ztwac72sfr9rusxg3298", False, "bech32m typo"),
        ("tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx", False, "testnet hrp"),
        ("mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn", False, "testnet version byte"),
        # The two checksums are not interchangeable: BIP-350 split them so a v1+
        # address could not be re-encoded under the flawed v0 scheme.
        ("bc1zw508d6qejxtdg4y5r3zarvary0c5xw7kn40wf2", False, "v2 with the v0 checksum"),
        ("BC1SW50QA3JX3S", False, "v16 with the v0 checksum"),
        ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kemeawh", False, "v0 with the v1+ checksum"),
        ("BC130XLXVLHEMJA6C4DQV22UAPCTQUPFHLXM9H8Z3K2E72Q4K9HCZ7VQ7ZWS8R", False,
         "witness version 17"),
        # Checksum-valid but the program is not a program. These reach the index
        # as a rejected request, and a rejected read fails the whole sync.
        ("bc1q2teqrxlduq", False, "v0, 2-byte program"),
        ("bc1qqqqqp399et2xygdj5xreqhjjvcmzhxw4aywxecjdzew6hylgvsesrxh6hy", False,
         "v0, 41-byte program"),
        ("bc1pw5dgrnzv", False, "v1, 1-byte program"),
        ("bc1gmk9yu", False, "empty data section"),
        ("", False, "empty"),
        ("1" * 34, False, "all base58 zeroes"),
    ],
)
def test_a_bitcoin_address_is_accepted_on_its_checksum_not_its_shape(address, valid, why):
    """A typo has to be caught here or it is watched forever as an empty wallet.

    Both Bitcoin forms carry a checksum, unlike a Solana or EVM address, so
    there is a real answer available and nothing is gained by only pattern
    matching. Vectors are BIP-173 and BIP-350's own, plus the program-length
    cases that separate a checksum check from address validation.
    """
    assert onchain.address_is_valid(BTC, address) is valid, why


def test_a_legacy_bitcoin_address_is_not_mistaken_for_a_solana_one():
    """Their base58 forms overlap; only Bitcoin's carries a checksum."""
    assert onchain.detect_chain(BTC_A) is BTC
    assert onchain.detect_chain(A) is SOL
    # Same shape, broken checksum: not a Bitcoin address, so it falls through
    # to the pattern that does match it rather than being rejected outright.
    assert onchain.detect_chain(BTC_A[:-1] + "s") is SOL


def test_bech32_casing_is_folded_but_base58_casing_is_not():
    """Case means nothing in bech32 and everything in base58."""
    upper = "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4"
    assert onchain.normalize_address(BTC, upper) == upper.lower()
    assert onchain.normalize_address(BTC, BTC_A) == BTC_A


@pytest.mark.asyncio
async def test_a_bitcoin_balance_is_unspent_outputs_including_the_still_pending_ones():
    """A spend that has broadcast but not confirmed is money already gone."""
    def handler(request):
        return httpx.Response(200, json={
            "chain_stats": {"funded_txo_sum": 3 * COIN, "spent_txo_sum": COIN},
            "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": COIN // 2},
        })

    with _settings(), _patched_client(handler):
        assert await onchain.native_balance(BTC, BTC_A) == Decimal("1.5")


@pytest.mark.asyncio
async def test_bitcoin_change_returning_to_an_input_address_is_not_a_payment():
    """Every output is a candidate recipient; the ones the sender already owns are not.

    Without this the trace would follow the spender's own change and report it
    as a second destination of the same coins.
    """
    history = {BTC_A: [_btc_tx(
        "spend", JAN23,
        vin=[(BTC_A, 10 * COIN)],
        vout=[(BTC_B, 4 * COIN), (BTC_A, 6 * COIN)],
    )]}
    with _settings(), _patched_client(_esplora_handler(history=history)):
        page = await onchain.transfers(BTC, BTC_A, limit=25)
    [transfer] = page.items
    assert (transfer.sender, transfer.recipient) == (BTC_A, BTC_B)
    assert transfer.amount == Decimal("4")


@pytest.mark.asyncio
async def test_a_bitcoin_spend_paying_several_outputs_is_several_transfers():
    """One transaction, two payees: dropping either would lose half the trail."""
    history = {BTC_A: [_btc_tx(
        "split", JAN23,
        vin=[(BTC_A, 10 * COIN)],
        vout=[(BTC_B, 6 * COIN), (BTC_C, 3 * COIN), (BTC_A, COIN)],
    )]}
    with _settings(), _patched_client(_esplora_handler(history=history)):
        page = await onchain.transfers(BTC, BTC_A, limit=25)
    assert {(t.recipient, t.amount) for t in page.items} == {
        (BTC_B, Decimal("6")),
        (BTC_C, Decimal("3")),
    }


@pytest.mark.asyncio
async def test_an_incoming_bitcoin_transfer_is_credited_to_its_largest_external_input():
    """A transaction has no sender field, so the biggest funder is the answer."""
    history = {BTC_A: [_btc_tx(
        "receive", JAN23,
        vin=[(BTC_B, 9 * COIN), (BTC_C, COIN)],
        vout=[(BTC_A, 7 * COIN), (BTC_B, 3 * COIN)],
    )]}
    with _settings(), _patched_client(_esplora_handler(history=history)):
        page = await onchain.transfers(BTC, BTC_A, limit=25)
    [transfer] = page.items
    assert (transfer.sender, transfer.recipient) == (BTC_B, BTC_A)
    assert transfer.amount == Decimal("7")


@pytest.mark.asyncio
async def test_a_bitcoin_self_consolidation_moved_nobody_else_s_money():
    history = {BTC_A: [_btc_tx(
        "sweep", JAN23,
        vin=[(BTC_A, 3 * COIN), (BTC_A, 2 * COIN)],
        vout=[(BTC_A, 5 * COIN)],
    )]}
    with _settings(), _patched_client(_esplora_handler(history=history)):
        assert (await onchain.transfers(BTC, BTC_A, limit=25)).items == []


@pytest.mark.asyncio
async def test_a_bitcoin_history_that_ran_out_is_the_whole_story_however_fast_it_was_written():
    """A short page is complete by definition, even at an exchange's rate."""
    rows = [
        _btc_tx(f"t{i}", JAN23 - i, vin=[(BTC_A, COIN)], vout=[(BTC_B, COIN)])
        for i in range(3)
    ]
    with _settings(), _patched_client(_esplora_handler(history={BTC_A: rows})):
        page = await onchain.transfers(BTC, BTC_A, limit=25)
    assert page.saturated is None
    assert page.complete


@pytest.mark.asyncio
async def test_a_pooled_bitcoin_address_is_recognized_by_its_rate_and_stops_the_trace():
    """Every page came back full and the whole cap spans an hour: an exchange."""
    total = onchain.BITCOIN_HISTORY_PAGE * onchain.BITCOIN_HISTORY_MAX_PAGES
    rows = [
        _btc_tx(f"t{i}", JAN23 - i * 3, vin=[(BTC_A, COIN)], vout=[(BTC_B, COIN)])
        for i in range(total)
    ]
    with _settings(), _patched_client(_esplora_handler(history={BTC_A: rows})):
        page = await onchain.transfers(BTC, BTC_A, limit=25)
    assert page.saturated == onchain.SATURATED_POOLED
    assert page.items == []


@pytest.mark.asyncio
async def test_bitcoin_paging_stops_once_it_has_reached_past_the_window():
    """Esplora pages 25 at a time; nothing older than the floor can be an answer."""
    requested: list[str] = []
    rows = [
        _btc_tx(f"t{i}", JAN23 - i * 3600, vin=[(BTC_A, COIN)], vout=[(BTC_B, COIN)])
        for i in range(onchain.BITCOIN_HISTORY_PAGE * 3)
    ]
    inner = _esplora_handler(history={BTC_A: rows})

    def handler(request):
        requested.append(request.url.path)
        return inner(request)

    since = datetime.fromtimestamp(JAN23 - 30 * 3600, tz=timezone.utc)
    with _settings(), _patched_client(handler):
        page = await onchain.transfers(BTC, BTC_A, limit=50, since=since)
    # Two pages reach back 50 hours, past a 30-hour floor. A third would be
    # spent reading transactions the window already excludes.
    assert len(requested) == 2
    assert page.saturated is None
    assert all(t.occurred_at >= since for t in page.items)


@pytest.mark.asyncio
async def test_a_bitcoin_trace_follows_the_money_and_ignores_the_sender_s_own_change():
    """BTC_A → BTC_B → BTC_C, with change to BTC_CHANGE on the way."""
    history = {
        BTC_A: [_btc_tx("a_out", JAN23, vin=[(BTC_A, 10 * COIN)],
                        vout=[(BTC_B, 9 * COIN), (BTC_A, COIN)])],
        BTC_B: [_btc_tx("b_out", JAN23 + 600, vin=[(BTC_B, 9 * COIN)],
                        vout=[(BTC_C, 8 * COIN), (BTC_B, COIN)])],
        BTC_C: [],
        BTC_CHANGE: [],
    }
    with _settings(), _patched_client(_esplora_handler(history=history)):
        result = await onchain_trace.trace("bitcoin", BTC_A, max_hops=3)
    assert [(e.source, e.target, e.amount) for e in result.edges] == [
        (f"bitcoin:{BTC_A}", f"bitcoin:{BTC_B}", Decimal("9")),
        (f"bitcoin:{BTC_B}", f"bitcoin:{BTC_C}", Decimal("8")),
    ]


@pytest.mark.asyncio
async def test_a_bitcoin_holding_is_priced_in_usd_like_every_other_chain():
    provider = OnChainProvider()
    handler = _esplora_handler(stats={BTC_A: (2 * COIN, 0)})
    with _settings(), _patched_client(handler), patch.object(
        onchain, "usd_spot_prices", return_value={"BTC": Decimal("60000")}
    ):
        [holding] = await provider.get_holdings({"addresses": [f"bitcoin:{BTC_A}"]})
    assert holding.ticker == "BTC"
    assert holding.quantity == Decimal("2")
    assert holding.current_value == Decimal("120000")
    assert holding.currency == "USD"

# ----- SPL and ERC-20 tokens ------------------------------------------------

MINT_A = "MMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM"
MINT_B = "NNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNN"
MINT_C = "PPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPP"
ERC20 = "0x" + "cd" * 20


def _token_account(mint: str, amount: str, decimals: int) -> dict:
    return {
        "account": {
            "data": {
                "parsed": {
                    "info": {
                        "mint": mint,
                        "tokenAmount": {"amount": amount, "decimals": decimals},
                    }
                }
            }
        }
    }


def _jupiter_row(mint: str, symbol: str, price, verified: bool) -> dict:
    return {"id": mint, "symbol": symbol, "decimals": 6, "usdPrice": price,
            "isVerified": verified, "tags": ["verified"] if verified else ["unknown"]}


def _token_handler(*, accounts=None, jupiter=None, blockscout=None, native=0):
    """Route SPL account reads, Jupiter lookups and Blockscout balances at once.

    ``accounts`` is keyed by token program so a test can put a mint in the
    original program, Token-2022, or both.
    """
    accounts = accounts or {}
    jupiter = jupiter or []
    blockscout = blockscout if blockscout is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if "jup.ag" in host:
            wanted = set((request.url.params.get("query") or "").split(","))
            return httpx.Response(200, json=[r for r in jupiter if r["id"] in wanted])
        if "blockscout" in host:
            return httpx.Response(200, json=blockscout)
        body = json.loads(request.content)
        if body["method"] == "getTokenAccountsByOwner":
            program = body["params"][1]["programId"]
            return httpx.Response(200, json={"result": {"value": accounts.get(program, [])}, "id": 1})
        if body["method"] == "getBalance":
            return httpx.Response(200, json={"result": {"value": native}, "id": 1})
        raise AssertionError(f"unexpected method {body['method']}")

    return handler


@pytest.mark.asyncio
async def test_spl_balances_are_read_from_both_token_programs():
    """A wallet holding a Token-2022 mint answers only half if one is skipped."""
    original, token22 = onchain.SOLANA_TOKEN_PROGRAMS
    handler = _token_handler(
        accounts={
            original: [_token_account(MINT_A, "5000098", 6)],
            token22: [_token_account(MINT_B, "2000000", 6)],
        },
        jupiter=[_jupiter_row(MINT_A, "USDC", 1, True), _jupiter_row(MINT_B, "NEW", 2, True)],
    )
    with _settings(), _patched_client(handler):
        held = (await onchain.token_holdings(SOL, A)).items
    assert {(t.symbol, t.quantity) for t in held} == {
        ("USDC", Decimal("5.000098")),
        ("NEW", Decimal("2")),
    }


@pytest.mark.asyncio
async def test_several_token_accounts_for_one_mint_are_one_position():
    """A wallet can hold the same mint in several accounts; the position is their sum."""
    original, _ = onchain.SOLANA_TOKEN_PROGRAMS
    handler = _token_handler(
        accounts={original: [_token_account(MINT_A, "1000000", 6),
                             _token_account(MINT_A, "500000", 6)]},
        jupiter=[_jupiter_row(MINT_A, "USDC", 1, True)],
    )
    with _settings(), _patched_client(handler):
        [held] = (await onchain.token_holdings(SOL, A)).items
    assert held.quantity == Decimal("1.5")


@pytest.mark.asyncio
async def test_a_mint_with_no_market_is_not_a_position():
    """The spam filter. A years-old address holds thousands of these."""
    original, _ = onchain.SOLANA_TOKEN_PROGRAMS
    handler = _token_handler(
        accounts={original: [_token_account(MINT_A, "1000000", 6),
                             _token_account(MINT_B, "9" * 12, 6)]},
        jupiter=[_jupiter_row(MINT_A, "REAL", "0.5", True),
                 _jupiter_row(MINT_B, "AIRDROP", None, False)],
    )
    with _settings(), _patched_client(handler):
        held = (await onchain.token_holdings(SOL, A)).items
    assert [t.symbol for t in held] == ["REAL"]


@pytest.mark.asyncio
async def test_an_unvouched_token_is_listed_with_its_quantity_and_never_valued():
    """Minting a token and a pool is cheap, so an unvouched quote may not
    reach a net worth. Hiding the position would answer the user's question
    with silence, so it is named and valued at nothing instead."""
    original, _ = onchain.SOLANA_TOKEN_PROGRAMS
    handler = _token_handler(
        accounts={original: [_token_account(MINT_A, "1000000000", 6)]},
        jupiter=[_jupiter_row(MINT_A, "USDC", "1000000", False)],
    )
    with _settings(), _patched_client(handler), patch.object(
        onchain, "usd_spot_prices", return_value={"SOL": Decimal("100")}
    ):
        [held] = (await onchain.token_holdings(SOL, A)).items
        holdings = await OnChainProvider().get_holdings({"addresses": [f"solana:{A}"]})
    assert held.trusted is False
    assert held.usd_price == Decimal("1000000")
    # No ticker: an unvouched symbol must not consolidate with a real position.
    token = next(h for h in holdings if h.external_id.endswith(MINT_A))
    assert token.ticker is None
    assert token.quantity == Decimal("1000")
    assert token.unit_price is None
    assert token.current_value == Decimal("0")
    # The refused quote stays inspectable rather than vanishing.
    assert token.metadata is not None
    observation = token.metadata.pop("onchain_observation")
    assert observation["complete"] is False
    assert observation["reasons"] == ["untrusted_price"]
    assert observation["observed_at"]
    assert token.metadata == {
        "chain": "solana",
        "address": A,
        "watch_only": True,
        "token_contract": MINT_A,
        "token_symbol": "USDC",
        "token_trusted": False,
        "token_quoted_usd": "1000000",
    }


@pytest.mark.asyncio
async def test_tokens_are_ranked_by_value_so_the_cap_discards_the_tail():
    original, _ = onchain.SOLANA_TOKEN_PROGRAMS
    # Zero-padding would collide — "2" and "20" pad to the same 44 chars.
    mints = [f"m{i}".ljust(44, "9") for i in range(onchain.MAX_TOKENS_PER_ADDRESS + 5)]
    handler = _token_handler(
        accounts={original: [_token_account(m, "1000000", 6) for m in mints]},
        # Ascending price, so the richest are the last few minted.
        jupiter=[_jupiter_row(m, f"T{i}", str(i + 1), True) for i, m in enumerate(mints)],
    )
    with _settings(), _patched_client(handler):
        held = (await onchain.token_holdings(SOL, A)).items
    assert len(held) == onchain.MAX_TOKENS_PER_ADDRESS
    assert held[0].symbol == f"T{len(mints) - 1}"
    assert held == sorted(held, key=lambda t: t.quoted_value, reverse=True)


@pytest.mark.asyncio
async def test_an_emptied_token_account_is_not_a_position():
    original, _ = onchain.SOLANA_TOKEN_PROGRAMS
    handler = _token_handler(
        accounts={original: [_token_account(MINT_A, "0", 6)]},
        jupiter=[_jupiter_row(MINT_A, "GONE", "1", True)],
    )
    with _settings(), _patched_client(handler):
        assert (await onchain.token_holdings(SOL, A)).items == []


def _blockscout_row(symbol: str, value: str, rate, reputation: str, kind="ERC-20") -> dict:
    return {
        "value": value,
        "token": {"address_hash": ERC20, "symbol": symbol, "decimals": "18",
                  "exchange_rate": rate, "reputation": reputation, "type": kind},
    }


@pytest.mark.asyncio
async def test_erc20_balances_need_no_explorer_key():
    """Blockscout answers where Etherscan's token endpoint is a paid tier, so
    token balances work on a deployment that cannot trace EVM at all."""
    handler = _token_handler(blockscout=[_blockscout_row("USDC", str(3 * 10**18), 0.9999, "ok")])
    with _settings(etherscan_api_key=""), _patched_client(handler):
        [held] = (await onchain.token_holdings(BASE, EVM)).items
    assert (held.symbol, held.quantity, held.trusted) == ("USDC", Decimal("3"), True)
    assert held.contract == ERC20


@pytest.mark.asyncio
async def test_a_token_blockscout_will_not_vouch_for_is_listed_without_a_value():
    handler = _token_handler(blockscout=[_blockscout_row("USDC", str(10**18), 1000, "scam")])
    with _settings(), _patched_client(handler):
        [held] = (await onchain.token_holdings(BASE, EVM)).items
    assert held.trusted is False


@pytest.mark.asyncio
async def test_an_nft_is_not_a_token_balance():
    handler = _token_handler(blockscout=[_blockscout_row("APE", "1", 5000, "ok", kind="ERC-721")])
    with _settings(), _patched_client(handler):
        assert (await onchain.token_holdings(BASE, EVM)).items == []


@pytest.mark.asyncio
async def test_bitcoin_has_no_tokens_and_its_index_is_never_asked():
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=[])

    with _settings(), _patched_client(handler):
        assert (await onchain.token_holdings(CHAINS["bitcoin"], BTC_A)).items == []
    assert calls == []


@pytest.mark.asyncio
async def test_a_wallet_holding_no_native_coin_still_reports_its_tokens():
    """Nothing about an empty SOL balance says anything about 500 USDC."""
    original, _ = onchain.SOLANA_TOKEN_PROGRAMS
    handler = _token_handler(
        accounts={original: [_token_account(MINT_A, "500000000", 6)]},
        jupiter=[_jupiter_row(MINT_A, "USDC", "1", True)],
        native=0,
    )
    with _settings(), _patched_client(handler), patch.object(
        onchain, "usd_spot_prices", return_value={"SOL": Decimal("100")}
    ):
        holdings = await OnChainProvider().get_holdings({"addresses": [f"solana:{A}"]})
    assert {h.ticker for h in holdings} == {"SOL", "USDC"}
    assert next(h for h in holdings if h.ticker == "USDC").current_value == Decimal("500")


@pytest.mark.asyncio
async def test_the_two_reads_are_independent_even_though_either_can_fail_the_sync():
    """A token failure is not caused by the native read, or the reverse.

    Both still raise — see the outage tests above — but they raise for their
    own reasons, so a diagnosis points at the index that actually broke.
    """
    original, _ = onchain.SOLANA_TOKEN_PROGRAMS
    handler = _token_handler(
        accounts={original: [_token_account(MINT_A, "500000000", 6)]},
        jupiter=[_jupiter_row(MINT_A, "USDC", "1", True)],
        native=2 * 10**9,
    )
    with _settings(), _patched_client(handler), patch.object(
        onchain, "usd_spot_prices", return_value={"SOL": Decimal("100")}
    ):
        holdings = await OnChainProvider().get_holdings({"addresses": [f"solana:{A}"]})
    assert {h.ticker for h in holdings} == {"SOL", "USDC"}
    assert next(h for h in holdings if h.ticker == "SOL").current_value == Decimal("200")
    assert next(h for h in holdings if h.ticker == "USDC").current_value == Decimal("500")


# ----- holdings -------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_watched_address_becomes_one_holding_priced_in_usd():
    provider = OnChainProvider()
    credentials = {"addresses": [f"solana:{A}"]}
    async def fake_prices():
        return {"SOL": Decimal("150")}

    with (
        _settings(),
        _patched_client(_solana_handler(balances={A: 2_000_000_000})),
        patch("app.providers.onchain.usd_spot_prices", fake_prices),
    ):
        holdings = await provider.get_holdings(credentials)
    [holding] = holdings
    assert holding.ticker == "SOL"
    assert holding.quantity == Decimal("2")
    assert holding.current_value == Decimal("300")
    assert holding.metadata is not None
    observation = holding.metadata.pop("onchain_observation")
    assert observation["complete"] is True
    assert observation["reasons"] == []
    assert observation["observed_at"]
    assert holding.metadata == {"chain": "solana", "address": A, "watch_only": True}


@pytest.mark.asyncio
async def test_an_address_the_node_cannot_answer_for_fails_the_sync_rather_than_emptying_it():
    """Dropping the holding is not a gap, it is a claim the position is gone.

    The sync layer archives every holding it stops being told about, and an
    archived asset does not come back on its own. Raising leaves the previous
    values stale instead, which the user can see and which heals itself.
    """
    provider = OnChainProvider()

    async def fake_prices():
        return {"SOL": Decimal("150")}

    def handler(request):
        return httpx.Response(500)

    with (
        _settings(),
        _patched_client(handler),
        patch("app.providers.onchain.usd_spot_prices", fake_prices),
    ):
        with pytest.raises(Exception):
            await provider.get_holdings({"addresses": [f"solana:{A}"]})


@pytest.mark.asyncio
async def test_a_token_index_outage_fails_the_sync_rather_than_emptying_the_wallet():
    """Same rule for the token half: an unreachable index is not a liquidation."""
    original, _ = onchain.SOLANA_TOKEN_PROGRAMS

    def handler(request):
        if "jup.ag" in request.url.host:
            return httpx.Response(503)
        body = json.loads(request.content)
        if body["method"] == "getBalance":
            return httpx.Response(200, json={"result": {"value": 10**9}, "id": 1})
        return httpx.Response(
            200, json={"result": {"value": [_token_account(MINT_A, "1000000", 6)]}, "id": 1}
        )

    with _settings(), _patched_client(handler), patch.object(
        onchain, "usd_spot_prices", return_value={"SOL": Decimal("100")}
    ):
        with pytest.raises(Exception):
            await OnChainProvider().get_holdings({"addresses": [f"solana:{A}"]})


@pytest.mark.asyncio
async def test_an_unvouched_token_cannot_push_a_real_holding_out_of_the_payload():
    """The cap ranks vouched first, so minting a fake price cannot displace.

    Ranking on value alone would let anyone mint `MAX_TOKENS_PER_ADDRESS`
    tokens quoted at a million dollars, sort them above every real position,
    and have the sync layer archive what fell off the end.
    """
    original, _ = onchain.SOLANA_TOKEN_PROGRAMS
    fakes = [f"fake{i}".ljust(44, "9") for i in range(onchain.MAX_TOKENS_PER_ADDRESS)]
    handler = _token_handler(
        accounts={original: [_token_account(m, "1000000", 6) for m in fakes + [MINT_A]]},
        jupiter=[_jupiter_row(m, f"SCAM{i}", "1000000", False) for i, m in enumerate(fakes)]
        + [_jupiter_row(MINT_A, "USDC", "1", True)],
    )
    with _settings(), _patched_client(handler):
        held = (await onchain.token_holdings(SOL, A)).items
    assert len(held) == onchain.MAX_TOKENS_PER_ADDRESS
    assert held[0].symbol == "USDC"
    assert "USDC" in {t.symbol for t in held}


@pytest.mark.asyncio
async def test_connecting_stores_addresses_and_never_a_secret():
    provider = OnChainProvider()

    async def fake_prices():
        return {"SOL": Decimal("150")}

    with (
        _settings(),
        _patched_client(_solana_handler(balances={A: 1_000_000_000})),
        patch("app.providers.onchain.usd_spot_prices", fake_prices),
    ):
        connection = await provider.handle_oauth_callback(f"solana:{A}")
    assert connection.credentials == {"addresses": [f"solana:{A}"]}
    assert connection.accounts[0].type == "investment"
    # bank_connections.external_id is VARCHAR(255); the addresses are ~50
    # characters each and a connection may hold 25 of them.
    assert len(connection.external_id) <= 255


@pytest.mark.asyncio
async def test_the_connection_id_is_the_address_set_not_its_length():
    """Reconnecting the same wallets must land on the same connection."""
    provider = OnChainProvider()

    async def fake_prices():
        return {"SOL": Decimal("150")}

    blob = "\n".join(f"{BASE58[i]}{A[1:]}" for i in range(onchain.MAX_WATCHED_ADDRESSES))
    balances = {f"{BASE58[i]}{A[1:]}": 0 for i in range(onchain.MAX_WATCHED_ADDRESSES)}
    with (
        _settings(),
        _patched_client(_solana_handler(balances=balances)),
        patch("app.providers.onchain.usd_spot_prices", fake_prices),
    ):
        first = await provider.handle_oauth_callback(blob)
        # Same set, pasted in a different order.
        second = await provider.handle_oauth_callback("\n".join(reversed(blob.splitlines())))
    assert len(first.external_id) <= 255
    assert first.external_id == second.external_id


# ----- trace ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_trace_follows_the_money_forward_and_inherits_each_hop_s_horizon():
    """A → B → C, the shape of a drainer sweep.

    B also received an unrelated payment out to D *before* A's funds arrived.
    A trace that ignored the timestamp would report D as a destination of A's
    money, which is the failure this window exists to prevent.
    """
    signatures = {
        A: [_sig("a_out", JAN23)],
        B: [_sig("b_old", JAN23 - 7200), _sig("b_out", JAN23 + 300)],
        C: [],
    }
    txs = {
        "a_out": _tx(JAN23, {A: -16_797_117_111, B: 16_797_112_111}),
        "b_old": _tx(JAN23 - 7200, {B: -5_000_000_000, D: 5_000_000_000}),
        "b_out": _tx(JAN23 + 300, {B: -16_797_112_111, C: 16_797_107_111}),
    }
    handler = _solana_handler(signatures=signatures, txs=txs, balances={A: 0, B: 0, C: 0})
    with _settings(), _patched_client(handler):
        result = await onchain_trace.trace("solana", A, max_hops=3)

    hops = [(e.source, e.target, e.amount) for e in result.edges]
    assert hops == [
        (f"solana:{A}", f"solana:{B}", Decimal("16.797117111")),
        (f"solana:{B}", f"solana:{C}", Decimal("16.797112111")),
    ]
    assert f"solana:{D}" not in {n.id for n in result.nodes}


@pytest.mark.asyncio
async def test_a_trace_stops_at_a_pooled_address_and_says_why():
    def handler(request):
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if method == "getBalance":
            return httpx.Response(200, json={"result": {"value": 0}, "id": 1})
        if method == "getSignaturesForAddress":
            if params[0] == B:
                rows = _full_page(600, newest=JAN23 + 86400)
            else:
                rows = [_sig("a_out", JAN23)]
            return httpx.Response(200, json={"result": rows, "id": 1})
        return httpx.Response(
            200,
            json={"result": _tx(JAN23, {A: -40_831_514_781, B: 40_831_514_781}), "id": 1},
        )

    with _settings(), _patched_client(handler):
        result = await onchain_trace.trace("solana", A, max_hops=4)

    exchange = next(n for n in result.nodes if n.address == B)
    assert exchange.terminal_reason == onchain_trace.TERMINAL_POOLED
    assert len(result.edges) == 1


@pytest.mark.asyncio
async def test_dust_below_the_floor_is_not_followed():
    signatures = {A: [_sig("dust", JAN23), _sig("real", JAN23 + 1)], B: [], C: []}
    txs = {
        "dust": _tx(JAN23, {A: -1000, C: 1000}),
        "real": _tx(JAN23 + 1, {A: -5_000_000_000, B: 5_000_000_000}),
    }
    with _settings(), _patched_client(_solana_handler(signatures=signatures, txs=txs)):
        result = await onchain_trace.trace(
            "solana", A, max_hops=2, min_amount=Decimal("0.01")
        )
    assert [e.target for e in result.edges] == [f"solana:{B}"]


@pytest.mark.asyncio
async def test_only_the_largest_branches_are_followed():
    signatures = {
        A: [_sig("s1", JAN23), _sig("s2", JAN23 + 1), _sig("s3", JAN23 + 2)],
        B: [],
        C: [],
        D: [],
    }
    txs = {
        "s1": _tx(JAN23, {A: -1_000_000_000, B: 1_000_000_000}),
        "s2": _tx(JAN23 + 1, {A: -9_000_000_000, C: 9_000_000_000}),
        "s3": _tx(JAN23 + 2, {A: -5_000_000_000, D: 5_000_000_000}),
    }
    with _settings(), _patched_client(_solana_handler(signatures=signatures, txs=txs)):
        result = await onchain_trace.trace("solana", A, max_hops=1, max_branches=2)
    assert {e.target for e in result.edges} == {f"solana:{C}", f"solana:{D}"}


@pytest.mark.asyncio
async def test_tracing_backwards_finds_where_the_money_came_from():
    signatures = {A: [_sig("in", JAN23)], B: [_sig("in", JAN23)]}
    txs = {"in": _tx(JAN23, {B: -3_000_000_000, A: 3_000_000_000})}
    with _settings(), _patched_client(_solana_handler(signatures=signatures, txs=txs)):
        result = await onchain_trace.trace("solana", A, direction="in", max_hops=1)
    assert [(e.source, e.target) for e in result.edges] == [(f"solana:{B}", f"solana:{A}")]


@pytest.mark.asyncio
async def test_an_unknown_chain_is_rejected_by_name():
    with pytest.raises(ValueError) as exc:
        await onchain_trace.trace("dogecoin", A)
    assert "solana" in str(exc.value)


@pytest.mark.asyncio
async def test_an_unreadable_hop_ends_that_branch_instead_of_the_whole_trace():
    def handler(request):
        body = json.loads(request.content)
        params = body["params"]
        if body["method"] == "getBalance":
            return httpx.Response(200, json={"result": {"value": 0}, "id": 1})
        if body["method"] == "getSignaturesForAddress":
            if params[0] == B:
                return httpx.Response(500)
            return httpx.Response(200, json={"result": [_sig("a_out", JAN23)], "id": 1})
        return httpx.Response(
            200, json={"result": _tx(JAN23, {A: -1_000_000_000, B: 1_000_000_000}), "id": 1}
        )

    with _settings(), _patched_client(handler):
        result = await onchain_trace.trace("solana", A, max_hops=3)
    assert len(result.edges) == 1
    assert next(n for n in result.nodes if n.address == B).terminal_reason == (
        onchain_trace.TERMINAL_UNAVAILABLE
    )


@pytest.mark.asyncio
async def test_a_capped_window_keeps_the_transfers_nearest_the_arrival_not_the_newest():
    """The bug this guards: money leaves soon after it arrives.

    An address with months of later history has plenty of large transfers, and
    keeping the newest ones traces a real path that is not *this* money's path.
    """
    arrival = JAN23
    rows = [_sig("later", arrival + 60 * 86400), _sig("sweep", arrival + 120)]
    txs = {
        "later": _tx(arrival + 60 * 86400, {A: -99_000_000_000, C: 99_000_000_000}),
        "sweep": _tx(arrival + 120, {A: -16_797_112_111, B: 16_797_112_111}),
    }
    handler = _solana_handler(signatures={A: rows}, txs=txs)
    since = datetime.fromtimestamp(arrival, tz=timezone.utc)
    with _settings(), _patched_client(handler):
        page = await onchain.transfers(SOL, A, limit=1, since=since)
    [transfer] = page.items
    assert transfer.recipient == B
    assert transfer.reference == "sweep"


@pytest.mark.asyncio
async def test_without_a_floor_the_newest_transfers_are_still_the_ones_kept():
    rows = [_sig("newest", JAN23), _sig("oldest", JAN23 - 86400)]
    txs = {
        "newest": _tx(JAN23, {A: -10**9, B: 10**9}),
        "oldest": _tx(JAN23 - 86400, {A: -10**9, C: 10**9}),
    }
    with _settings(), _patched_client(_solana_handler(signatures={A: rows}, txs=txs)):
        page = await onchain.transfers(SOL, A, limit=1)
    assert page.items[0].reference == "newest"


# ----- the walk must never overstate what it saw ----------------------------


@pytest.mark.asyncio
async def test_an_unattributable_transaction_is_unknown_not_absent():
    """A swap moves value between parties this code cannot pair up.

    Reporting the largest opposing account as the counterparty would attach a
    real signature to a wallet that received nothing, and the trace would then
    walk that wallet's history as if it were this money's path.
    """
    handler = _solana_handler(
        signatures={A: [_sig("swap", JAN23)]},
        # A pays B a little while D pays C a lot, in one transaction.
        txs={
            "swap": _tx(
                JAN23,
                {A: -1_000_000_000, B: 1_000_000_000, D: -50_000_000_000, C: 50_000_000_000},
            )
        },
    )
    with _settings(), _patched_client(handler):
        page = await onchain.transfers(SOL, A, limit=25)
    assert [t.recipient for t in page.items] == [B]


@pytest.mark.asyncio
async def test_a_transaction_the_node_will_not_return_is_counted_as_unreadable():
    handler = _solana_handler(signatures={A: [_sig("gone", JAN23)]}, txs={})
    with _settings(), _patched_client(handler):
        page = await onchain.transfers(SOL, A, limit=25)
    assert page.items == []
    assert page.unreadable == 1
    assert page.complete is False


@pytest.mark.asyncio
async def test_a_trimmed_window_is_reported_as_incomplete():
    rows = [_sig(f"s{i}", JAN23 + i) for i in range(5)]
    handler = _solana_handler(
        signatures={A: rows},
        txs={r["signature"]: _tx(r["blockTime"], {A: -10**9, B: 10**9}) for r in rows},
    )
    with _settings(), _patched_client(handler):
        page = await onchain.transfers(SOL, A, limit=2)
    assert page.trimmed is True and page.complete is False


@pytest.mark.asyncio
async def test_a_dusted_wallet_is_never_reported_as_having_moved_nothing():
    """The false-exoneration case, and the reason `complete` exists.

    A drained wallet gets dust-spammed afterwards. The sweep sits below the
    dust in a newest-first page, so a capped read can miss it entirely — and
    "nothing has moved out of this address" would be a lie told about a wallet
    that was emptied.
    """
    dust = [_sig(f"d{i}", JAN23 + 100 + i) for i in range(30)]
    rows = dust + [_sig("sweep", JAN23)]
    txs = {r["signature"]: _tx(r["blockTime"], {A: 1_000, C: -1_000}) for r in dust}
    txs["sweep"] = _tx(JAN23, {A: -500_000_000_000, B: 500_000_000_000})
    handler = _solana_handler(signatures={A: rows}, txs=txs, balances={A: 0, B: 0, C: 0})
    with _settings(), _patched_client(handler):
        result = await onchain_trace.trace("solana", A, max_hops=2)
    root = next(n for n in result.nodes if n.address == A)
    assert root.terminal_reason != onchain_trace.TERMINAL_NO_MOVEMENT
    assert result.truncated or root.terminal_reason == onchain_trace.TERMINAL_PARTIAL


@pytest.mark.asyncio
async def test_a_filter_that_matched_nothing_is_not_a_claim_that_nothing_moved():
    signatures = {A: [_sig("small", JAN23)], C: []}
    txs = {"small": _tx(JAN23, {A: -1_000_000, C: 1_000_000})}
    with _settings(), _patched_client(_solana_handler(signatures=signatures, txs=txs)):
        result = await onchain_trace.trace(
            "solana", A, max_hops=2, min_amount=Decimal("100")
        )
    root = next(n for n in result.nodes if n.address == A)
    assert root.terminal_reason == onchain_trace.TERMINAL_NO_MATCH


@pytest.mark.asyncio
async def test_a_deployment_with_no_history_source_fails_the_whole_evm_trace():
    """Not one unreadable node — no source exists for any address alike."""
    with (
        _settings(),
        patch.dict(onchain.CHAINS, {"base": replace(BASE, token_index_url=None)}),
        _patched_client(lambda request: httpx.Response(200, json={})),
    ):
        with pytest.raises(ProviderNotConfiguredError):
            await onchain_trace.trace("base", EVM, max_hops=2)


@pytest.mark.asyncio
async def test_a_throttle_partway_through_keeps_the_trail_it_already_walked(_no_backoff):
    """A trail that is real as far as it goes beats an error that discards it.

    The throttle still ends the walk — it will refuse every address left, not
    just this one — but the addresses it never reached say `rate_limited`
    rather than going unmentioned.
    """
    reached = _solana_handler(
        signatures={A: [_sig("out", JAN23)]},
        txs={"out": _tx(JAN23, {A: -2_000_000_000, C: 2_000_000_000})},
    )

    def handler(request):
        body = json.loads(request.content)
        if body["method"] == "getSignaturesForAddress" and body["params"][0] != A:
            return httpx.Response(429)
        return reached(request)

    with _settings(), _patched_client(handler):
        result = await onchain_trace.trace("solana", A, max_hops=3)

    assert [edge.target for edge in result.edges] == [f"solana:{C}"]
    assert result.truncated
    unreached = next(node for node in result.nodes if node.address == C)
    assert unreached.terminal_reason == onchain_trace.TERMINAL_RATE_LIMITED


@pytest.mark.asyncio
async def test_a_throttled_node_fails_the_trace_rather_than_ending_the_trail(_no_backoff):
    with _settings(), _patched_client(lambda request: httpx.Response(429)):
        with pytest.raises(ProviderRateLimited):
            await onchain_trace.trace("solana", A, max_hops=2)


@pytest.mark.asyncio
async def test_a_malformed_address_is_rejected_before_any_request():
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"result": None, "id": 1})

    with _settings(), _patched_client(handler):
        with pytest.raises(ValueError):
            await onchain_trace.trace("solana", "not-an-address")
    assert calls == []


@pytest.mark.asyncio
async def test_a_naive_window_is_read_as_utc_not_as_the_server_s_local_time():
    seen: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        if body["method"] == "getSignaturesForAddress":
            return httpx.Response(
                200, json={"result": [_sig("a", JAN23), _sig("b", JAN23 - 7200)], "id": 1}
            )
        if body["method"] == "getTransaction":
            seen.append(body)
            return httpx.Response(
                200,
                json={"result": _tx(JAN23, {A: -10**9, B: 10**9}), "id": 1},
            )
        return httpx.Response(200, json={"result": {"value": 0}, "id": 1})

    naive = datetime.fromtimestamp(JAN23 - 3600, tz=timezone.utc).replace(tzinfo=None)
    with _settings(), _patched_client(handler):
        await onchain_trace.trace("solana", A, max_hops=1, since=naive)
    # Only the signature inside the UTC window was worth fetching. Read in a
    # non-UTC server zone the window would slide and pick up the other one.
    assert len(seen) == 1
