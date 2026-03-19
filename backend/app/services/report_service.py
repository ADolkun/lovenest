import uuid
from datetime import date, timedelta

from sqlalchemy import String, select, desc, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_value import AssetValue
from app.models.transaction import Transaction
from app.models.category import Category
from app.schemas.report import (
    CategoryTrendItem,
    ReportBreakdown,
    ReportCompositionItem,
    ReportDataPoint,
    ReportMeta,
    ReportResponse,
    ReportSummary,
)
from app.services.dashboard_service import _get_open_accounts, _account_balance_at


async def _asset_value_at(
    session: AsyncSession, user_id: uuid.UUID, cutoff: date
) -> float:
    """Sum of all active (non-archived, non-sold) asset values at a given date.

    For each asset, finds the most recent AssetValue with date <= cutoff.
    Falls back to purchase_price if no value entries exist before the cutoff
    (but only if purchase_date <= cutoff or purchase_date is None).
    """
    result = await session.execute(
        select(Asset).where(
            Asset.user_id == user_id,
            Asset.is_archived == False,
            Asset.sell_date.is_(None),
        )
    )
    assets = list(result.scalars().all())

    total = 0.0
    for asset in assets:
        # Find most recent value entry at or before the cutoff
        val_result = await session.execute(
            select(AssetValue.amount)
            .where(
                AssetValue.asset_id == asset.id,
                AssetValue.date <= cutoff,
            )
            .order_by(desc(AssetValue.date), desc(AssetValue.id))
            .limit(1)
        )
        row = val_result.scalar_one_or_none()
        if row is not None:
            total += float(row)
        elif asset.purchase_price is not None:
            # Fall back to purchase_price if the asset existed by cutoff
            if asset.purchase_date is None or asset.purchase_date <= cutoff:
                total += float(asset.purchase_price)

    return total


async def _net_worth_at(
    session: AsyncSession, user_id: uuid.UUID, cutoff: date
) -> ReportDataPoint:
    """Compute a single net worth snapshot at a given date."""
    accounts = await _get_open_accounts(session, user_id)

    accounts_total = 0.0
    liabilities_total = 0.0

    for account in accounts:
        bal = await _account_balance_at(session, account, cutoff)
        if account.type == "credit_card":
            # _account_balance_at already negates credit_card; show as positive liability
            liabilities_total += abs(bal)
        else:
            accounts_total += bal

    assets_total = await _asset_value_at(session, user_id, cutoff)
    net_worth = accounts_total + assets_total - liabilities_total

    return ReportDataPoint(
        date=cutoff.isoformat(),
        value=round(net_worth, 2),
        breakdowns={
            "accounts": round(accounts_total, 2),
            "assets": round(assets_total, 2),
            "liabilities": round(liabilities_total, 2),
        },
    )


def _format_date_label(d: date, interval: str) -> str:
    """Format a date point based on interval granularity."""
    if interval == "daily":
        return d.isoformat()
    elif interval == "weekly":
        iso_year, iso_week, _ = d.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    elif interval == "monthly":
        return f"{d.year}-{d.month:02d}"
    elif interval == "yearly":
        return str(d.year)
    return d.isoformat()


def _date_points(
    start: date, end: date, interval: str
) -> list[date]:
    """Generate date points between start and end for the given interval."""
    points: list[date] = []
    current = start

    if interval == "daily":
        while current <= end:
            points.append(current)
            current += timedelta(days=1)
    elif interval == "weekly":
        while current <= end:
            points.append(current)
            current += timedelta(weeks=1)
    elif interval == "monthly":
        while current <= end:
            points.append(current)
            # Advance by one month
            month = current.month + 1
            year = current.year
            if month > 12:
                month = 1
                year += 1
            day = min(current.day, 28)
            current = date(year, month, day)
    elif interval == "yearly":
        while current <= end:
            points.append(current)
            current = date(current.year + 1, current.month, current.day)
    else:
        # Default to monthly
        return _date_points(start, end, "monthly")

    # Ensure the last point uses `end` so the final snapshot reflects today's data.
    # If the last generated point is in the same period as `end`, replace it;
    # otherwise append `end` as a new point.
    if points and points[-1] < end:
        if _format_date_label(points[-1], interval) == _format_date_label(end, interval):
            points[-1] = end  # same label, use today's cutoff
        else:
            points.append(end)

    return points


