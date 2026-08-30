"""API-level tests for the investment transaction ledger (issue #235).

Exercises the HTTP surface: per-asset CRUD, the workspace-wide list + filters,
the find-or-create buy endpoint, validation, 404s and auth.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.asset_transaction import AssetTransaction
from app.models.user import User


@pytest_asyncio.fixture
async def market_asset_api(session: AsyncSession, test_user: User) -> Asset:
    """A market-priced holding with a cached last price (no provider needed)."""
    asset = Asset(
        id=uuid.uuid4(),
        user_id=test_user.id,
        name="Petrobras",
        type="stock",
        currency="BRL",
        valuation_method="market_price",
        ticker="PETR4.SA",
        last_price=Decimal("30.00"),
        position=0,
    )
    session.add(asset)
    await session.commit()
    await session.refresh(asset)
    return asset


@pytest.mark.asyncio
async def test_add_transaction_via_api(client: AsyncClient, auth_headers: dict, market_asset_api: Asset):
    resp = await client.post(
        f"/api/assets/{market_asset_api.id}/transactions",
        headers=auth_headers,
        json={"kind": "buy", "quantity": 10, "price": 20, "date": "2026-01-01"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["units"] == 10
    assert round(body["average_price"], 2) == 20.00
    assert body["transaction_count"] == 1


@pytest.mark.asyncio
async def test_transaction_list_and_filters(client: AsyncClient, auth_headers: dict, market_asset_api: Asset):
    for tx in [
        {"kind": "buy", "quantity": 10, "price": 20, "date": "2026-01-01"},
        {"kind": "sell", "quantity": 4, "price": 30, "date": "2026-03-01"},
    ]:
        r = await client.post(f"/api/assets/{market_asset_api.id}/transactions", headers=auth_headers, json=tx)
        assert r.status_code == 201

    # Per-asset list
    r = await client.get(f"/api/assets/{market_asset_api.id}/transactions", headers=auth_headers)
    assert r.status_code == 200
    assert len(r.json()) == 2

    # Workspace-wide list
    r = await client.get("/api/assets/transactions", headers=auth_headers)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert all(row["ticker"] == "PETR4.SA" for row in rows)

    # Filter by kind
    r = await client.get("/api/assets/transactions", headers=auth_headers, params={"kind": "sell"})
    assert [row["kind"] for row in r.json()] == ["sell"]

    # Filter by ticker
    r = await client.get("/api/assets/transactions", headers=auth_headers, params={"ticker": "PETR4.SA"})
    assert len(r.json()) == 2
    r = await client.get("/api/assets/transactions", headers=auth_headers, params={"ticker": "NOPE"})
    assert r.json() == []


@pytest.mark.asyncio
async def test_update_and_delete_transaction_via_api(
    client: AsyncClient, auth_headers: dict, market_asset_api: Asset
):
    r = await client.post(
        f"/api/assets/{market_asset_api.id}/transactions",
        headers=auth_headers,
        json={"kind": "buy", "quantity": 10, "price": 20, "date": "2026-01-01"},
    )
    assert r.status_code == 201
    tx_id = (await client.get(f"/api/assets/{market_asset_api.id}/transactions", headers=auth_headers)).json()[0]["id"]

    r = await client.patch(f"/api/assets/transactions/{tx_id}", headers=auth_headers, json={"quantity": 25})
    assert r.status_code == 200
    assert r.json()["units"] == 25

    r = await client.delete(f"/api/assets/transactions/{tx_id}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["units"] == 0
    assert r.json()["average_price"] is None


@pytest.mark.asyncio
async def test_update_transaction_all_fields_via_api(
    client: AsyncClient, auth_headers: dict, market_asset_api: Asset
):
    r = await client.post(
        f"/api/assets/{market_asset_api.id}/transactions",
        headers=auth_headers,
        json={"kind": "buy", "quantity": 10, "price": 20, "date": "2026-01-01"},
    )
    assert r.status_code == 201
    tx_id = (
        await client.get(f"/api/assets/{market_asset_api.id}/transactions", headers=auth_headers)
    ).json()[0]["id"]

    r = await client.patch(
        f"/api/assets/transactions/{tx_id}",
        headers=auth_headers,
        json={"kind": "buy", "quantity": 119, "price": 43.47, "fee": 0, "date": "2025-04-15"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["units"] == 119
    assert round(body["average_price"], 2) == 43.47

    r = await client.get(f"/api/assets/{market_asset_api.id}/transactions", headers=auth_headers)
    tx = r.json()[0]
    assert tx["kind"] == "buy"
    assert tx["quantity"] == 119
    assert round(tx["price"], 2) == 43.47
    assert tx["fee"] == 0
    assert tx["date"] == "2025-04-15"


@pytest.mark.asyncio
async def test_buy_consolidates_existing_ticker_via_api(
    client: AsyncClient, auth_headers: dict, session: AsyncSession, market_asset_api: Asset
):
    # First seed a buy so the holding has a position.
    await client.post(
        f"/api/assets/{market_asset_api.id}/transactions",
        headers=auth_headers,
        json={"kind": "buy", "quantity": 10, "price": 20, "date": "2026-01-01"},
    )
    # /assets/buy on the SAME ticker hits the find path (no provider call needed).
    r = await client.post(
        "/api/assets/buy",
        headers=auth_headers,
        json={"ticker": "PETR4.SA", "quantity": 10, "price": 30, "date": "2026-02-01"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == str(market_asset_api.id)  # consolidated, not a new asset
    assert body["units"] == 20
    assert round(body["average_price"], 2) == 25.00


@pytest.mark.asyncio
async def test_add_transaction_unknown_asset_404(client: AsyncClient, auth_headers: dict):
    r = await client.post(
        f"/api/assets/{uuid.uuid4()}/transactions",
        headers=auth_headers,
        json={"kind": "buy", "quantity": 1, "price": 1, "date": "2026-01-01"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_add_transaction_invalid_kind_422(client: AsyncClient, auth_headers: dict, market_asset_api: Asset):
    r = await client.post(
        f"/api/assets/{market_asset_api.id}/transactions",
        headers=auth_headers,
        json={"kind": "gift", "quantity": 1, "price": 1, "date": "2026-01-01"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_add_transaction_zero_quantity_422(client: AsyncClient, auth_headers: dict, market_asset_api: Asset):
    r = await client.post(
        f"/api/assets/{market_asset_api.id}/transactions",
        headers=auth_headers,
        json={"kind": "buy", "quantity": 0, "price": 10, "date": "2026-01-01"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_oversell_rejected_via_api(client: AsyncClient, auth_headers: dict, market_asset_api: Asset):
    await client.post(
        f"/api/assets/{market_asset_api.id}/transactions",
        headers=auth_headers,
        json={"kind": "buy", "quantity": 10, "price": 20, "date": "2026-01-01"},
    )
    r = await client.post(
        f"/api/assets/{market_asset_api.id}/transactions",
        headers=auth_headers,
        json={"kind": "sell", "quantity": 11, "price": 30, "date": "2026-02-01"},
    )
    assert r.status_code == 422
    assert "only" in r.json()["detail"].lower()
    # Rejected sell left no trace — still just the one buy.
    r2 = await client.get(f"/api/assets/{market_asset_api.id}/transactions", headers=auth_headers)
    assert len(r2.json()) == 1


@pytest.mark.asyncio
async def test_transactions_require_auth(client: AsyncClient):
    assert (await client.get("/api/assets/transactions")).status_code == 401


@pytest.mark.asyncio
async def test_reportable_gain_endpoint_excludes_tax_advantaged(
    client: AsyncClient, auth_headers: dict, market_asset_api: Asset
):
    """A profitable Roth sale is Realised Gain but never Reportable (issue #78)."""
    wallet = await client.post(
        "/api/asset-groups", headers=auth_headers, json={"name": "Roth IRA", "tax_treatment": "roth"}
    )
    assert wallet.status_code in (200, 201)
    r = await client.patch(
        f"/api/assets/{market_asset_api.id}", headers=auth_headers, json={"group_id": wallet.json()["id"]}
    )
    assert r.status_code == 200

    for tx in [
        {"kind": "buy", "quantity": 10, "price": 20, "date": "2026-01-01"},
        {"kind": "sell", "quantity": 4, "price": 30, "date": "2026-03-01"},
    ]:
        assert (await client.post(
            f"/api/assets/{market_asset_api.id}/transactions", headers=auth_headers, json=tx
        )).status_code == 201

    r = await client.get("/api/assets/reportable-gain", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {
        "reportable_gain": 0.0,
        "non_reportable_gain": 40.0,
        "start": None,
        "end": None,
    }

    r = await client.get(
        "/api/assets/reportable-gain",
        headers=auth_headers,
        params={"start": "2027-01-01", "end": "2028-01-01"},
    )
    # The 2026 sell is outside the window, so the year bounds really bind.
    assert r.json()["non_reportable_gain"] == 0.0


@pytest.mark.asyncio
async def test_tax_lots_endpoint_lists_lots_for_a_taxable_wallet(
    client: AsyncClient, auth_headers: dict, market_asset_api: Asset
):
    """Lots, their holding period, and the realised split (issue #65)."""
    wallet = await client.post(
        "/api/asset-groups",
        headers=auth_headers,
        json={"name": "Brokerage", "tax_treatment": "taxable"},
    )
    assert wallet.status_code in (200, 201)
    assert (await client.patch(
        f"/api/assets/{market_asset_api.id}",
        headers=auth_headers,
        json={"group_id": wallet.json()["id"]},
    )).status_code == 200

    for tx in [
        {"kind": "buy", "quantity": 10, "price": 20, "date": "2025-01-01"},
        {"kind": "buy", "quantity": 10, "price": 40, "date": "2026-03-01"},
        {"kind": "sell", "quantity": 12, "price": 50, "date": "2026-06-01"},
    ]:
        assert (await client.post(
            f"/api/assets/{market_asset_api.id}/transactions", headers=auth_headers, json=tx
        )).status_code == 201

    r = await client.get(
        f"/api/assets/{market_asset_api.id}/tax-lots",
        headers=auth_headers,
        params={"as_of": "2026-06-01"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tax_character"] is True
    # FIFO took the whole 2025 lot plus 2 of the 2026 one; 8 short units remain.
    assert body["lots"] == [
        {
            "acquired": "2026-03-01",
            "quantity": 8.0,
            "unit_price": 40.0,
            "cost": 320.0,
            "holding_days": 92,
            "written": False,
            "long_term": False,
            "days_until_long_term": 273,
        }
    ]
    assert body["short_quantity"] == 8.0 and body["long_quantity"] == 0.0
    # Sold 12 @ 50 against an average of 30 → 240, of which 10/12 is long.
    assert body["realised_long"] == 200.0
    assert body["realised_short"] == 40.0


@pytest.mark.asyncio
async def test_tax_lots_endpoint_404s_for_an_unknown_asset(client: AsyncClient, auth_headers: dict):
    r = await client.get(f"/api/assets/{uuid.uuid4()}/tax-lots", headers=auth_headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_tax_lots_require_auth(client: AsyncClient, market_asset_api: Asset):
    assert (await client.get(f"/api/assets/{market_asset_api.id}/tax-lots")).status_code == 401


@pytest.mark.asyncio
async def test_wash_sale_endpoint_warns_across_wallets(
    client: AsyncClient, auth_headers: dict, session: AsyncSession,
    test_user: User, market_asset_api: Asset,
):
    """Selling the taxable holding at a loss while the IRA bought the same
    ticker inside the window (issue #66)."""
    taxable = await client.post(
        "/api/asset-groups",
        headers=auth_headers,
        json={"name": "Individual", "tax_treatment": "taxable"},
    )
    ira = await client.post(
        "/api/asset-groups", headers=auth_headers, json={"name": "ROTH IRA", "tax_treatment": "roth"}
    )
    assert (await client.patch(
        f"/api/assets/{market_asset_api.id}",
        headers=auth_headers,
        json={"group_id": taxable.json()["id"]},
    )).status_code == 200
    # Bought at 40, last quoted at 30 — a candidate sale is at a loss.
    assert (await client.post(
        f"/api/assets/{market_asset_api.id}/transactions",
        headers=auth_headers,
        json={"kind": "buy", "quantity": 10, "price": 40, "date": "2025-01-01"},
    )).status_code == 201

    replacement = Asset(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=market_asset_api.workspace_id,
        name="Petrobras", type="stock", currency="BRL", valuation_method="market_price",
        ticker="PETR4.SA", group_id=uuid.UUID(ira.json()["id"]), units=Decimal("3"), position=0,
    )
    session.add(replacement)
    session.add(
        AssetTransaction(
            id=uuid.uuid4(), asset_id=replacement.id, workspace_id=market_asset_api.workspace_id,
            kind="buy", quantity=Decimal("3"), price=Decimal("31"), fee=Decimal("0"),
            date=date(2026, 6, 20), source="manual",
        )
    )
    await session.commit()

    r = await client.get(
        f"/api/assets/{market_asset_api.id}/wash-sale",
        headers=auth_headers,
        params={"sell_date": "2026-06-15"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["warning"] is True
    assert body["at_loss"] is True
    assert body["window_start"] == "2026-05-16" and body["window_end"] == "2026-07-15"
    assert [(a["wallet"], a["date"]) for a in body["acquisitions"]] == [("ROTH IRA", "2026-06-20")]
    assert [(w["wallet"], w["unrecoverable"]) for w in body["wallets"]] == [("ROTH IRA", True)]


@pytest.mark.asyncio
async def test_wash_sale_endpoint_404s_for_an_unknown_asset(client: AsyncClient, auth_headers: dict):
    r = await client.get(f"/api/assets/{uuid.uuid4()}/wash-sale", headers=auth_headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_wash_sale_requires_auth(client: AsyncClient, market_asset_api: Asset):
    assert (await client.get(f"/api/assets/{market_asset_api.id}/wash-sale")).status_code == 401
