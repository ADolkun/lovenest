"""Buy/sell ledger for market-priced holdings (issue #235).

Transactions are the source of truth; a holding's `units`, `average_price`
(preço médio), cost basis and realized gain are *derived* by replaying the
ledger in date order and cached on the asset row so list views stay cheap.

Average price uses the weighted-average method (the Brazilian preço médio
convention), not FIFO/LIFO: a sell realizes `(price - avg) * qty` and lowers
the cost basis proportionally, leaving the per-unit average unchanged.
"""
import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.providers.market_price import MarketPriceProvider, get_market_price_provider
from app.schemas.asset import (
    AssetBuyCreate,
    AssetRead,
    AssetTransactionCreate,
    AssetTransactionRead,
    AssetTransactionUpdate,
)
from app.schemas.asset_group import REPORTABLE_TAX_TREATMENTS
from app.services import asset_service
from app.services.asset_group_service import ensure_group_in_workspace
from app.services.cash_equivalent import CASH_EQUIVALENT_TYPE, is_cash_equivalent_ticker

logger = logging.getLogger(__name__)

_VALID_KINDS = {"buy", "sell"}


def _d(value) -> Decimal:
    return Decimal(str(value or 0))


def _recompute(transactions: list[AssetTransaction]) -> dict:
    """Replay a holding's ledger in date order → derived position.

    Returns units (current quantity), average_price (per unit, None when flat),
    cost_basis (of held units), realized_gain, first_buy/last_sell dates, and
    realized_events — the per-sell (date, gain) pairs the cumulative figure is
    made of, so a caller can restrict the gain to a tax year.
    """
    txs = sorted(transactions, key=lambda t: (t.date, t.created_at or datetime.min.replace(tzinfo=timezone.utc)))
    qty = Decimal("0")
    cost = Decimal("0")
    realized = Decimal("0")
    realized_events: list[tuple[date, Decimal]] = []
    first_buy: Optional[date] = None
    last_sell: Optional[date] = None

    for tx in txs:
        q = _d(tx.quantity)
        p = _d(tx.price)
        fee = _d(tx.fee)
        if tx.kind == "buy":
            cost += q * p + fee
            qty += q
            if first_buy is None:
                first_buy = tx.date
        elif tx.kind == "sell":
            avg = (cost / qty) if qty > 0 else Decimal("0")
            sell_qty = q if q <= qty else qty  # clamp oversell defensively
            gain = (p - avg) * sell_qty - fee
            realized += gain
            realized_events.append((tx.date, gain))
            cost -= avg * sell_qty
            qty -= sell_qty
            last_sell = tx.date

    avg_price = (cost / qty) if qty > 0 else None
    return {
        "units": qty,
        "average_price": avg_price,
        "cost_basis": cost if qty > 0 else Decimal("0"),
        "realized_gain": realized,
        "realized_events": realized_events,
        "first_buy": first_buy,
        "last_sell": last_sell,
    }


def _detect_oversell(transactions: list[AssetTransaction]) -> Optional[tuple[Decimal, Decimal]]:
    """Replay the ledger in date order and return the first sell that exceeds
    the units held at that point as (attempted, available), else None.

    We keep the portfolio buy-and-hold: a position can't go negative (no
    shorting), so an over-sell is rejected rather than silently clamped.
    """
    txs = sorted(
        transactions,
        key=lambda t: (t.date, t.created_at or datetime.min.replace(tzinfo=timezone.utc)),
    )
    qty = Decimal("0")
    for tx in txs:
        q = _d(tx.quantity)
        if tx.kind == "buy":
            qty += q
        elif tx.kind == "sell":
            if q > qty:
                return (q, qty)
            qty -= q
    return None


async def _load_txs(session: AsyncSession, asset_id: uuid.UUID) -> list[AssetTransaction]:
    result = await session.execute(
        select(AssetTransaction).where(AssetTransaction.asset_id == asset_id)
    )
    return list(result.scalars().all())


