"""Tests for the investment transaction ledger (issue #235).

Covers the weighted-average (preço médio) algorithm and the service paths:
buy/sell recompute, realized gains, find-or-create consolidation by ticker.
"""
import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.asset_transaction import AssetTransaction
from app.models.user import User
from app.providers.market_price import MarketPriceProvider
from app.schemas.asset import (
    AssetBuyCreate,
    AssetTransactionCreate,
    AssetTransactionUpdate,
    MarketSymbolMatch,
    MarketSymbolQuote,
)
from app.services import asset_transaction_service
from app.services.asset_transaction_service import _recompute


# ---------------------------------------------------------------------------
# Pure algorithm: _recompute (no DB)
# ---------------------------------------------------------------------------

def _tx(kind: str, qty: str, price: str, d: date, fee: str = "0") -> AssetTransaction:
    return AssetTransaction(
        id=uuid.uuid4(), asset_id=uuid.uuid4(), workspace_id=uuid.uuid4(),
        kind=kind, quantity=Decimal(qty), price=Decimal(price), fee=Decimal(fee), date=d,
    )


def test_recompute_weighted_average_across_buys():
    # 10 @ 100 then 5 @ 110 → avg = (1000 + 550) / 15 = 103.3333...
    pos = _recompute([
        _tx("buy", "10", "100", date(2026, 1, 1)),
        _tx("buy", "5", "110", date(2026, 2, 1)),
    ])
    assert pos["units"] == Decimal("15")
    assert pos["average_price"].quantize(Decimal("0.0001")) == Decimal("103.3333")
    assert pos["cost_basis"] == Decimal("1550")
    assert pos["realized_gain"] == Decimal("0")


def test_recompute_partial_sell_keeps_average_and_realizes():
    # buy 10 @ 100, buy 5 @ 110 (avg 103.3333), sell 6 @ 130
    # realized = (130 - 103.3333) * 6 = 160.0002 (≈ 160), avg unchanged
    pos = _recompute([
        _tx("buy", "10", "100", date(2026, 1, 1)),
        _tx("buy", "5", "110", date(2026, 1, 2)),
        _tx("sell", "6", "130", date(2026, 3, 1)),
    ])
    assert pos["units"] == Decimal("9")
    assert pos["average_price"].quantize(Decimal("0.0001")) == Decimal("103.3333")
    assert pos["realized_gain"].quantize(Decimal("0.01")) == Decimal("160.00")


def test_recompute_sell_all_flattens_position():
    pos = _recompute([
        _tx("buy", "10", "100", date(2026, 1, 1)),
        _tx("sell", "10", "120", date(2026, 2, 1)),
    ])
    assert pos["units"] == Decimal("0")
    assert pos["average_price"] is None
    assert pos["cost_basis"] == Decimal("0")
    assert pos["realized_gain"].quantize(Decimal("0.01")) == Decimal("200.00")


def test_recompute_includes_fees_in_cost_basis():
    # buy 10 @ 100 with 9.90 fee → cost basis 1009.90, avg 100.99
    pos = _recompute([_tx("buy", "10", "100", date(2026, 1, 1), fee="9.90")])
    assert pos["cost_basis"] == Decimal("1009.90")
    assert pos["average_price"].quantize(Decimal("0.0001")) == Decimal("100.9900")


def test_recompute_clamps_oversell():
    # Selling more than held shouldn't drive quantity negative.
    pos = _recompute([
        _tx("buy", "5", "100", date(2026, 1, 1)),
        _tx("sell", "10", "120", date(2026, 2, 1)),
    ])
    assert pos["units"] == Decimal("0")
    assert pos["average_price"] is None


def test_recompute_orders_by_date_not_insertion():
    # A backdated buy must be processed first.
    pos = _recompute([
        _tx("sell", "5", "130", date(2026, 3, 1)),
        _tx("buy", "10", "100", date(2026, 1, 1)),
    ])
    assert pos["units"] == Decimal("5")
    assert pos["average_price"].quantize(Decimal("0.01")) == Decimal("100.00")


# ---------------------------------------------------------------------------
# Service paths (DB)
# ---------------------------------------------------------------------------

