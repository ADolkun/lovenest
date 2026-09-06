"""The /api/onchain surface. The chain walk itself is faked — see test_providers_onchain."""

import uuid
from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_connection import BankConnection
from app.models.user import User
from app.providers import register_provider
from app.providers.base import ProviderNotConfiguredError, ProviderRateLimited
from app.providers.onchain import CHAINS, OnChainProvider
from app.services import onchain_trace

A = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
B = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


def _traces(result_or_error):
    async def fake_trace(*args, **kwargs):
        if isinstance(result_or_error, Exception):
            raise result_or_error
        return result_or_error

    return patch.object(onchain_trace, "trace", fake_trace)


@pytest.mark.asyncio
async def test_the_onchain_provider_is_offered_as_a_paste_a_token_connection(
    client: AsyncClient, auth_headers
):
    response = await client.get("/api/connections/providers", headers=auth_headers)
    by_name = {p["name"]: p for p in response.json()["providers"]}
    assert by_name["onchain"]["flow_type"] == "token"
    # The suite empties the provider registry, so nothing is configured until
    # it is registered — which is the wiring worth asserting, rather than the
    # static entry three lines away in KNOWN_PROVIDERS.
    assert by_name["onchain"]["configured"] is False

    register_provider("onchain", OnChainProvider)
    response = await client.get("/api/connections/providers", headers=auth_headers)
    configured = {p["name"]: p["configured"] for p in response.json()["providers"]}
    assert configured["onchain"] is True


@pytest.mark.asyncio
async def test_chains_report_whether_this_deployment_can_trace_them(
    client: AsyncClient, auth_headers
):
    """Every shipped chain has a keyless history source, so all of them trace.

    The flag is not decoration: it disables a chain in the picker, so a chain
    added later with neither an explorer key nor a Blockscout instance has to
    come back False rather than fail on submit.
    """
    with patch(
        "app.api.onchain.get_settings",
        lambda: SimpleNamespace(etherscan_api_key=""),
    ):
        response = await client.get("/api/onchain/chains", headers=auth_headers)
    assert response.status_code == 200
    assert all(chain["traceable"] for chain in response.json())

    unindexed = replace(CHAINS["base"], token_index_url=None)
    with (
        patch("app.api.onchain.get_settings", lambda: SimpleNamespace(etherscan_api_key="")),
        patch.dict("app.providers.onchain.CHAINS", {"base": unindexed}),
    ):
        response = await client.get("/api/onchain/chains", headers=auth_headers)
    assert {c["key"]: c["traceable"] for c in response.json()}["base"] is False


