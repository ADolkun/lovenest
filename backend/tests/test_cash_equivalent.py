import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.base import HoldingData
from app.services.asset_transaction_service import _type_from_quote
from app.services.cash_equivalent import CASH_EQUIVALENT_TYPE, is_cash_equivalent_ticker
from app.services.connection_service import _upsert_asset_from_holding


def test_known_money_market_and_stablecoin_tickers_are_cash_equivalents():
    assert is_cash_equivalent_ticker("SPAXX")
    assert is_cash_equivalent_ticker("VMFXX")
    assert is_cash_equivalent_ticker("USDC")


def test_ticker_match_ignores_case_and_surrounding_space():
    assert is_cash_equivalent_ticker("  spaxx ")


def test_ordinary_and_missing_tickers_are_not_cash_equivalents():
    assert not is_cash_equivalent_ticker("VOO")
    assert not is_cash_equivalent_ticker("BTC")
    assert not is_cash_equivalent_ticker(None)
    assert not is_cash_equivalent_ticker("")


def test_type_from_quote_prefers_the_ticker_over_the_quote_type():
    # yfinance reports SPAXX as a mutual fund; it is still Liquid Cash.
    assert _type_from_quote("MUTUALFUND", "SPAXX") == CASH_EQUIVALENT_TYPE


def test_type_from_quote_maps_money_market_quotes():
    assert _type_from_quote("MONEYMARKET", "XYZXX") == CASH_EQUIVALENT_TYPE


def test_type_from_quote_leaves_ordinary_holdings_alone():
    assert _type_from_quote("EQUITY", "AAPL") == "stock"
    assert _type_from_quote("ETF", "VOO") == "etf"
    assert _type_from_quote(None, "VOO") == "investment"


def _holding(ticker: str) -> HoldingData:
    return HoldingData(
        external_id=f"h-{ticker}",
        name=ticker,
        currency="USD",
        current_value=Decimal("100"),
        quantity=Decimal("1"),
        ticker=ticker,
    )


@pytest.mark.asyncio
async def test_sync_classifies_a_known_cash_equivalent_on_create(
    session: AsyncSession, test_user, test_workspace
):
    asset = await _upsert_asset_from_holding(
        session, None, _holding("SPAXX"), test_user.id, uuid.uuid4(), "simplefin"
    )

    assert asset.type == CASH_EQUIVALENT_TYPE


@pytest.mark.asyncio
async def test_sync_leaves_an_ordinary_holding_as_an_investment(
    session: AsyncSession, test_user, test_workspace
):
    asset = await _upsert_asset_from_holding(
        session, None, _holding("VOO"), test_user.id, uuid.uuid4(), "simplefin"
    )

    assert asset.type == "investment"


@pytest.mark.asyncio
async def test_a_later_sync_never_overwrites_the_users_classification(
    session: AsyncSession, test_user, test_workspace
):
    asset = await _upsert_asset_from_holding(
        session, None, _holding("VOO"), test_user.id, uuid.uuid4(), "simplefin"
    )
    asset.type = CASH_EQUIVALENT_TYPE

    resynced = await _upsert_asset_from_holding(
        session, asset, _holding("VOO"), test_user.id, uuid.uuid4(), "simplefin"
    )

    assert resynced.type == CASH_EQUIVALENT_TYPE