class _FakeProvider(MarketPriceProvider):
    name = "fake"

    def __init__(self, quotes: dict[str, MarketSymbolQuote]):
        self._quotes = quotes

    async def search(self, query: str, limit: int = 20) -> list[MarketSymbolMatch]:
        return []

    async def get_quote(self, symbol: str) -> Optional[MarketSymbolQuote]:
        return self._quotes.get(symbol.upper())

    async def get_latest_prices(self, symbols: list[str]) -> dict[str, Optional[Decimal]]:
        return {s.upper(): Decimal(str(self._quotes[s.upper()].price)) for s in symbols if s.upper() in self._quotes}


def _quote(symbol: str, price: float, currency: str = "BRL") -> MarketSymbolQuote:
    return MarketSymbolQuote(
        symbol=symbol, name=f"{symbol} SA", exchange="SAO",
        currency=currency, price=price, quote_type="EQUITY",
    )


@pytest_asyncio.fixture
async def market_asset(session: AsyncSession, test_user: User, test_workspace) -> Asset:
    asset = Asset(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name="Petrobras", type="stock", currency="BRL",
        valuation_method="market_price", ticker="PETR4.SA",
        last_price=Decimal("30.00"),
    )
    session.add(asset)
    await session.commit()
    await session.refresh(asset)
    return asset


@pytest.mark.asyncio
async def test_add_buys_sets_units_average_and_cost_basis(session, test_workspace, market_asset):
    await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="buy", quantity=Decimal("10"), price=Decimal("20"), date=date(2026, 1, 1)),
    )
    read = await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="buy", quantity=Decimal("5"), price=Decimal("26"), date=date(2026, 2, 1)),
    )
    # avg = (200 + 130) / 15 = 22.00
    assert read is not None
    assert read.units == 15

    assert read.average_price is not None
    assert round(read.average_price, 2) == 22.00
    assert read.total_invested is not None
    assert round(read.total_invested, 2) == 330.00
    assert read.transaction_count == 2


@pytest.mark.asyncio
async def test_sell_records_realized_gain(session, test_workspace, market_asset):
    await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="buy", quantity=Decimal("10"), price=Decimal("20"), date=date(2026, 1, 1)),
    )
    read = await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="sell", quantity=Decimal("4"), price=Decimal("30"), date=date(2026, 3, 1)),
    )
    assert read is not None
    assert read.units == 6
    assert read.average_price is not None
    assert round(read.average_price, 2) == 20.00  # average unchanged by sell
    assert read.realized_gain is not None
    assert round(read.realized_gain, 2) == 40.00  # (30 - 20) * 4


@pytest.mark.asyncio
async def test_delete_transaction_recomputes(session, test_workspace, market_asset):
    await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="buy", quantity=Decimal("10"), price=Decimal("20"), date=date(2026, 1, 1)),
    )
    txs = await asset_transaction_service.list_asset_transactions(session, market_asset.id, test_workspace.id)
    assert txs is not None
    read = await asset_transaction_service.delete_transaction(session, txs[0].id, test_workspace.id)
    assert read is not None
    assert read.units == 0
    assert read.average_price is None


@pytest.mark.asyncio
async def test_update_transaction_recomputes(session, test_workspace, market_asset):
    await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="buy", quantity=Decimal("10"), price=Decimal("20"), date=date(2026, 1, 1)),
    )
    txs = await asset_transaction_service.list_asset_transactions(session, market_asset.id, test_workspace.id)
    assert txs is not None
    read = await asset_transaction_service.update_transaction(
        session, txs[0].id, test_workspace.id, AssetTransactionUpdate(quantity=Decimal("20")),
    )
    assert read is not None
    assert read.units == 20
    assert read.average_price is not None
    assert round(read.average_price, 2) == 20.00


@pytest.mark.asyncio
async def test_oversell_is_rejected(session, test_workspace, market_asset):
    from fastapi import HTTPException

    await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="buy", quantity=Decimal("10"), price=Decimal("20"), date=date(2026, 1, 1)),
    )
    with pytest.raises(HTTPException) as exc:
        await asset_transaction_service.add_transaction(
            session, market_asset.id, test_workspace.id,
            AssetTransactionCreate(kind="sell", quantity=Decimal("11"), price=Decimal("30"), date=date(2026, 2, 1)),
        )
    assert exc.value.status_code == 422
    # The rejected sell must not have changed the position.
    txs = await asset_transaction_service.list_asset_transactions(session, market_asset.id, test_workspace.id)
    assert txs is not None
    assert len(txs) == 1


