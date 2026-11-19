"""Every income and expense total reads the same P&L.

The dashboard card, the income/expense report, the category statement and
the cash-flow forecast's spending burn are built by different queries. On a
month holding every case the fork classifies on its own terms they must
still give the same income and the same expenses:

- a credit in an income category is income (fork ADR 0011),
- a credit in a spending category, and an uncategorized card refund, lower
  spending instead (fork ADR 0011),
- a reimbursement linked to the expense it repays leaves both (fork ADR 0012),
- a row split across categories counts part by part,
- a valid transfer pair counts for nothing,
- a group-split expense counts only its owner's share.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.group import Group, GroupMember
from app.models.transaction import Transaction
from app.models.transaction_category_line import TransactionCategoryLine
from app.models.transaction_reimbursement import TransactionReimbursement
from app.models.transaction_split import TransactionSplit
from app.services import dashboard_service, report_service
from app.services.cashflow_forecast_service import generate_cashflow_forecast
from app.services.category_statement_service import get_category_statement

MONTH = date(2025, 2, 1)
INCOME = 5000.0
# 300 - 50 + 200 - 80 + (400 - 300) + 100 + 120 / 2
EXPENSES = 630.0


@pytest.fixture
async def month_of_cases(session: AsyncSession, test_user, test_workspace, test_categories):
    food, transport, salary = test_categories
    ws = test_workspace.id

    def account(name, type_):
        return Account(
            id=uuid.uuid4(), user_id=test_user.id, workspace_id=ws, name=name,
            type=type_, balance=Decimal("0"), currency="BRL",
        )

    checking, card, savings = account("Checking", "checking"), account("Card", "credit_card"), account("Savings", "savings")
    session.add_all([checking, card, savings])
    await session.flush()

    def tx(acct, amount, day, type_, description, category=None, **extra):
        return Transaction(
            id=uuid.uuid4(), user_id=test_user.id, workspace_id=ws, account_id=acct.id,
            category_id=category.id if category else None, description=description,
            amount=Decimal(amount), currency="BRL", date=date(2025, 2, day), type=type_,
            source="manual", status="posted", created_at=datetime.now(timezone.utc), **extra,
        )

    pair_id = uuid.uuid4()
    flight = tx(checking, "400", 6, "debit", "Flight", transport)
    repayment = tx(checking, "300", 20, "credit", "Employer repays flight", salary)
    split = tx(checking, "100", 8, "debit", "Market and fuel", has_category_lines=True)
    shared = tx(checking, "120", 9, "debit", "Dinner", food)
    session.add_all([
        tx(checking, "5000", 5, "credit", "Salary", salary),
        tx(checking, "300", 10, "debit", "Groceries", food),
        tx(checking, "50", 12, "credit", "Grocer refund", food),
        tx(card, "200", 14, "debit", "Store"),
        tx(card, "80", 16, "credit", "Store return"),
        tx(checking, "1000", 18, "debit", "To savings", transfer_pair_id=pair_id),
        tx(savings, "1000", 18, "credit", "From checking", transfer_pair_id=pair_id),
        flight, repayment, split, shared,
    ])
    await session.flush()
    session.add(TransactionReimbursement(
        workspace_id=ws, reimbursement_id=repayment.id, expense_id=flight.id, amount=Decimal("300"),
    ))
    session.add_all([
        TransactionCategoryLine(
            workspace_id=ws, transaction_id=split.id, category_id=food.id,
            amount=Decimal("60"), position=0,
        ),
        TransactionCategoryLine(
            workspace_id=ws, transaction_id=split.id, category_id=transport.id,
            amount=Decimal("40"), position=1,
        ),
    ])
    group = Group(id=uuid.uuid4(), user_id=test_user.id, workspace_id=ws, name="Friends")
    session.add(group)
    await session.flush()
    me = GroupMember(id=uuid.uuid4(), group_id=group.id, workspace_id=ws, name="Me", is_self=True)
    friend = GroupMember(id=uuid.uuid4(), group_id=group.id, workspace_id=ws, name="Friend")
    session.add_all([me, friend])
    await session.flush()
    session.add_all([
        TransactionSplit(transaction_id=shared.id, workspace_id=ws, group_member_id=member.id,
                         share_amount=Decimal("60"))
        for member in (me, friend)
    ])
    await session.commit()


async def test_dashboard_card(session, test_user, test_workspace, month_of_cases):
    summary = await dashboard_service.get_summary(session, test_workspace.id, test_user.id, MONTH)
    assert summary.monthly_income_primary == pytest.approx(INCOME)
    assert summary.monthly_expenses_primary == pytest.approx(EXPENSES)


async def test_income_expense_report(session, test_user, test_workspace, month_of_cases):
    if session.bind.dialect.name == "sqlite":
        # The report buckets by `to_char(date, 'YYYY-MM')`; PostgreSQL has it.
        connection = await session.connection()
        await connection.run_sync(
            lambda conn: conn.connection.dbapi_connection.create_function(
                "to_char", 2, lambda value, _fmt: None if value is None else str(value)[:7]
            )
        )
    report = await report_service.get_income_expenses_report(
        session, test_workspace.id, test_user.id, currency="BRL",
        start_date=MONTH, end_date=date(2025, 2, 28),
    )
    totals = {b.key: b.value for b in report.summary.breakdowns}
    assert totals["income"] == pytest.approx(INCOME)
    assert totals["expenses"] == pytest.approx(EXPENSES)


async def test_category_statement(session, test_user, test_workspace, month_of_cases):
    statement = await get_category_statement(
        session, test_workspace.id, test_user.id, MONTH, months=1, primary_currency="BRL",
    )
    assert statement.income.totals == [pytest.approx(INCOME)]
    assert statement.expenses.totals == [pytest.approx(-EXPENSES)]


async def test_cashflow_forecast_burn(session, test_workspace, month_of_cases):
    """The burn is the spending of the 90 days before the reference date,
    which hold only this month."""
    forecast = await generate_cashflow_forecast(
        session, test_workspace.id, horizon_days=1, reference_date=date(2025, 3, 15),
    )
    expected = (Decimal(str(EXPENSES)) / 90).quantize(Decimal("0.01"))
    assert forecast.daily_trajectory[1].discretionary_burn == expected


async def test_the_statement_and_burn_queries_run_on_postgresql(postgres_sessions):
    """SQLite accepts correlated subqueries PostgreSQL may refuse under
    GROUP BY; plan both queries there."""
    from sqlalchemy import select

    from app.services.recurring_detector_service import pnl_transaction_amounts

    async with postgres_sessions() as session:
        await session.execute(select(pnl_transaction_amounts(uuid.uuid4())))
        statement = await get_category_statement(
            session, uuid.uuid4(), uuid.uuid4(), MONTH, months=1, primary_currency="BRL",
        )
    assert statement.income.rows == [] and statement.expenses.rows == []
