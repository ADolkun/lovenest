"""Importing broker orders: reading the file, and applying it to holdings."""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.asset_transaction import AssetTransaction
from app.models.user import User
from app.models.workspace import Workspace
from app.providers.market_price import MarketSymbolQuote
from app.services import asset_import_service


class FakeProvider:
    """Knows two tickers, and counts calls so the batching can be asserted."""

    def __init__(self, known=("AAPL", "PETR4.SA")):
        self.known = {t.upper() for t in known}
        self.latest_price_calls = 0
        self.quote_calls = 0

    async def get_latest_prices(self, symbols):
        self.latest_price_calls += 1
        return {s.upper(): (Decimal("100") if s.upper() in self.known else None) for s in symbols}

    async def get_quote(self, symbol):
        self.quote_calls += 1
        if symbol.upper() not in self.known:
            return None
        return MarketSymbolQuote(
            symbol=symbol.upper(), name=f"{symbol.upper()} Inc", price=100.0,
            currency="USD", exchange="XNAS", quote_type="EQUITY", logo_url=None,
        )

    async def get_quotes(self, symbols):
        return {s.upper(): await self.get_quote(s) for s in symbols}


def _csv(*rows: str) -> bytes:
    return "\n".join(rows).encode("utf-8")


async def _only_asset(session: AsyncSession, workspace: Workspace) -> Asset:
    """The single holding the import created, asserted rather than assumed."""
    result = await session.execute(select(Asset).where(Asset.workspace_id == workspace.id))
    assets = result.scalars().all()
    assert len(assets) == 1, f"expected one holding, found {len(assets)}"
    return assets[0]


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------


def test_headers_are_recognised_without_a_mapping():
    orders, errors, _, columns = asset_import_service.parse_orders_csv(_csv(
        "ticker,date,quantity,price,fee",
        "AAPL,2026-01-15,10,150.00,1.20",
    ))
    assert errors == []
    assert columns == ["ticker", "date", "quantity", "price", "fee"]
    assert (orders[0].ticker, orders[0].kind, orders[0].quantity) == ("AAPL", "buy", Decimal("10"))
    assert orders[0].fee == Decimal("1.20")


def test_portuguese_broker_headers_are_recognised():
    """A Brazilian export names its columns in Portuguese and prices with commas."""
    orders, errors, _, _ = asset_import_service.parse_orders_csv(_csv(
        "Ativo;Data;Quantidade;Preço;Corretagem",
        "PETR4.SA;10/02/2026;100;38,50;2,90",
    ))
    assert errors == []
    assert orders[0].ticker == "PETR4.SA"
    assert orders[0].price == Decimal("38.50")
    assert orders[0].fee == Decimal("2.90")
    assert str(orders[0].date) == "2026-02-10"


def test_negative_quantity_reads_as_a_sale():
    """The convention most broker exports use for a sale."""
    orders, _, _, _ = asset_import_service.parse_orders_csv(_csv(
        "ticker,date,quantity,price",
        "AAPL,2026-03-02,-4,178.30",
    ))
    assert orders[0].kind == "sell"
    assert orders[0].quantity == Decimal("4")  # stored unsigned


def test_explicit_kind_column_wins_over_the_sign():
    orders, _, _, _ = asset_import_service.parse_orders_csv(_csv(
        "ticker,date,quantity,price,tipo",
        "AAPL,2026-03-02,4,178.30,venda",
    ))
    assert orders[0].kind == "sell"


def test_unreadable_rows_are_reported_not_silently_dropped():
    orders, errors, _, _ = asset_import_service.parse_orders_csv(_csv(
        "ticker,date,quantity,price",
        "AAPL,2026-01-15,10,150.00",
        ",2026-01-15,10,150.00",
        "MSFT,not-a-date,10,150.00",
        "MSFT,2026-01-15,zero,150.00",
    ))
    assert len(orders) == 1
    assert [(e.row, e.reason) for e in errors] == [
        (3, "missing_ticker"), (4, "invalid_date"), (5, "invalid_quantity"),
    ]


def test_latin1_file_does_not_blow_up():
    """Broker exports are not always UTF-8; the transaction importer raises here."""
    content = "ticker,date,quantity,price,name\nAAPL,2026-01-15,10,150.00,Ação\n".encode("latin-1")
    orders, errors, _, _ = asset_import_service.parse_orders_csv(content)
    assert errors == []
    assert orders[0].ticker == "AAPL"


def test_missing_required_column_is_a_parse_error():
    with pytest.raises(ValueError, match="quantity"):
        asset_import_service.parse_orders_csv(_csv("ticker,date,price", "AAPL,2026-01-15,150.00"))


def test_explicit_mapping_overrides_the_guess():
    orders, errors, _, _ = asset_import_service.parse_orders_csv(
        _csv("col_a,col_b,col_c,col_d", "AAPL,2026-01-15,10,150.00"),
        column_mapping={"ticker": "col_a", "date": "col_b", "quantity": "col_c", "price": "col_d"},
    )
    assert errors == []
    assert orders[0].ticker == "AAPL"


