import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.asset_value import AssetValue
from app.schemas.asset import AssetCreate, AssetUpdate, AssetValueCreate, AssetRead, AssetValueRead


def _next_due_date(last_date: date, frequency: str) -> date:
    """Calculate the next due date based on frequency."""
    if frequency == "daily":
        return last_date + timedelta(days=1)
    elif frequency == "weekly":
        return last_date + timedelta(weeks=1)
    elif frequency == "monthly":
        month = last_date.month + 1
        year = last_date.year
        if month > 12:
            month = 1
            year += 1
        day = min(last_date.day, 28)
        return date(year, month, day)
    elif frequency == "yearly":
        return date(last_date.year + 1, last_date.month, last_date.day)
    return last_date + timedelta(days=1)


def _compute_current_value(asset: Asset, latest_value: Optional[AssetValue]) -> Optional[float]:
    """Compute the current value of an asset from its latest AssetValue.
    Falls back to purchase_price if no value entries exist yet."""
    if latest_value is None:
        if asset.purchase_price is not None:
            return float(asset.purchase_price)
        return None
    return float(latest_value.amount)


def _generate_growth_values(
    asset_id: uuid.UUID,
    base_amount: float,
    base_date: date,
    growth_type: str,
    growth_rate: float,
    growth_frequency: str,
    growth_start_date: Optional[date],
) -> list[AssetValue]:
    """Generate all AssetValue rows from base_date to today using the growth rule."""
    today = date.today()
    if growth_start_date and today < growth_start_date:
        return []

    values: list[AssetValue] = []
    current_amount = base_amount
    current_date = base_date

    while True:
        next_due = _next_due_date(current_date, growth_frequency)
        if next_due > today:
            break
        if growth_type == "percentage":
            current_amount = current_amount * (1 + growth_rate / 100)
        elif growth_type == "absolute":
            current_amount = current_amount + growth_rate
        else:
            break
        values.append(AssetValue(
            asset_id=asset_id,
            amount=Decimal(str(round(current_amount, 6))),
            date=next_due,
            source="rule",
        ))
        current_date = next_due
        if len(values) >= 10000:
            break

    return values


def _asset_to_read(asset: Asset, latest_value: Optional[AssetValue], value_count: int) -> AssetRead:
    """Convert an Asset model + computed fields to AssetRead schema."""
    current_value = _compute_current_value(asset, latest_value)
    gain_loss = None
    if current_value is not None and asset.purchase_price is not None:
        gain_loss = current_value - float(asset.purchase_price)

    return AssetRead(
        id=asset.id,
        user_id=asset.user_id,
        name=asset.name,
        type=asset.type,
        currency=asset.currency,
        units=float(asset.units) if asset.units is not None else None,
        valuation_method=asset.valuation_method,
        purchase_date=asset.purchase_date,
        purchase_price=float(asset.purchase_price) if asset.purchase_price is not None else None,
        sell_date=asset.sell_date,
        sell_price=float(asset.sell_price) if asset.sell_price is not None else None,
        growth_type=asset.growth_type,
        growth_rate=float(asset.growth_rate) if asset.growth_rate is not None else None,
        growth_frequency=asset.growth_frequency,
        growth_start_date=asset.growth_start_date,
        is_archived=asset.is_archived,
        position=asset.position,
        current_value=current_value,
        gain_loss=gain_loss,
        value_count=value_count,
    )


