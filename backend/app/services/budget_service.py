import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today
from app.models.budget import Budget
from app.models.category import Category
from app.models.category_group import CategoryGroup
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.budget import BudgetCreate, BudgetUpdate, BudgetVsActual
from app.services._query_filters import (
    counts_as_user_pnl,
    owner_split_offset_by_category,
    reporting_date_col,
    user_pnl_expense_amount,
    viewer_shared_spending_by_category,
)
from app.services.admin_service import get_credit_card_accounting_mode
from app.services.dashboard_service import (
    _get_forecast_pnl_rows,
    _get_recurring_projections,
)
from app.services.fx_rate_service import convert
from app.core.config import get_settings


def _primary_amount_expr():
    """Amount in primary currency: uses amount_primary when available, falls back to amount."""
    return func.coalesce(Transaction.amount_primary, Transaction.amount)


async def _build_budget_map(
    session: AsyncSession, workspace_id: uuid.UUID, month_start: date
) -> dict[str, tuple[Decimal, bool]]:
    """Build a map of category_id -> (amount, is_recurring) for the given month.

    Resolution order:
    1. Month-specific override (is_recurring=false, month=M) takes priority
    2. Most recent recurring default (is_recurring=true, month<=M) as fallback
    """
    budget_map: dict[str, tuple[Decimal, bool]] = {}

    # Query 1: Get effective recurring defaults (most recent per category where month <= M)
    # Use a subquery to get the max month per category for recurring budgets
    max_month_subq = (
        select(
            Budget.category_id,
            func.max(Budget.month).label("max_month"),
        )
        .where(
            Budget.workspace_id == workspace_id,
            Budget.is_recurring == True,  # noqa: E712
            Budget.month <= month_start,
        )
        .group_by(Budget.category_id)
        .subquery()
    )

    recurring_result = await session.execute(
        select(Budget)
        .join(
            max_month_subq,
            and_(
                Budget.category_id == max_month_subq.c.category_id,
                Budget.month == max_month_subq.c.max_month,
            ),
        )
        .where(
            Budget.workspace_id == workspace_id,
            Budget.is_recurring == True,  # noqa: E712
        )
    )
    for b in recurring_result.scalars().all():
        budget_map[str(b.category_id)] = (b.amount, True)

    # Query 2: Month-specific overrides (take priority over recurring)
    overrides_result = await session.execute(
        select(Budget).where(
            Budget.workspace_id == workspace_id,
            Budget.is_recurring == False,  # noqa: E712
            Budget.month == month_start,
        )
    )
    for b in overrides_result.scalars().all():
        budget_map[str(b.category_id)] = (b.amount, False)

    return budget_map


async def get_budgets(
    session: AsyncSession, workspace_id: uuid.UUID, month: Optional[date] = None
) -> list[Budget]:
    if not month:
        query = select(Budget).where(Budget.workspace_id == workspace_id)
        result = await session.execute(query.order_by(Budget.month.desc()))
        return list(result.scalars().all())

    month_start = month.replace(day=1)

    # Get month-specific overrides
    overrides_result = await session.execute(
        select(Budget).where(
            Budget.workspace_id == workspace_id,
            Budget.is_recurring == False,  # noqa: E712
            Budget.month == month_start,
        )
    )
    overrides = list(overrides_result.scalars().all())
    override_category_ids = {str(b.category_id) for b in overrides}

    # Get effective recurring defaults for this month
    max_month_subq = (
        select(
            Budget.category_id,
            func.max(Budget.month).label("max_month"),
        )
        .where(
            Budget.workspace_id == workspace_id,
            Budget.is_recurring == True,  # noqa: E712
            Budget.month <= month_start,
        )
        .group_by(Budget.category_id)
        .subquery()
    )

    recurring_result = await session.execute(
        select(Budget)
        .join(
            max_month_subq,
            and_(
                Budget.category_id == max_month_subq.c.category_id,
                Budget.month == max_month_subq.c.max_month,
            ),
        )
        .where(
            Budget.workspace_id == workspace_id,
            Budget.is_recurring == True,  # noqa: E712
        )
    )
    recurring = [
        b for b in recurring_result.scalars().all()
        if str(b.category_id) not in override_category_ids
    ]

    return sorted(overrides + recurring, key=lambda b: b.month, reverse=True)