# ---------------------------------------------------------------------------
# Applying it
# ---------------------------------------------------------------------------


@pytest.fixture
def provider():
    return FakeProvider()


async def _import(session, workspace, user, csv_bytes, provider, **kwargs):
    orders, _, _, _ = asset_import_service.parse_orders_csv(csv_bytes)
    return await asset_import_service.import_orders(
        session, workspace.id, user.id, orders, market_provider=provider, **kwargs
    )


@pytest.mark.asyncio
async def test_import_creates_the_holding_and_the_position(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price,fee",
        "AAPL,2026-01-15,10,100.00,0",
        "AAPL,2026-02-15,10,200.00,0",
    ), provider)

    assert summary["imported"] == 2
    assert summary["holdings_created"] == 1

    stored = await _only_asset(session, test_workspace)
    assert stored.ticker == "AAPL"
    assert stored.units == Decimal("20")
    assert stored.average_price == Decimal("150")  # the math the ledger already had


@pytest.mark.asyncio
async def test_distinct_tickers_are_resolved_in_one_call(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    """200 rows over 2 tickers must not mean 200 provider calls."""
    rows = ["ticker,date,quantity,price"]
    for i in range(1, 51):
        rows.append(f"AAPL,2026-01-{i % 28 + 1:02d},1,100.00")
        rows.append(f"PETR4.SA,2026-01-{i % 28 + 1:02d},1,30.00")
    await _import(session, test_workspace, test_user, _csv(*rows), provider)
    assert provider.latest_price_calls == 1


@pytest.mark.asyncio
async def test_unknown_ticker_is_refused_with_its_row(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price",
        "AAPL,2026-01-15,10,100.00",
        "NOSUCH,2026-01-16,5,10.00",
    ), provider)

    assert summary["imported"] == 1
    assert [(e.row, e.reason, e.ticker) for e in summary["errors"]] == [(3, "unknown_ticker", "NOSUCH")]


@pytest.mark.asyncio
async def test_a_sell_beyond_the_position_is_caught_before_anything_is_written(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    """A file that starts mid-history would otherwise fail halfway through."""
    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price",
        "AAPL,2026-01-15,5,100.00",
        "AAPL,2026-02-15,-9,120.00",
    ), provider)

    assert summary["imported"] == 1
    assert [(e.row, e.reason) for e in summary["errors"]] == [(3, "oversell")]
    assert (await _only_asset(session, test_workspace)).units == Decimal("5")


@pytest.mark.asyncio
async def test_rows_are_replayed_in_date_order_not_file_order(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    """Brokers export newest-first; a sell listed above its buy is still valid."""
    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price",
        "AAPL,2026-02-15,-4,120.00",
        "AAPL,2026-01-15,10,100.00",
    ), provider)

    assert summary["imported"] == 2
    assert summary["errors"] == []


@pytest.mark.asyncio
async def test_reimporting_the_same_file_adds_nothing(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    """Fixing a mapping and re-uploading must not double the position."""
    content = _csv("ticker,date,quantity,price", "AAPL,2026-01-15,10,100.00")
    await _import(session, test_workspace, test_user, content, provider)
    summary = await _import(session, test_workspace, test_user, content, provider)

    assert summary["imported"] == 0
    assert summary["skipped"] == 1
    rows = (await session.execute(AssetTransaction.__table__.select())).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_orders_land_on_an_existing_holding(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    existing = Asset(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name="Apple", type="stock", currency="USD", valuation_method="market_price",
        ticker="AAPL", units=Decimal("0"),
    )
    session.add(existing)
    await session.commit()

    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price",
        "AAPL,2026-01-15,10,100.00",
    ), provider)

    assert summary["holdings_created"] == 0
    assert summary["holdings_matched"] == 1
    reloaded = await session.get(Asset, existing.id)
    assert reloaded is not None and reloaded.units == Decimal("10")


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price",
        "AAPL,2026-01-15,10,100.00",
    ), provider, dry_run=True)

    assert summary["imported"] == 1
    assert summary["holdings_created"] == 1
    assert (await session.execute(select(Asset))).scalars().first() is None


