"""Income & expense statement: net amounts per category, month by month.

Each category group (or ungrouped category) becomes one row holding its net
amount (income minus expense) for the selected month and the months before
it. Income and expense are the user-P&L amounts the dashboard and reports
total (`user_pnl_income_amount` / `user_pnl_expense_amount`), so a refund
lowers the spending of the category it is filed under, a reimbursement link
takes its amount out of both rows it joins (Lovenest ADR 0012), and the
statement adds up to the same income minus expense. A row lands in the
income or the expense section by the sign of its all-time net, which keeps
it in the same place as the user steps through months. Uncategorized money
has no such history to lean on, so it is split into one row per section by
how P&L classifies it.
"""

import uuid
from datetime import date
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today
from app.models.account import Account
from app.models.category import Category
from app.models.category_group import CategoryGroup
from app.models.transaction import Transaction
from app.schemas.report import (
    CategoryStatementResponse,
    CategoryStatementRow,
    CategoryStatementSection,
)
from app.services._query_filters import (
    counts_as_user_pnl,
    line_category_id,
    line_primary_or_amount,
    reporting_date_col,
    user_pnl_expense_amount,
    user_pnl_income_amount,
    viewer_share_of_row,
    viewer_shared_spending_by_category,
    with_category_lines,
)
from app.services.admin_service import get_credit_card_accounting_mode

UNCATEGORIZED_KEY = "uncategorized"

# {category_id or None: (income, expense)}; an expense goes negative when
# spending offsets outweigh the spending.
_MonthTotals = dict[Optional[uuid.UUID], tuple[float, float]]


