"""Tests for cross-account Wash Sale exposure (#66).

The pure section covers what decides the warning: the window boundary either
side of the sale, the asset classes the rule reaches, and the fact that a buy in
*any* wallet counts. The service section covers the cross-wallet lookup and the
promise that nothing here adjusts a basis.
"""
import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.models.user import User
from app.models.workspace import Workspace
from app.services.wash_sale import (
    assess,
    in_window,
    is_covered,
    wash_sale_exposure,
    window,
)

SELL = date(2026, 6, 15)


SELLING_WALLET = "wallet-selling"


def _buy(
    when: date,
    wallet: str = "Individual",
    treatment: str = "taxable",
    wallet_id: str = SELLING_WALLET,
) -> dict:
    return {
        "date": when,
        "quantity": Decimal("1"),
        "wallet": wallet,
        "wallet_id": wallet_id,
        "tax_treatment": treatment,
        "same_wallet": wallet_id == SELLING_WALLET,
    }


def _elsewhere(when: date, wallet: str = "ROTH IRA", treatment: str = "roth") -> dict:
    return _buy(when, wallet, treatment, wallet_id=f"wallet-{wallet}")


def _assess(
    acquisitions: list[dict],
    *,
    asset_type: Optional[str] = "stock",
    reportable: bool = True,
    at_loss: bool = True,
    holdings: Optional[list[dict]] = None,
) -> dict:
    return assess(
        asset_type=asset_type,
        reportable=reportable,
        at_loss=at_loss,
        sell_date=SELL,
        acquisitions=acquisitions,
        holdings=holdings or [],
    )


# ---------------------------------------------------------------------------
# The window (no DB)
# ---------------------------------------------------------------------------

def test_window_reaches_thirty_days_either_side():
    assert window(SELL) == (date(2026, 5, 16), date(2026, 7, 15))


def test_both_ends_of_the_window_are_inside_it():
    assert in_window(date(2026, 5, 16), SELL) is True
    assert in_window(date(2026, 7, 15), SELL) is True


def test_a_day_past_either_end_is_outside():
    assert in_window(date(2026, 5, 15), SELL) is False
    assert in_window(date(2026, 7, 16), SELL) is False


def test_a_buy_inside_the_window_warns():
    result = _assess([_buy(date(2026, 6, 20))])
    assert result["warning"] is True
    assert [a["date"] for a in result["acquisitions"]] == [date(2026, 6, 20)]


def test_a_buy_before_the_window_does_not_warn():
    result = _assess([_buy(date(2026, 4, 1))])
    assert result["warning"] is False
    assert result["acquisitions"] == []


def test_a_buy_after_the_window_does_not_warn():
    result = _assess([_buy(date(2026, 9, 1))])
    assert result["warning"] is False


def test_a_sale_at_a_gain_does_not_warn():
    # The rule only disallows losses; a profitable sale has nothing to lose.
    assert _assess([_buy(date(2026, 6, 20))], at_loss=False)["warning"] is False


def test_a_loss_in_a_non_taxable_wallet_does_not_warn():
    # No deduction arises there in the first place, so none can be disallowed.
    assert _assess([_buy(date(2026, 6, 20))], reportable=False)["warning"] is False


# ---------------------------------------------------------------------------
# Asset class (no DB)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("asset_type", ["stock", "etf", "fund", "investment"])
def test_securities_are_covered(asset_type: str):
    # `investment` is the bucket every synced brokerage holding lands in, so
    # leaving it out would make the check inert on real portfolios.
    assert is_covered(asset_type) is True
    assert _assess([_buy(date(2026, 6, 20))], asset_type=asset_type)["warning"] is True


@pytest.mark.parametrize(
    "asset_type", ["crypto", "cash_equivalent", "real_estate", "vehicle", "valuable", "other", None]
)
def test_uncovered_classes_never_warn(asset_type):
    # Crypto is property rather than a security, so the rule does not reach it.
    result = _assess([_buy(date(2026, 6, 20))], asset_type=asset_type)
    assert is_covered(asset_type) is False
    assert result["covered"] is False
    assert result["warning"] is False
    assert result["acquisitions"] == []


