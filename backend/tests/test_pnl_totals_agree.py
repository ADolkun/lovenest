"""Every income and expense total reads the same P&L.

The dashboard card, the income/expense report, the category statement and
the cash-flow forecast's spending burn are built by different queries. On a
month holding every case the fork classifies on its own terms they must
still give the same income and the same expenses:

- a credit in an income category is income (Lovenest ADR 0011),
- a credit in a spending category, and an uncategorized card refund, lower
  spending instead (Lovenest ADR 0011),
- a reimbursement linked to the expense it repays leaves both (Lovenest ADR 0012),
- a row split across categories counts part by part,
- a valid transfer pair counts for nothing,
- a group-split expense counts only its owner's share.

A group-split row is then read from every seat (the payer, a linked member
and a bystander in its workspace, and a linked member in another workspace),
and every reader must give each of them the same figure.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.budget import Budget
from app.models.category import Category
from app.models.group import Group, GroupMember
from app.models.transaction import Transaction
from app.models.transaction_category_line import TransactionCategoryLine
from app.models.transaction_reimbursement import TransactionReimbursement
from app.models.transaction_split import TransactionSplit
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.services import budget_service, dashboard_service, report_service
from app.services.cashflow_forecast_service import generate_cashflow_forecast
from app.services.category_statement_service import get_category_statement
from app.services.transaction_service import get_transactions

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


# The payer pays 120 on Dining, split 60 / 40 / 20 between payer, member and
# outsider, then gets a 30 refund split 15 / 10 / 5. Each seat's expenses:
SPLIT_EXPENSES = {"payer": 45.0, "member": 30.0, "bystander": 90.0, "outsider": 15.0}


async def _shared_workspace_split(session: AsyncSession) -> dict:
    """Seed the split; return {seat: (user_id, workspace_id)} and the shared
    workspace's Dining category under "dining"."""

    def user(name):
        return User(
            id=uuid.uuid4(), email=f"{name}@example.com", hashed_password="x",
            is_active=True, is_superuser=False, is_verified=True,
            preferences={"currency_display": "BRL"},
        )

    payer, member, bystander, outsider = (
        user(name) for name in ("payer", "member", "bystander", "outsider")
    )
    session.add_all([payer, member, bystander, outsider])
    await session.flush()
    shared = Workspace(id=uuid.uuid4(), name="Home", created_by_user_id=payer.id, default_currency="BRL")
    elsewhere = Workspace(
        id=uuid.uuid4(), name="Outsider's", created_by_user_id=outsider.id, default_currency="BRL",
    )
    session.add_all([shared, elsewhere])
    await session.flush()
    session.add_all([
        WorkspaceMember(id=uuid.uuid4(), workspace_id=shared.id, user_id=payer.id, role="owner"),
        WorkspaceMember(id=uuid.uuid4(), workspace_id=shared.id, user_id=member.id, role="editor"),
        WorkspaceMember(id=uuid.uuid4(), workspace_id=shared.id, user_id=bystander.id, role="editor"),
        WorkspaceMember(id=uuid.uuid4(), workspace_id=elsewhere.id, user_id=outsider.id, role="owner"),
    ])
    dining = Category(id=uuid.uuid4(), user_id=payer.id, workspace_id=shared.id, name="Dining")
    checking = Account(
        id=uuid.uuid4(), user_id=payer.id, workspace_id=shared.id, name="Payer's",
        type="checking", balance=Decimal("0"), currency="BRL",
    )
    group = Group(id=uuid.uuid4(), user_id=payer.id, workspace_id=shared.id, name="Dinners")
    session.add_all([dining, checking, group])
    await session.flush()
    members = [
        GroupMember(
            id=uuid.uuid4(), group_id=group.id, workspace_id=shared.id, name=name,
            is_self=is_self, linked_user_id=linked.id,
        )
        for name, is_self, linked in (
            ("Payer", True, payer), ("Member", False, member), ("Outsider", False, outsider),
        )
    ]
    session.add_all(members)
    rows = [
        (
            Transaction(
                id=uuid.uuid4(), user_id=payer.id, workspace_id=shared.id, account_id=checking.id,
                category_id=dining.id, description=f"Dinner {type_}", amount=Decimal(amount),
                amount_primary=Decimal(amount), currency="BRL", date=date(2025, 2, day),
                type=type_, source="manual", status="posted", created_at=datetime.now(timezone.utc),
            ),
            shares,
        )
        for amount, type_, day, shares in (
            ("120", "debit", 8, ("60", "40", "20")),
            ("30", "credit", 18, ("15", "10", "5")),
        )
    ]
    session.add_all([row for row, _ in rows])
    await session.flush()
    session.add_all([
        TransactionSplit(
            transaction_id=row.id, workspace_id=shared.id, group_member_id=split_member.id,
            share_amount=Decimal(share),
        )
        for row, shares in rows
        for split_member, share in zip(members, shares)
    ])
    await session.commit()
    return {
        "payer": (payer.id, shared.id),
        "member": (member.id, shared.id),
        "bystander": (bystander.id, shared.id),
        "outsider": (outsider.id, elsewhere.id),
        "dining": dining.id,
    }


