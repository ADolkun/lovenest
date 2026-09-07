"""One bounded synthetic observation through the real connection sync path."""

import asyncio
import json
import uuid
from collections import Counter
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.models.account import Account
from app.models.workspace import Workspace
from app.providers import onchain, register_provider
from app.providers.base import PartialHoldings
from app.providers.coinbase import CoinbaseProvider
from app.providers.onchain import OnChainProvider
from app.services import connection_service
from app.services.asset_group_service import get_group
from app.services.connection_service import handle_oauth_callback, sync_connection
from tests.test_onchain_holdings_sync import _assets, _connection, _wallet_handler
from tests import test_onchain_rpc
from tests.test_providers_onchain import (
    A, B, MINT_A, _jupiter_row, _patched_client, _settings, _token_account,
)

pytestmark = pytest.mark.asyncio
clocked_rpc = test_onchain_rpc.clocked_rpc


@contextmanager
def _clients(handler):
    """Fake Coinbase's real spot GET as well as every chain/index request."""
    async def spot_client(self):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://spot.synthetic.invalid"
        )

    with _patched_client(handler), patch.object(CoinbaseProvider, "_client", spot_client):
        yield


def _counting_wallet(counts, *, native=10**9, mints=None, index_status=200):
    wallet = _wallet_handler(
        mints=mints if mints is not None else {MINT_A: ("5000000", "USDC", True)},
        native=native,
    )

    def handler(request):
        if request.url.host == "spot.synthetic.invalid":
            counts["spot"] += 1
            return httpx.Response(200, json={"data": {"rates": {"SOL": "0.01"}}})
        if "jup.ag" in request.url.host:
            counts["jupiter"] += 1
            if index_status != 200:
                return httpx.Response(index_status)
        else:
            body = json.loads(request.content)
            key = body["method"]
            if key == "getTokenAccountsByOwner":
                key = body["params"][1]["programId"]
            counts[key] += 1
        return wallet(request)

    return handler


async def test_actual_sync_reads_accounts_and_holdings_once_and_next_sync_is_fresh(
    session, test_user, test_workspace,
):
    register_provider("onchain", OnChainProvider)
    conn = await _connection(session, test_user, test_workspace)
    conn.credentials = {"addresses": [f"solana:{A}", f"solana:{B}"]}
    await session.commit()
    counts = Counter()
    with _settings(), _clients(_counting_wallet(counts)):
        await sync_connection(session, conn.id, test_workspace.id, test_user.id)
    assert counts == Counter({
        "spot": 1, "getBalance": 2, "jupiter": 1,
        **{program: 2 for program in onchain.SOLANA_TOKEN_PROGRAMS},
    })
    [account] = (await session.execute(select(Account).where(Account.connection_id == conn.id))).scalars().all()
    assert account.balance == Decimal("210")
    assert len(await _assets(session, conn)) == 4

    counts.clear()
    with _settings(), _clients(_counting_wallet(counts, native=2 * 10**9)):
        await sync_connection(session, conn.id, test_workspace.id, test_user.id)
    assert counts["spot"] == 1
    assert counts["getBalance"] == 2
    assert counts["jupiter"] == 1
    await session.refresh(account)
    assert account.balance == Decimal("410")