# ---------------------------------------------------------------------------
# Across wallets (no DB)
# ---------------------------------------------------------------------------

def test_a_buy_in_another_wallet_warns():
    result = _assess([_elsewhere(date(2026, 6, 20))])
    assert result["warning"] is True
    assert result["acquisitions"][0]["wallet"] == "ROTH IRA"
    assert result["acquisitions"][0]["same_wallet"] is False


def test_a_repurchase_in_the_selling_wallet_warns_and_names_itself():
    # Recoverable — the disallowed loss moves into the replacement's basis —
    # but still a wash sale, and still the user's to know about.
    result = _assess([_buy(date(2026, 6, 20))])
    assert result["warning"] is True
    assert result["acquisitions"][0]["same_wallet"] is True
    assert [w["wallet"] for w in result["wallets"]] == ["Individual"]


def test_a_replacement_inside_an_ira_is_flagged_unrecoverable():
    # The disallowed loss has no replacement basis to move into — it is gone.
    result = _assess([_buy(date(2026, 6, 20)), _elsewhere(date(2026, 6, 21))])
    assert [a["unrecoverable"] for a in result["acquisitions"]] == [False, True]


def test_the_warning_names_every_other_wallet_holding_the_instrument():
    result = _assess(
        [_buy(date(2026, 6, 20))],
        holdings=[
            {"wallet": "ROTH IRA", "wallet_id": "w-roth", "tax_treatment": "roth"},
            {"wallet": "Individual - TOD", "wallet_id": "w-tod", "tax_treatment": "taxable"},
        ],
    )
    assert [w["wallet"] for w in result["wallets"]] == [
        "Individual", "ROTH IRA", "Individual - TOD",
    ]
    assert [w["unrecoverable"] for w in result["wallets"]] == [False, True, False]


def test_two_wallets_sharing_a_name_stay_two_warnings():
    # Deduplication is on identity, not the label the user happened to type.
    result = _assess(
        [_buy(date(2026, 6, 20))],
        holdings=[
            {"wallet": "Brokerage", "wallet_id": "w-1", "tax_treatment": "roth"},
            {"wallet": "Brokerage", "wallet_id": "w-2", "tax_treatment": "hsa"},
        ],
    )
    assert [w["wallet_id"] for w in result["wallets"]] == [SELLING_WALLET, "w-1", "w-2"]


def test_a_wallet_holding_it_without_a_recent_buy_is_named_but_does_not_warn():
    result = _assess(
        [_elsewhere(date(2026, 1, 1))],
        holdings=[{"wallet": "ROTH IRA", "wallet_id": "w-roth", "tax_treatment": "roth"}],
    )
    assert result["warning"] is False
    assert [w["wallet"] for w in result["wallets"]] == ["ROTH IRA"]


# ---------------------------------------------------------------------------
# Service: the cross-wallet lookup
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
    session,
    test_user,
    test_workspace,
    group_id,
    *,
    ticker: str,
    buys: list[tuple[str, date]],
    asset_type: str = "stock",
    last_price: str = "80",
    average_price: str = "100",
) -> Asset:
    asset = Asset(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name=ticker, type=asset_type, currency="USD", valuation_method="market_price",
        ticker=ticker, group_id=group_id, position=0,
        units=sum((Decimal(qty) for qty, _ in buys), Decimal("0")),
        last_price=Decimal(last_price), average_price=Decimal(average_price),
    )
    session.add(asset)
    for qty, when in buys:
        session.add(
            AssetTransaction(
                id=uuid.uuid4(), asset_id=asset.id, workspace_id=test_workspace.id,
                kind="buy", quantity=Decimal(qty), price=Decimal(average_price),
                fee=Decimal("0"), date=when, source="manual",
            )
        )
    await session.commit()
    return asset