async def get_budget(
    session: AsyncSession, budget_id: uuid.UUID, workspace_id: uuid.UUID
) -> Optional[Budget]:
    result = await session.execute(
        select(Budget).where(Budget.id == budget_id, Budget.workspace_id == workspace_id)
    )
    return result.scalar_one_or_none()


async def create_budget(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    data: BudgetCreate,
) -> Budget:
    budget = Budget(
        user_id=user_id,
        workspace_id=workspace_id,
        category_id=data.category_id,
        amount=data.amount,
        month=data.month.replace(day=1),
        is_recurring=data.is_recurring,
    )
    session.add(budget)
    await session.commit()
    await session.refresh(budget)
    return budget


async def update_budget(
    session: AsyncSession, budget_id: uuid.UUID, workspace_id: uuid.UUID, data: BudgetUpdate
) -> Optional[Budget]:
    budget = await get_budget(session, budget_id, workspace_id)
    if not budget:
        return None

    if budget.is_recurring and data.effective_month:
        effective = data.effective_month.replace(day=1)
        if effective != budget.month:
            # Create a new recurring record with new effective-from month
            new_budget = Budget(
                user_id=budget.user_id,
                workspace_id=budget.workspace_id,
                category_id=budget.category_id,
                amount=data.amount if data.amount is not None else budget.amount,
                month=effective,
                is_recurring=True,
            )
            session.add(new_budget)
            await session.commit()
            await session.refresh(new_budget)
            return new_budget

    # Update in place (non-recurring, or same effective-from month)
    for key, value in data.model_dump(exclude_unset=True, exclude={"effective_month"}).items():
        setattr(budget, key, value)

    await session.commit()
    await session.refresh(budget)
    return budget


async def delete_budget(
    session: AsyncSession, budget_id: uuid.UUID, workspace_id: uuid.UUID
) -> bool:
    budget = await get_budget(session, budget_id, workspace_id)
    if not budget:
        return False

    await session.delete(budget)
    await session.commit()
    return True