async def test_partial_sync_keeps_prior_total_and_assets_then_heals_under_allowlist(
    session, test_user, test_workspace,
):
    register_provider("onchain", OnChainProvider)
    conn = await _connection(session, test_user, test_workspace)
    conn.settings = {"account_allowlist": [onchain.ACCOUNT_EXTERNAL_ID]}
    await session.commit()
    counts = Counter()
    with _settings(), _clients(_counting_wallet(counts)):
        await sync_connection(session, conn.id, test_workspace.id, test_user.id)
    [account] = (await session.execute(select(Account).where(Account.connection_id == conn.id))).scalars().all()
    assert account.balance == Decimal("105")
    last_sync_at = conn.last_sync_at

    counts.clear()
    with _settings(), _clients(_counting_wallet(counts, native=2 * 10**9, index_status=503)):
        await sync_connection(session, conn.id, test_workspace.id, test_user.id)
    assert counts["getBalance"] == counts["jupiter"] == counts["spot"] == 1
    assets = await _assets(session, conn)
    assert assets[f"solana:{A}:{MINT_A}"].is_archived is False
    assert assets[f"solana:{A}"].units == Decimal("2")
    await session.refresh(account)
    assert account.balance == Decimal("105")
    assert conn.status == "sync_error"
    assert conn.last_sync_at == last_sync_at
    assert conn.settings is not None
    assert conn.settings["unavailable_account_balance_ids"] == [onchain.ACCOUNT_EXTERNAL_ID]
    group_id = assets[f"solana:{A}"].group_id
    assert group_id is not None
    group = await get_group(session, group_id, test_workspace.id, test_user.id)
    assert group is not None
    assert group.account_balance is None
    assert group.account_id == account.id

    counts.clear()
    with _settings(), _clients(_counting_wallet(counts, native=2 * 10**9, mints={})):
        await sync_connection(session, conn.id, test_workspace.id, test_user.id)
    assert counts["getBalance"] == counts["spot"] == 1
    assert (await _assets(session, conn))[f"solana:{A}:{MINT_A}"].is_archived is True
    await session.refresh(account)
    assert account.balance == Decimal("200")
    assert conn.status == "active"
    assert "unavailable_account_balance_ids" not in conn.settings


async def test_first_partial_sync_keeps_good_holdings_without_creating_zero_balance_account(
    session, test_user, test_workspace,
):
    register_provider("onchain", OnChainProvider)
    conn = await _connection(session, test_user, test_workspace)
    conn.settings = {"account_allowlist": [onchain.ACCOUNT_EXTERNAL_ID]}
    await session.commit()
    counts = Counter()
    with _settings(), _clients(_counting_wallet(counts, index_status=503)):
        await sync_connection(session, conn.id, test_workspace.id, test_user.id)
    accounts = (await session.execute(select(Account).where(Account.connection_id == conn.id))).scalars().all()
    assert accounts == []
    assert (await _assets(session, conn))[f"solana:{A}"].units == 1
    assert counts["getBalance"] == counts["jupiter"] == 1


async def test_partial_observation_reuse_preserves_scope_and_is_consumed_once():
    counts = Counter()
    provider = OnChainProvider()
    credentials = {"addresses": [f"solana:{A}"]}
    with _settings(), _clients(_counting_wallet(counts, index_status=503)):
        [account] = await provider.get_accounts(credentials)
        assert account.balance is None
        for expected_calls in (1, 2):
            with pytest.raises(PartialHoldings) as raised:
                await provider.get_holdings(credentials)
            assert raised.value.unreadable
            assert raised.value.holdings
            assert counts["getBalance"] == expected_calls
            assert counts["jupiter"] == expected_calls


@pytest.mark.parametrize("change", ["addresses", "source", "credentials", "provider", "expired"])
async def test_incompatible_or_expired_observations_are_not_reused(change, clocked_rpc):
    clock, _ = clocked_rpc
    counts = Counter()
    credentials = {"addresses": [f"solana:{A}"]}
    provider = OnChainProvider()
    with _settings(), _clients(_counting_wallet(counts)):
        await provider.get_accounts(credentials)
        if change == "addresses":
            credentials = {"addresses": [f"solana:{A}", f"solana:{B}"]}
        elif change == "credentials":
            credentials = {**credentials, "connection_id": "synthetic-other-connection"}
        elif change == "provider":
            provider = OnChainProvider()
        elif change == "expired":
            clock.now += onchain.HOLDINGS_REUSE_SECONDS + 1
        if change == "source":
            with _settings(onchain_rpc_urls={"solana": "https://changed.synthetic.invalid/rpc"}):
                await provider.get_holdings(credentials)
        else:
            await provider.get_holdings(credentials)
    assert counts["spot"] == 2
    assert counts["getBalance"] == (3 if change == "addresses" else 2)


async def test_normalized_address_order_reuses_one_observation():
    counts = Counter()
    provider = OnChainProvider()
    with _settings(), _clients(_counting_wallet(counts)):
        await provider.get_accounts({"addresses": [f"solana:{A}", f"solana:{B}"]})
        await provider.get_holdings({"addresses": [f"solana:{B}", f"solana:{A}"]})
    assert counts["spot"] == 1
    assert counts["getBalance"] == 2