def _raise_if_oversell(transactions: list[AssetTransaction]) -> None:
    """Reject a ledger that would drive the position negative (no shorting).

    Checked in-memory on the prospective ledger before any insert, so a
    rejected transaction never touches the DB.
    """
    over = _detect_oversell(transactions)
    if over is not None:
        attempted, available = over
        fmt = lambda q: f"{q:.6f}".rstrip("0").rstrip(".")  # noqa: E731
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Cannot sell {fmt(attempted)} units — only {fmt(available)} held at that date. "
                "Short positions aren't supported."
            ),
        )


async def recompute_and_cache(session: AsyncSession, asset: Asset) -> None:
    """Recompute the derived position from the ledger and cache it on the asset.

    Keeps `purchase_price` as the cost basis of the held units so the existing
    `gain_loss = current_value - purchase_price` math reads as the unrealized
    gain, and refreshes today's AssetValue so the portfolio chart matches the
    new quantity.
    """
    result = await session.execute(
        select(AssetTransaction).where(AssetTransaction.asset_id == asset.id)
    )
    txs = list(result.scalars().all())
    pos = _recompute(txs)

    asset.units = pos["units"]
    asset.average_price = pos["average_price"]
    asset.realized_gain = pos["realized_gain"].quantize(Decimal("0.01"))
    asset.purchase_price = (
        pos["cost_basis"].quantize(Decimal("0.01")) if pos["units"] > 0 else None
    )
    asset.purchase_date = pos["first_buy"]

    if pos["units"] > 0:
        # Re-opened position (or still open): clear any prior full-exit marker.
        asset.sell_date = None
        asset.sell_price = None
    elif txs and pos["last_sell"] is not None:
        # Fully exited — mark sold so it drops out of the active portfolio.
        asset.sell_date = pos["last_sell"]

    # Keep today's chart point in step with the new quantity.
    if asset.valuation_method == "market_price" and asset.last_price is not None:
        await asset_service._apply_price_to_asset(session, asset, Decimal(str(asset.last_price)))


def _tx_to_read(tx: AssetTransaction, asset: Optional[Asset] = None) -> AssetTransactionRead:
    return AssetTransactionRead(
        id=tx.id,
        asset_id=tx.asset_id,
        kind=tx.kind,
        quantity=float(tx.quantity),
        price=float(tx.price),
        fee=float(tx.fee or 0),
        date=tx.date,
        source=tx.source,
        notes=tx.notes,
        asset_name=asset.name if asset else None,
        ticker=asset.ticker if asset else None,
        currency=asset.currency if asset else None,
        logo_url=asset.logo_url if asset else None,
    )


async def _load_asset(
    session: AsyncSession, asset_id: uuid.UUID, workspace_id: uuid.UUID
) -> Optional[Asset]:
    result = await session.execute(
        select(Asset).where(Asset.id == asset_id, Asset.workspace_id == workspace_id)
    )
    return result.scalar_one_or_none()


async def list_asset_transactions(
    session: AsyncSession, asset_id: uuid.UUID, workspace_id: uuid.UUID
) -> Optional[list[AssetTransactionRead]]:
    asset = await _load_asset(session, asset_id, workspace_id)
    if asset is None:
        return None
    result = await session.execute(
        select(AssetTransaction)
        .where(AssetTransaction.asset_id == asset_id)
        .order_by(AssetTransaction.date.desc(), AssetTransaction.created_at.desc())
    )
    return [_tx_to_read(tx, asset) for tx in result.scalars().all()]


async def list_workspace_transactions(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    ticker: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 500,
) -> list[AssetTransactionRead]:
    """All ledger transactions in a workspace — powers the Transactions tab."""
    query = (
        select(AssetTransaction, Asset)
        .join(Asset, AssetTransaction.asset_id == Asset.id)
        .where(AssetTransaction.workspace_id == workspace_id)
    )
    if ticker:
        query = query.where(Asset.ticker == ticker.upper())
    if kind in _VALID_KINDS:
        query = query.where(AssetTransaction.kind == kind)
    query = query.order_by(
        AssetTransaction.date.desc(), AssetTransaction.created_at.desc()
    ).limit(limit)
    result = await session.execute(query)
    return [_tx_to_read(tx, asset) for tx, asset in result.all()]