@pytest.mark.asyncio
async def test_sell_exact_holding_is_allowed(session, test_workspace, market_asset):
    await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="buy", quantity=Decimal("10"), price=Decimal("20"), date=date(2026, 1, 1)),
    )
    read = await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="sell", quantity=Decimal("10"), price=Decimal("30"), date=date(2026, 2, 1)),
    )
    assert read is not None
    assert read.units == 0


@pytest.mark.asyncio
async def test_sell_before_any_buy_is_rejected(session, test_workspace, market_asset):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await asset_transaction_service.add_transaction(
            session, market_asset.id, test_workspace.id,
            AssetTransactionCreate(kind="sell", quantity=Decimal("5"), price=Decimal("30"), date=date(2026, 1, 1)),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_full_exit_marks_sold(session, test_workspace, market_asset):
    await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="buy", quantity=Decimal("10"), price=Decimal("20"), date=date(2026, 1, 1)),
    )
    read = await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="sell", quantity=Decimal("10"), price=Decimal("30"), date=date(2026, 6, 1)),
    )
    assert read is not None
    assert read.units == 0
    assert read.average_price is None
    assert read.sell_date == date(2026, 6, 1)  # drops out of the active portfolio
    assert read.realized_gain is not None
    assert round(read.realized_gain, 2) == 100.00


@pytest.mark.asyncio
async def test_rebuy_after_full_exit_resets_position(session, test_workspace, market_asset):
    await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="buy", quantity=Decimal("10"), price=Decimal("20"), date=date(2026, 1, 1)),
    )
    await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="sell", quantity=Decimal("10"), price=Decimal("30"), date=date(2026, 2, 1)),
    )
    read = await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="buy", quantity=Decimal("5"), price=Decimal("40"), date=date(2026, 3, 1)),
    )
    assert read is not None
    assert read.units == 5
    assert read.average_price is not None
    assert round(read.average_price, 2) == 40.00
    assert read.sell_date is None  # re-entered → no longer "sold"
    # Realized gain from the earlier round-trip is retained.
    assert read.realized_gain is not None
    assert round(read.realized_gain, 2) == 100.00


@pytest.mark.asyncio
async def test_multiple_sells_accumulate_realized_gain(session, test_workspace, market_asset):
    await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="buy", quantity=Decimal("10"), price=Decimal("20"), date=date(2026, 1, 1)),
    )
    await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="sell", quantity=Decimal("3"), price=Decimal("30"), date=date(2026, 2, 1)),
    )
    read = await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="sell", quantity=Decimal("2"), price=Decimal("25"), date=date(2026, 3, 1)),
    )
    # (30-20)*3 + (25-20)*2 = 30 + 10 = 40
    assert read is not None
    assert read.units == 5
    assert read.realized_gain is not None
    assert round(read.realized_gain, 2) == 40.00


@pytest.mark.asyncio
async def test_buy_into_holding_separate_across_wallets(session, test_workspace, test_user):
    from app.models.asset_group import AssetGroup

    wallet = AssetGroup(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name="Broker A", icon="wallet", color="#0EA5E9", position=0, source="manual",
    )
    session.add(wallet)
    await session.commit()

    provider = _FakeProvider({"ITUB4.SA": _quote("ITUB4.SA", 30.0)})
    ungrouped = await asset_transaction_service.buy_into_holding(
        session, test_workspace.id, test_user.id,
        AssetBuyCreate(ticker="ITUB4.SA", quantity=Decimal("10"), price=Decimal("28"), date=date(2026, 1, 1)),
        market_provider=provider,
    )
    walleted = await asset_transaction_service.buy_into_holding(
        session, test_workspace.id, test_user.id,
        AssetBuyCreate(ticker="ITUB4.SA", quantity=Decimal("5"), price=Decimal("32"), date=date(2026, 2, 1), group_id=wallet.id),
        market_provider=provider,
    )
    # Same ticker, different wallet → distinct holdings (not consolidated).
    assert ungrouped.id != walleted.id
    rows = (
        await session.execute(
            select(Asset).where(Asset.workspace_id == test_workspace.id, Asset.ticker == "ITUB4.SA")
        )
    ).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_buy_into_holding_consolidates_by_ticker(session, test_workspace, test_user):
    provider = _FakeProvider({"VALE3.SA": _quote("VALE3.SA", 60.0)})
    first = await asset_transaction_service.buy_into_holding(
        session, test_workspace.id, test_user.id,
        AssetBuyCreate(ticker="VALE3.SA", quantity=Decimal("10"), price=Decimal("50"), date=date(2026, 1, 1)),
        market_provider=provider,
    )
    second = await asset_transaction_service.buy_into_holding(
        session, test_workspace.id, test_user.id,
        AssetBuyCreate(ticker="VALE3.SA", quantity=Decimal("10"), price=Decimal("70"), date=date(2026, 2, 1)),
        market_provider=provider,
    )
    # Same logical holding — not two assets.
    assert first.id == second.id
    assert second is not None
    assert second.units == 20
    assert second.average_price is not None
    assert round(second.average_price, 2) == 60.00

    all_vale = (
        await session.execute(
            select(Asset).where(Asset.workspace_id == test_workspace.id, Asset.ticker == "VALE3.SA")
        )
    ).scalars().all()
    assert len(all_vale) == 1