async def test_capped_token_inventory_preserves_omitted_vouched_asset(
    session, test_user, test_workspace,
):
    register_provider("onchain", OnChainProvider)
    conn = await _connection(session, test_user, test_workspace)
    counts = Counter()
    with _settings(), _clients(_counting_wallet(counts)):
        await sync_connection(session, conn.id, test_workspace.id, test_user.id)
    mints = {f"synthetic-mint-{i:04}": ("1000000", f"JUNK{i}", False) for i in range(120)}
    mints[MINT_A] = ("5000000", "USDC", True)
    counts.clear()
    with _settings(), _clients(_counting_wallet(counts, mints=mints)):
        await sync_connection(session, conn.id, test_workspace.id, test_user.id)
    assert counts["jupiter"] == onchain.MAX_JUPITER_BATCHES_PER_ADDRESS
    assets = await _assets(session, conn)
    assert assets[f"solana:{A}:{MINT_A}"].is_archived is False
    assert assets[f"solana:{A}:{MINT_A}"].units == 5
    assert all(asset.ticker is None for key, asset in assets.items() if "synthetic-mint" in key)


async def test_observation_jupiter_budget_is_shared_by_all_addresses(monkeypatch):
    counts = Counter()
    monkeypatch.setattr(onchain, "MAX_JUPITER_BATCHES", 3)
    mints = {f"synthetic-mint-{i:04}": ("1000000", f"T{i}", True) for i in range(101)}
    provider = OnChainProvider()
    with _settings(), _clients(_counting_wallet(counts, mints=mints)):
        with pytest.raises(PartialHoldings) as raised:
            await provider.get_holdings({"addresses": [f"solana:{A}", f"solana:{B}"]})
    assert counts["jupiter"] == 3
    assert counts["getBalance"] == 2
    assert len(raised.value.unreadable) >= 2


@pytest.mark.parametrize("kind", ["null_program", "malformed_quantity", "null_index", "missing_price"])
async def test_unknown_token_data_is_partial_and_never_a_complete_zero(kind):
    good = _counting_wallet(Counter())

    def handler(request):
        if "jup.ag" in request.url.host:
            if kind == "null_index":
                return httpx.Response(200, json=None)
            if kind == "missing_price":
                return httpx.Response(200, json=[_jupiter_row(MINT_A, "USDC", None, True)])
        if request.method == "POST":
            method = json.loads(request.content)["method"]
            if method == "getTokenAccountsByOwner":
                if kind == "null_program":
                    return httpx.Response(200, json={"result": {"value": None}})
                if kind == "malformed_quantity":
                    return httpx.Response(200, json={"result": {"value": [_token_account(MINT_A, "bad", 6)]}})
        return good(request)

    provider = OnChainProvider()
    with _settings(), _clients(handler):
        [account] = await provider.get_accounts({"addresses": [f"solana:{A}"]})
        assert account.balance is None
        with pytest.raises(PartialHoldings) as raised:
            await provider.get_holdings({"addresses": [f"solana:{A}"]})
    assert raised.value.holdings[0].quantity == 1


async def test_explicit_zero_native_balance_is_complete_without_a_price():
    counts = Counter()
    with _settings(), _clients(_counting_wallet(counts, native=0, mints={})), patch.object(
        onchain, "usd_spot_prices", AsyncMock(return_value={})
    ):
        provider = OnChainProvider()
        [account] = await provider.get_accounts({"addresses": [f"solana:{A}"]})
        [holding] = await provider.get_holdings({"addresses": [f"solana:{A}"]})
    assert account.balance == 0
    assert holding.quantity == holding.current_value == 0


async def test_deadline_retains_completed_address_and_starts_no_later_requests(clocked_rpc):
    clock, _ = clocked_rpc
    counts = Counter()
    good = _counting_wallet(counts)

    def handler(request):
        response = good(request)
        if "jup.ag" in request.url.host:
            clock.now = onchain.ONCHAIN_SYNC_SECONDS + 1
        return response

    with _settings(), _clients(handler):
        with pytest.raises(PartialHoldings) as raised:
            await OnChainProvider().get_holdings({"addresses": [f"solana:{A}", f"solana:{B}"]})
    assert counts["getBalance"] == 1
    assert counts["jupiter"] == 1
    assert raised.value.unreadable
    assert any(holding.quantity == 1 for holding in raised.value.holdings)