@pytest.mark.parametrize(
    "header,row,expected_kind",
    [
        # One export per language Securo is translated into.
        ("Symbol,Date,Quantity,Price,Fee,Side", "AAPL,2026-01-15,10,150.00,1.20,buy", "buy"),
        ("Ativo;Data;Quantidade;Preço;Corretagem;Operação", "AAPL;15/01/2026;10;150,00;1,20;compra", "buy"),
        ("Activo,Fecha,Cantidad,Precio,Comisión,Operación", "AAPL,15/01/2026,10,150.00,1.20,venta", "sell"),
        ("Symbole,Date,Quantité,Cours,Frais,Sens", "AAPL,15/01/2026,10,150.00,1.20,achat", "buy"),
        ("Wertpapier;Datum;Stück;Kurs;Gebühr;Art", "AAPL;15.01.2026;10;150,00;1,20;verkauf", "sell"),
        ("Titolo,Data,Quantità,Prezzo,Commissioni,Operazione", "AAPL,15/01/2026,10,150.00,1.20,acquisto", "buy"),
        ("Walor;Data;Ilość;Cena;Prowizja;Rodzaj", "AAPL;15/01/2026;10;150,00;1,20;kupno", "buy"),
        ("Тикер,Дата,Количество,Цена,Комиссия,Операция", "AAPL,15/01/2026,10,150.00,1.20,продажа", "sell"),
        ("Тікер,Дата,Кількість,Ціна,Комісія,Операція", "AAPL,15/01/2026,10,150.00,1.20,купівля", "buy"),
    ],
)
def test_headers_are_recognised_in_every_language_the_app_ships(header, row, expected_kind):
    """A broker export is written in the language of whoever downloaded it."""
    orders, errors, _, _ = asset_import_service.parse_orders_csv(_csv(header, row))
    assert errors == [], header
    assert len(orders) == 1, header
    assert orders[0].ticker == "AAPL"
    assert orders[0].quantity == Decimal("10")
    assert orders[0].price == Decimal("150.00")
    assert orders[0].fee == Decimal("1.20")
    assert orders[0].kind == expected_kind
    assert str(orders[0].date) == "2026-01-15"


@pytest.mark.asyncio
async def test_created_holding_takes_the_quote_currency_not_the_file(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    """A file reporting a US stock in BRL must not label the holding BRL while
    its price feed keeps returning USD."""
    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price,currency",
        "AAPL,2026-01-15,10,150.00,BRL",
    ), provider)

    assert summary["imported"] == 1
    stored = await _only_asset(session, test_workspace)
    assert stored.currency == "USD"  # what the provider quotes it in


@pytest.mark.asyncio
async def test_warns_when_the_ticker_already_sits_in_another_wallet(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    """Two brokers, two positions is legitimate; a mis-picked wallet looks the
    same, so the preview says it rather than leaving it to be noticed later."""
    from app.models.asset_group import AssetGroup

    wallet = AssetGroup(id=uuid.uuid4(), workspace_id=test_workspace.id, user_id=test_user.id, name="Corretora B")
    session.add(wallet)
    await session.flush()
    session.add(Asset(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name="Apple", type="stock", currency="USD", valuation_method="market_price",
        ticker="AAPL", group_id=wallet.id, units=Decimal("5"),
    ))
    await session.commit()

    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price",
        "AAPL,2026-01-15,10,100.00",
    ), provider, dry_run=True)

    assert [(w.ticker, w.reason, w.wallet) for w in summary["warnings"]] == [
        ("AAPL", "exists_in_other_wallet", "Corretora B"),
    ]


@pytest.mark.asyncio
async def test_warns_harder_when_the_same_orders_are_already_in_another_wallet(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    """Importing the same file into a second wallet counts the shares twice,
    and the wallet-scoped dedup cannot see it."""
    from app.models.asset_group import AssetGroup

    wallet = AssetGroup(id=uuid.uuid4(), workspace_id=test_workspace.id, user_id=test_user.id, name="Corretora B")
    session.add(wallet)
    await session.flush()
    await session.commit()

    content = _csv("ticker,date,quantity,price", "AAPL,2026-01-15,10,100.00")
    await _import(session, test_workspace, test_user, content, provider, group_id=wallet.id)

    summary = await _import(session, test_workspace, test_user, content, provider, dry_run=True)

    assert [(w.ticker, w.reason, w.wallet) for w in summary["warnings"]] == [
        ("AAPL", "orders_already_in_other_wallet", "Corretora B"),
    ]


# ---------------------------------------------------------------------------
# History and undo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_records_a_history_entry(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    from app.models.import_log import ImportLog

    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price",
        "AAPL,2026-01-15,10,100.00",
        "AAPL,2026-02-15,5,120.00",
    ), provider, filename="corretora.csv")

    log = (await session.execute(select(ImportLog))).scalars().one()
    assert (log.entity, log.filename, log.transaction_count) == ("asset_orders", "corretora.csv", 2)
    assert log.account_id is None  # an order import has no account
    assert summary["import_log_id"] == str(log.id)


