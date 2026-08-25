"""Cover the trade ledger through `_sync_trades`.

This is the first automatic writer to the ledger — everything else that
writes it records something a person typed or uploaded — so the load-bearing
property is that running it twice changes nothing. These tests pin that,
plus the recompute that makes the ledger rather than the reported balance the
authority for quantity and cost basis.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.asset_transaction import AssetTransaction
from app.models.bank_connection import BankConnection
from app.models.user import User
from app.providers import register_provider
from app.providers.base import (
    AccountData,
    BankProvider,
    ConnectionData,
    HoldingData,
    TradeData,
    TransactionData,
)
from app.services.connection_service import _sync_holdings, _sync_trades


class _MockProvider(BankProvider):
    """BankProvider serving caller-supplied holdings and trades."""

    _holdings: list[HoldingData] = []
    _trades: list[TradeData] = []
    _raise: Optional[Exception] = None

    @property
    def name(self) -> str:
        return "mocktrades"

    async def get_oauth_url(self, redirect_uri, state, flow_params=None) -> str:  # pragma: no cover
        return "http://mock"

    async def handle_oauth_callback(self, code: str) -> ConnectionData:  # pragma: no cover
        raise NotImplementedError

    async def get_accounts(self, credentials: dict) -> list[AccountData]:  # pragma: no cover
        return []

    async def get_transactions(
        self, credentials: dict, account_external_id: str, since=None, payee_source: str = "auto"
    ) -> list[TransactionData]:  # pragma: no cover
        return []

    async def refresh_credentials(self, credentials: dict) -> dict:  # pragma: no cover
        return credentials

    async def get_holdings(self, credentials: dict) -> list[HoldingData]:
        return list(_MockProvider._holdings)

    async def get_trades(self, credentials: dict) -> list[TradeData]:
        if _MockProvider._raise is not None:
            raise _MockProvider._raise
        return list(_MockProvider._trades)


@pytest.fixture(autouse=True)
def _register_mock_provider():
    register_provider("mocktrades", _MockProvider)
    _MockProvider._holdings = []
    _MockProvider._trades = []
    _MockProvider._raise = None
    yield


@pytest_asyncio.fixture
async def connection(session: AsyncSession, test_user: User) -> BankConnection:
    conn = BankConnection(
        id=uuid.uuid4(),
        user_id=test_user.id,
        provider="mocktrades",
        external_id="portfolio-1",
        institution_name="Mock Exchange",
        credentials={"key": "x"},
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.commit()
    await session.refresh(conn)
    return conn


def _holding(external_id: str = "w-xrp", quantity: str = "30") -> HoldingData:
    return HoldingData(
        external_id=external_id,
        name="XRP Wallet",
        currency="USD",
        ticker="XRP",
        quantity=Decimal(quantity),
        unit_price=Decimal("3"),
        current_value=Decimal(quantity) * Decimal("3"),
        account_external_id="portfolio-1",
    )


def _trade(
    external_id: str,
    kind: str = "buy",
    quantity: str = "20",
    price: str = "2",
    when: date = date(2024, 3, 4),
    holding_external_id: str = "w-xrp",
    at: str = "12:00",
    notes: str | None = None,
) -> TradeData:
    hour, minute = (int(part) for part in at.split(":"))
    return TradeData(
        external_id=external_id,
        holding_external_id=holding_external_id,
        kind=kind,
        quantity=Decimal(quantity),
        price=Decimal(price),
        occurred_at=datetime(
            when.year, when.month, when.day, hour, minute, tzinfo=timezone.utc
        ),
        notes=notes,
    )


async def _ledger(session: AsyncSession) -> list[AssetTransaction]:
    rows = await session.execute(
        select(AssetTransaction).order_by(AssetTransaction.date, AssetTransaction.external_id)
    )
    return list(rows.scalars().all())


async def _sync(
    session: AsyncSession,
    user_id,
    connection: BankConnection,
    synced_account_ids: set[str] | None = None,
) -> None:
    """One full pass in production order: holdings, then the ledger."""
    await _sync_holdings(
        session, user_id, connection, connection.credentials or {}, synced_account_ids
    )
    await _sync_trades(
        session, connection, connection.credentials or {}, synced_account_ids
    )
    await session.commit()


@pytest.mark.asyncio
async def test_trades_land_on_the_ledger_and_drive_the_position(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    _MockProvider._holdings = [_holding(quantity="30")]
    _MockProvider._trades = [
        _trade("tx-1", "buy", "20", "2"),
        _trade("tx-2", "buy", "20", "4", when=date(2024, 6, 1)),
        _trade("tx-3", "sell", "10", "5", when=date(2024, 9, 1)),
    ]

    await _sync(session, test_user.id, connection)

    ledger = await _ledger(session)
    assert [(t.external_id, t.kind, t.source) for t in ledger] == [
        ("tx-1", "buy", "mocktrades"),
        ("tx-2", "buy", "mocktrades"),
        ("tx-3", "sell", "mocktrades"),
    ]
    assert ledger[0].quantity == Decimal("20")
    assert ledger[0].price == Decimal("2")
    assert ledger[0].date == date(2024, 3, 4)

    asset = (await session.execute(select(Asset))).scalar_one()
    assert ledger[0].workspace_id == asset.workspace_id
    # 40 bought for 120 → average 3; 10 sold leaves 30 at a 90 basis.
    assert asset.units == Decimal("30")
    assert asset.average_price == Decimal("3")
    assert asset.purchase_price == Decimal("90.00")
    assert asset.realized_gain == Decimal("20.00")
    # First buy, not the day the exchange was first asked.
    assert asset.purchase_date == date(2024, 3, 4)


@pytest.mark.asyncio
async def test_a_second_sync_over_the_same_payload_changes_nothing(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    _MockProvider._holdings = [_holding(quantity="40")]
    _MockProvider._trades = [
        _trade("tx-1"),
        _trade(
            "tx-2", when=date(2024, 6, 1),
            notes="Coinbase staking_reward — income at receipt",
        ),
    ]

    await _sync(session, test_user.id, connection)
    before = [
        (t.id, t.external_id, t.quantity, t.price, t.notes) for t in await _ledger(session)
    ]
    asset_before = (await session.execute(select(Asset))).scalar_one()
    position_before = (asset_before.units, asset_before.average_price, asset_before.realized_gain)

    await _sync(session, test_user.id, connection)

    after = [
        (t.id, t.external_id, t.quantity, t.price, t.notes) for t in await _ledger(session)
    ]
    assert after == before
    assert before[1][-1] == "Coinbase staking_reward — income at receipt"
    asset_after = (await session.execute(select(Asset))).scalar_one()
    assert (asset_after.units, asset_after.average_price, asset_after.realized_gain) == position_before


@pytest.mark.asyncio
async def test_a_later_sync_appends_only_what_is_new(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    _MockProvider._holdings = [_holding(quantity="20")]
    _MockProvider._trades = [_trade("tx-1")]
    await _sync(session, test_user.id, connection)

    _MockProvider._holdings = [_holding(quantity="40")]
    _MockProvider._trades = [_trade("tx-1"), _trade("tx-2", "buy", "20", "4", date(2024, 6, 1))]
    await _sync(session, test_user.id, connection)

    assert [t.external_id for t in await _ledger(session)] == ["tx-1", "tx-2"]
    asset = (await session.execute(select(Asset))).scalar_one()
    assert asset.units == Decimal("40")
    assert asset.average_price == Decimal("3")


@pytest.mark.asyncio
async def test_a_repeated_id_inside_one_payload_is_written_once(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    """An overlapping cursor page hands the same transaction back twice."""
    _MockProvider._holdings = [_holding(quantity="20")]
    _MockProvider._trades = [_trade("tx-1"), _trade("tx-1")]

    await _sync(session, test_user.id, connection)

    assert [t.external_id for t in await _ledger(session)] == ["tx-1"]


@pytest.mark.asyncio
async def test_a_trade_with_no_holding_is_left_alone(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    """The allowlist excluded the position; the ledger must not resurrect it."""
    _MockProvider._holdings = [_holding()]
    _MockProvider._trades = [_trade("tx-1", holding_external_id="w-doge")]

    await _sync(session, test_user.id, connection)

    assert await _ledger(session) == []
    assert (await session.execute(select(Asset))).scalar_one().units == Decimal("30")


@pytest.mark.asyncio
async def test_a_provider_failure_costs_the_ledger_not_the_balances(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    _MockProvider._holdings = [_holding()]
    _MockProvider._trades = [_trade("tx-1")]
    _MockProvider._raise = RuntimeError("history endpoint is down")

    await _sync(session, test_user.id, connection)

    assert await _ledger(session) == []
    asset = (await session.execute(select(Asset))).scalar_one()
    assert asset.units == Decimal("30")


@pytest.mark.asyncio
async def test_no_trades_leaves_the_snapshot_holding_untouched(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    _MockProvider._holdings = [_holding()]

    await _sync(session, test_user.id, connection)

    asset = (await session.execute(select(Asset))).scalar_one()
    assert asset.units == Decimal("30")
    assert asset.average_price is None


@pytest.mark.asyncio
async def test_a_sync_reporting_no_trades_still_reconciles_the_ledger(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    """The holdings pass stomps `units` on every sync, so the replay runs on
    every sync — including one where the provider hands back nothing new."""
    _MockProvider._holdings = [_holding(quantity="20")]
    _MockProvider._trades = [_trade("tx-1")]
    await _sync(session, test_user.id, connection)

    _MockProvider._holdings = [_holding(quantity="999")]
    _MockProvider._trades = []
    await _sync(session, test_user.id, connection)

    asset = (await session.execute(select(Asset))).scalar_one()
    # The balance no longer matches the ledger, so the ledger is not believed.
    assert asset.units == Decimal("999")

    _MockProvider._holdings = [_holding(quantity="20")]
    await _sync(session, test_user.id, connection)

    asset = (await session.execute(select(Asset))).scalar_one()
    assert asset.units == Decimal("20")
    assert asset.average_price == Decimal("2")


@pytest.mark.asyncio
async def test_trades_for_a_connection_with_no_holdings_go_nowhere(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    """Assets sync disabled, or every holding excluded by the allowlist."""
    _MockProvider._trades = [_trade("tx-1")]

    await _sync_trades(session, connection, connection.credentials or {})
    await session.commit()

    assert await _ledger(session) == []


@pytest.mark.asyncio
async def test_a_ledger_short_of_the_balance_leaves_the_holding_a_snapshot(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    """A sell whose buys arrived as transfers replays to zero (issue #70).

    `recompute_and_cache` reads zero units as a full exit and stamps a sell
    date, and `sell_date IS NULL` is what puts a holding in the portfolio — so
    believing that replay would delete a position the exchange still reports a
    balance for.
    """
    _MockProvider._holdings = [_holding(quantity="25")]
    _MockProvider._trades = [_trade("tx-1", "sell", "5", "4")]

    await _sync(session, test_user.id, connection)

    assert [t.external_id for t in await _ledger(session)] == ["tx-1"]
    asset = (await session.execute(select(Asset))).scalar_one()
    assert asset.units == Decimal("25")
    assert asset.sell_date is None
    assert asset.average_price is None


@pytest.mark.asyncio
async def test_a_ledger_that_reconciles_once_the_history_is_whole_takes_over(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    """The same holding, after the missing rows arrive: the ledger wins."""
    _MockProvider._holdings = [_holding(quantity="25")]
    _MockProvider._trades = [_trade("tx-1", "sell", "5", "4")]
    await _sync(session, test_user.id, connection)

    _MockProvider._trades = [
        _trade("tx-0", "buy", "30", "2", when=date(2024, 1, 1)),
        _trade("tx-1", "sell", "5", "4"),
    ]
    await _sync(session, test_user.id, connection)

    asset = (await session.execute(select(Asset))).scalar_one()
    assert asset.units == Decimal("25")
    assert asset.average_price == Decimal("2")
    assert asset.realized_gain == Decimal("10.00")
    assert asset.purchase_date == date(2024, 1, 1)


@pytest.mark.asyncio
async def test_an_excluded_account_gets_no_ledger_rows(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    """An account off the allowlist stops syncing, and a ledger row is a sync."""
    _MockProvider._holdings = [_holding(quantity="20")]
    _MockProvider._trades = [_trade("tx-1")]
    await _sync(session, test_user.id, connection, {"portfolio-1"})
    assert [t.external_id for t in await _ledger(session)] == ["tx-1"]

    _MockProvider._trades = [_trade("tx-1"), _trade("tx-2", when=date(2024, 6, 1))]
    await _sync(session, test_user.id, connection, set())

    assert [t.external_id for t in await _ledger(session)] == ["tx-1"]


@pytest.mark.asyncio
async def test_a_same_day_buy_and_sell_replay_in_the_order_they_happened(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    """Coinbase lists newest first, so the sell arrives before the buy."""
    _MockProvider._holdings = [_holding(quantity="0")]
    _MockProvider._trades = [
        _trade("tx-sell", "sell", "20", "5", at="15:00"),
        _trade("tx-buy", "buy", "20", "2", at="09:00"),
    ]

    await _sync(session, test_user.id, connection)

    asset = (await session.execute(select(Asset))).scalar_one()
    assert asset.units == Decimal("0")
    # Replayed backwards the sell finds nothing to sell, and the round trip
    # books no gain at all.
    assert asset.realized_gain == Decimal("60.00")


@pytest.mark.asyncio
async def test_a_hand_set_sell_date_survives_the_replay(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    """`_sync_holdings` declines to value a holding the user marked sold; the
    trade sync must not undo the marking itself."""
    _MockProvider._holdings = [_holding(quantity="20")]
    _MockProvider._trades = [_trade("tx-1")]
    await _sync(session, test_user.id, connection)

    asset = (await session.execute(select(Asset))).scalar_one()
    asset.sell_date = date(2025, 5, 5)
    asset.sell_price = Decimal("77.00")
    await session.commit()

    await _sync(session, test_user.id, connection)

    asset = (await session.execute(select(Asset))).scalar_one()
    assert asset.sell_date == date(2025, 5, 5)
    assert asset.sell_price == Decimal("77.00")


@pytest.mark.asyncio
async def test_an_over_selling_ledger_is_not_believed(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    """Sold out entirely, but most of it arrived as a transfer (issue #70).

    The replay clamps the over-sell to zero and the exchange reports zero too,
    so the quantities agree by coincidence — while the realised gain counts
    only the units that happen to have a traceable buy.
    """
    _MockProvider._holdings = [_holding(quantity="0")]
    _MockProvider._trades = [
        _trade("tx-buy", "buy", "10", "2", when=date(2024, 1, 1)),
        _trade("tx-sell", "sell", "30", "5", when=date(2024, 6, 1)),
    ]

    await _sync(session, test_user.id, connection)

    assert len(await _ledger(session)) == 2
    asset = (await session.execute(select(Asset))).scalar_one()
    assert asset.realized_gain is None
    assert asset.sell_date is None


@pytest.mark.asyncio
async def test_an_archived_holding_gets_no_new_ledger_rows(
    session: AsyncSession, test_user: User, connection: BankConnection
):
    """The provider stopped reporting it, so the holdings pass archived it."""
    _MockProvider._holdings = [_holding(quantity="20")]
    _MockProvider._trades = [_trade("tx-1")]
    await _sync(session, test_user.id, connection)

    _MockProvider._holdings = []
    _MockProvider._trades = [_trade("tx-1"), _trade("tx-2", when=date(2024, 6, 1))]
    await _sync(session, test_user.id, connection)

    asset = (await session.execute(select(Asset))).scalar_one()
    assert asset.is_archived is True
    assert [t.external_id for t in await _ledger(session)] == ["tx-1"]