async def _get_latest_value(session: AsyncSession, asset_id: uuid.UUID) -> Optional[AssetValue]:
    """Get the most recent AssetValue for an asset."""
    result = await session.execute(
        select(AssetValue)
        .where(AssetValue.asset_id == asset_id)
        .order_by(desc(AssetValue.date), desc(AssetValue.id))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _get_value_count(session: AsyncSession, asset_id: uuid.UUID) -> int:
    """Get the number of AssetValue entries for an asset."""
    result = await session.scalar(
        select(func.count()).select_from(AssetValue).where(AssetValue.asset_id == asset_id)
    )
    return result or 0


async def get_assets(
    session: AsyncSession, user_id: uuid.UUID, include_archived: bool = False
) -> list[AssetRead]:
    """List all assets for a user with computed current_value."""
    query = select(Asset).where(Asset.user_id == user_id)
    if not include_archived:
        query = query.where(Asset.is_archived == False)
    query = query.order_by(Asset.position, Asset.name)

    result = await session.execute(query)
    assets = list(result.scalars().all())

    reads = []
    for asset in assets:
        latest = await _get_latest_value(session, asset.id)
        count = await _get_value_count(session, asset.id)
        reads.append(_asset_to_read(asset, latest, count))
    return reads


async def get_asset(
    session: AsyncSession, asset_id: uuid.UUID, user_id: uuid.UUID
) -> Optional[AssetRead]:
    """Get a single asset with computed fields."""
    result = await session.execute(
        select(Asset).where(Asset.id == asset_id, Asset.user_id == user_id)
    )
    asset = result.scalar_one_or_none()
    if not asset:
        return None
    latest = await _get_latest_value(session, asset.id)
    count = await _get_value_count(session, asset.id)
    return _asset_to_read(asset, latest, count)


async def create_asset(
    session: AsyncSession, user_id: uuid.UUID, data: AssetCreate
) -> AssetRead:
    """Create an asset, optionally with an initial value."""
    asset = Asset(
        user_id=user_id,
        name=data.name,
        type=data.type,
        currency=data.currency,
        units=data.units,
        valuation_method=data.valuation_method,
        purchase_date=data.purchase_date,
        purchase_price=data.purchase_price,
        sell_date=data.sell_date,
        sell_price=data.sell_price,
        growth_type=data.growth_type,
        growth_rate=data.growth_rate,
        growth_frequency=data.growth_frequency,
        growth_start_date=data.growth_start_date,
        is_archived=data.is_archived,
        position=data.position,
    )
    session.add(asset)
    await session.flush()

    # Create initial value if provided
    if data.current_value is not None:
        value = AssetValue(
            asset_id=asset.id,
            amount=data.current_value,
            date=date.today(),
            source="manual",
        )
        session.add(value)
    elif data.valuation_method == "growth_rule" and data.purchase_price is not None:
        # Seed the initial value from purchase price
        base_date = data.purchase_date or data.growth_start_date or date.today()
        seed = AssetValue(
            asset_id=asset.id,
            amount=data.purchase_price,
            date=base_date,
            source="manual",
        )
        session.add(seed)

        # Backfill all growth values from the seed date to today
        if data.growth_type and data.growth_rate and data.growth_frequency:
            backfill = _generate_growth_values(
                asset_id=asset.id,
                base_amount=float(data.purchase_price),
                base_date=base_date,
                growth_type=data.growth_type,
                growth_rate=float(data.growth_rate),
                growth_frequency=data.growth_frequency,
                growth_start_date=data.growth_start_date,
            )
            for v in backfill:
                session.add(v)

    await session.commit()
    await session.refresh(asset)
    latest = await _get_latest_value(session, asset.id)
    count = await _get_value_count(session, asset.id)
    return _asset_to_read(asset, latest, count)


async def update_asset(
    session: AsyncSession, asset_id: uuid.UUID, user_id: uuid.UUID, data: AssetUpdate,
    regenerate_growth: bool = False,
) -> Optional[AssetRead]:
    """Partial update of an asset."""
    result = await session.execute(
        select(Asset).where(Asset.id == asset_id, Asset.user_id == user_id)
    )
    asset = result.scalar_one_or_none()
    if not asset:
        return None

    update_data = data.model_dump(exclude_unset=True)
    # Prevent changing valuation_method on existing assets
    update_data.pop("valuation_method", None)
    for key, value in update_data.items():
        setattr(asset, key, value)

    # Regenerate growth-rule values if requested
    if regenerate_growth and asset.valuation_method == "growth_rule":
        # Delete all rule-generated values
        await session.execute(
            select(AssetValue)
            .where(AssetValue.asset_id == asset.id, AssetValue.source == "rule")
        )
        from sqlalchemy import delete as sa_delete
        await session.execute(
            sa_delete(AssetValue).where(
                AssetValue.asset_id == asset.id,
                AssetValue.source == "rule",
            )
        )
        # Regenerate from purchase_price
        if asset.purchase_price and asset.growth_type and asset.growth_rate and asset.growth_frequency:
            base_date = asset.purchase_date or asset.growth_start_date or date.today()
            backfill = _generate_growth_values(
                asset_id=asset.id,
                base_amount=float(asset.purchase_price),
                base_date=base_date,
                growth_type=asset.growth_type,
                growth_rate=float(asset.growth_rate),
                growth_frequency=asset.growth_frequency,
                growth_start_date=asset.growth_start_date,
            )
            for v in backfill:
                session.add(v)

    await session.commit()
    await session.refresh(asset)
    latest = await _get_latest_value(session, asset.id)
    count = await _get_value_count(session, asset.id)
    return _asset_to_read(asset, latest, count)


async def delete_asset(
    session: AsyncSession, asset_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Delete an asset (cascades to values)."""
    result = await session.execute(
        select(Asset).where(Asset.id == asset_id, Asset.user_id == user_id)
    )
    asset = result.scalar_one_or_none()
    if not asset:
        return False
    await session.delete(asset)
    await session.commit()
    return True


async def get_asset_values(
    session: AsyncSession, asset_id: uuid.UUID, user_id: uuid.UUID
) -> Optional[list[AssetValueRead]]:
    """Get value history for an asset, most recent first."""
    # Verify ownership
    owner_check = await session.execute(
        select(Asset.id).where(Asset.id == asset_id, Asset.user_id == user_id)
    )
    if not owner_check.scalar_one_or_none():
        return None

    result = await session.execute(
        select(AssetValue)
        .where(AssetValue.asset_id == asset_id)
        .order_by(desc(AssetValue.date), desc(AssetValue.id))
    )
    values = result.scalars().all()
    return [AssetValueRead.model_validate(v) for v in values]


async def add_asset_value(
    session: AsyncSession, asset_id: uuid.UUID, user_id: uuid.UUID, data: AssetValueCreate
) -> Optional[AssetValueRead]:
    """Add a new value entry for an asset."""
    # Verify ownership
    owner_check = await session.execute(
        select(Asset.id).where(Asset.id == asset_id, Asset.user_id == user_id)
    )
    if not owner_check.scalar_one_or_none():
        return None

    value = AssetValue(
        asset_id=asset_id,
        amount=data.amount,
        date=data.date,
        source="manual",
    )
    session.add(value)
    await session.commit()
    await session.refresh(value)
    return AssetValueRead.model_validate(value)


async def delete_asset_value(
    session: AsyncSession, value_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Delete a specific asset value entry."""
    result = await session.execute(
        select(AssetValue)
        .join(Asset, AssetValue.asset_id == Asset.id)
        .where(AssetValue.id == value_id, Asset.user_id == user_id)
    )
    value = result.scalar_one_or_none()
    if not value:
        return False
    await session.delete(value)
    await session.commit()
    return True


async def get_asset_value_trend(
    session: AsyncSession, asset_id: uuid.UUID, user_id: uuid.UUID, months: int = 12
) -> Optional[list[dict]]:
    """Get value trend data for charting."""
    # Verify ownership
    owner_check = await session.execute(
        select(Asset.id).where(Asset.id == asset_id, Asset.user_id == user_id)
    )
    if not owner_check.scalar_one_or_none():
        return None

    result = await session.execute(
        select(AssetValue.date, AssetValue.amount)
        .where(AssetValue.asset_id == asset_id)
        .order_by(AssetValue.date)
    )
    rows = result.all()
    return [{"date": row[0].isoformat(), "amount": float(row[1])} for row in rows]


async def get_portfolio_trend(
    session: AsyncSession, user_id: uuid.UUID
) -> dict:
    """Get portfolio trend data for stacked area chart.
    Returns asset metadata + pivoted trend with fill-forward values."""
    result = await session.execute(
        select(Asset).where(
            Asset.user_id == user_id,
            Asset.is_archived == False,
            Asset.sell_date.is_(None),
        ).order_by(Asset.position, Asset.name)
    )
    active_assets = list(result.scalars().all())

    if not active_assets:
        return {"assets": [], "trend": [], "total": 0.0}

    # Collect all values per asset and all unique dates
    asset_meta = []
    asset_values_map: dict[str, list[tuple[date, float]]] = {}
    all_dates: set[date] = set()

    for asset in active_assets:
        aid = str(asset.id)
        asset_meta.append({"id": aid, "name": asset.name, "type": asset.type})

        rows = await session.execute(
            select(AssetValue.date, AssetValue.amount)
            .where(AssetValue.asset_id == asset.id)
            .order_by(AssetValue.date)
        )
        vals = [(r[0], float(r[1])) for r in rows.all()]

        # Prepend purchase_price as the first data point if it predates existing values
        if asset.purchase_price is not None and asset.purchase_date is not None:
            if not vals or asset.purchase_date < vals[0][0]:
                vals.insert(0, (asset.purchase_date, float(asset.purchase_price)))

        asset_values_map[aid] = vals
        for d, _ in vals:
            all_dates.add(d)

    if not all_dates:
        return {"assets": asset_meta, "trend": [], "total": 0.0}

    sorted_dates = sorted(all_dates)

    # Build lookup: aid -> {date: value}
    value_lookup: dict[str, dict[date, float]] = {}
    first_date: dict[str, date] = {}
    for aid in [a["id"] for a in asset_meta]:
        value_lookup[aid] = {d: v for d, v in asset_values_map[aid]}
        if asset_values_map[aid]:
            first_date[aid] = asset_values_map[aid][0][0]

    # Build trend with fill-forward; 0 before first date (for stacking)
    trend = []
    last_known: dict[str, float] = {}
    for aid in [a["id"] for a in asset_meta]:
        last_known[aid] = 0.0

    for d in sorted_dates:
        row: dict[str, object] = {"date": d.isoformat()}
        date_total = 0.0
        for aid in [a["id"] for a in asset_meta]:
            if d in value_lookup[aid]:
                last_known[aid] = value_lookup[aid][d]
            # Use 0 before asset exists (stacking needs numeric values)
            if aid in first_date and d >= first_date[aid]:
                val = round(last_known[aid], 2)
            else:
                val = 0
            row[aid] = val
            date_total += val
        row["_total"] = round(date_total, 2)
        trend.append(row)

    # Total = sum of last known values
    total = sum(last_known[a["id"]] for a in asset_meta)

    return {"assets": asset_meta, "trend": trend, "total": round(total, 2)}


async def get_total_asset_value(
    session: AsyncSession, user_id: uuid.UUID
) -> dict[str, float]:
    """Get total asset value grouped by currency (for dashboard).
    Only includes non-archived assets that haven't been sold."""
    result = await session.execute(
        select(Asset).where(
            Asset.user_id == user_id,
            Asset.is_archived == False,
            Asset.sell_date.is_(None),
        )
    )
    assets = list(result.scalars().all())

    totals: dict[str, float] = {}
    for asset in assets:
        latest = await _get_latest_value(session, asset.id)
        current = _compute_current_value(asset, latest)
        if current is not None:
            totals[asset.currency] = totals.get(asset.currency, 0.0) + current
    return totals
