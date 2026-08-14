"""Tests for Tax Lots, Holding Period and the realised gain split (#65).

The pure section covers the boundary that decides what a sale costs — one year
exactly, one day either side, and the leap-year anniversary — plus FIFO lot
matching and partial sales. The service section covers the tax-character gate.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.user import User
from app.models.asset_transaction import AssetTransaction
from app.services.asset_transaction_service import _recompute
from app.services.tax_lots import (
    asset_tax_lots,
    build_lots,
    days_until_long_term,
    is_long_term,
    long_term_on,
)


def _tx(kind: str, qty: str, price: str, d: date, fee: str = "0") -> AssetTransaction:
    return AssetTransaction(
        id=uuid.uuid4(), asset_id=uuid.uuid4(), workspace_id=uuid.uuid4(),
        kind=kind, quantity=Decimal(qty), price=Decimal(price), fee=Decimal(fee), date=d,
    )


# ---------------------------------------------------------------------------
# Holding period boundary (no DB)
# ---------------------------------------------------------------------------

def test_exactly_one_year_is_long_term():
    assert is_long_term(date(2025, 3, 10), date(2026, 3, 10)) is True


def test_one_day_short_of_a_year_is_short_term():
    assert is_long_term(date(2025, 3, 10), date(2026, 3, 9)) is False


def test_the_day_after_the_anniversary_is_still_long_term():
    assert is_long_term(date(2025, 3, 10), date(2026, 3, 11)) is True


def test_leap_day_lot_turns_long_on_the_first_of_march():
    # 29 Feb 2024 has no 2025 anniversary; 28 Feb is not yet a full year.
    assert long_term_on(date(2024, 2, 29)) == date(2025, 3, 1)
    assert is_long_term(date(2024, 2, 29), date(2025, 2, 28)) is False
    assert is_long_term(date(2024, 2, 29), date(2025, 3, 1)) is True


def test_leap_year_lengthens_the_wait_by_a_day():
    # 1 Mar 2023 → 1 Mar 2024 spans 29 Feb, so it is 366 days, not 365.
    assert long_term_on(date(2023, 3, 1)) == date(2024, 3, 1)
    assert days_until_long_term(date(2023, 3, 1), date(2023, 3, 1)) == 366
    assert days_until_long_term(date(2025, 3, 1), date(2025, 3, 1)) == 365


def test_days_until_long_term_is_zero_once_long():
    assert days_until_long_term(date(2024, 1, 1), date(2025, 1, 1)) == 0
    assert days_until_long_term(date(2024, 1, 1), date(2030, 1, 1)) == 0


# ---------------------------------------------------------------------------
# Open lots (no DB)
# ---------------------------------------------------------------------------

def test_each_buy_is_a_lot_at_its_own_date_and_price():
    pos = build_lots(
        [_tx("buy", "10", "100", date(2025, 1, 1)), _tx("buy", "5", "110", date(2026, 2, 1))],
        as_of=date(2026, 3, 1),
    )
    assert [(lot["acquired"], lot["quantity"], lot["unit_price"]) for lot in pos["lots"]] == [
        (date(2025, 1, 1), Decimal("10"), Decimal("100")),
        (date(2026, 2, 1), Decimal("5"), Decimal("110")),
    ]


def test_a_fee_lands_in_the_unit_price_of_its_lot():
    pos = build_lots([_tx("buy", "10", "100", date(2026, 1, 1), fee="20")], as_of=date(2026, 2, 1))
    assert pos["lots"][0]["unit_price"] == Decimal("102")
    assert pos["lots"][0]["cost"] == Decimal("1020")


def test_open_position_is_measured_to_today():
    buy = date(2025, 6, 1)
    still_short = build_lots([_tx("buy", "10", "100", buy)], as_of=date(2026, 5, 31))
    now_long = build_lots([_tx("buy", "10", "100", buy)], as_of=date(2026, 6, 1))

    assert still_short["lots"][0]["long_term"] is False
    assert still_short["lots"][0]["holding_days"] == 364
    assert still_short["lots"][0]["days_until_long_term"] == 1
    assert now_long["lots"][0]["long_term"] is True
    assert now_long["lots"][0]["days_until_long_term"] == 0


def test_position_splits_its_open_quantity_and_cost_by_holding_period():
    pos = build_lots(
        [_tx("buy", "10", "100", date(2025, 1, 1)), _tx("buy", "5", "200", date(2026, 2, 1))],
        as_of=date(2026, 3, 1),
    )
    assert pos["long_quantity"] == Decimal("10")
    assert pos["short_quantity"] == Decimal("5")
    assert pos["long_cost"] == Decimal("1000")
    assert pos["short_cost"] == Decimal("1000")


def test_a_holding_with_no_trades_has_no_lots():
    # A Snapshot Holding: the provider reported a position, not the trades
    # behind it, so Holding Period is unknown rather than short (ADR 0002).
    assert build_lots([], as_of=date(2026, 3, 1))["lots"] == []


# ---------------------------------------------------------------------------
# Sales: lot matching, partial sales, holding-period split (no DB)
# ---------------------------------------------------------------------------

def test_a_sale_consumes_the_oldest_lot_first():
    pos = build_lots(
        [
            _tx("buy", "10", "100", date(2025, 1, 1)),
            _tx("buy", "10", "100", date(2026, 1, 1)),
            _tx("sell", "10", "150", date(2026, 6, 1)),
        ],
        as_of=date(2026, 6, 1),
    )
    # The 2025 lot is gone; what is left is the short-term 2026 lot.
    assert [(lot["acquired"], lot["quantity"]) for lot in pos["lots"]] == [
        (date(2026, 1, 1), Decimal("10"))
    ]
    assert pos["sales"][0]["long_quantity"] == Decimal("10")
    assert pos["sales"][0]["short_quantity"] == Decimal("0")


def test_a_partial_sale_leaves_the_rest_of_the_lot_open():
    pos = build_lots(
        [_tx("buy", "10", "100", date(2025, 1, 1)), _tx("sell", "4", "150", date(2026, 6, 1))],
        as_of=date(2026, 6, 1),
    )
    assert [(lot["acquired"], lot["quantity"]) for lot in pos["lots"]] == [
        (date(2025, 1, 1), Decimal("6"))
    ]
    assert pos["sales"][0]["quantity"] == Decimal("4")


def test_a_sale_spanning_two_lots_splits_the_gain_by_the_quantity_of_each():
    # 10 @ 100 (long by the sell date) + 10 @ 200 (short) → avg 150.
    # Sell 15 @ 250 → gain (250 - 150) * 15 = 1500, from 10 long + 5 short.
    pos = build_lots(
        [
            _tx("buy", "10", "100", date(2025, 1, 1)),
            _tx("buy", "10", "200", date(2026, 3, 1)),
            _tx("sell", "15", "250", date(2026, 6, 1)),
        ],
        as_of=date(2026, 6, 1),
    )
    sale = pos["sales"][0]
    assert sale["gain"] == Decimal("1500")
    assert (sale["long_quantity"], sale["short_quantity"]) == (Decimal("10"), Decimal("5"))
    assert sale["long_gain"] == Decimal("1000")
    assert sale["short_gain"] == Decimal("500")
    assert pos["realised_long"] == Decimal("1000")
    assert pos["realised_short"] == Decimal("500")


def test_a_sale_is_measured_against_its_own_date_not_today():
    ledger = [_tx("buy", "10", "100", date(2025, 1, 1)), _tx("sell", "10", "150", date(2025, 12, 31))]
    # Sold one day short of a year — and it stays short-term years later.
    assert build_lots(ledger, as_of=date(2025, 12, 31))["realised_short"] == Decimal("500")
    assert build_lots(ledger, as_of=date(2030, 1, 1))["realised_short"] == Decimal("500")
    assert build_lots(ledger, as_of=date(2030, 1, 1))["realised_long"] == Decimal("0")


def test_the_split_always_sums_back_to_the_ledgers_realised_gain():
    # An uneven ratio (2 of 3 units long) with a fee, where a per-part
    # rounding would leave the two halves off by a cent.
    ledger = [
        _tx("buy", "2", "10.05", date(2025, 1, 1)),
        _tx("buy", "1", "20.07", date(2026, 1, 1)),
        _tx("sell", "3", "33.33", date(2026, 6, 1), fee="1.37"),
    ]
    pos = build_lots(ledger, as_of=date(2026, 6, 1))
    assert pos["realised_long"] + pos["realised_short"] == _recompute(ledger)["realized_gain"]


def test_a_full_exit_leaves_no_open_lots():
    pos = build_lots(
        [_tx("buy", "10", "100", date(2025, 1, 1)), _tx("sell", "10", "150", date(2026, 6, 1))],
        as_of=date(2026, 6, 1),
    )
    assert pos["lots"] == []
    assert pos["long_quantity"] == Decimal("0")
    assert pos["realised_long"] == Decimal("500")


def test_a_re_bought_position_lots_from_the_new_buy_date():
    pos = build_lots(
        [
            _tx("buy", "10", "100", date(2024, 1, 1)),
            _tx("sell", "10", "150", date(2025, 6, 1)),
            _tx("buy", "10", "200", date(2026, 5, 1)),
        ],
        as_of=date(2026, 6, 1),
    )
    assert [(lot["acquired"], lot["long_term"]) for lot in pos["lots"]] == [(date(2026, 5, 1), False)]


# ---------------------------------------------------------------------------
# Service: tax character gate
# ---------------------------------------------------------------------------

async def _wallet(session, test_user, test_workspace, name: str, treatment: str) -> AssetGroup:
    wallet = AssetGroup(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name=name, icon="wallet", color="#0EA5E9", position=0, source="manual",
        tax_treatment=treatment,
    )
    session.add(wallet)
    await session.commit()
    return wallet


async def _holding(
    session, test_user, test_workspace, group_id, *, ticker: str, ledger: list[tuple]
) -> Asset:
    asset = Asset(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name=ticker, type="stock", currency="USD", valuation_method="market_price",
        ticker=ticker, group_id=group_id, units=Decimal("0"), position=0,
    )
    session.add(asset)
    units = Decimal("0")
    for kind, qty, price, when in ledger:
        session.add(
            AssetTransaction(
                id=uuid.uuid4(), asset_id=asset.id, workspace_id=test_workspace.id,
                kind=kind, quantity=Decimal(qty), price=Decimal(price), fee=Decimal("0"),
                date=when, source="manual",
            )
        )
        units += Decimal(qty) if kind == "buy" else -Decimal(qty)
    asset.units = units
    await session.commit()
    return asset


@pytest.mark.asyncio
async def test_taxable_wallet_lists_its_lots(
    session: AsyncSession, test_workspace, test_user: User
):
    wallet = await _wallet(session, test_user, test_workspace, "Brokerage", "taxable")
    asset = await _holding(
        session, test_user, test_workspace, wallet.id, ticker="VOO",
        ledger=[("buy", "10", "100", date(2025, 1, 1)), ("buy", "5", "200", date(2026, 3, 1))],
    )

    result = await asset_tax_lots(session, asset.id, test_workspace.id, as_of=date(2026, 6, 1))

    assert result is not None
    assert result["tax_character"] is True
    assert [(lot["acquired"], lot["quantity"], lot["long_term"]) for lot in result["lots"]] == [
        ("2025-01-01", 10.0, True),
        ("2026-03-01", 5.0, False),
    ]
    assert result["long_quantity"] == 10.0
    assert result["short_quantity"] == 5.0
    assert result["lots"][1]["days_until_long_term"] == 273


@pytest.mark.asyncio
@pytest.mark.parametrize("treatment", ["roth", "traditional", "hsa", "other"])
async def test_non_taxable_wallet_surfaces_no_lots(
    session: AsyncSession, test_workspace, test_user: User, treatment: str
):
    wallet = await _wallet(session, test_user, test_workspace, treatment, treatment)
    asset = await _holding(
        session, test_user, test_workspace, wallet.id, ticker="VTI",
        ledger=[("buy", "10", "100", date(2025, 1, 1)), ("sell", "4", "150", date(2026, 6, 1))],
    )

    result = await asset_tax_lots(session, asset.id, test_workspace.id, as_of=date(2026, 6, 1))

    assert result is not None
    assert result["tax_character"] is False
    assert result["lots"] == []
    assert result["sales"] == []
    assert result["realised_long"] == 0.0
    assert result["realised_short"] == 0.0


@pytest.mark.asyncio
async def test_holding_in_no_wallet_has_no_tax_character(
    session: AsyncSession, test_workspace, test_user: User
):
    asset = await _holding(
        session, test_user, test_workspace, None, ticker="ITOT",
        ledger=[("buy", "10", "100", date(2025, 1, 1))],
    )

    result = await asset_tax_lots(session, asset.id, test_workspace.id, as_of=date(2026, 6, 1))

    assert result is not None and result["tax_character"] is False
    assert result["lots"] == []


@pytest.mark.asyncio
async def test_snapshot_holding_is_flagged_rather_than_dated(
    session: AsyncSession, test_workspace, test_user: User
):
    wallet = await _wallet(session, test_user, test_workspace, "Synced", "taxable")
    asset = await _holding(
        session, test_user, test_workspace, wallet.id, ticker="SCHD", ledger=[]
    )
    asset.units = Decimal("42")
    await session.commit()

    result = await asset_tax_lots(session, asset.id, test_workspace.id, as_of=date(2026, 6, 1))

    assert result is not None
    assert result["snapshot"] is True
    assert result["lots"] == []


@pytest.mark.asyncio
async def test_missing_asset_is_not_found(session: AsyncSession, test_workspace, test_user: User):
    assert await asset_tax_lots(session, uuid.uuid4(), test_workspace.id) is None