def test_recompute_dates_each_realized_gain():
    pos = _recompute([
        _tx("buy", "10", "20", date(2026, 1, 1)),
        _tx("sell", "3", "30", date(2026, 2, 1)),
        _tx("sell", "2", "25", date(2027, 3, 1)),
    ])
    assert pos["realized_events"] == [
        (date(2026, 2, 1), Decimal("30")),
        (date(2027, 3, 1), Decimal("10")),
    ]
    assert sum(g for _, g in pos["realized_events"]) == pos["realized_gain"]


async def _wallet(session, test_user, test_workspace, name: str, treatment: str):
    from app.models.asset_group import AssetGroup

    wallet = AssetGroup(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name=name, icon="wallet", color="#0EA5E9", position=0, source="manual",
        tax_treatment=treatment,
    )
    session.add(wallet)
    await session.commit()
    return wallet


async def _sell_at_profit(session, test_workspace, test_user, ticker: str, group_id, when=date(2026, 6, 1)):
    """Buy 10 @ 20 then sell 5 @ 30 → 50.00 of Realised Gain."""
    provider = _FakeProvider({ticker: _quote(ticker, 30.0)})
    holding = await asset_transaction_service.buy_into_holding(
        session, test_workspace.id, test_user.id,
        AssetBuyCreate(
            ticker=ticker, quantity=Decimal("10"), price=Decimal("20"),
            date=date(2026, 1, 1), group_id=group_id,
        ),
        market_provider=provider,
    )
    return await asset_transaction_service.add_transaction(
        session, holding.id, test_workspace.id,
        AssetTransactionCreate(kind="sell", quantity=Decimal("5"), price=Decimal("30"), date=when),
    )


@pytest.mark.asyncio
async def test_roth_profit_is_realised_but_never_reportable(session, test_workspace, test_user):
    roth = await _wallet(session, test_user, test_workspace, "Roth IRA", "roth")
    read = await _sell_at_profit(session, test_workspace, test_user, "PETR4.SA", roth.id)

    # The trade made real money and the performance figure says so...
    assert read is not None and read.realized_gain is not None
    assert round(read.realized_gain, 2) == 50.00

    # ...and none of it reaches a tax calculation.
    totals = await asset_transaction_service.reportable_gain(session, test_workspace.id)
    assert totals["reportable_gain"] == 0.0
    assert totals["non_reportable_gain"] == 50.0


@pytest.mark.asyncio
async def test_taxable_wallet_gain_is_reportable(session, test_workspace, test_user):
    taxable = await _wallet(session, test_user, test_workspace, "Brokerage", "taxable")
    await _sell_at_profit(session, test_workspace, test_user, "VALE3.SA", taxable.id)

    totals = await asset_transaction_service.reportable_gain(session, test_workspace.id)
    assert totals["reportable_gain"] == 50.0
    assert totals["non_reportable_gain"] == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("treatment", ["traditional", "hsa", "other"])
async def test_only_taxable_is_allowlisted(session, test_workspace, test_user, treatment):
    wallet = await _wallet(session, test_user, test_workspace, treatment, treatment)
    await _sell_at_profit(session, test_workspace, test_user, "ITUB4.SA", wallet.id)

    totals = await asset_transaction_service.reportable_gain(session, test_workspace.id)
    assert totals["reportable_gain"] == 0.0
    assert totals["non_reportable_gain"] == 50.0


