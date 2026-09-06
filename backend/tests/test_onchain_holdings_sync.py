"""What the sync layer concludes from an on-chain holdings payload.

`_sync_holdings` archives every asset the provider stops reporting, which is
right for a redeemed bond and catastrophic for a wallet whose index was briefly
unreachable — an archived asset leaves net worth and does not come back on its
own. These tests pin the boundary between the two.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.bank_connection import BankConnection
from app.providers import register_provider
from app.providers.onchain import SOLANA_TOKEN_PROGRAMS, OnChainProvider
from app.services.connection_service import _sync_holdings

from tests.test_providers_onchain import (
    A,
    MINT_A,
    MINT_B,
    _jupiter_row,
    _patched_client,
    _settings,
    _token_account,
    _token_handler,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _register_onchain():
    register_provider("onchain", OnChainProvider)


async def _connection(session: AsyncSession, test_user, test_workspace) -> BankConnection:
    conn = BankConnection(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        provider="onchain",
        institution_name="On-chain wallets",
        external_id="onchain-test",
        credentials={"addresses": [f"solana:{A}"]},
    )
    session.add(conn)
    await session.commit()
    return conn


async def _assets(session: AsyncSession, connection: BankConnection) -> dict[str, Asset]:
    rows = (
        await session.execute(select(Asset).where(Asset.connection_id == connection.id))
    ).scalars().all()
    return {a.external_id: a for a in rows if a.external_id}


def _wallet_handler(*, mints: dict[str, tuple[str, str, bool]], native: int = 10**9):
    """A wallet holding `mints` — each mapped to (raw amount, symbol, vouched)."""
    return _token_handler(
        accounts={
            SOLANA_TOKEN_PROGRAMS[0]: [
                _token_account(mint, amount, 6) for mint, (amount, _, _) in mints.items()
            ]
        },
        jupiter=[
            _jupiter_row(mint, symbol, "1", vouched)
            for mint, (_, symbol, vouched) in mints.items()
        ],
        native=native,
    )


async def test_a_token_becomes_an_asset_keyed_by_its_contract(
    session: AsyncSession, test_user, test_workspace
):
    conn = await _connection(session, test_user, test_workspace)
    handler = _wallet_handler(mints={MINT_A: ("5000000", "USDC", True)})
    with _settings(), _patched_client(handler), patch(
        "app.providers.onchain.usd_spot_prices", return_value={"SOL": Decimal("100")}
    ):
        await _sync_holdings(session, test_user.id, conn, conn.credentials or {})
    await session.commit()

    assets = await _assets(session, conn)
    assert f"solana:{A}" in assets
    token = assets[f"solana:{A}:{MINT_A}"]
    assert token.ticker == "USDC"
    assert token.units == Decimal("5")
    assert token.is_archived is False


async def test_an_index_outage_leaves_the_holdings_alone_rather_than_archiving_them(
    session: AsyncSession, test_user, test_workspace
):
    """The failure mode this whole contract exists for.

    A 503 from the token index used to yield an empty token list, which the
    sweep below reads as every token in the wallet having been disposed of.
    """
    conn = await _connection(session, test_user, test_workspace)
    healthy = _wallet_handler(mints={MINT_A: ("5000000", "USDC", True)})
    with _settings(), _patched_client(healthy), patch(
        "app.providers.onchain.usd_spot_prices", return_value={"SOL": Decimal("100")}
    ):
        await _sync_holdings(session, test_user.id, conn, conn.credentials or {})
    await session.commit()
    assert (await _assets(session, conn))[f"solana:{A}:{MINT_A}"].is_archived is False

    def broken(request: httpx.Request) -> httpx.Response:
        if "jup.ag" in request.url.host:
            return httpx.Response(503)
        return healthy(request)

    with _settings(), _patched_client(broken), patch(
        "app.providers.onchain.usd_spot_prices", return_value={"SOL": Decimal("100")}
    ):
        await _sync_holdings(session, test_user.id, conn, conn.credentials or {})
    await session.commit()

    assets = await _assets(session, conn)
    assert assets[f"solana:{A}:{MINT_A}"].is_archived is False
    assert assets[f"solana:{A}"].is_archived is False


async def test_a_spent_token_is_archived_because_that_is_a_real_disposal(
    session: AsyncSession, test_user, test_workspace
):
    """The other side of the boundary: a zero balance is positive evidence."""
    conn = await _connection(session, test_user, test_workspace)
    both = _wallet_handler(
        mints={MINT_A: ("5000000", "USDC", True), MINT_B: ("2000000", "JUP", True)}
    )
    with _settings(), _patched_client(both), patch(
        "app.providers.onchain.usd_spot_prices", return_value={"SOL": Decimal("100")}
    ):
        await _sync_holdings(session, test_user.id, conn, conn.credentials or {})
    await session.commit()

    spent = _wallet_handler(mints={MINT_A: ("5000000", "USDC", True)})
    with _settings(), _patched_client(spent), patch(
        "app.providers.onchain.usd_spot_prices", return_value={"SOL": Decimal("100")}
    ):
        await _sync_holdings(session, test_user.id, conn, conn.credentials or {})
    await session.commit()

    assets = await _assets(session, conn)
    assert assets[f"solana:{A}:{MINT_B}"].is_archived is True
    assert assets[f"solana:{A}:{MINT_A}"].is_archived is False


async def test_an_unvouched_token_is_stored_without_a_ticker_or_a_value(
    session: AsyncSession, test_user, test_workspace
):
    """A junk token calling itself USDC must not be filed as cash, and must not
    consolidate with — or take the cost basis off — a real USDC position."""
    conn = await _connection(session, test_user, test_workspace)
    handler = _wallet_handler(mints={MINT_A: ("9" * 12, "USDC", False)})
    with _settings(), _patched_client(handler), patch(
        "app.providers.onchain.usd_spot_prices", return_value={"SOL": Decimal("100")}
    ):
        await _sync_holdings(session, test_user.id, conn, conn.credentials or {})
    await session.commit()

    token = (await _assets(session, conn))[f"solana:{A}:{MINT_A}"]
    assert token.ticker is None
    # Not `cash_equivalent`: that classification is keyed on the ticker, and a
    # symbol nobody vouched for must not be able to reach it.
    assert token.type == "crypto"
    assert (token.external_metadata or {})["token_trusted"] is False