@pytest.mark.asyncio
async def test_a_buy_in_the_ira_warns_against_selling_the_taxable_holding(
    session: AsyncSession, test_workspace, test_user: User
):
    taxable = await _wallet(session, test_user, test_workspace, "Individual", "taxable")
    ira = await _wallet(session, test_user, test_workspace, "ROTH IRA", "roth")
    selling = await _holding(
        session, test_user, test_workspace, taxable.id, ticker="VOO",
        buys=[("10", date(2025, 1, 5))],
    )
    await _holding(
        session, test_user, test_workspace, ira.id, ticker="VOO",
        buys=[("3", date(2026, 6, 20))],
    )

    result = await wash_sale_exposure(session, selling.id, test_workspace.id, sell_date=SELL)

    assert result is not None
    assert result["warning"] is True
    assert result["at_loss"] is True
    assert result["window_start"] == "2026-05-16" and result["window_end"] == "2026-07-15"
    assert [(a["wallet"], a["date"], a["unrecoverable"]) for a in result["acquisitions"]] == [
        ("ROTH IRA", "2026-06-20", True)
    ]
    assert [w["wallet"] for w in result["wallets"]] == ["ROTH IRA"]


@pytest.mark.asyncio
async def test_the_same_buy_outside_the_window_does_not_warn(
    session: AsyncSession, test_workspace, test_user: User
):
    taxable = await _wallet(session, test_user, test_workspace, "Individual", "taxable")
    ira = await _wallet(session, test_user, test_workspace, "ROTH IRA", "roth")
    selling = await _holding(
        session, test_user, test_workspace, taxable.id, ticker="VOO",
        buys=[("10", date(2025, 1, 5))],
    )
    await _holding(
        session, test_user, test_workspace, ira.id, ticker="VOO",
        buys=[("3", date(2026, 1, 20))],
    )

    result = await wash_sale_exposure(session, selling.id, test_workspace.id, sell_date=SELL)

    assert result is not None
    assert result["warning"] is False
    assert result["acquisitions"] == []
    # Still named: it is where a replacement would come from.
    assert [w["wallet"] for w in result["wallets"]] == ["ROTH IRA"]


@pytest.mark.asyncio
async def test_a_repurchase_in_the_selling_wallet_warns_too(
    session: AsyncSession, test_workspace, test_user: User
):
    taxable = await _wallet(session, test_user, test_workspace, "Individual", "taxable")
    selling = await _holding(
        session, test_user, test_workspace, taxable.id, ticker="VTI",
        buys=[("10", date(2025, 1, 5)), ("2", date(2026, 6, 1))],
    )

    result = await wash_sale_exposure(session, selling.id, test_workspace.id, sell_date=SELL)

    assert result is not None
    assert result["warning"] is True
    assert [(a["wallet"], a["same_wallet"]) for a in result["acquisitions"]] == [
        ("Individual", True)
    ]
    # Named even though it is the selling wallet — otherwise the warning would
    # fire with nothing to point at.
    assert [w["wallet"] for w in result["wallets"]] == ["Individual"]


@pytest.mark.asyncio
async def test_a_synced_holding_in_the_generic_bucket_is_covered(
    session: AsyncSession, test_workspace, test_user: User
):
    """Every provider-synced holding is created as `investment`, so this is the
    shape the warning has to reach in practice."""
    taxable = await _wallet(session, test_user, test_workspace, "Individual", "taxable")
    ira = await _wallet(session, test_user, test_workspace, "ROTH IRA", "roth")
    selling = await _holding(
        session, test_user, test_workspace, taxable.id, ticker="VOO",
        buys=[("10", date(2025, 1, 5))], asset_type="investment",
    )
    await _holding(
        session, test_user, test_workspace, ira.id, ticker="VOO",
        buys=[("3", date(2026, 6, 20))], asset_type="investment",
    )

    result = await wash_sale_exposure(session, selling.id, test_workspace.id, sell_date=SELL)

    assert result is not None
    assert result["covered"] is True
    assert result["warning"] is True