async def get_net_worth_report(
    session: AsyncSession,
    user_id: uuid.UUID,
    months: int = 12,
    interval: str = "monthly",
    currency: str = "BRL",
) -> ReportResponse:
    """Build a full ReportResponse for net worth over time."""
    today = date.today()
    start = date(today.year, today.month, 1) - timedelta(days=months * 30)
    start = start.replace(day=1)  # Align to month start

    points = _date_points(start, today, interval)

    # Compute snapshot at each date point
    trend: list[ReportDataPoint] = []
    for point in points:
        dp = await _net_worth_at(session, user_id, point)
        dp.date = _format_date_label(point, interval)
        trend.append(dp)

    # Current snapshot (last point) and previous (first point) for summary
    current = trend[-1] if trend else ReportDataPoint(
        date="", value=0, breakdowns={"accounts": 0, "assets": 0, "liabilities": 0}
    )
    previous = trend[0] if len(trend) > 1 else current

    change_amount = current.value - previous.value
    change_percent = (
        (change_amount / abs(previous.value) * 100)
        if previous.value != 0
        else None
    )

    summary = ReportSummary(
        primary_value=current.value,
        change_amount=round(change_amount, 2),
        change_percent=round(change_percent, 2) if change_percent is not None else None,
        breakdowns=[
            ReportBreakdown(
                key="accounts",
                label="Accounts",
                value=current.breakdowns.get("accounts", 0),
                color="#6366F1",
            ),
            ReportBreakdown(
                key="assets",
                label="Assets",
                value=current.breakdowns.get("assets", 0),
                color="#F59E0B",
            ),
            ReportBreakdown(
                key="liabilities",
                label="Liabilities",
                value=current.breakdowns.get("liabilities", 0),
                color="#F43F5E",
            ),
        ],
    )

    meta = ReportMeta(
        type="net_worth",
        series_keys=["accounts", "assets", "liabilities"],
        currency=currency,
        interval=interval,
    )

    # Build per-item composition from current snapshot
    account_type_colors = {
        "checking": "#6366F1",
        "savings": "#3B82F6",
        "credit_card": "#F43F5E",
        "investment": "#8B5CF6",
        "wallet": "#F59E0B",
    }
    asset_type_colors = {
        "real_estate": "#0EA5E9",
        "vehicle": "#14B8A6",
        "valuable": "#F59E0B",
        "investment": "#8B5CF6",
        "other": "#6B7280",
    }
    composition: list[ReportCompositionItem] = []
    accounts = await _get_open_accounts(session, user_id)
    for account in accounts:
        bal = await _account_balance_at(session, account, today)
        if account.type == "credit_card":
            composition.append(ReportCompositionItem(
                key=str(account.id),
                label=account.name,
                value=round(abs(bal), 2),
                color=account_type_colors.get(account.type, "#6B7280"),
                group="liabilities",
            ))
        else:
            if bal > 0:
                composition.append(ReportCompositionItem(
                    key=str(account.id),
                    label=account.name,
                    value=round(bal, 2),
                    color=account_type_colors.get(account.type, "#6B7280"),
                    group="accounts",
                ))

    # Assets
    asset_result = await session.execute(
        select(Asset).where(
            Asset.user_id == user_id,
            Asset.is_archived == False,
            Asset.sell_date.is_(None),
        )
    )
    for asset in asset_result.scalars().all():
        val_result = await session.execute(
            select(AssetValue.amount)
            .where(AssetValue.asset_id == asset.id, AssetValue.date <= today)
            .order_by(desc(AssetValue.date), desc(AssetValue.id))
            .limit(1)
        )
        val = val_result.scalar_one_or_none()
        if val is not None:
            amount = float(val)
        elif asset.purchase_price is not None and (
            asset.purchase_date is None or asset.purchase_date <= today
        ):
            amount = float(asset.purchase_price)
        else:
            amount = 0.0
        if amount > 0:
            composition.append(ReportCompositionItem(
                key=str(asset.id),
                label=asset.name,
                value=round(amount, 2),
                color=asset_type_colors.get(asset.type, "#6B7280"),
                group="assets",
            ))

    return ReportResponse(summary=summary, trend=trend, meta=meta, composition=composition)