@pytest.mark.asyncio
async def test_an_import_that_writes_nothing_leaves_no_history(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    from app.models.import_log import ImportLog

    content = _csv("ticker,date,quantity,price", "AAPL,2026-01-15,10,100.00")
    await _import(session, test_workspace, test_user, content, provider)
    summary = await _import(session, test_workspace, test_user, content, provider)  # all duplicates

    assert summary["imported"] == 0
    assert len((await session.execute(select(ImportLog))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_undo_removes_the_orders_and_the_holding_it_created(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    from app.models.import_log import ImportLog

    await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price",
        "AAPL,2026-01-15,10,100.00",
    ), provider)
    log = (await session.execute(select(ImportLog))).scalars().one()

    removed = await asset_import_service.undo_import(session, test_workspace.id, log)

    assert removed == 1
    assert (await session.execute(select(Asset))).scalars().first() is None
    assert (await session.execute(select(AssetTransaction))).scalars().first() is None
    assert (await session.execute(select(ImportLog))).scalars().first() is None


@pytest.mark.asyncio
async def test_undo_keeps_a_holding_that_has_other_orders_and_recomputes_it(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    """A holding the user also fed by hand survives the undo, with the position
    it would have had if the import had never run."""
    from app.models.import_log import ImportLog

    await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price",
        "AAPL,2026-01-15,10,100.00",
    ), provider)
    asset = await _only_asset(session, test_workspace)
    session.add(AssetTransaction(
        asset_id=asset.id, workspace_id=test_workspace.id, kind="buy",
        quantity=Decimal("4"), price=Decimal("200.00"), fee=Decimal("0"),
        date=date(2026, 3, 1), source="manual",
    ))
    await session.commit()

    log = (await session.execute(select(ImportLog))).scalars().one()
    await asset_import_service.undo_import(session, test_workspace.id, log)

    survivor = await _only_asset(session, test_workspace)
    assert survivor.units == Decimal("4")          # only the manual buy is left
    assert survivor.average_price == Decimal("200")


@pytest.mark.asyncio
async def test_undo_leaves_a_pre_existing_holding_alone(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    from app.models.import_log import ImportLog

    existing = Asset(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name="Apple", type="stock", currency="USD", valuation_method="market_price",
        ticker="AAPL", units=Decimal("0"),
    )
    session.add(existing)
    session.add(AssetTransaction(
        asset_id=existing.id, workspace_id=test_workspace.id, kind="buy",
        quantity=Decimal("2"), price=Decimal("50.00"), fee=Decimal("0"),
        date=date(2025, 1, 1), source="manual",
    ))
    await session.commit()

    await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price",
        "AAPL,2026-01-15,10,100.00",
    ), provider)
    log = (await session.execute(select(ImportLog))).scalars().one()
    await asset_import_service.undo_import(session, test_workspace.id, log)

    survivor = await session.get(Asset, existing.id)
    assert survivor is not None
    assert survivor.units == Decimal("2")


class FlakyBatchProvider(FakeProvider):
    """The bulk endpoint answers empty even for tickers it knows.

    Not hypothetical: yfinance's bulk download returned a price for AAPL and
    then nothing for the same ticker seconds later, which used to reject the
    whole file as unknown tickers.
    """

    async def get_latest_prices(self, symbols):
        self.latest_price_calls += 1
        return {s.upper(): None for s in symbols}


@pytest.mark.asyncio
async def test_a_bulk_miss_is_confirmed_against_the_quote_endpoint(
    session: AsyncSession, test_user: User, test_workspace: Workspace
):
    provider = FlakyBatchProvider()
    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price",
        "AAPL,2026-01-15,10,100.00",
    ), provider)

    assert summary["errors"] == []
    assert summary["imported"] == 1
    assert provider.quote_calls >= 1  # the bulk miss was double-checked


@pytest.mark.asyncio
async def test_a_ticker_neither_call_knows_is_still_refused(
    session: AsyncSession, test_user: User, test_workspace: Workspace
):
    provider = FlakyBatchProvider()
    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price",
        "NOSUCH,2026-01-15,10,100.00",
    ), provider)

    assert [(e.row, e.reason) for e in summary["errors"]] == [(2, "unknown_ticker")]
    assert summary["imported"] == 0


# ---------------------------------------------------------------------------
# The lot shape: a crypto tax tool's reconciled history
# ---------------------------------------------------------------------------


#: The header a CoinLedger 8949 attachment writes, verbatim — timezone
#: parenthetical and all, because that is what the file actually contains.
_CLOSED_LOT_HEADER = (
    "Amount,Asset,Date Acquired (America/Los_Angeles),Date Sold (America/Los_Angeles),"
    "Cost Basis,Proceeds,Gain,Term"
)


def test_a_closed_lot_row_becomes_the_buy_that_opened_it_and_the_sell_that_closed_it():
    orders, errors, skips, _ = asset_import_service.parse_orders_csv(_csv(
        _CLOSED_LOT_HEADER,
        "0.5,AAPL,2024-01-02 10:00:00,2024-06-01 12:00:00,50,80,30,Short",
    ))
    assert (errors, skips) == ([], [])
    assert [(o.kind, str(o.date), o.quantity, o.price) for o in orders] == [
        ("buy", "2024-01-02", Decimal("0.5"), Decimal("100")),
        ("sell", "2024-06-01", Decimal("0.5"), Decimal("160")),
    ]