async def _category_spending(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    start: date,
    end: date,
    *,
    accounting_mode: str,
    primary_currency: str,
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """Per-category (actual, actual plus forecast) spending for [start, end),
    in primary currency.

    Both stay signed through every layer and are left for the caller to
    clamp: a category that nets to zero or less on its own rows can still
    carry spending once the others' share of a shared refund comes back out,
    and a negative net has to absorb the viewer's shares and the forecast
    rather than be reset to zero before them.
    """
    report_date = reporting_date_col(accounting_mode)
    use_effective_date = accounting_mode == "accrual"

    # Same spending definition as the dashboard, so a budget bar and the
    # category's spending figure never disagree about a credit.
    result = await session.execute(
        select(
            Transaction.category_id,
            func.sum(user_pnl_expense_amount(_primary_amount_expr())),
        )
        .where(
            Transaction.workspace_id == workspace_id,
            report_date >= start,
            report_date < end,
            Transaction.category_id.isnot(None),
            report_date <= app_today(),
            Transaction.status == "posted",
            counts_as_user_pnl(),
        )
        .group_by(Transaction.category_id)
    )
    actual: dict[str, Decimal] = {
        str(cat_uuid): net or Decimal("0") for cat_uuid, net in result.all()
    }

    # Only the user's share of their own splits counts.
    own_offset = await owner_split_offset_by_category(
        session, user_id, start, end,
        use_effective_date=use_effective_date,
        primary_currency=primary_currency,
        workspace_id=workspace_id,
    )
    for cat_uuid, total in own_offset.items():
        if cat_uuid is None:
            continue
        cat_id = str(cat_uuid)
        if cat_id in actual:
            actual[cat_id] -= Decimal(str(total))

    # Concert tickets paid by a friend are still the user's expense in the
    # matching category.
    shared_by_cat = await viewer_shared_spending_by_category(
        session, user_id, start, end,
        use_effective_date=use_effective_date,
        primary_currency=primary_currency,
    )
    for cat_uuid, total in shared_by_cat.items():
        if cat_uuid is None:
            continue
        cat_id = str(cat_uuid)
        actual[cat_id] = actual.get(cat_id, Decimal("0")) + Decimal(str(total))

    projected = dict(actual)
    for proj in await _get_recurring_projections(session, workspace_id, start, end):
        if proj["pnl_group"] != "expenses" or not proj["category_id"]:
            continue
        cat_id = str(proj["category_id"])
        converted, _ = await convert(
            session, Decimal(str(proj["amount"])), proj["currency"], primary_currency,
        )
        projected[cat_id] = (
            projected.get(cat_id, Decimal("0")) + converted * proj["expense_share"]
        )

    for row in await _get_forecast_pnl_rows(
        session, workspace_id, start, end, range_date_col=report_date,
    ):
        tx = row.transaction
        if not tx.category_id or not row.expense_share:
            continue
        cat_id = str(tx.category_id)
        magnitude = await row.magnitude_primary(session, primary_currency)
        projected[cat_id] = (
            projected.get(cat_id, Decimal("0")) + magnitude * row.expense_share
        )

    return actual, projected


async def get_budget_vs_actual(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    month: Optional[date] = None,
) -> list[BudgetVsActual]:
    if not month:
        month = app_today().replace(day=1)

    month_start = month.replace(day=1)
    if month.month == 12:
        month_end = month.replace(year=month.year + 1, month=1, day=1)
    else:
        month_end = month.replace(month=month.month + 1, day=1)

    # Previous month range
    if month_start.month == 1:
        prev_month_start = month_start.replace(year=month_start.year - 1, month=12)
    else:
        prev_month_start = month_start.replace(month=month_start.month - 1)
    prev_month_end = month_start

    # Get all categories for this workspace with their groups
    cats_result = await session.execute(
        select(Category, CategoryGroup)
        .outerjoin(CategoryGroup, Category.group_id == CategoryGroup.id)
        .where(Category.workspace_id == workspace_id)
    )
    all_categories = cats_result.all()

    if not all_categories:
        return []

    # Get budgets for this month (with recurring resolution)
    budget_map = await _build_budget_map(session, workspace_id, month_start)

    # Get user's primary currency for FX conversion + reporting mode
    user = await session.get(User, user_id)
    primary_currency = user.primary_currency if user else get_settings().default_currency
    accounting_mode = await get_credit_card_accounting_mode(session)

    spending_map, projected_spending_map = await _category_spending(
        session, workspace_id, user_id, month_start, month_end,
        accounting_mode=accounting_mode, primary_currency=primary_currency,
    )
    # Same layers for the previous month so the trend comparison is
    # apples-to-apples.
    prev_spending_map, projected_prev_spending_map = await _category_spending(
        session, workspace_id, user_id, prev_month_start, prev_month_end,
        accounting_mode=accounting_mode, primary_currency=primary_currency,
    )

    def _bar(net_map: dict[str, Decimal], cat_id: str) -> Decimal:
        # A budget bar has no meaning below zero, so money back that
        # outweighs the spending reads as nothing spent.
        return max(net_map.get(cat_id, Decimal("0")), Decimal("0"))

    comparisons = []
    for category, group in all_categories:
        cat_id = str(category.id)
        actual = _bar(spending_map, cat_id)
        projected = _bar(projected_spending_map, cat_id)
        prev_actual = _bar(prev_spending_map, cat_id)
        projected_prev = _bar(projected_prev_spending_map, cat_id)
        budget_entry = budget_map.get(cat_id)
        budget_amount = budget_entry[0] if budget_entry else None
        is_recurring = budget_entry[1] if budget_entry else False

        # Skip categories with no spending in either month and no budget
        if actual == 0 and projected == 0 and prev_actual == 0 and projected_prev == 0 and budget_amount is None:
            continue

        percentage = None
        if budget_amount and budget_amount > 0:
            percentage = round(float(projected / budget_amount * 100), 1)

        comparisons.append(BudgetVsActual(
            category_id=category.id,
            category_name=category.name,
            category_icon=category.icon,
            category_color=category.color,
            group_id=group.id if group else None,
            group_name=group.name if group else None,
            budget_amount=budget_amount,
            actual_amount=actual,
            projected_amount=projected,
            prev_month_amount=prev_actual,
            projected_prev_month_amount=projected_prev,
            percentage_used=percentage,
            is_recurring=is_recurring,
        ))

    return sorted(comparisons, key=lambda x: float(x.actual_amount), reverse=True)