async def test_cancelled_observation_is_not_reused():
    started = asyncio.Event()
    counts = Counter()
    good = _counting_wallet(counts)

    async def blocked(request):
        if "jup.ag" in request.url.host:
            started.set()
            await asyncio.Event().wait()
        return good(request)

    provider = OnChainProvider()
    credentials = {"addresses": [f"solana:{A}"]}
    with _settings(), _clients(blocked):
        task = asyncio.create_task(provider.get_accounts(credentials))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    counts.clear()
    with _settings(), _clients(good):
        holdings = await provider.get_holdings(credentials)
    assert len(holdings) == 2
    assert counts["spot"] == counts["jupiter"] == counts["getBalance"] == 1


@pytest.mark.parametrize("header,delay", [("10", 10), (None, 1.5), ("invalid", 1.5)])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_jupiter_retries_use_shared_cooldown_and_reuse_the_result(
    clocked_rpc, header, delay, exhausted,
):
    clock, coordinator = clocked_rpc
    counts = Counter()
    good = _counting_wallet(counts)
    attempts = []

    def handler(request):
        if "jup.ag" in request.url.host:
            attempts.append(clock.now)
            if exhausted or len(attempts) == 1:
                return httpx.Response(429, headers={"Retry-After": header} if header else {})
        return good(request)

    provider = OnChainProvider()
    credentials = {"addresses": [f"solana:{A}"]}
    with _settings(), _clients(handler), patch.object(
        test_onchain_rpc.rpc, "_redis_client", lambda: coordinator,
    ):
        [account] = await provider.get_accounts(credentials)
        if exhausted:
            assert account.balance is None
            with pytest.raises(PartialHoldings):
                await provider.get_holdings(credentials)
        else:
            assert account.balance == Decimal("105")
            assert len(await provider.get_holdings(credentials)) == 2
    assert attempts[:2] == [0, delay]
    assert len(attempts) == (3 if exhausted else 2)
    assert counts["getBalance"] == counts["spot"] == 1


async def test_later_jupiter_batch_failure_keeps_successful_batch_and_coverage(clocked_rpc):
    clock, coordinator = clocked_rpc
    counts = Counter()
    mints = {f"synthetic-mint-{i:04}": ("1000000", f"T{i}", True) for i in range(60)}
    good = _counting_wallet(counts, mints=mints)
    batches = []

    def handler(request):
        if "jup.ag" in request.url.host:
            query = request.url.params["query"]
            batches.append(query)
            if query != batches[0]:
                return httpx.Response(429, headers={"Retry-After": "10"})
        return good(request)

    provider = OnChainProvider()
    credentials = {"addresses": [f"solana:{A}"]}
    with _settings(), _clients(handler), patch.object(
        test_onchain_rpc.rpc, "_redis_client", lambda: coordinator,
    ):
        [account] = await provider.get_accounts(credentials)
        with pytest.raises(PartialHoldings) as raised:
            await provider.get_holdings(credentials)
    assert account.balance is None
    assert len(batches) == 4
    assert len(set(batches)) == 2
    assert clock.now < onchain.ONCHAIN_SYNC_SECONDS
    assert len(raised.value.holdings) == onchain.MAX_TOKENS_PER_ADDRESS + 1
    assert raised.value.unreadable
    assert all(holding.metadata for holding in raised.value.holdings)


async def test_long_jupiter_cooldown_stops_without_waiting_past_observation_deadline(clocked_rpc):
    clock, coordinator = clocked_rpc
    counts = Counter()
    good = _counting_wallet(counts)
    attempts = []

    def handler(request):
        if "jup.ag" in request.url.host:
            attempts.append(clock.now)
            return httpx.Response(429, headers={"Retry-After": "120"})
        return good(request)

    with _settings(), _clients(handler), patch.object(
        test_onchain_rpc.rpc, "_redis_client", lambda: coordinator,
    ):
        with pytest.raises(PartialHoldings) as raised:
            await OnChainProvider().get_holdings({"addresses": [f"solana:{A}"]})
    assert attempts == [0]
    assert coordinator.cooldown_until == 120
    assert clock.now < onchain.ONCHAIN_SYNC_SECONDS
    assert raised.value.holdings[0].quantity == 1