def test_a_lot_reports_total_cost_not_a_unit_price():
    """Mapping `Cost Basis` straight onto `price` would multiply the basis by
    the quantity — 4 units at a $50 total are $12.50 each, not $50 each."""
    orders, _, _, _ = asset_import_service.parse_orders_csv(_csv(
        "Asset,Date Acquired,Amount,Cost Basis",
        "AAPL,2024-01-02,4,50",
    ))
    assert [(o.kind, o.price) for o in orders] == [("buy", Decimal("12.5"))]


def test_an_open_lot_is_only_a_buy():
    orders, errors, _, _ = asset_import_service.parse_orders_csv(_csv(
        "Date Acquired (America/Los_Angeles),Asset,Amount Remaining,Cost Basis Remaining,Platform Account",
        "2025-01-20 10:42:58,ALEO,1.525786,2.50228904,Coinbase",
    ))
    assert errors == []
    assert len(orders) == 1
    assert orders[0].kind == "buy"
    assert orders[0].quantity == Decimal("1.525786")


def test_a_disposal_with_no_proceeds_is_reported_rather_than_half_imported():
    """Importing only the buy would leave units the ledger never disposed of."""
    orders, errors, _, _ = asset_import_service.parse_orders_csv(_csv(
        _CLOSED_LOT_HEADER,
        "0.5,AAPL,2024-01-02 10:00:00,2024-06-01 12:00:00,50,,,Short",
    ))
    assert [(e.row, e.reason) for e in errors] == [(2, "invalid_proceeds")]
    assert [o.kind for o in orders] == ["buy"]


def test_sixteen_decimal_places_survive_the_round_trip():
    """A crypto lot report carries more decimals than a share count ever does,
    and the ledger has to hold them (migration 082)."""
    orders, _, _, _ = asset_import_service.parse_orders_csv(_csv(
        "Asset,Date Acquired,Amount,Cost Basis",
        "USDC,2024-12-20,0.1956715971120125,0.1956715971120125",
    ))
    assert orders[0].quantity == Decimal("0.1956715971120125")
    assert orders[0].price == Decimal("1")


def test_a_quantity_below_the_ledger_scale_is_refused_not_silently_zeroed():
    _, errors, _, _ = asset_import_service.parse_orders_csv(_csv(
        "Asset,Date Acquired,Amount,Cost Basis",
        "BTC,2024-12-20,0.0000000000000000001,1",
    ))
    assert [e.reason for e in errors] == ["below_ledger_scale"]


# ---------------------------------------------------------------------------
# The transaction types a crypto history uses and a broker file never did
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("type_word,kind,price", [
    ("Interest Income", "buy", Decimal("1")),
    ("Staking Reward", "buy", Decimal("1")),
    ("Referred Award", "buy", Decimal("1")),
    ("CLAIM_DISTRIBUTION_4", "buy", Decimal("1")),
    ("Buy", "buy", Decimal("1")),
    ("Sell", "sell", Decimal("1")),
])
def test_a_crypto_type_word_opens_or_closes_a_lot(type_word, kind, price):
    orders, errors, skips, _ = asset_import_service.parse_orders_csv(_csv(
        "Asset,Timestamp,Amount,Price At Acquisition,Type",
        f"USDC,2025-01-04,10,{price},{type_word}",
    ))
    assert (errors, skips) == ([], []), type_word
    assert [(o.kind, o.price) for o in orders] == [(kind, price)]


def test_an_airdrop_with_no_stated_value_opens_a_lot_at_zero_basis():
    """Units that cost nothing make the whole eventual disposal a gain, which
    is the honest answer — not an unreadable price."""
    orders, errors, _, _ = asset_import_service.parse_orders_csv(_csv(
        "Asset,Timestamp,Amount,Cost Basis,Type",
        "AERO,2025-01-04,120,,Airdrop",
    ))
    assert errors == []
    assert [(o.kind, o.price) for o in orders] == [("buy", Decimal("0"))]


def test_a_transfer_between_the_users_own_wallets_is_skipped_with_its_reason():
    """Basis travels with the coins, so importing one would invent a lot."""
    orders, errors, skips, _ = asset_import_service.parse_orders_csv(_csv(
        "Coin type,Date and time,Coin amount,USD Value,Transaction type,Internal id",
        '"BTC","May 5, 2023 4:05 PM","-0.000372","-10.72","Withdrawal","abc-1"',
        '"BTC","May 4, 2023 7:07 PM","0.0004","11.50","Reward","abc-2"',
    ))
    assert errors == []
    assert [(s.row, s.ticker, s.reason) for s in skips] == [(2, "BTC", "transfer")]
    assert [(o.row, o.kind, str(o.date)) for o in orders] == [(3, "buy", "2023-05-04")]