async def reportable_gain(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> dict:
    """Reportable Gain for a workspace: the Realised Gain arising in Taxable
    Wallets, and nothing else. The only gain figure a tax calculation may
    consume (CONTEXT.md).

    `start`/`end` bound the sell dates (end exclusive) so a tax year can be
    asked for; the whole ledger is replayed regardless, because the average
    cost a sell books against depends on every buy before it.

    `non_reportable_gain` is the rest of the Realised Gain — tax-advantaged
    wallets, `other`, and holdings sitting in no wallet — reported so a caller
    can say what it left out instead of silently dropping it.

    Tax character is read from the wallet a holding sits in *now*, so moving a
    holding between wallets re-characterises gains already closed in earlier
    years. Fixing that needs the wallet a trade happened in, which the ledger
    does not record.
    """
    rows = (
        await session.execute(
            select(AssetTransaction, AssetGroup.tax_treatment)
            .join(Asset, AssetTransaction.asset_id == Asset.id)
            .outerjoin(
                AssetGroup,
                (Asset.group_id == AssetGroup.id)
                & (AssetGroup.workspace_id == workspace_id),
            )
            .where(AssetTransaction.workspace_id == workspace_id)
        )
    ).all()

    ledgers: dict[uuid.UUID, list[AssetTransaction]] = {}
    treatments: dict[uuid.UUID, Optional[str]] = {}
    for tx, treatment in rows:
        ledgers.setdefault(tx.asset_id, []).append(tx)
        treatments[tx.asset_id] = treatment

    reportable = Decimal("0")
    non_reportable = Decimal("0")
    for asset_id, txs in ledgers.items():
        gain = sum(
            (
                g
                for d, g in _recompute(txs)["realized_events"]
                if (start is None or d >= start) and (end is None or d < end)
            ),
            Decimal("0"),
        )
        if treatments[asset_id] in REPORTABLE_TAX_TREATMENTS:
            reportable += gain
        else:
            non_reportable += gain

    return {
        "reportable_gain": float(reportable.quantize(Decimal("0.01"))),
        "non_reportable_gain": float(non_reportable.quantize(Decimal("0.01"))),
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
    }


def _validate(kind: str, quantity: Decimal, price: Decimal) -> None:
    if kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="kind must be 'buy' or 'sell'",
        )
    if quantity is None or quantity <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="quantity must be > 0",
        )
    if price is None or price < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="price must be >= 0",
        )


async def add_transaction(
    session: AsyncSession,
    asset_id: uuid.UUID,
    workspace_id: uuid.UUID,
    data: AssetTransactionCreate,
) -> Optional[AssetRead]:
    asset = await _load_asset(session, asset_id, workspace_id)
    if asset is None:
        return None
    _validate(data.kind, data.quantity, data.price)
    new_tx = AssetTransaction(
        asset_id=asset.id,
        workspace_id=workspace_id,
        kind=data.kind,
        quantity=data.quantity,
        price=data.price,
        fee=data.fee or Decimal("0"),
        date=data.date,
        source="manual",
        notes=data.notes,
        created_at=datetime.now(timezone.utc),
    )
    if data.kind == "sell":
        _raise_if_oversell(await _load_txs(session, asset.id) + [new_tx])
    session.add(new_tx)
    await session.flush()
    await recompute_and_cache(session, asset)
    await session.commit()
    return await asset_service.get_asset(session, asset.id, workspace_id)


async def update_transaction(
    session: AsyncSession,
    tx_id: uuid.UUID,
    workspace_id: uuid.UUID,
    data: AssetTransactionUpdate,
) -> Optional[AssetRead]:
    result = await session.execute(
        select(AssetTransaction).where(
            AssetTransaction.id == tx_id, AssetTransaction.workspace_id == workspace_id
        )
    )
    tx = result.scalar_one_or_none()
    if tx is None:
        return None
    fields = data.model_dump(exclude_unset=True)
    # Validate the prospective ledger before mutating the row — editing a buy
    # down or a sell up could drive the position negative.
    others = [t for t in await _load_txs(session, tx.asset_id) if t.id != tx.id]
    edited = AssetTransaction(
        asset_id=tx.asset_id,
        kind=fields.get("kind", tx.kind),
        quantity=fields.get("quantity", tx.quantity),
        price=fields.get("price", tx.price),
        date=fields.get("date", tx.date),
        created_at=tx.created_at,
    )
    _raise_if_oversell(others + [edited])

    # Resolve the asset before mutating the row: bailing out after the
    # setattr loop would leave the edits pending in the session, so a later
    # commit in the same request would persist an update we reported failed.
    asset = await _load_asset(session, tx.asset_id, workspace_id)
    if asset is None:
        return None

    for key, value in fields.items():
        setattr(tx, key, value)
    _validate(tx.kind, _d(tx.quantity), _d(tx.price))
    await session.flush()
    await recompute_and_cache(session, asset)
    await session.commit()
    return await asset_service.get_asset(session, asset.id, workspace_id)


