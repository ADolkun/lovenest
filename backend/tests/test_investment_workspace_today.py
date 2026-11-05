"""The investment endpoints take "today" from the workspace calendar.

The clock is frozen at an instant where the workspace's day (Kiritimati,
already 1 Jan 2025) differs from UTC's and Los Angeles's (still 31 Dec 2024),
and from the unfrozen host date, so any other source of "today" shows.
"""

import uuid
from datetime import date, datetime, timezone, tzinfo
from decimal import Decimal
from typing import Self
from unittest.mock import patch

import pytest
import pytest_asyncio

from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.providers.base import INCOME_AT_RECEIPT_NOTE
from tests.clock_helpers import instant_in

WORKSPACE_ZONE = "Pacific/Kiritimati"
INSTANT = datetime(2024, 12, 31, 10, 30, tzinfo=timezone.utc)
WORKSPACE_TODAY = date(2025, 1, 1)


class _FrozenClock(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        return instant_in(cls, INSTANT, tz)


@pytest_asyncio.fixture
async def headers(session, test_workspace, auth_headers):
    test_workspace.timezone = WORKSPACE_ZONE
    await session.commit()
    with patch("app.core.app_clock.datetime", _FrozenClock):
        yield {**auth_headers, "X-Workspace-Id": str(test_workspace.id)}


async def _wallet(session, test_user, test_workspace, treatment: str = "taxable") -> AssetGroup:
    wallet = AssetGroup(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name="Individual", icon="wallet", color="#0EA5E9", position=0, source="manual",
        tax_treatment=treatment,
    )
    session.add(wallet)
    await session.commit()
    return wallet


async def _holding(session, test_user, test_workspace, wallet, *, bought: date) -> Asset:
    asset = Asset(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name="VOO", type="stock", currency="USD", valuation_method="market_price",
        ticker="VOO", group_id=wallet.id, position=0, units=Decimal("10"),
        last_price=Decimal("80"), average_price=Decimal("100"),
    )
    session.add(asset)
    session.add(
        AssetTransaction(
            id=uuid.uuid4(), asset_id=asset.id, workspace_id=test_workspace.id,
            kind="buy", quantity=Decimal("10"), price=Decimal("100"), fee=Decimal("0"),
            date=bought, source="manual",
        )
    )
    await session.commit()
    return asset


@pytest.mark.asyncio
async def test_the_projection_feed_reports_the_workspace_day_and_tax_year(client, headers):
    feed = (await client.get("/api/assets/projection-feed", headers=headers)).json()

    assert feed["as_of"] == WORKSPACE_TODAY.isoformat()
    assert feed["tax_year"] == 2025


@pytest.mark.asyncio
async def test_contributions_judge_vesting_on_the_workspace_day(client, headers):
    wallet = await client.post(
        "/api/asset-groups",
        json={"name": "401(k)", "tax_treatment": "traditional"},
        headers=headers,
    )
    assert wallet.status_code == 201, wallet.text
    group_id = wallet.json()["id"]

    def employer(vested_on: date) -> dict:
        return {
            "group_id": group_id, "kind": "contribution", "party": "employer",
            "amount": 1000, "date": "2024-12-15", "vested_on": vested_on.isoformat(),
        }

    vests_today = await client.post(
        "/api/contributions", json=employer(WORKSPACE_TODAY), headers=headers
    )
    assert vests_today.status_code == 201, vests_today.text
    assert vests_today.json()["is_vested"] is True

    vests_tomorrow = await client.post(
        "/api/contributions", json=employer(date(2025, 1, 2)), headers=headers
    )
    assert vests_tomorrow.status_code == 201, vests_tomorrow.text
    assert vests_tomorrow.json()["is_vested"] is False

    edited = await client.patch(
        f"/api/contributions/{vests_tomorrow.json()['id']}",
        json={"amount": 2000},
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["is_vested"] is False

    rows = (await client.get("/api/contributions", headers=headers)).json()
    assert sorted((row["vested_on"], row["is_vested"]) for row in rows) == [
        ("2025-01-01", True),
        ("2025-01-02", False),
    ]

    summary = (await client.get("/api/contributions/summary", headers=headers)).json()
    assert summary[0]["employer_unvested"] == 2000.0


@pytest.mark.asyncio
async def test_open_lots_are_aged_to_the_workspace_day(
    client, headers, session, test_user, test_workspace
):
    wallet = await _wallet(session, test_user, test_workspace)
    bought = date(2024, 12, 1)
    asset = await _holding(session, test_user, test_workspace, wallet, bought=bought)

    response = await client.get(f"/api/assets/{asset.id}/tax-lots", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["as_of"] == WORKSPACE_TODAY.isoformat()
    assert [lot["holding_days"] for lot in body["lots"]] == [(WORKSPACE_TODAY - bought).days]


@pytest.mark.asyncio
async def test_a_wash_sale_check_defaults_to_selling_on_the_workspace_day(
    client, headers, session, test_user, test_workspace
):
    wallet = await _wallet(session, test_user, test_workspace)
    asset = await _holding(session, test_user, test_workspace, wallet, bought=date(2024, 6, 1))

    response = await client.get(f"/api/assets/{asset.id}/wash-sale", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["sell_date"] == WORKSPACE_TODAY.isoformat()


@pytest.mark.asyncio
async def test_trailing_income_runs_through_the_workspace_day(
    client, headers, session, test_user, test_workspace
):
    wallet = await _wallet(session, test_user, test_workspace)
    asset = Asset(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name="USDC", type="cash_equivalent", currency="USD", valuation_method="manual",
        ticker="USDC", units=Decimal("1000"), position=0, group_id=wallet.id,
    )
    session.add(asset)
    session.add(
        AssetTransaction(
            id=uuid.uuid4(), asset_id=asset.id, workspace_id=test_workspace.id,
            kind="buy", quantity=Decimal("2"), price=Decimal("1"), fee=Decimal("0"),
            date=WORKSPACE_TODAY, source="coinbase",
            notes=f"Coinbase interest — {INCOME_AT_RECEIPT_NOTE}",
        )
    )
    await session.commit()

    response = await client.get("/api/assets/income", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["wallets"][str(wallet.id)]["total"] == pytest.approx(2.0)