def test_a_type_this_module_does_not_model_is_a_skip_not_a_malformed_row():
    orders, errors, skips, _ = asset_import_service.parse_orders_csv(_csv(
        "ticker,date,quantity,price,type",
        "AAPL,2026-01-15,10,150.00,margin call",
    ))
    assert (orders, errors) == ([], [])
    assert [(s.row, s.reason, s.detail) for s in skips] == [(2, "unsupported_type", "margin call")]


def test_a_broker_action_sentence_is_read_by_the_word_that_matters():
    """`Action` is a sentence, not a word: "YOU BOUGHT FIDELITY ZERO (FZROX)"."""
    orders, _, _, _ = asset_import_service.parse_orders_csv(_csv(
        "Run Date,Action,Symbol,Price ($),Quantity,Fees ($),Settlement Date",
        "2021-12-03,YOU BOUGHT FIDELITY ZERO TOTAL MARKET INDEX (FZROX) (Cash),FZROX,16,2.501,,",
    ))
    assert [(o.kind, o.ticker, str(o.date)) for o in orders] == [("buy", "FZROX", "2021-12-03")]


def test_a_cash_dividend_moves_no_units_and_is_skipped_not_failed():
    _, errors, skips, _ = asset_import_service.parse_orders_csv(_csv(
        "Run Date,Action,Symbol,Price ($),Quantity,Fees ($),Settlement Date",
        "2021-12-03,DIVIDEND RECEIVED FIDELITY ZERO (FZROX) (Cash),FZROX,,0,,",
    ))
    assert errors == []
    assert [(s.row, s.reason) for s in skips] == [(2, "no_units")]


def test_leading_blank_lines_do_not_become_the_header():
    """Fidelity writes a BOM and an empty line above its header row."""
    content = "﻿\n\nticker,date,quantity,price\nAAPL,2026-01-15,10,150.00\n".encode("utf-8")
    orders, errors, _, columns = asset_import_service.parse_orders_csv(content)
    assert (errors, columns) == ([], ["ticker", "date", "quantity", "price"])
    assert orders[0].ticker == "AAPL"


def test_padded_header_names_still_find_their_cells():
    """`id, Coin type, Coin amount` keys every row by the padded name."""
    orders, errors, _, _ = asset_import_service.parse_orders_csv(_csv(
        "Internal id, Coin type, Date and time, Coin amount, USD Value",
        "abc-1,BTC,2023-05-04,0.0004,11.50",
    ))
    assert errors == []
    assert orders[0].ticker == "BTC"
    assert orders[0].external_id == "abc-1"


# ---------------------------------------------------------------------------
# Files with nothing in them, and files nobody can map
# ---------------------------------------------------------------------------


def test_a_file_with_only_a_header_imports_nothing_and_complains_about_nothing():
    orders, errors, skips, columns = asset_import_service.parse_orders_csv(
        _csv("ticker,date,quantity,price")
    )
    assert (orders, errors, skips) == ([], [], [])
    assert columns == ["ticker", "date", "quantity", "price"]


def test_a_completely_empty_file_is_a_parse_error_not_a_crash():
    with pytest.raises(ValueError, match="no header row"):
        asset_import_service.parse_orders_csv(b"")


def test_columns_nobody_recognises_name_every_field_the_file_still_needs():
    """The endpoint turns this into the mapping dropdowns, so the message has
    to say which fields are still unanswered."""
    with pytest.raises(ValueError) as exc:
        asset_import_service.parse_orders_csv(_csv(
            "col_a,col_b,col_c", "x,y,z",
        ))
    assert "ticker" in str(exc.value)
    assert "quantity" in str(exc.value)
    assert "price" in str(exc.value)


def test_a_file_with_a_basis_column_needs_no_price_column():
    orders, errors, _, _ = asset_import_service.parse_orders_csv(_csv(
        "Asset,Date Acquired,Amount,Cost Basis", "AAPL,2024-01-02,4,50",
    ))
    assert (errors, len(orders)) == ([], 1)


# ---------------------------------------------------------------------------
# Applying the lot shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_closed_lot_lands_as_both_halves_and_leaves_the_position_flat(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    summary = await _import(session, test_workspace, test_user, _csv(
        _CLOSED_LOT_HEADER,
        "0.5,AAPL,2024-01-02 10:00:00,2024-06-01 12:00:00,50,80,30,Short",
    ), provider)

    assert summary["imported"] == 2
    stored = await _only_asset(session, test_workspace)
    assert stored.units == Decimal("0")
    assert stored.realized_gain == Decimal("30")