def _interval_label_expr(interval: str):
    """SQL expression that groups transaction dates into interval buckets."""
    if interval == "daily":
        return func.to_char(Transaction.date, 'YYYY-MM-DD')
    elif interval == "weekly":
        return func.concat(
            func.extract('isoyear', Transaction.date).cast(String),
            '-W',
            func.lpad(func.extract('week', Transaction.date).cast(String), 2, '0'),
        )
    elif interval == "yearly":
        return func.to_char(Transaction.date, 'YYYY')
    else:  # monthly (default)
        return func.to_char(Transaction.date, 'YYYY-MM')


async def get_income_expenses_report(
    session: AsyncSession,
    user_id: uuid.UUID,
    months: int = 12,
    interval: str = "monthly",
    currency: str = "BRL",
) -> ReportResponse:
    """Build a ReportResponse for income vs expenses over time."""
    today = date.today()
    start = date(today.year, today.month, 1) - timedelta(days=months * 30)
    start = start.replace(day=1)

    label_expr = _interval_label_expr(interval).label('period')

    result = await session.execute(
        select(
            label_expr,
            func.sum(case((Transaction.type == "credit", Transaction.amount), else_=0)),
            func.sum(case((Transaction.type == "debit", Transaction.amount), else_=0)),
        )
        .join(Account, Transaction.account_id == Account.id)
        .where(
            Transaction.user_id == user_id,
            Account.is_closed == False,
            Transaction.date >= start,
            Transaction.date <= today,
            Transaction.source != "opening_balance",
            Transaction.transfer_pair_id.is_(None),
        )
        .group_by(label_expr)
        .order_by(label_expr)
    )

    # Build data map from query results
    data_map: dict[str, tuple[float, float]] = {}
    for row in result.all():
        income = float(row[1] or 0)
        expenses = abs(float(row[2] or 0))
        data_map[row[0]] = (income, expenses)

    # Generate all expected date points and map to results
    points = _date_points(start, today, interval)
    trend: list[ReportDataPoint] = []
    total_income = 0.0
    total_expenses = 0.0

    for point in points:
        label = _format_date_label(point, interval)
        income, expenses = data_map.get(label, (0.0, 0.0))
        net = round(income - expenses, 2)
        total_income += income
        total_expenses += expenses
        trend.append(ReportDataPoint(
            date=label,
            value=net,
            breakdowns={
                "income": round(income, 2),
                "expenses": round(expenses, 2),
            },
        ))

    total_net = round(total_income - total_expenses, 2)

    # Compare last point vs first point net income
    current_net = trend[-1].value if trend else 0.0
    previous_net = trend[0].value if len(trend) > 1 else 0.0
    change_amount = current_net - previous_net
    change_percent = (
        (change_amount / abs(previous_net) * 100)
        if previous_net != 0
        else None
    )

    summary = ReportSummary(
        primary_value=total_net,
        change_amount=round(change_amount, 2),
        change_percent=round(change_percent, 2) if change_percent is not None else None,
        breakdowns=[
            ReportBreakdown(
                key="income",
                label="Income",
                value=round(total_income, 2),
                color="#10B981",
            ),
            ReportBreakdown(
                key="expenses",
                label="Expenses",
                value=round(total_expenses, 2),
                color="#F43F5E",
            ),
            ReportBreakdown(
                key="netIncome",
                label="Net Income",
                value=total_net,
                color="#6366F1",
            ),
        ],
    )

    meta = ReportMeta(
        type="income_expenses",
        series_keys=["income", "expenses"],
        currency=currency,
        interval=interval,
    )

    # Build per-category composition for the full date range
    cat_result = await session.execute(
        select(
            Category.id,
            Category.name,
            Category.color,
            Transaction.type,
            func.sum(Transaction.amount),
        )
        .select_from(Transaction)
        .join(Account, Transaction.account_id == Account.id)
        .outerjoin(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.user_id == user_id,
            Account.is_closed == False,
            Transaction.date >= start,
            Transaction.date <= today,
            Transaction.source != "opening_balance",
            Transaction.transfer_pair_id.is_(None),
        )
        .group_by(Category.id, Category.name, Category.color, Transaction.type)
    )
    composition: list[ReportCompositionItem] = []
    for row in cat_result.all():
        cat_id, cat_name, cat_color, txn_type, total_amount = row
        amount = abs(float(total_amount or 0))
        if amount <= 0:
            continue
        composition.append(ReportCompositionItem(
            key=str(cat_id) if cat_id else "uncategorized",
            label=cat_name if cat_name else "Uncategorized",
            value=round(amount, 2),
            color=cat_color if cat_color else "#6B7280",
            group="income" if txn_type == "credit" else "expenses",
        ))

    # Build per-category trend (sparklines) for the full date range
    cat_trend_result = await session.execute(
        select(
            label_expr,
            Category.id,
            Category.name,
            Category.color,
            Transaction.type,
            func.sum(Transaction.amount),
        )
        .select_from(Transaction)
        .join(Account, Transaction.account_id == Account.id)
        .outerjoin(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.user_id == user_id,
            Account.is_closed == False,
            Transaction.date >= start,
            Transaction.date <= today,
            Transaction.source != "opening_balance",
            Transaction.transfer_pair_id.is_(None),
        )
        .group_by(label_expr, Category.id, Category.name, Category.color, Transaction.type)
    )

    # Collect into dict[(cat_key, group)] -> {label, color, total, periods}
    cat_trend_map: dict[tuple[str, str], dict] = {}
    for row in cat_trend_result.all():
        period_label, cat_id, cat_name, cat_color, txn_type, total_amount = row
        amount = abs(float(total_amount or 0))
        if amount <= 0:
            continue
        cat_key = str(cat_id) if cat_id else "uncategorized"
        group = "income" if txn_type == "credit" else "expenses"
        map_key = (cat_key, group)
        if map_key not in cat_trend_map:
            cat_trend_map[map_key] = {
                "label": cat_name if cat_name else "Uncategorized",
                "color": cat_color if cat_color else "#6B7280",
                "total": 0.0,
                "periods": {},
            }
        cat_trend_map[map_key]["total"] += amount
        cat_trend_map[map_key]["periods"][period_label] = (
            cat_trend_map[map_key]["periods"].get(period_label, 0.0) + amount
        )

    # Build period labels from the same points used by the trend
    period_labels = [_format_date_label(p, interval) for p in points]

    # Top 6 + Other per group
    category_trend: list[CategoryTrendItem] = []
    for group in ("expenses", "income"):
        group_items = [
            (k, v) for (k, g), v in cat_trend_map.items() if g == group
        ]
        group_items.sort(key=lambda x: x[1]["total"], reverse=True)
        top = group_items[:6]
        rest = group_items[6:]

        for cat_key, info in top:
            series = [
                ReportDataPoint(
                    date=pl,
                    value=round(info["periods"].get(pl, 0.0), 2),
                    breakdowns={},
                )
                for pl in period_labels
            ]
            category_trend.append(CategoryTrendItem(
                key=cat_key,
                label=info["label"],
                color=info["color"],
                total=round(info["total"], 2),
                group=group,
                series=series,
            ))

        if rest:
            other_total = sum(v["total"] for _, v in rest)
            other_periods: dict[str, float] = {}
            for _, v in rest:
                for pl, amt in v["periods"].items():
                    other_periods[pl] = other_periods.get(pl, 0.0) + amt
            series = [
                ReportDataPoint(
                    date=pl,
                    value=round(other_periods.get(pl, 0.0), 2),
                    breakdowns={},
                )
                for pl in period_labels
            ]
            category_trend.append(CategoryTrendItem(
                key="other",
                label="Other",
                color="#6B7280",
                total=round(other_total, 2),
                group=group,
                series=series,
            ))

    return ReportResponse(
        summary=summary, trend=trend, meta=meta,
        composition=composition, category_trend=category_trend,
    )