async def _expenses_by_reader(session: AsyncSession, user_id, workspace_id, dining_id) -> dict:
    summary = await dashboard_service.get_summary(session, workspace_id, user_id, MONTH)
    spending = await dashboard_service.get_spending_by_category(
        session, workspace_id, user_id, month=MONTH,
    )
    report = await report_service.get_income_expenses_report(
        session, workspace_id, user_id, currency="BRL",
        start_date=MONTH, end_date=date(2025, 2, 28),
    )
    statement = await get_category_statement(
        session, workspace_id, user_id, MONTH, months=1, primary_currency="BRL",
    )
    forecast = await generate_cashflow_forecast(
        session, workspace_id, horizon_days=1, reference_date=date(2025, 3, 15), user_id=user_id,
    )
    _, _, footer = await get_transactions(
        session, workspace_id, user_id, from_date=MONTH, to_date=date(2025, 2, 28),
        include_summary=True, user_pnl_only=True,
    )
    assert footer is not None
    readers = {
        "dashboard card": summary.monthly_expenses_primary,
        "dashboard card (native)": summary.monthly_expenses,
        "spending by category": sum(row.total for row in spending),
        "income/expense report": {b.key: b.value for b in report.summary.breakdowns}["expenses"],
        "category statement": -(statement.income.totals[0] + statement.expenses.totals[0]),
        "cash-flow burn": float(forecast.daily_trajectory[1].discretionary_burn),
        "transactions footer": float(footer["expense"]),
    }
    # Another workspace's category has no budget row in the viewer's own.
    dining = await session.get(Category, dining_id)
    if dining is not None and dining.workspace_id == workspace_id:
        session.add(Budget(
            user_id=user_id, workspace_id=workspace_id, category_id=dining_id,
            amount=Decimal("500"), month=MONTH,
        ))
        await session.flush()
        rows = await budget_service.get_budget_vs_actual(session, workspace_id, user_id, month=MONTH)
        readers["budget actual"] = float(
            next(row for row in rows if row.category_id == dining_id).actual_amount
        )
        await session.rollback()
    return readers


def _expected(readers: dict, expenses: float) -> dict:
    """The burn is the 90-day window's spending a day, to the cent."""
    burn = float((Decimal(str(expenses)) / 90).quantize(Decimal("0.01")))
    return {
        name: pytest.approx(burn if name == "cash-flow burn" else expenses)
        for name in readers
    }


@pytest.mark.parametrize("seat", list(SPLIT_EXPENSES))
async def test_a_split_reads_the_viewers_part_in_every_reader(session, clean_db, seat):
    connection = await session.connection()
    await connection.run_sync(
        lambda conn: conn.connection.dbapi_connection.create_function(
            "to_char", 2, lambda value, _fmt: None if value is None else str(value)[:7]
        )
    )
    seats = await _shared_workspace_split(session)
    user_id, workspace_id = seats[seat]
    readers = await _expenses_by_reader(session, user_id, workspace_id, seats["dining"])
    assert readers == _expected(readers, SPLIT_EXPENSES[seat])


async def test_a_split_reads_the_viewers_part_in_every_reader_on_postgresql(postgres_sessions):
    async with postgres_sessions() as session:
        seats = await _shared_workspace_split(session)
    for seat, expenses in SPLIT_EXPENSES.items():
        user_id, workspace_id = seats[seat]
        async with postgres_sessions() as session:
            readers = await _expenses_by_reader(session, user_id, workspace_id, seats["dining"])
        assert readers == _expected(readers, expenses), seat
