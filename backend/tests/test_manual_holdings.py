"""The hand-kept half of the portfolio (issue #68).

A Celsius claim, a BlockFi distribution, a stake in something unlisted: these
sit at institutions with no API, so their value is whatever the user last
typed. The contract that makes that trustworthy is narrow — nothing automatic
may revalue them, they carry no ticker for a refresh to resolve, and every
figure says when it was entered.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.asset_value import AssetValue
from app.models.user import User
from app.schemas.asset import AssetCreate, AssetUpdate, AssetValueCreate
from app.services import asset_service
from app.services.asset_service import refresh_all_market_prices

from tests.test_market_price_assets import FakeMarketProvider, _quote


# A provider that knows USA (the New York closed-end fund) and nothing else —
# so USA is a symbol the refresh would resolve, and ALEO is not.
def _knows_usa() -> FakeMarketProvider:
    return FakeMarketProvider({"USA": _quote("USA", 6.5)})


async def _claim(session: AsyncSession, workspace, user, **kwargs):
    """A holding with no market anyone quotes."""
    provider = kwargs.pop("provider", None) or FakeMarketProvider({})
    data = AssetCreate(
        name=kwargs.pop("name", "BlockFi bankruptcy claim"),
        type=kwargs.pop("type", "other"),
        valuation_method="manual",
        currency="USD",
        **kwargs,
    )
    return await asset_service.create_asset(
        session, workspace.id, user.id, data, market_provider=provider
    )


@pytest.mark.asyncio
async def test_hand_set_value_survives_a_market_refresh(
    session: AsyncSession, test_user: User, test_workspace
):
    """The scheduled refresh does not reach a holding the user values."""
    claim = await _claim(session, test_workspace, test_user, current_value=Decimal("4200"))

    result = await refresh_all_market_prices(
        session, market_provider=FakeMarketProvider({"AAPL": _quote("AAPL", 180.0)})
    )
    assert result == {"refreshed": 0, "skipped": 0, "rate_limited": 0}

    after = await asset_service.get_asset(session, claim.id, test_workspace.id)
    assert after is not None
    assert after.current_value == 4200.0
    assert after.last_price is None


@pytest.mark.asyncio
async def test_a_refresh_does_not_rewrite_a_hand_set_value_as_its_own(
    session: AsyncSession, test_user: User, test_workspace
):
    """Even where a quote does exist, today's hand-set row is not the row the
    refresh overwrites — it writes its own alongside and leaves the user's
    figure with its `manual` provenance intact."""
    provider = FakeMarketProvider({"AAPL": _quote("AAPL", 180.0)})
    created = await asset_service.create_asset(
        session,
        test_workspace.id,
        test_user.id,
        AssetCreate(
            name="Apple",
            type="investment",
            valuation_method="market_price",
            ticker="AAPL",
            units=Decimal("10"),
        ),
        market_provider=provider,
    )
    await asset_service.add_asset_value(
        session,
        created.id,
        test_workspace.id,
        AssetValueCreate(amount=Decimal("999"), date=date.today()),
    )

    await refresh_all_market_prices(
        session, market_provider=FakeMarketProvider({"AAPL": _quote("AAPL", 190.0)})
    )

    rows = (
        await session.execute(
            select(AssetValue).where(
                AssetValue.asset_id == created.id,
                AssetValue.date == date.today(),
                AssetValue.source == "manual",
            )
        )
    ).scalars().all()
    assert [float(r.amount) for r in rows] == [999.0]


@pytest.mark.asyncio
async def test_a_hand_valued_holding_cannot_be_created_with_a_ticker(
    session: AsyncSession, test_user: User, test_workspace
):
    """`USA` is a Solana memecoin and a New York closed-end equity fund. A
    ticker the refresh resolves invites it to quote the wrong one over the
    user's figure, so it is refused outright."""
    with pytest.raises(HTTPException) as exc:
        await _claim(
            session, test_workspace, test_user,
            name="USA (Solana)", ticker="USA", provider=_knows_usa(),
        )
    assert exc.value.status_code == 422
    assert "USA" in exc.value.detail


@pytest.mark.asyncio
async def test_a_symbol_nothing_quotes_stays_a_label(
    session: AsyncSession, test_user: User, test_workspace
):
    """The rule is about resolvability, not about tickers. An unlisted token
    keeps the symbol its exchange used — which is what
    `asset_import_service._new_holding` already does for an unpriced order,
    and the API must not disagree with the importer."""
    holding = await _claim(
        session, test_workspace, test_user,
        name="Aleo", ticker="ALEO", provider=_knows_usa(),
    )
    assert holding.ticker == "ALEO"
    assert holding.valuation_method == "manual"