@pytest.mark.parametrize("review_first", [False, True])
async def test_first_connect_retains_unknown_account_identity_and_review_selection(
    session, test_user, test_workspace, client, auth_headers, review_first,
):
    register_provider("onchain", OnChainProvider)
    counts = Counter()
    with _settings(), _clients(_counting_wallet(counts, index_status=503)):
        connection = await handle_oauth_callback(
            session, test_workspace.id, test_user.id, f"solana:{A}",
            provider_name="onchain", account_allowlist=[] if review_first else None,
        )
        assert counts["spot"] == counts["getBalance"] == counts["jupiter"] == 1
        accounts = (await session.execute(select(Account).where(Account.connection_id == connection.id))).scalars().all()
        assert accounts == []
        assets = await _assets(session, connection)
        assert len(assets) == (0 if review_first else 1)
        if review_first:
            assert (connection.settings or {})["account_allowlist"] == []
        response = await client.get(
            f"/api/connections/{connection.id}/provider-accounts", headers=auth_headers,
        )
    assert response.status_code == 200
    [candidate] = response.json()
    assert candidate["external_id"] == onchain.ACCOUNT_EXTERNAL_ID
    assert candidate["balance"] is None


async def test_same_addresses_in_two_workspaces_read_independent_observations(
    session, test_user, test_workspace,
):
    register_provider("onchain", OnChainProvider)
    other = Workspace(id=uuid.uuid4(), name="Synthetic other", created_by_user_id=test_user.id)
    session.add(other)
    await session.commit()
    counts = Counter()
    connections = []
    for workspace, native in ((test_workspace, 10**9), (other, 2 * 10**9)):
        with _settings(), _clients(_counting_wallet(counts, native=native)):
            connections.append(await handle_oauth_callback(
                session, workspace.id, test_user.id, f"solana:{A}", provider_name="onchain",
            ))
    assert counts["spot"] == counts["getBalance"] == counts["jupiter"] == 2
    assert connections[0].id != connections[1].id
    first = (await _assets(session, connections[0]))[f"solana:{A}"]
    second = (await _assets(session, connections[1]))[f"solana:{A}"]
    assert first.workspace_id == test_workspace.id
    assert second.workspace_id == other.id
    assert first.units == 1
    assert second.units == 2


async def test_explicit_zero_token_quote_is_complete_and_keeps_quantity():
    good = _counting_wallet(Counter())

    def handler(request):
        if "jup.ag" in request.url.host:
            return httpx.Response(200, json=[_jupiter_row(MINT_A, "USDC", "0", True)])
        return good(request)

    provider = OnChainProvider()
    credentials = {"addresses": [f"solana:{A}"]}
    with _settings(), _clients(handler):
        [account] = await provider.get_accounts(credentials)
        holdings = await provider.get_holdings(credentials)
    token = next(holding for holding in holdings if holding.external_id.endswith(MINT_A))
    assert token.quantity == 5
    assert token.unit_price == token.current_value == 0
    assert account.balance == Decimal("100")
    assert provider.holdings_observation is not None
    assert provider.holdings_observation["complete"] is True


async def test_missing_native_quantity_preserves_successful_tokens_without_zero_native():
    good = _counting_wallet(Counter())

    def handler(request):
        if request.method == "POST" and json.loads(request.content)["method"] == "getBalance":
            return httpx.Response(200, json={"result": {"value": None}})
        return good(request)

    provider = OnChainProvider()
    credentials = {"addresses": [f"solana:{A}"]}
    with _settings(), _clients(handler):
        [account] = await provider.get_accounts(credentials)
        with pytest.raises(PartialHoldings) as raised:
            await provider.get_holdings(credentials)
    assert account.balance is None
    [holding] = raised.value.holdings
    assert holding.external_id.endswith(MINT_A)
    assert holding.quantity == 5