@pytest.mark.asyncio
async def test_holding_without_a_wallet_is_not_reportable(session, test_workspace, test_user):
    await _sell_at_profit(session, test_workspace, test_user, "BBAS3.SA", None)

    totals = await asset_transaction_service.reportable_gain(session, test_workspace.id)
    assert totals["reportable_gain"] == 0.0
    assert totals["non_reportable_gain"] == 50.0


@pytest.mark.asyncio
async def test_reportable_gain_bounds_by_sell_date(session, test_workspace, test_user):
    taxable = await _wallet(session, test_user, test_workspace, "Brokerage", "taxable")
    await _sell_at_profit(
        session, test_workspace, test_user, "WEGE3.SA", taxable.id, when=date(2027, 2, 1)
    )

    in_2026 = await asset_transaction_service.reportable_gain(
        session, test_workspace.id, start=date(2026, 1, 1), end=date(2027, 1, 1)
    )
    in_2027 = await asset_transaction_service.reportable_gain(
        session, test_workspace.id, start=date(2027, 1, 1), end=date(2028, 1, 1)
    )
    assert in_2026["reportable_gain"] == 0.0
    assert in_2027["reportable_gain"] == 50.0


@pytest.mark.asyncio
async def test_deleting_a_buy_a_later_sell_needs_is_rejected(
    session, test_workspace, market_asset
):
    """`add` and `update` both refuse a ledger that goes negative; `delete`
    used to let one through, and `_recompute` clamped the shortfall silently
    while booking the gain against units that never left."""
    from fastapi import HTTPException

    await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="buy", quantity=Decimal("10"), price=Decimal("20"), date=date(2026, 1, 1)),
    )
    await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="sell", quantity=Decimal("8"), price=Decimal("30"), date=date(2026, 2, 1)),
    )
    txs = await asset_transaction_service.list_asset_transactions(
        session, market_asset.id, test_workspace.id
    )
    assert txs is not None
    buy = next(t for t in txs if t.kind == "buy")

    with pytest.raises(HTTPException) as exc:
        await asset_transaction_service.delete_transaction(
            session, buy.id, test_workspace.id
        )
    assert exc.value.status_code == 422

    remaining = await asset_transaction_service.list_asset_transactions(
        session, market_asset.id, test_workspace.id
    )
    assert remaining is not None
    assert len(remaining) == 2


@pytest.mark.asyncio
async def test_deleting_the_sell_first_then_the_buy_is_allowed(
    session, test_workspace, market_asset
):
    """The refusal is about the resulting ledger, not about deletion."""
    await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="buy", quantity=Decimal("10"), price=Decimal("20"), date=date(2026, 1, 1)),
    )
    await asset_transaction_service.add_transaction(
        session, market_asset.id, test_workspace.id,
        AssetTransactionCreate(kind="sell", quantity=Decimal("8"), price=Decimal("30"), date=date(2026, 2, 1)),
    )
    txs = await asset_transaction_service.list_asset_transactions(
        session, market_asset.id, test_workspace.id
    )
    assert txs is not None
    for kind in ("sell", "buy"):
        tx = next(t for t in txs if t.kind == kind)
        await asset_transaction_service.delete_transaction(session, tx.id, test_workspace.id)

    remaining = await asset_transaction_service.list_asset_transactions(
        session, market_asset.id, test_workspace.id
    )
    assert remaining == []


# ---------------------------------------------------------------------------
# Option contracts: the hundred-share multiplier and written positions
# ---------------------------------------------------------------------------

def test_a_contract_is_a_hundred_shares():
    # NVDA 1/17/2025 Call $140: bought 2 at $4.00 for $800.06, sold one at a
    # time at $4.66 for $465.94 each. The broker booked $131.82.
    pos = _recompute([
        _tx("buy", "2", "4.00", date(2024, 12, 17), fee="0.06"),
        _tx("sell", "1", "4.66", date(2024, 12, 19), fee="0.06"),
        _tx("sell", "1", "4.66", date(2024, 12, 19), fee="0.06"),
    ], asset_type="option")
    assert pos["realized_gain"] == Decimal("131.82")
    # Per contract, not per share — `cost_basis = average_price × quantity` is
    # what the read surface is built on, so the average carries the multiplier.
    assert pos["average_price"] is None
    assert pos["units"] == Decimal("0")


