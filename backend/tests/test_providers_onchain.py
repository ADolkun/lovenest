"""On-chain watch-only wallets and the transfer trace.

Every chain node is faked through httpx.MockTransport, so nothing here
touches the network and no address in this file is anybody's wallet.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

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


def _patched_client(handler):
    """Point the module's shared client factory at a MockTransport."""
    transport = httpx.MockTransport(handler)

    def fake_client():
        return httpx.AsyncClient(transport=transport, timeout=5)

    return patch.object(onchain, "_client", fake_client)


def _settings(**overrides):
    base = {
        "onchain_rpc_urls": {},
        "etherscan_api_key": "",
        "coinbase_api_url": "https://api.coinbase.com",
    }
    base.update(overrides)
    return patch.object(onchain, "get_settings", lambda: SimpleNamespace(**base))


def _sig(signature: str, block_time: int, err=None) -> dict:
    return {"signature": signature, "blockTime": block_time, "err": err, "slot": 1}


def _tx(block_time: int, deltas: dict[str, int]) -> dict:
    """A Solana transaction whose only evidence is its balance deltas."""
    keys = list(deltas)
    return {
        "blockTime": block_time,
        "meta": {
            "preBalances": [10**12] * len(keys),
            "postBalances": [10**12 + deltas[k] for k in keys],
        },
        "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in keys]}},
    }


def _solana_handler(*, balances=None, signatures=None, txs=None):
    """Serve getBalance / getSignaturesForAddress / getTransaction from dicts."""
    balances = balances or {}
    signatures = signatures or {}
    txs = txs or {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if method == "getBalance":
            return httpx.Response(
                200, json={"result": {"value": balances.get(params[0], 0)}, "id": 1}
            )
        if method == "getSignaturesForAddress":
            return httpx.Response(200, json={"result": signatures.get(params[0], []), "id": 1})
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
    # Every signature is newer than the window we asked about, and the page
    # came back full — so the transfers in question exist beyond it.
    rows = _full_page(90 * 86400, newest=JAN23 + 400 * 86400)
    handler = _solana_handler(signatures={A: rows})
    since = datetime.fromtimestamp(JAN23, tz=timezone.utc)
    with _settings(), _patched_client(handler):
        page = await onchain.transfers(SOL, A, limit=25, since=since)
    assert page.saturated == onchain.SATURATED_UNPAGEABLE


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


@pytest.mark.asyncio
async def test_tracing_an_evm_chain_without_an_explorer_key_says_so_rather_than_returning_nothing():
    with _settings(), _patched_client(lambda request: httpx.Response(200, json={})):
        with pytest.raises(ProviderNotConfiguredError) as exc:
            await onchain.transfers(BASE, EVM, limit=25)
    assert "ETHERSCAN_API_KEY" in str(exc.value)


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
            return httpx.Response(200, json={"status": "0", "result": []})
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
    assert holding.metadata == {"chain": "solana", "address": A, "watch_only": True}


@pytest.mark.asyncio
async def test_an_address_the_node_cannot_answer_for_is_skipped_not_zeroed():
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
        assert await provider.get_holdings({"addresses": [f"solana:{A}"]}) == []


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
async def test_a_deployment_without_an_explorer_key_fails_the_whole_evm_trace():
    """Not one unreadable node — the key is missing for every address alike."""
    with _settings(), _patched_client(lambda request: httpx.Response(200, json={})):
        with pytest.raises(ProviderNotConfiguredError):
            await onchain_trace.trace("base", EVM, max_hops=2)


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