async def delete_transaction(
    session: AsyncSession, tx_id: uuid.UUID, workspace_id: uuid.UUID
) -> Optional[AssetRead]:
    result = await session.execute(
        select(AssetTransaction).where(
            AssetTransaction.id == tx_id, AssetTransaction.workspace_id == workspace_id
        )
    )
    tx = result.scalar_one_or_none()
    if tx is None:
        return None
    asset = await _load_asset(session, tx.asset_id, workspace_id)
    if asset is None:
        return None
    # Deleting a buy can strand a later sell that depended on its units, which
    # is the same negative position `add`/`update` already refuse. `_recompute`
    # would clamp it silently and book the realized gain against a quantity
    # that never left, so the ledger has to be checked before the row goes.
    _raise_if_oversell([t for t in await _load_txs(session, tx.asset_id) if t.id != tx.id])
    await session.delete(tx)
    await session.flush()
    await recompute_and_cache(session, asset)
    await session.commit()
    return await asset_service.get_asset(session, asset.id, workspace_id)


async def buy_into_holding(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    data: AssetBuyCreate,
    *,
    market_provider: Optional[MarketPriceProvider] = None,
) -> AssetRead:
    """Record a buy, consolidating onto the existing ticker holding in the
    chosen wallet (`group_id`) or creating a new market-priced holding."""
    _validate("buy", data.quantity, data.price)
    await ensure_group_in_workspace(session, data.group_id, workspace_id)
    ticker = data.ticker.upper()

    result = await session.execute(
        select(Asset).where(
            Asset.workspace_id == workspace_id,
            Asset.ticker == ticker,
            Asset.valuation_method == "market_price",
            Asset.group_id == data.group_id,
        )
    )
    asset = result.scalar_one_or_none()

    if asset is None:
        provider = market_provider or get_market_price_provider()
        quote = await provider.get_quote(ticker)
        if quote is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Could not fetch quote for {ticker}",
            )
        asset = Asset(
            user_id=user_id,
            workspace_id=workspace_id,
            name=data.name or quote.name or ticker,
            type=_type_from_quote(quote.quote_type, ticker),
            currency=quote.currency,
            valuation_method="market_price",
            group_id=data.group_id,
            ticker=ticker,
            ticker_exchange=quote.exchange,
            last_price=Decimal(str(quote.price)),
            last_price_at=datetime.now(timezone.utc),
            logo_url=quote.logo_url,
            source="yfinance",
        )
        session.add(asset)
        await session.flush()

    session.add(
        AssetTransaction(
            asset_id=asset.id,
            workspace_id=workspace_id,
            kind="buy",
            quantity=data.quantity,
            price=data.price,
            fee=data.fee or Decimal("0"),
            date=data.date,
            source="manual",
            notes=data.notes,
        )
    )
    await session.flush()
    await recompute_and_cache(session, asset)
    await session.commit()
    result = await asset_service.get_asset(session, asset.id, workspace_id)
    if result is None:
        raise RuntimeError("Asset disappeared after ledger transaction")
    return result


def _type_from_quote(quote_type: Optional[str], ticker: Optional[str] = None) -> str:
    """Mirror the frontend's quoteType → asset type mapping so a holding
    created from the ledger lands on a sensible icon/type."""
    if is_cash_equivalent_ticker(ticker):
        return CASH_EQUIVALENT_TYPE
    mapping = {
        "EQUITY": "stock",
        "ETF": "etf",
        "CRYPTOCURRENCY": "crypto",
        "MUTUALFUND": "fund",
        "INDEX": "fund",
        "MONEYMARKET": CASH_EQUIVALENT_TYPE,
    }
    return mapping.get((quote_type or "").upper(), "investment")