@pytest.mark.parametrize("initial_complete", [True, False])
async def test_expired_sync_observation_never_combines_old_account_with_fresh_holdings(
    session, test_user, test_workspace, clocked_rpc, initial_complete,
):
    clock, _ = clocked_rpc
    register_provider("onchain", OnChainProvider)
    conn = await _connection(session, test_user, test_workspace)
    original_sync = connection_service._sync_holdings
    counts = Counter()
    before = _counting_wallet(counts, index_status=200 if initial_complete else 503)
    after = _counting_wallet(counts, native=2 * 10**9)

    def handler(request):
        return (before if clock.now == 0 else after)(request)

    async def after_database_work(*args, **kwargs):
        clock.now += onchain.HOLDINGS_REUSE_SECONDS + 1
        await original_sync(*args, **kwargs)

    with _settings(), _clients(handler), patch.object(
        connection_service, "_sync_holdings", after_database_work,
    ):
        await sync_connection(session, conn.id, test_workspace.id, test_user.id)
    assert counts["spot"] == counts["getBalance"] == counts["jupiter"] == 2
    native = (await _assets(session, conn))[f"solana:{A}"]
    assert native.units == 2
    assert native.group_id is not None
    group = await get_group(session, native.group_id, test_workspace.id, test_user.id)
    assert group is not None
    assert group.account_balance is None
    assert (conn.settings or {}).get("unavailable_account_balance_ids") == [onchain.ACCOUNT_EXTERNAL_ID]
    assert conn.status == "sync_error"
    assert conn.last_sync_at is None

    with _settings(), _clients(after):
        await sync_connection(session, conn.id, test_workspace.id, test_user.id)
    group = await get_group(session, native.group_id, test_workspace.id, test_user.id)
    assert group is not None
    assert group.account_balance == 205
    assert conn.status == "active"
    assert conn.last_sync_at is not None
    assert "unavailable_account_balance_ids" not in (conn.settings or {})


@pytest.mark.parametrize("skip", ["reconnect", "excluded", "disabled_holdings", "closed", "fetch_failed"])
async def test_incomplete_balance_requires_persisted_account_and_holdings_to_heal(
    session, test_user, test_workspace, skip,
):
    register_provider("onchain", OnChainProvider)
    conn = await _connection(session, test_user, test_workspace)
    with _settings(), _clients(_counting_wallet(Counter())):
        await sync_connection(session, conn.id, test_workspace.id, test_user.id)
    with _settings(), _clients(_counting_wallet(Counter(), native=2 * 10**9, index_status=503)):
        await sync_connection(session, conn.id, test_workspace.id, test_user.id)
    account = (await session.execute(
        select(Account).where(Account.connection_id == conn.id)
    )).scalar_one()
    native = (await _assets(session, conn))[f"solana:{A}"]
    assert account.balance == Decimal("105")
    assert native.units == 2
    assert native.group_id is not None
    group = await get_group(session, native.group_id, test_workspace.id, test_user.id)
    assert group is not None and group.account_balance is None

    if skip == "excluded":
        conn.settings = {**(conn.settings or {}), "account_allowlist": []}
    elif skip == "disabled_holdings":
        conn.settings = {**(conn.settings or {}), "sync_assets": False}
    elif skip == "closed":
        account.is_closed = True
    await session.commit()

    with _settings(), _clients(_counting_wallet(Counter(), native=3 * 10**9)):
        if skip == "reconnect":
            await handle_oauth_callback(
                session, test_workspace.id, test_user.id, f"solana:{A}",
                provider_name="onchain", reconnect_connection_id=conn.id,
            )
        elif skip == "fetch_failed":
            with patch.object(OnChainProvider, "get_holdings", side_effect=RuntimeError("Synthetic outage")):
                await sync_connection(session, conn.id, test_workspace.id, test_user.id)
        else:
            await sync_connection(session, conn.id, test_workspace.id, test_user.id)
    group = await get_group(session, native.group_id, test_workspace.id, test_user.id)
    assert group is not None and group.account_balance is None
    assert (conn.settings or {}).get("unavailable_account_balance_ids") == [onchain.ACCOUNT_EXTERNAL_ID]
    assert native.is_archived is False

    account.is_closed = False
    conn.settings = {
        **(conn.settings or {}), "account_allowlist": [onchain.ACCOUNT_EXTERNAL_ID], "sync_assets": True,
    }
    await session.commit()
    with _settings(), _clients(_counting_wallet(Counter(), native=3 * 10**9)):
        await sync_connection(session, conn.id, test_workspace.id, test_user.id)
    group = await get_group(session, native.group_id, test_workspace.id, test_user.id)
    assert group is not None and group.account_balance == 305
    assert account.balance == Decimal("305")
    assert native.units == 3
    assert conn.status == "active"
    assert "unavailable_account_balance_ids" not in (conn.settings or {})