@pytest.mark.asyncio
async def test_watched_addresses_come_back_labelled_for_one_click_tracing(
    client: AsyncClient, auth_headers, session: AsyncSession, test_user: User, test_workspace
):
    session.add(
        BankConnection(
            id=uuid.uuid4(),
            user_id=test_user.id,
            workspace_id=test_workspace.id,
            provider="onchain",
            external_id="onchain:x",
            institution_name="On-chain wallets",
            display_name="My wallet",
            credentials={"addresses": [f"solana:{A}", "garbage"]},
        )
    )
    await session.commit()

    response = await client.get("/api/onchain/addresses", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    # The unreadable entry is skipped rather than failing the whole listing.
    assert len(data) == 1
    assert data[0]["chain"] == "solana"
    assert data[0]["label"] == f"Solana {A[:4]}…{A[-4:]}"
    assert data[0]["connection_name"] == "My wallet"


@pytest.mark.asyncio
async def test_a_trace_returns_its_hops_and_its_endpoints(client: AsyncClient, auth_headers):
    result = onchain_trace.TraceResult(
        root=f"solana:{A}",
        direction="out",
        nodes=[
            onchain_trace.TraceNode(
                id=f"solana:{A}", chain="solana", address=A, depth=0, symbol="SOL"
            ),
            onchain_trace.TraceNode(
                id=f"solana:{B}",
                chain="solana",
                address=B,
                depth=1,
                symbol="SOL",
                balance=Decimal("1.5"),
                terminal_reason=onchain_trace.TERMINAL_POOLED,
            ),
        ],
        edges=[
            onchain_trace.TraceEdge(
                source=f"solana:{A}",
                target=f"solana:{B}",
                chain="solana",
                symbol="SOL",
                amount=Decimal("16.797117111"),
                reference="sig",
                occurred_at=datetime(2025, 1, 23, 23, 0, 8, tzinfo=timezone.utc),
            )
        ],
        truncated=True,
    )
    with _traces(result):
        response = await client.post(
            "/api/onchain/trace",
            headers=auth_headers,
            json={"chain": "solana", "address": A, "max_hops": 3},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is True
    assert body["edges"][0]["amount"] == "16.797117111"
    assert body["nodes"][1]["terminal_reason"] == "pooled"


@pytest.mark.asyncio
async def test_an_unknown_chain_is_a_bad_request_not_a_server_error(
    client: AsyncClient, auth_headers
):
    with _traces(ValueError("Unknown chain 'dogecoin'.")):
        response = await client.post(
            "/api/onchain/trace",
            headers=auth_headers,
            json={"chain": "dogecoin", "address": A},
        )
    assert response.status_code == 400
    assert "dogecoin" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_missing_explorer_key_is_reported_as_unconfigured_not_as_no_activity(
    client: AsyncClient, auth_headers
):
    with _traces(ProviderNotConfiguredError("needs ETHERSCAN_API_KEY")):
        response = await client.post(
            "/api/onchain/trace",
            headers=auth_headers,
            json={"chain": "base", "address": "0x" + "ab" * 20},
        )
    assert response.status_code == 503
    assert "ETHERSCAN_API_KEY" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_throttled_node_surfaces_as_429_with_the_fix_named(
    client: AsyncClient, auth_headers
):
    with _traces(ProviderRateLimited("throttled")):
        response = await client.post(
            "/api/onchain/trace",
            headers=auth_headers,
            json={"chain": "solana", "address": A},
        )
    assert response.status_code == 429
    assert "ONCHAIN_RPC_URLS" in response.json()["detail"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_hops": 99},
        {"max_hops": 0},
        {"max_branches": 99},
        {"max_branches": 0},
        {"min_amount": -1},
    ],
)
@pytest.mark.asyncio
async def test_the_hop_and_branch_caps_are_enforced_by_the_schema(
    client: AsyncClient, auth_headers, overrides
):
    response = await client.post(
        "/api/onchain/trace",
        headers=auth_headers,
        json={"chain": "solana", "address": A, **overrides},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_a_workspace_never_sees_another_workspace_s_watched_addresses(
    client: AsyncClient,
    auth_headers,
    other_workspace_headers,
    session: AsyncSession,
    test_user: User,
    test_workspace,
):
    """Wallet addresses are workspace data, and the boundary is absolute."""
    session.add(
        BankConnection(
            id=uuid.uuid4(),
            user_id=test_user.id,
            workspace_id=test_workspace.id,
            provider="onchain",
            external_id="onchain:mine",
            institution_name="On-chain wallets",
            credentials={"addresses": [f"solana:{A}"]},
        )
    )
    await session.commit()

    mine = await client.get("/api/onchain/addresses", headers=auth_headers)
    theirs = await client.get("/api/onchain/addresses", headers=other_workspace_headers)
    assert [row["address"] for row in mine.json()] == [A]
    assert theirs.json() == []


@pytest.mark.asyncio
async def test_a_trace_that_ran_out_of_budget_says_it_is_incomplete(
    client: AsyncClient, auth_headers
):
    """`truncated` is the only signal that the trail continues past the view."""
    result = onchain_trace.TraceResult(
        root=f"solana:{A}",
        direction="out",
        nodes=[
            onchain_trace.TraceNode(
                id=f"solana:{A}",
                chain="solana",
                address=A,
                depth=0,
                symbol="SOL",
                terminal_reason=onchain_trace.TERMINAL_BUDGET,
            )
        ],
        truncated=True,
    )
    with _traces(result):
        response = await client.post(
            "/api/onchain/trace",
            headers=auth_headers,
            json={"chain": "solana", "address": A},
        )
    body = response.json()
    assert body["truncated"] is True
    assert body["nodes"][0]["terminal_reason"] == "budget"


@pytest.mark.asyncio
async def test_a_node_that_could_not_be_read_never_carries_the_upstream_url(
    client: AsyncClient, auth_headers
):
    """The URL of a keyed RPC endpoint must not reach the response body."""
    result = onchain_trace.TraceResult(
        root=f"solana:{A}",
        direction="out",
        nodes=[
            onchain_trace.TraceNode(
                id=f"solana:{A}",
                chain="solana",
                address=A,
                depth=0,
                symbol="SOL",
                terminal_reason=onchain_trace.TERMINAL_UNAVAILABLE,
            )
        ],
    )
    with _traces(result):
        response = await client.post(
            "/api/onchain/trace",
            headers=auth_headers,
            json={"chain": "solana", "address": A},
        )
    node = response.json()["nodes"][0]
    assert node["terminal_reason"] == "unavailable"
    assert "detail" not in node


@pytest.mark.asyncio
async def test_a_malformed_address_is_rejected_before_any_node_is_contacted(
    client: AsyncClient, auth_headers
):
    response = await client.post(
        "/api/onchain/trace",
        headers=auth_headers,
        json={"chain": "solana", "address": "not-a-real-address"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_tracing_requires_a_session(client: AsyncClient):
    response = await client.post(
        "/api/onchain/trace", json={"chain": "solana", "address": A}
    )
    assert response.status_code in (401, 403)