@pytest.mark.asyncio
async def test_reimporting_a_lot_report_adds_nothing(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    content = _csv(
        _CLOSED_LOT_HEADER,
        "0.5,AAPL,2024-01-02 10:00:00,2024-06-01 12:00:00,50,80,30,Short",
        "0.25,AAPL,2024-02-02 10:00:00,,25,,,Short",
    )
    await _import(session, test_workspace, test_user, content, provider)
    summary = await _import(session, test_workspace, test_user, content, provider)

    assert summary["imported"] == 0
    assert summary["skipped"] == 3
    assert {s.reason for s in summary["skips"]} == {"already_imported"}
    assert all(s.row in (2, 3) for s in summary["skips"])
    rows = (await session.execute(AssetTransaction.__table__.select())).all()
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_two_identical_buys_on_one_day_are_two_lots_not_one(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    """A crypto history does this several times a page. Under a plain
    already-seen set the second one could never be imported after the first."""
    content = _csv(
        "ticker,date,quantity,price",
        "AAPL,2026-01-15,10,100.00",
        "AAPL,2026-01-15,10,100.00",
    )
    first = await _import(session, test_workspace, test_user, content, provider)
    assert first["imported"] == 2

    second = await _import(session, test_workspace, test_user, content, provider)
    assert (second["imported"], second["skipped"]) == (0, 2)
    stored = await _only_asset(session, test_workspace)
    assert stored.units == Decimal("20")


@pytest.mark.asyncio
async def test_a_skip_names_its_row_and_says_why(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    content = _csv("ticker,date,quantity,price", "AAPL,2026-01-15,10,100.00")
    await _import(session, test_workspace, test_user, content, provider)
    summary = await _import(session, test_workspace, test_user, content, provider)

    skip = summary["skips"][0]
    assert (skip.row, skip.ticker, skip.reason) == (2, "AAPL", "already_imported")
    assert "2026-01-15" in skip.detail


# ---------------------------------------------------------------------------
# Holdings no price provider will ever answer for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unpriced_holding_can_be_imported_when_it_is_asked_for(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    """A token from an insolvency estate has a real basis and no market."""
    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price", "IONIC,2024-01-31,163,10.00",
    ), provider, allow_unpriced=True)

    assert summary["imported"] == 1
    assert [w.reason for w in summary["warnings"]] == ["unpriced_holding"]
    stored = await _only_asset(session, test_workspace)
    assert stored.valuation_method == "manual"
    assert (stored.ticker, stored.units, stored.last_price) == ("IONIC", Decimal("163"), None)


@pytest.mark.asyncio
async def test_an_unpriced_holding_is_matched_rather_than_duplicated_next_time(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    first = _csv("ticker,date,quantity,price", "IONIC,2024-01-31,163,10.00")
    later = _csv("ticker,date,quantity,price", "IONIC,2024-06-30,50,12.00")
    await _import(session, test_workspace, test_user, first, provider, allow_unpriced=True)
    summary = await _import(session, test_workspace, test_user, later, provider, allow_unpriced=True)

    assert summary["holdings_created"] == 0
    stored = await _only_asset(session, test_workspace)
    assert stored.units == Decimal("213")


@pytest.mark.asyncio
async def test_the_escape_hatch_stays_shut_unless_it_is_asked_for(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    """An unrecognised ticker is nearly always a typo."""
    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price", "IONIC,2024-01-31,163,10.00",
    ), provider)
    assert [e.reason for e in summary["errors"]] == ["unknown_ticker"]
    assert summary["imported"] == 0


# ---------------------------------------------------------------------------
# Seeding a Snapshot Holding
# ---------------------------------------------------------------------------


async def _snapshot_holding(session, workspace, user, *, units: str) -> Asset:
    """A holding a provider reported with no Trades behind it (CONTEXT.md)."""
    asset = Asset(
        id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id,
        name="Apple", type="stock", currency="USD", valuation_method="market_price",
        ticker="AAPL", units=Decimal(units), source="coinbase",
    )
    session.add(asset)
    await session.flush()
    return asset


@pytest.mark.asyncio
async def test_a_partial_history_over_a_snapshot_holding_is_flagged_not_absorbed(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    """`recompute_and_cache` rewrites units from the ledger, so seeding half a
    history would quietly replace the provider's quantity with the file's."""
    await _snapshot_holding(session, test_workspace, test_user, units="20")
    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price", "AAPL,2026-01-15,8,100.00",
    ), provider)

    warning = next(w for w in summary["warnings"] if w.reason == "units_differ_from_provider")
    assert warning.ticker == "AAPL"
    assert (warning.imported_units, warning.reported_units) == ("8", "20")


@pytest.mark.asyncio
async def test_a_history_that_reconciles_with_the_provider_says_nothing(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    await _snapshot_holding(session, test_workspace, test_user, units="20")
    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price",
        "AAPL,2026-01-15,30,100.00",
        "AAPL,2026-02-15,-10,120.00",
    ), provider)

    assert [w.reason for w in summary["warnings"]] == []
    stored = await _only_asset(session, test_workspace)
    assert stored.units == Decimal("20")