def test_the_multiplier_does_not_reach_a_stock():
    """The same ledger typed `stock` is two shares, not two contracts — and
    not the contract figure over a hundred either, because a fee is the same
    number of dollars whichever instrument paid it."""
    ledger = [
        _tx("buy", "2", "4.00", date(2024, 12, 17), fee="0.06"),
        _tx("sell", "1", "4.66", date(2024, 12, 19), fee="0.06"),
        _tx("sell", "1", "4.66", date(2024, 12, 19), fee="0.06"),
    ]
    assert _recompute(ledger, asset_type="stock")["realized_gain"] == Decimal("1.14")
    # And an unstated class is a share, which is what leaves every holding
    # already on the ledger exactly where it was.
    assert _recompute(ledger)["realized_gain"] == Decimal("1.14")


def test_an_open_contract_averages_per_contract():
    pos = _recompute([_tx("buy", "2", "4.00", date(2024, 12, 17), fee="0.06")], asset_type="option")
    assert pos["units"] == Decimal("2")
    assert pos["average_price"] == Decimal("400.03")
    assert pos["cost_basis"] == Decimal("800.06")


def test_expiring_worthless_realises_the_whole_premium():
    # F 6/12/2026 Call $20: six contracts at $0.15 that ran out of time. The
    # expiry is an ordinary sell at nothing, so the premium is the loss.
    pos = _recompute([
        _tx("buy", "6", "0.15", date(2026, 5, 29), fee="0.24"),
        _tx("sell", "6", "0", date(2026, 6, 12)),
    ], asset_type="option")
    assert pos["realized_gain"] == Decimal("-90.24")
    assert pos["units"] == Decimal("0")


def test_a_written_contract_opens_for_a_credit():
    """Selling a contract nobody bought is writing it: the position goes
    negative and the basis is the premium received, not a cost."""
    pos = _recompute([_tx("sell", "1", "4.00", date(2026, 1, 2), fee="0.03")], asset_type="option")
    assert pos["units"] == Decimal("-1")
    assert pos["cost_basis"] == Decimal("-399.97")
    # A magnitude either way: the premium per contract, as the average of a
    # bought contract is its cost per contract.
    assert pos["average_price"] == Decimal("399.97")
    # Premium received is not income at receipt — nothing is realised until the
    # contract is bought back or expires.
    assert pos["realized_gain"] == Decimal("0")


def test_buying_a_written_contract_back_cheaper_is_the_gain():
    pos = _recompute([
        _tx("sell", "1", "4.00", date(2026, 1, 2), fee="0.03"),
        _tx("buy", "1", "1.00", date(2026, 2, 2), fee="0.02"),
    ], asset_type="option")
    assert pos["realized_gain"] == Decimal("299.95")
    assert pos["units"] == Decimal("0")


def test_a_written_contract_that_expires_keeps_the_whole_premium():
    pos = _recompute([
        _tx("sell", "2", "1.10", date(2026, 1, 2), fee="0.04"),
        _tx("buy", "2", "0", date(2026, 2, 20)),
    ], asset_type="option")
    assert pos["realized_gain"] == Decimal("219.96")
    assert pos["units"] == Decimal("0")


def test_a_stock_still_cannot_go_short():
    """The clamp `_recompute` has always applied stays put for everything that
    is not a contract — an over-sell holds the position at zero."""
    pos = _recompute([_tx("sell", "5", "100", date(2026, 1, 2))])
    assert pos["units"] == Decimal("0")
    assert pos["cost_basis"] == Decimal("0")


def test_selling_through_flat_turns_a_contract_around():
    pos = _recompute([
        _tx("buy", "1", "2.00", date(2026, 1, 2)),
        _tx("sell", "3", "3.00", date(2026, 2, 2)),
    ], asset_type="option")
    # One contract closed at a 100 gain, two written for 600 of premium.
    assert pos["realized_gain"] == Decimal("100")
    assert pos["units"] == Decimal("-2")
    assert pos["cost_basis"] == Decimal("-600")