def _shift_month(month_start: date, delta: int) -> date:
    index = month_start.year * 12 + month_start.month - 1 + delta
    return date(index // 12, index % 12 + 1, 1)


def _sum_values(rows: list[CategoryStatementRow], width: int) -> list[float]:
    return [round(sum(row.values[i] for row in rows), 2) for i in range(width)]


def _pnl_sums(amount_expr, category_id=None):
    return (
        func.sum(user_pnl_income_amount(amount_expr, category_id)),
        func.sum(user_pnl_expense_amount(amount_expr, category_id)),
    )


def _totals(rows) -> _MonthTotals:
    return {
        cat_id: (float(income or 0), float(expense or 0))
        for cat_id, income, expense in rows
    }


async def get_category_statement(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    month: date,
    months: int = 3,
    primary_currency: str = "USD",
    account_ids: Optional[list[uuid.UUID]] = None,
) -> CategoryStatementResponse:
    """Build the statement for ``month`` and the ``months - 1`` months before it.

    Posted transactions only, bucketed by the same reporting date as the other
    reports, and never past today. Closed accounts keep their history here,
    as in the other period reports. A group-split row counts only the
    viewer's part of it (`viewer_share_of_row`), and someone else's split
    from outside the workspace adds the viewer's share. Both are skipped when
    the report is scoped to some accounts.
    """
    filtered = account_ids is not None
    acct_filter = [Transaction.account_id.in_(account_ids)] if filtered else []
    accounting_mode = await get_credit_card_accounting_mode(session)
    report_date = reporting_date_col(accounting_mode)
    today = app_today()

    selected = month.replace(day=1)
    month_starts = [_shift_month(selected, -i) for i in range(months)]
    base_filters = [
        Transaction.workspace_id == workspace_id,
        Transaction.source != "opening_balance",
        Transaction.status == "posted",
        report_date <= today,
        *acct_filter,
    ]
    # The statement's sums go part by part, so a row split across
    # categories lands in each of them.
    line_sums = _pnl_sums(
        line_primary_or_amount() * (1 if filtered else viewer_share_of_row(user_id)),
        line_category_id(),
    )
    line_filters = [*base_filters, counts_as_user_pnl(line_category_id())]

    cats_result = await session.execute(
        select(Category, CategoryGroup)
        .outerjoin(CategoryGroup, Category.group_id == CategoryGroup.id)
        .where(Category.workspace_id == workspace_id)
    )
    categories = cats_result.all()
    known_ids = {cat.id for cat, _ in categories}

    per_month: list[_MonthTotals] = []
    for m_start in month_starts:
        totals: _MonthTotals = {}
        if m_start > today:
            per_month.append(totals)
            continue
        m_end = _shift_month(m_start, 1)
        result = await session.execute(
            with_category_lines(
                select(line_category_id(), *line_sums).select_from(Transaction)
            )
            .join(Account, Transaction.account_id == Account.id)
            .where(*line_filters, report_date >= m_start, report_date < m_end)
            .group_by(line_category_id())
        )
        totals.update(_totals(result.all()))

        if not filtered:
            shared = await viewer_shared_spending_by_category(
                session, user_id, m_start, m_end,
                use_effective_date=accounting_mode == "accrual",
                primary_currency=primary_currency,
                workspace_id=workspace_id,
            )
            for cat_id, share in shared.items():
                # Shares of someone else's transaction carry that person's
                # category, which this workspace can't show.
                key = cat_id if cat_id in known_ids else None
                income, expense = totals.get(key, (0.0, 0.0))
                totals[key] = (income, expense + float(share))
        per_month.append(totals)

    # All-time net per category, up to the end of the selected month, decides
    # which section each row belongs to. It follows the same split rules as
    # the monthly values, so a row sits in the section its numbers point to.
    lifetime_end = _shift_month(selected, 1)
    lifetime_result = await session.execute(
        with_category_lines(
            select(line_category_id(), *line_sums).select_from(Transaction)
        )
        .join(Account, Transaction.account_id == Account.id)
        .where(
            *line_filters,
            line_category_id().isnot(None),
            report_date < lifetime_end,
        )
        .group_by(line_category_id())
    )
    lifetime_net = {
        cat_id: income - expense
        for cat_id, (income, expense) in _totals(lifetime_result.all()).items()
    }
    if not filtered:
        shared = await viewer_shared_spending_by_category(
            session, user_id, date.min, lifetime_end,
            use_effective_date=accounting_mode == "accrual",
            primary_currency=primary_currency,
            workspace_id=workspace_id,
        )
        for cat_id, share in shared.items():
            if cat_id in known_ids:
                lifetime_net[cat_id] = lifetime_net.get(cat_id, 0.0) - float(share)

    def category_values(cat_id: uuid.UUID) -> list[float]:
        values = []
        for totals in per_month:
            income, expense = totals.get(cat_id, (0.0, 0.0))
            values.append(round(income - expense, 2))
        return values

    # Group categories into rows: one per group, one per ungrouped category.
    grouped: dict[uuid.UUID, tuple[CategoryGroup, list[Category]]] = {}
    ungrouped: list[Category] = []
    for cat, group in categories:
        if group is None:
            ungrouped.append(cat)
        else:
            grouped.setdefault(group.id, (group, []))[1].append(cat)

    income_rows: list[CategoryStatementRow] = []
    expense_rows: list[CategoryStatementRow] = []

    def place(row: CategoryStatementRow, cat_ids: list[uuid.UUID]) -> None:
        net = sum(lifetime_net.get(cat_id, 0.0) for cat_id in cat_ids)
        (income_rows if net > 0 else expense_rows).append(row)

    for group, cats in sorted(
        grouped.values(), key=lambda item: (item[0].position, item[0].name.lower())
    ):
        children = []
        for cat in sorted(cats, key=lambda c: c.name.lower()):
            values = category_values(cat.id)
            if any(values):
                children.append(
                    CategoryStatementRow(
                        key=str(cat.id), kind="category", label=cat.name,
                        icon=cat.icon, color=cat.color,
                        category_ids=[str(cat.id)], values=values,
                    )
                )
        if not children:
            continue
        place(
            CategoryStatementRow(
                key=str(group.id), kind="group", label=group.name,
                icon=group.icon, color=group.color,
                category_ids=[str(cat.id) for cat in cats],
                values=_sum_values(children, months),
                children=children,
            ),
            [cat.id for cat in cats],
        )

    for cat in sorted(ungrouped, key=lambda c: c.name.lower()):
        values = category_values(cat.id)
        if any(values):
            place(
                CategoryStatementRow(
                    key=str(cat.id), kind="category", label=cat.name,
                    icon=cat.icon, color=cat.color,
                    category_ids=[str(cat.id)], values=values,
                ),
                [cat.id],
            )

    uncategorized_income = [round(t.get(None, (0.0, 0.0))[0], 2) for t in per_month]
    # `or 0.0` turns the -0.0 of an empty month back into a plain zero.
    uncategorized_expense = [-round(t.get(None, (0.0, 0.0))[1], 2) or 0.0 for t in per_month]
    if any(uncategorized_income):
        income_rows.append(
            CategoryStatementRow(
                key=UNCATEGORIZED_KEY, kind="uncategorized", label=None,
                txn_type="credit", values=uncategorized_income,
            )
        )
    if any(uncategorized_expense):
        expense_rows.append(
            CategoryStatementRow(
                key=UNCATEGORIZED_KEY, kind="uncategorized", label=None,
                txn_type="debit", values=uncategorized_expense,
            )
        )

    return CategoryStatementResponse(
        currency=primary_currency,
        months=[m.strftime("%Y-%m") for m in month_starts],
        income=CategoryStatementSection(
            totals=_sum_values(income_rows, months), rows=income_rows
        ),
        expenses=CategoryStatementSection(
            totals=_sum_values(expense_rows, months), rows=expense_rows
        ),
    )
