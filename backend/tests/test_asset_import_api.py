"""HTTP surface of the asset-order importer: template, preview, commit, auth."""
import json
from decimal import Decimal
from typing import Optional

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.models.asset import Asset
from app.models.asset_transaction import AssetTransaction
from app.models.asset_value import AssetValue
from app.models.import_log import ImportLog
from app.providers.market_price import (
    MarketPriceProvider,
    MarketSymbolQuote,
    set_market_price_provider,
)


class StubProvider(MarketPriceProvider):
    name = "stub"

    async def search(self, query: str, limit: int = 20):
        return []

    async def get_quote(self, symbol: str) -> Optional[MarketSymbolQuote]:
        if symbol.upper() != "AAPL":
            return None
        return MarketSymbolQuote(
            symbol="AAPL", name="Apple Inc", exchange="NASDAQ",
            currency="USD", price=180.0, quote_type="EQUITY",
        )

    async def get_latest_prices(self, symbols: list[str]) -> dict[str, Optional[Decimal]]:
        return {s.upper(): (Decimal("180") if s.upper() == "AAPL" else None) for s in symbols}


@pytest_asyncio.fixture(autouse=True)
async def stub_provider():
    set_market_price_provider(StubProvider())
    yield
    set_market_price_provider(None)


@pytest_asyncio.fixture
async def wallet_id(client: AsyncClient, auth_headers) -> str:
    """A Taxable wallet — the commit path requires one (#94)."""
    resp = await client.post(
        "/api/asset-groups",
        json={"name": "Corretora A", "tax_treatment": "taxable"},
        headers=auth_headers,
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


CSV = b"ticker,date,quantity,price,fee\nAAPL,2026-01-15,10,150.00,1.20\nAAPL,2026-02-15,-4,180.00,1.20\n"


@pytest.mark.asyncio
async def test_template_is_downloadable(client: AsyncClient, auth_headers):
    resp = await client.get("/api/assets/import/template", headers=auth_headers)
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.text.splitlines()[0].startswith("ticker*,date*,quantity*,price*")


@pytest.mark.asyncio
async def test_preview_reports_what_the_import_would_do(client: AsyncClient, auth_headers):
    resp = await client.post(
        "/api/assets/import/preview",
        files={"file": ("orders.csv", CSV, "text/csv")},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["orders"]) == 2
    assert body["holdings_created"] == 1
    assert body["errors"] == []
    assert body["csv_columns"] == ["ticker", "date", "quantity", "price", "fee"]


@pytest.mark.asyncio
async def test_preview_writes_nothing(client: AsyncClient, auth_headers):
    await client.post(
        "/api/assets/import/preview",
        files={"file": ("orders.csv", CSV, "text/csv")},
        headers=auth_headers,
    )
    assets = await client.get("/api/assets", headers=auth_headers)
    assert assets.json() == []


@pytest.mark.asyncio
async def test_unmappable_file_returns_the_headers_for_the_mapping_step(
    client: AsyncClient, auth_headers
):
    """A soft failure, like the transaction preview: the UI needs the columns."""
    resp = await client.post(
        "/api/assets/import/preview",
        files={"file": ("orders.csv", b"col_a,col_b\nAAPL,10\n", "text/csv")},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["parse_error"]
    assert body["csv_columns"] == ["col_a", "col_b"]
    assert body["orders"] == []


@pytest.mark.asyncio
async def test_preview_honours_an_explicit_mapping(client: AsyncClient, auth_headers):
    resp = await client.post(
        "/api/assets/import/preview",
        files={"file": ("orders.csv", b"a,b,c,d\nAAPL,2026-01-15,10,150.00\n", "text/csv")},
        data={"column_mapping": json.dumps({"ticker": "a", "date": "b", "quantity": "c", "price": "d"})},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert len(resp.json()["orders"]) == 1


@pytest.mark.asyncio
async def test_import_creates_the_holding(client: AsyncClient, auth_headers, wallet_id):
    preview = await client.post(
        "/api/assets/import/preview",
        files={"file": ("orders.csv", CSV, "text/csv")},
        headers=auth_headers,
    )
    resp = await client.post(
        "/api/assets/import",
        json={"orders": preview.json()["orders"], "group_id": wallet_id},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["imported"] == 2
    assert resp.json()["holdings_created"] == 1

    assets = (await client.get("/api/assets", headers=auth_headers)).json()
    assert [a["ticker"] for a in assets] == ["AAPL"]
    assert Decimal(str(assets[0]["units"])) == Decimal("6")  # 10 bought, 4 sold


@pytest.mark.asyncio
async def test_import_requires_authentication(client: AsyncClient):
    resp = await client.post("/api/assets/import", json={"orders": []})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_preview_requires_authentication(client: AsyncClient):
    resp = await client.post(
        "/api/assets/import/preview",
        files={"file": ("orders.csv", CSV, "text/csv")},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_preview_lists_only_the_rows_it_will_import(client: AsyncClient, auth_headers):
    """A row the dry run rejects must not sit in the table above a button that
    promises to import it."""
    csv = b"ticker,date,quantity,price\nAAPL,2026-01-15,10,150.00\nNOSUCH,2026-01-16,5,10.00\n"
    resp = await client.post(
        "/api/assets/import/preview",
        files={"file": ("orders.csv", csv, "text/csv")},
        headers=auth_headers,
    )
    body = resp.json()
    assert [o["ticker"] for o in body["orders"]] == ["AAPL"]
    assert [(e["row"], e["reason"]) for e in body["errors"]] == [(3, "unknown_ticker")]


LOT_CSV = (
    b"Amount,Asset,Date Acquired (America/Los_Angeles),Date Sold (America/Los_Angeles),"
    b"Cost Basis,Proceeds,Gain,Term\n"
    b"0.5,AAPL,2024-01-02 10:00:00,2024-06-01 12:00:00,50,80,30,Short\n"
)


@pytest.mark.asyncio
async def test_preview_reads_a_lot_report_without_a_mapping_step(
    client: AsyncClient, auth_headers
):
    resp = await client.post(
        "/api/assets/import/preview",
        files={"file": ("gains.csv", LOT_CSV, "text/csv")},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["parse_error"] is None
    assert [(o["kind"], o["date"]) for o in body["orders"]] == [
        ("buy", "2024-01-02"), ("sell", "2024-06-01"),
    ]


@pytest.mark.asyncio
async def test_preview_reports_skipped_rows_with_their_reason(
    client: AsyncClient, auth_headers
):
    content = b"ticker,date,quantity,price,type\nAAPL,2026-01-15,1,150.00,transfer in\n"
    resp = await client.post(
        "/api/assets/import/preview",
        files={"file": ("history.csv", content, "text/csv")},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [(s["row"], s["reason"]) for s in body["skips"]] == [(2, "transfer")]
    assert body["skipped"] == 1


@pytest.mark.asyncio
async def test_an_unpriced_ticker_is_refused_unless_the_caller_asks_for_it(
    client: AsyncClient, auth_headers
):
    content = b"ticker,date,quantity,price\nIONIC,2024-01-31,163,10.00\n"
    files = {"file": ("estate.csv", content, "text/csv")}

    refused = await client.post(
        "/api/assets/import/preview", files=files, headers=auth_headers
    )
    assert [e["reason"] for e in refused.json()["errors"]] == ["unknown_ticker"]

    allowed = await client.post(
        "/api/assets/import/preview",
        files={"file": ("estate.csv", content, "text/csv")},
        data={"allow_unpriced": "true"},
        headers=auth_headers,
    )
    body = allowed.json()
    assert body["errors"] == []
    assert [w["reason"] for w in body["warnings"]] == ["unpriced_holding"]
    assert len(body["orders"]) == 1


@pytest.mark.asyncio
async def test_committing_an_unpriced_holding_creates_it(
    client: AsyncClient, auth_headers, wallet_id
):
    resp = await client.post(
        "/api/assets/import",
        json={
            "orders": [{
                "row": 2, "ticker": "IONIC", "date": "2024-01-31",
                "kind": "buy", "quantity": "163", "price": "10.00",
            }],
            "allow_unpriced": True,
            "filename": "estate.csv",
            "group_id": wallet_id,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported"] == 1
    assert resp.json()["holdings_created"] == 1

    listed = await client.get("/api/assets", headers=auth_headers)
    ionic = next(a for a in listed.json() if a["ticker"] == "IONIC")
    assert ionic["valuation_method"] == "manual"


@pytest.mark.asyncio
async def test_an_unpriced_holding_is_worth_its_basis_not_zero(
    client: AsyncClient, auth_headers, wallet_id
):
    """No quote will ever arrive, so the basis is the only honest figure —
    and it must not read as a total loss of everything imported."""
    await client.post(
        "/api/assets/import",
        json={
            "orders": [{
                "row": 2, "ticker": "IONIC", "date": "2024-01-31",
                "kind": "buy", "quantity": "163", "price": "10.00",
            }],
            "allow_unpriced": True,
            "group_id": wallet_id,
        },
        headers=auth_headers,
    )
    listed = await client.get("/api/assets", headers=auth_headers)
    ionic = next(a for a in listed.json() if a["ticker"] == "IONIC")
    assert ionic["current_value"] == 1630.0
    assert ionic["gain_loss"] == 0


# ---------------------------------------------------------------------------
# The wallet the lots hang off (#94)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_commit_without_a_wallet_is_refused(client: AsyncClient, auth_headers):
    """Silently the worst outcome: the ledger lands, and #65's Lots view is
    blank for it with nothing to explain why."""
    preview = await client.post(
        "/api/assets/import/preview",
        files={"file": ("orders.csv", CSV, "text/csv")},
        headers=auth_headers,
    )
    resp = await client.post(
        "/api/assets/import",
        json={"orders": preview.json()["orders"]},
        headers=auth_headers,
    )

    assert resp.status_code == 422, resp.text
    assert "wallet" in resp.json()["detail"].lower()
    assert (await client.get("/api/assets", headers=auth_headers)).json() == []


@pytest.mark.asyncio
async def test_importing_into_a_taxable_wallet_yields_lots(
    client: AsyncClient, auth_headers, wallet_id
):
    """The seam the whole epic rests on: orders in, tax lots out."""
    preview = await client.post(
        "/api/assets/import/preview",
        files={"file": ("orders.csv", CSV, "text/csv")},
        data={"group_id": wallet_id},
        headers=auth_headers,
    )
    commit = await client.post(
        "/api/assets/import",
        json={"orders": preview.json()["orders"], "group_id": wallet_id},
        headers=auth_headers,
    )
    assert commit.status_code == 200, commit.text

    assets = (await client.get("/api/assets", headers=auth_headers)).json()
    asset_id = next(a["id"] for a in assets if a["ticker"] == "AAPL")
    lots = (await client.get(f"/api/assets/{asset_id}/tax-lots", headers=auth_headers)).json()

    assert lots["no_wallet"] is False
    assert lots["tax_character"] is True
    assert [lot["quantity"] for lot in lots["lots"]] == [6.0]
    assert [sale["quantity"] for sale in lots["sales"]] == [4.0]


@pytest.mark.asyncio
async def test_invalid_acquisitions_preview_and_apply_write_nothing(
    client: AsyncClient, auth_headers, wallet_id, session
):
    content = (
        b"ticker,date,quantity,price,kind\n"
        b"AAPL,2026-01-01,2,,claim\n"
        b"AAPL,2026-01-02,3,invalid,reward\n"
    )
    preview = await client.post(
        "/api/assets/import/preview",
        files={"file": ("synthetic.csv", content, "text/csv")},
        data={"group_id": wallet_id},
        headers=auth_headers,
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["parse_error"] is None
    assert [(e["row"], e["reason"]) for e in body["errors"]] == [
        (2, "invalid_price"), (3, "invalid_price"),
    ]
    assert body["orders"] == []
    assert body["holdings_created"] == 0
    for model in (Asset, AssetTransaction, AssetValue, ImportLog):
        assert (await session.scalars(select(model))).all() == []

    applied = await client.post(
        "/api/assets/import",
        json={"orders": body["orders"], "group_id": wallet_id},
        headers=auth_headers,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["imported"] == 0
    assert applied.json()["holdings_created"] == 0
    assert applied.json()["import_log_id"] is None
    assert applied.json()["errors"] == []
    # Lots are derived from transactions; no ledger means no invented basis.
    for model in (Asset, AssetTransaction, AssetValue, ImportLog):
        assert (await session.scalars(select(model))).all() == []


@pytest.mark.asyncio
async def test_mixed_acquisitions_preserve_stated_values_and_reject_an_unsupported_sale(
    client: AsyncClient, auth_headers, wallet_id, session
):
    content = (
        b"ticker,date,quantity,price,kind\n"
        b"AAPL,2026-01-01,10,,claim\n"
        b"AAPL,2026-01-02,10,invalid,reward\n"
        b"AAPL,2026-01-03,2,0,claim\n"
        b"AAPL,2026-01-04,3,7,acquire\n"
        b"AAPL,2026-01-05,4,0,airdrop\n"
        b"AAPL,2026-01-06,5,7,staking reward\n"
        b"AAPL,2026-01-07,15,20,sell\n"
    )
    preview = await client.post(
        "/api/assets/import/preview",
        files={"file": ("synthetic.csv", content, "text/csv")},
        data={"group_id": wallet_id},
        headers=auth_headers,
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["parse_error"] is None
    assert [(e["row"], e["reason"]) for e in body["errors"]] == [
        (2, "invalid_price"), (3, "invalid_price"), (8, "oversell"),
    ]
    assert [(o["row"], o["kind"], Decimal(o["quantity"]), Decimal(o["price"]))
            for o in body["orders"]] == [
        (4, "buy", Decimal("2"), Decimal("0")),
        (5, "buy", Decimal("3"), Decimal("7")),
        (6, "buy", Decimal("4"), Decimal("0")),
        (7, "buy", Decimal("5"), Decimal("7")),
    ]
    assert body["holdings_created"] == 1
    for model in (Asset, AssetTransaction, AssetValue, ImportLog):
        assert (await session.scalars(select(model))).all() == []

    applied = await client.post(
        "/api/assets/import",
        json={"orders": body["orders"], "group_id": wallet_id},
        headers=auth_headers,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["imported"] == 4
    assert applied.json()["holdings_created"] == 1
    assert applied.json()["errors"] == []
    assets = (await client.get("/api/assets", headers=auth_headers)).json()
    assert len(assets) == 1
    assert assets[0]["units"] == 14
    assert assets[0]["purchase_price"] == 56
    assert assets[0]["average_price"] == 4
    transactions = (await session.scalars(
        select(AssetTransaction).order_by(AssetTransaction.date)
    )).all()
    assert [(t.kind, t.quantity, t.price) for t in transactions] == [
        (o["kind"], Decimal(o["quantity"]), Decimal(o["price"])) for o in body["orders"]
    ]
    lots = (await client.get(
        f"/api/assets/{assets[0]['id']}/tax-lots", headers=auth_headers
    )).json()
    assert [(lot["quantity"], lot["unit_price"], lot["cost"]) for lot in lots["lots"]] == [
        (2, 0, 0), (3, 7, 21), (4, 0, 0), (5, 7, 35),
    ]
    assert lots["sales"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("price_fields", [
    pytest.param({}, id="omitted"),
    pytest.param({"price": None}, id="null"),
    pytest.param({"price": ""}, id="blank"),
    pytest.param({"price": "invalid"}, id="nonnumeric"),
    pytest.param({"price": "NaN"}, id="nan"),
    pytest.param({"price": "Infinity"}, id="infinity"),
    pytest.param({"price": "-Infinity"}, id="negative-infinity"),
])
async def test_direct_import_rejects_an_unusable_price(
    client: AsyncClient, auth_headers, wallet_id, session, price_fields
):
    applied = await client.post(
        "/api/assets/import",
        json={
            "orders": [{
                "row": 2, "ticker": "AAPL", "date": "2026-01-01",
                "kind": "buy", "quantity": "2", **price_fields,
            }],
            "group_id": wallet_id,
        },
        headers=auth_headers,
    )
    assert applied.status_code == 422, applied.text
    assert [e["loc"] for e in applied.json()["detail"]] == [["body", "orders", 0, "price"]]
    for model in (Asset, AssetTransaction, AssetValue, ImportLog):
        assert (await session.scalars(select(model))).all() == []


@pytest.mark.asyncio
async def test_direct_import_preserves_explicit_zero_price(
    client: AsyncClient, auth_headers, wallet_id, session
):
    applied = await client.post(
        "/api/assets/import",
        json={
            "orders": [{
                "row": 2, "ticker": "AAPL", "date": "2026-01-01",
                "kind": "buy", "quantity": "2", "price": "0",
            }],
            "group_id": wallet_id,
        },
        headers=auth_headers,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["imported"] == 1
    assert applied.json()["errors"] == []
    transaction = (await session.scalars(select(AssetTransaction))).one()
    assert transaction.price == Decimal("0")
    assets = (await client.get("/api/assets", headers=auth_headers)).json()
    assert len(assets) == 1
    assert assets[0]["units"] == 2
    assert assets[0]["purchase_price"] == 0