@pytest.mark.asyncio
async def test_a_provider_outage_does_not_refuse_the_holding(
    session: AsyncSession, test_user: User, test_workspace
):
    """Failing to reach Yahoo is not evidence the symbol resolves."""

    class _Broken(FakeMarketProvider):
        async def get_quote(self, symbol):
            raise RuntimeError("yahoo is down")

    holding = await _claim(
        session, test_workspace, test_user, ticker="ALEO", provider=_Broken({}),
    )
    assert holding.ticker == "ALEO"


@pytest.mark.asyncio
async def test_a_hand_valued_holding_cannot_be_given_a_ticker_later(
    session: AsyncSession, test_user: User, test_workspace
):
    claim = await _claim(session, test_workspace, test_user)

    with pytest.raises(HTTPException) as exc:
        await asset_service.update_asset(
            session, claim.id, test_workspace.id, test_user.id,
            AssetUpdate(ticker="USA"), market_provider=_knows_usa(),
        )
    assert exc.value.status_code == 422

    # Clearing one is still allowed — that is how a mis-imported symbol goes away.
    cleared = await asset_service.update_asset(
        session, claim.id, test_workspace.id, test_user.id,
        AssetUpdate(ticker=None), market_provider=_knows_usa(),
    )
    assert cleared is not None
    assert cleared.ticker is None


@pytest.mark.asyncio
async def test_a_market_priced_holding_keeps_its_ticker_on_edit(
    session: AsyncSession, test_user: User, test_workspace
):
    """The guard is about the hand-valued method, not about tickers."""
    provider = FakeMarketProvider({"AAPL": _quote("AAPL", 180.0)})
    created = await asset_service.create_asset(
        session,
        test_workspace.id,
        test_user.id,
        AssetCreate(
            name="Apple",
            type="investment",
            valuation_method="market_price",
            ticker="AAPL",
            units=Decimal("10"),
        ),
        market_provider=provider,
    )
    updated = await asset_service.update_asset(
        session, created.id, test_workspace.id, test_user.id,
        AssetUpdate(ticker="AAPL"), market_provider=provider,
    )
    assert updated is not None
    assert updated.ticker == "AAPL"


@pytest.mark.asyncio
async def test_a_hand_set_value_reports_when_it_was_entered(
    session: AsyncSession, test_user: User, test_workspace
):
    """The day a figure is *about* is not the day it was typed: a balance
    entered today for last month must still read as entered today."""
    claim = await _claim(session, test_workspace, test_user)
    last_month = date.today() - timedelta(days=30)

    recorded = await asset_service.add_asset_value(
        session, claim.id, test_workspace.id, AssetValueCreate(amount=Decimal("4200"), date=last_month)
    )
    assert recorded is not None
    # Stamped when it was typed, not on the day it values.
    assert recorded.recorded_at.date() != last_month

    read = await asset_service.get_asset(session, claim.id, test_workspace.id)
    assert read is not None
    assert read.value_updated_at == recorded.recorded_at


@pytest.mark.asyncio
async def test_re_entering_a_day_corrects_it_rather_than_doubling_it(
    session: AsyncSession, test_user: User, test_workspace
):
    """One hand-set figure per day. A second entry is the user correcting
    themselves, and the freshness stamp moves with it."""
    claim = await _claim(session, test_workspace, test_user)
    today = date.today()

    first = await asset_service.add_asset_value(
        session, claim.id, test_workspace.id, AssetValueCreate(amount=Decimal("4200"), date=today)
    )
    second = await asset_service.add_asset_value(
        session, claim.id, test_workspace.id, AssetValueCreate(amount=Decimal("4500"), date=today)
    )
    assert first is not None and second is not None
    assert second.id == first.id
    assert second.recorded_at >= first.recorded_at

    values = await asset_service.get_asset_values(session, claim.id, test_workspace.id)
    assert values is not None
    assert [v.amount for v in values if v.date == today] == [4500.0]

    read = await asset_service.get_asset(session, claim.id, test_workspace.id)
    assert read is not None
    assert read.current_value == 4500.0


@pytest.mark.asyncio
async def test_the_newest_row_wins_when_two_share_a_date(
    session: AsyncSession, test_user: User, test_workspace
):
    """Regression: the tiebreak used to be `ORDER BY id DESC` on a random
    UUID4, so which of two same-day rows read as current was a coin flip that
    could land differently on the next request."""
    claim = await _claim(session, test_workspace, test_user)
    asset = await session.get(Asset, claim.id)
    assert asset is not None
    today = date.today()

    stale = AssetValue(
        asset_id=asset.id,
        amount=Decimal("100"),
        date=today,
        source="sync",
        recorded_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    fresh = AssetValue(
        asset_id=asset.id,
        amount=Decimal("4200"),
        date=today,
        source="sync",
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    session.add_all([stale, fresh])
    await session.commit()

    read = await asset_service.get_asset(session, claim.id, test_workspace.id)
    assert read is not None
    assert read.current_value == 4200.0