@pytest.mark.asyncio
async def test_crypto_held_in_two_wallets_does_not_warn(
    session: AsyncSession, test_workspace, test_user: User
):
    taxable = await _wallet(session, test_user, test_workspace, "Individual", "taxable")
    exchange = await _wallet(session, test_user, test_workspace, "Coinbase", "taxable")
    selling = await _holding(
        session, test_user, test_workspace, taxable.id, ticker="BTC-USD",
        buys=[("1", date(2025, 1, 5))], asset_type="crypto",
    )
    await _holding(
        session, test_user, test_workspace, exchange.id, ticker="BTC-USD",
        buys=[("1", date(2026, 6, 20))], asset_type="crypto",
    )

    result = await wash_sale_exposure(session, selling.id, test_workspace.id, sell_date=SELL)

    assert result is not None
    assert result["covered"] is False
    assert result["warning"] is False
    assert result["acquisitions"] == []


@pytest.mark.asyncio
async def test_a_candidate_price_above_average_is_not_a_loss(
    session: AsyncSession, test_workspace, test_user: User
):
    taxable = await _wallet(session, test_user, test_workspace, "Individual", "taxable")
    selling = await _holding(
        session, test_user, test_workspace, taxable.id, ticker="VOO",
        buys=[("10", date(2025, 1, 5)), ("2", date(2026, 6, 1))],
    )

    result = await wash_sale_exposure(
        session, selling.id, test_workspace.id, sell_date=SELL, price=Decimal("140")
    )

    assert result is not None
    assert result["at_loss"] is False
    assert result["warning"] is False


@pytest.mark.asyncio
async def test_no_basis_is_adjusted_by_the_check(
    session: AsyncSession, test_workspace, test_user: User
):
    """Exposure is reported, never accounted for (#47, Out of Scope)."""
    taxable = await _wallet(session, test_user, test_workspace, "Individual", "taxable")
    ira = await _wallet(session, test_user, test_workspace, "ROTH IRA", "roth")
    selling = await _holding(
        session, test_user, test_workspace, taxable.id, ticker="VOO",
        buys=[("10", date(2025, 1, 5))],
    )
    replacement = await _holding(
        session, test_user, test_workspace, ira.id, ticker="VOO",
        buys=[("3", date(2026, 6, 20))],
    )

    result = await wash_sale_exposure(session, selling.id, test_workspace.id, sell_date=SELL)

    assert result is not None
    assert result["warning"] is True
    await session.refresh(selling)
    await session.refresh(replacement)
    assert selling.average_price == Decimal("100") and replacement.average_price == Decimal("100")
    assert selling.purchase_price is None and replacement.purchase_price is None
    assert not any(key.startswith("adjusted") for key in result)


@pytest.mark.asyncio
async def test_another_workspaces_holding_of_the_same_ticker_is_invisible(
    session: AsyncSession, test_workspace, test_user: User
):
    other = Workspace(
        id=uuid.uuid4(), name="Other", kind="personal", created_by_user_id=test_user.id
    )
    session.add(other)
    await session.commit()
    taxable = await _wallet(session, test_user, test_workspace, "Individual", "taxable")
    selling = await _holding(
        session, test_user, test_workspace, taxable.id, ticker="VOO",
        buys=[("10", date(2025, 1, 5))],
    )
    await _holding(
        session, test_user, other, None, ticker="VOO", buys=[("3", date(2026, 6, 20))],
    )

    result = await wash_sale_exposure(session, selling.id, test_workspace.id, sell_date=SELL)

    assert result is not None
    assert result["warning"] is False
    assert result["wallets"] == []


@pytest.mark.asyncio
async def test_missing_asset_is_not_found(session: AsyncSession, test_workspace):
    assert await wash_sale_exposure(session, uuid.uuid4(), test_workspace.id) is None