@pytest.mark.asyncio
async def test_a_holding_that_already_has_a_ledger_is_not_reconciled_against(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    """Only a Snapshot Holding has a provider figure to disagree with; once
    there are Trades the ledger is authoritative (CONTEXT.md)."""
    content = _csv("ticker,date,quantity,price", "AAPL,2026-01-15,10,100.00")
    await _import(session, test_workspace, test_user, content, provider)
    summary = await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price", "AAPL,2026-03-15,5,120.00",
    ), provider)

    assert [w.reason for w in summary["warnings"]] == []


@pytest.mark.asyncio
async def test_a_lot_line_is_charged_its_fee_once_not_on_both_halves(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    orders, _, _, _ = asset_import_service.parse_orders_csv(_csv(
        "Amount,Asset,Date Acquired,Date Sold,Cost Basis,Proceeds,Fee",
        "0.5,AAPL,2024-01-02,2024-06-01,50,80,3",
    ))
    assert [(o.kind, o.fee) for o in orders] == [
        ("buy", Decimal("3")), ("sell", Decimal("0")),
    ]


def test_a_meme_coin_lot_is_not_refused_for_being_large():
    """Twelve digits before the point is ordinary for a token like SHIB, and
    is exactly the file this importer exists for."""
    orders, errors, _, _ = asset_import_service.parse_orders_csv(_csv(
        "Asset,Date Acquired,Amount,Cost Basis",
        "SHIB,2024-12-20,412500000000.123456,900",
    ))
    assert errors == []
    assert orders[0].quantity == Decimal("412500000000.123456")


def test_a_missing_cost_column_names_both_ways_of_stating_one():
    with pytest.raises(ValueError, match="price or cost_basis"):
        asset_import_service.parse_orders_csv(_csv("ticker,date,quantity", "AAPL,2026-01-15,10"))


@pytest.mark.asyncio
async def test_a_hand_made_manual_asset_is_not_claimed_by_the_importer(
    session: AsyncSession, test_user: User, test_workspace: Workspace, provider
):
    """A manual asset carrying a ticker is the user's own record; rewriting its
    units from a file it never came from would be taking something not ours."""
    mine = Asset(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name="Apple, by hand", type="stock", currency="USD",
        valuation_method="manual", ticker="AAPL", units=Decimal("99"), source="manual",
    )
    session.add(mine)
    await session.flush()

    await _import(session, test_workspace, test_user, _csv(
        "ticker,date,quantity,price", "AAPL,2026-01-15,10,100.00",
    ), provider)

    await session.refresh(mine)
    assert mine.units == Decimal("99")


# ---------------------------------------------------------------------------
# Which half of `12/07/2021` is the day
# ---------------------------------------------------------------------------


def test_one_unambiguous_row_settles_the_whole_file_as_month_first():
    """A US broker writes 12/07/2021 for 7 December. Reading it per row gets
    the days 13-31 right and the days 1-12 wrong in the same import."""
    orders, errors, _, _ = asset_import_service.parse_orders_csv(_csv(
        "ticker,date,quantity,price",
        "FZROX,12/16/2022,1,13.39",   # only December has a 16th
        "FZROX,12/07/2021,1,16.00",
    ))
    assert errors == []
    assert [str(o.date) for o in orders] == ["2022-12-16", "2021-12-07"]


def test_a_day_first_file_is_still_read_day_first():
    orders, errors, _, _ = asset_import_service.parse_orders_csv(_csv(
        "ticker,date,quantity,price",
        "PETR4.SA,16/12/2022,1,13.39",
        "PETR4.SA,07/12/2021,1,16.00",
    ))
    assert errors == []
    assert [str(o.date) for o in orders] == ["2022-12-16", "2021-12-07"]


def test_an_entirely_ambiguous_file_keeps_the_day_first_default():
    orders, _, _, _ = asset_import_service.parse_orders_csv(_csv(
        "ticker,date,quantity,price", "AAPL,07/12/2021,1,16.00",
    ))
    assert str(orders[0].date) == "2021-12-07"


def test_an_explicit_choice_beats_what_the_file_looks_like():
    orders, _, _, _ = asset_import_service.parse_orders_csv(
        _csv("ticker,date,quantity,price", "AAPL,07/12/2021,1,16.00"),
        date_format="MM/DD/YYYY",
    )
    assert str(orders[0].date) == "2021-07-12"


def test_the_disposal_date_votes_on_the_order_too():
    """A closed-lot report can carry its only unambiguous date in `Date Sold`."""
    orders, errors, _, _ = asset_import_service.parse_orders_csv(_csv(
        _CLOSED_LOT_HEADER,
        "0.5,AAPL,01/02/2024,06/30/2024,50,80,30,Short",
    ))
    assert errors == []
    assert [str(o.date) for o in orders] == ["2024-01-02", "2024-06-30"]
