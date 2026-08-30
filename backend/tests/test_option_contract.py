"""Tests for the OCC symbol, which is an option contract's whole identity.

The symbol carries the underlying, the expiry, the right and the strike, so
recognising one is what decides an asset's class, its multiplier and its name.
A shape that matched an ordinary ticker would silently multiply a stock's cost
basis by a hundred, so the rejections matter more than the acceptances.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.services.asset_transaction_service import _type_from_quote
from app.services.option_contract import (
    OPTION_TYPE,
    describe,
    expiry_of,
    is_option_symbol,
    multiplier_for,
    strike_of,
    underlying_of,
)


@pytest.mark.parametrize(
    "symbol",
    [
        "NVDA250117C00140000",
        "TSM250117C00210000",
        "F260612C00020000",
        "SPXW251219P05000000",
        "A250117C00050000",
    ],
)
def test_real_contracts_are_recognised(symbol: str):
    assert is_option_symbol(symbol) is True


@pytest.mark.parametrize(
    "ticker",
    [
        "NVDA", "SPCX", "BTC-USD", "PETR4.SA", "SPAXX", "FZROX",
        "TD:A1B2C3D4:2029-01-01",
        None, "",
        # Right shape, wrong right: only a call or a put exists.
        "NVDA250117X00140000",
        # A strike of seven digits, not eight.
        "NVDA250117C0014000",
    ],
)
def test_every_other_ticker_shape_is_rejected(ticker):
    assert is_option_symbol(ticker) is False


def test_the_symbol_answers_for_the_whole_contract():
    assert underlying_of("NVDA250117C00140000") == "NVDA"
    assert expiry_of("NVDA250117C00140000") == date(2025, 1, 17)
    assert strike_of("NVDA250117C00140000") == Decimal("140")
    assert describe("NVDA250117C00140000") == "NVDA 1/17/2025 Call $140.00"


def test_a_description_reads_as_the_broker_wrote_it():
    # Byte-identical to Robinhood's own Description column, so a holding and
    # the row it was imported from can be matched by eye.
    assert describe("F260612C00020000") == "F 6/12/2026 Call $20.00"
    assert describe("HWM250221C00130000") == "HWM 2/21/2025 Call $130.00"
    assert describe("SPXW251219P05000000") == "SPXW 12/19/2025 Put $5,000.00"


def test_an_ordinary_ticker_describes_as_nothing():
    assert describe("NVDA") is None
    assert expiry_of("NVDA") is None


def test_only_a_contract_carries_a_multiplier():
    assert multiplier_for(OPTION_TYPE) == Decimal("100")
    for asset_type in ("stock", "etf", "crypto", "fund", "cash_equivalent", "other", None):
        assert multiplier_for(asset_type) == Decimal("1")


def test_the_symbol_types_the_holding_whatever_the_quote_says():
    # No provider answers for most contracts, so the symbol is usually all
    # there is — and where one does answer, it does not get to disagree.
    assert _type_from_quote(None, "NVDA250117C00140000") == OPTION_TYPE
    assert _type_from_quote("EQUITY", "NVDA250117C00140000") == OPTION_TYPE
    assert _type_from_quote("EQUITY", "NVDA") == "stock"