def test_a_year_of_contracts_nets_what_the_broker_reported():
    """Six real round trips, netting the $691.04 the broker's own history
    shows: $261.66 in 2024, $519.62 in 2025, and the $90.24 that expired."""
    ledgers = [
        [_tx("buy", "2", "4.00", date(2024, 12, 17), fee="0.06"),
         _tx("sell", "1", "4.66", date(2024, 12, 19), fee="0.06"),
         _tx("sell", "1", "4.66", date(2024, 12, 19), fee="0.06")],
        [_tx("buy", "2", "1.25", date(2024, 12, 20), fee="0.06"),
         _tx("sell", "1", "1.90", date(2024, 12, 23), fee="0.05"),
         _tx("sell", "1", "1.90", date(2024, 12, 23), fee="0.05")],
        [_tx("buy", "1", "9.00", date(2024, 12, 23), fee="0.03"),
         _tx("sell", "1", "12.00", date(2025, 1, 6), fee="0.08")],
        [_tx("buy", "1", "2.70", date(2024, 12, 27), fee="0.03"),
         _tx("sell", "1", "3.50", date(2025, 1, 6), fee="0.05")],
        [_tx("buy", "2", "1.45", date(2025, 1, 13), fee="0.08"),
         _tx("sell", "2", "2.15", date(2025, 1, 14), fee="0.11")],
        [_tx("buy", "6", "0.15", date(2026, 5, 29), fee="0.24"),
         _tx("sell", "6", "0", date(2026, 6, 12))],
    ]
    by_year: dict[int, Decimal] = {}
    for ledger in ledgers:
        for when, gain in _recompute(ledger, asset_type="option")["realized_events"]:
            by_year[when.year] = by_year.get(when.year, Decimal("0")) + gain
    assert by_year == {
        2024: Decimal("261.66"), 2025: Decimal("519.62"), 2026: Decimal("-90.24"),
    }
    assert sum(by_year.values()) == Decimal("691.04")


@pytest_asyncio.fixture
async def contract(session: AsyncSession, test_user: User, test_workspace) -> Asset:
    asset = Asset(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name="TSM 1/17/2025 Call $210.00", type="option", currency="USD",
        valuation_method="market_price", ticker="TSM250117C00210000",
        last_price=Decimal("12.00"),
    )
    session.add(asset)
    await session.commit()
    await session.refresh(asset)
    return asset


@pytest.mark.asyncio
async def test_writing_a_contract_is_allowed_where_an_over_sell_is_not(
    session, test_workspace, contract, market_asset
):
    from fastapi import HTTPException

    read = await asset_transaction_service.add_transaction(
        session, contract.id, test_workspace.id,
        AssetTransactionCreate(
            kind="sell", quantity=Decimal("1"), price=Decimal("4.00"), date=date(2026, 1, 2)
        ),
    )
    assert read is not None and read.units == -1.0

    with pytest.raises(HTTPException) as exc:
        await asset_transaction_service.add_transaction(
            session, market_asset.id, test_workspace.id,
            AssetTransactionCreate(
                kind="sell", quantity=Decimal("1"), price=Decimal("30"), date=date(2026, 1, 2)
            ),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_a_contract_closed_in_january_is_the_new_years_gain(
    session, test_workspace, test_user, contract
):
    """Opened in December, closed in January: the whole $299.89 falls in the
    year the sell landed in, not the year the buy did."""
    taxable = await _wallet(session, test_user, test_workspace, "Brokerage", "taxable")
    contract.group_id = taxable.id
    await session.commit()

    await asset_transaction_service.add_transaction(
        session, contract.id, test_workspace.id,
        AssetTransactionCreate(
            kind="buy", quantity=Decimal("1"), price=Decimal("9.00"),
            fee=Decimal("0.03"), date=date(2024, 12, 23),
        ),
    )
    await asset_transaction_service.add_transaction(
        session, contract.id, test_workspace.id,
        AssetTransactionCreate(
            kind="sell", quantity=Decimal("1"), price=Decimal("12.00"),
            fee=Decimal("0.08"), date=date(2025, 1, 6),
        ),
    )

    from_2025 = await asset_transaction_service.reportable_gain(
        session, test_workspace.id, start=date(2025, 1, 1)
    )
    before_2025 = await asset_transaction_service.reportable_gain(
        session, test_workspace.id, end=date(2025, 1, 1)
    )
    assert from_2025["reportable_gain"] == 299.89
    assert before_2025["reportable_gain"] == 0.0
