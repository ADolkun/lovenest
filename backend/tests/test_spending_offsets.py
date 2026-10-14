"""A credit's category decides whether it is income or money back on spending.

ADR 0011. Every figure here goes through the shared user P&L helpers, so the
dashboard card, the category breakdown, the transaction list and the budget
bar are checked against one another as well as against the rule.
"""

import importlib.util
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.category_group import CategoryGroup
from app.models.transaction import Transaction
from app.schemas.budget import BudgetCreate
from app.services.budget_service import create_budget, get_budget_vs_actual
from app.services.category_service import create_default_categories
from app.services.dashboard_service import get_spending_by_category, get_summary
from app.services.transaction_service import get_transactions

MONTH = date(2025, 3, 1)
DAY = date(2025, 3, 10)


async def _account(session: AsyncSession, user_id, name: str, acc_type: str = "checking") -> Account:
    account = Account(
        id=uuid.uuid4(), user_id=user_id, name=name, type=acc_type,
        balance=Decimal("0"), currency="BRL",
    )
    session.add(account)
    await session.commit()
    return account


async def _category(session: AsyncSession, user_id, name: str, *, is_income: bool = False) -> Category:
    category = Category(
        id=uuid.uuid4(), user_id=user_id, name=name, icon="tag", color="#000000",
        is_income=is_income,
    )
    session.add(category)
    await session.commit()
    return category


async def _txn(
    session: AsyncSession, user_id, account: Account, amount: str, txn_type: str,
    *, category: Category | None = None, description: str | None = None,
    txn_date: date = DAY,
) -> Transaction:
    txn = Transaction(
        id=uuid.uuid4(), user_id=user_id, account_id=account.id,
        category_id=category.id if category else None,
        description=description or f"{txn_type} {amount}",
        amount=Decimal(amount), date=txn_date, type=txn_type, source="manual",
        currency="BRL", status="posted", created_at=datetime.now(timezone.utc),
    )
    session.add(txn)
    await session.commit()
    return txn


async def _spending(session, workspace_id, user_id) -> dict[str | None, float]:
    rows = await get_spending_by_category(session, workspace_id, user_id, month=MONTH)
    return {row.category_name: row.total for row in rows}


@pytest.mark.asyncio
async def test_reimbursement_into_checking_nets_against_its_category(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id, "Checking")
    dining = await _category(session, test_user.id, "Dining & Drinks")
    await _txn(session, test_user.id, checking, "120", "debit", category=dining)
    await _txn(
        session, test_user.id, checking, "45", "credit", category=dining,
        description="ACME CORP EXPENSE REIMBURSEMENT",
    )

    assert (await _spending(session, test_workspace.id, test_user.id)) == {
        "Dining & Drinks": pytest.approx(75.0)
    }
    summary = await get_summary(session, test_workspace.id, test_user.id, month=MONTH)
    assert summary.monthly_expenses == pytest.approx(75.0)
    assert summary.monthly_income == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_credit_in_an_income_category_is_income_not_an_offset(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id, "Checking")
    salary = await _category(session, test_user.id, "Salary", is_income=True)
    groceries = await _category(session, test_user.id, "Groceries")
    await _txn(session, test_user.id, checking, "80", "debit", category=groceries)
    await _txn(session, test_user.id, checking, "3000", "credit", category=salary)

    assert (await _spending(session, test_workspace.id, test_user.id)) == {
        "Groceries": pytest.approx(80.0)
    }
    summary = await get_summary(session, test_workspace.id, test_user.id, month=MONTH)
    assert summary.monthly_income == pytest.approx(3000.0)
    assert summary.monthly_expenses == pytest.approx(80.0)


@pytest.mark.asyncio
async def test_uncategorized_credit_into_checking_stays_income(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id, "Checking")
    groceries = await _category(session, test_user.id, "Groceries")
    await _txn(session, test_user.id, checking, "60", "debit", category=groceries)
    await _txn(session, test_user.id, checking, "500", "credit", description="ZELLE FROM MOM")

    summary = await get_summary(session, test_workspace.id, test_user.id, month=MONTH)
    assert summary.monthly_income == pytest.approx(500.0)
    assert summary.monthly_expenses == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_uncategorized_card_refund_still_offsets(
    session: AsyncSession, test_user, test_workspace,
):
    card = await _account(session, test_user.id, "Card", acc_type="credit_card")
    await _txn(session, test_user.id, card, "100", "debit")
    await _txn(session, test_user.id, card, "30", "credit", description="MERCHANT REFUND")
    await _txn(session, test_user.id, card, "70", "credit", description="AUTOMATIC PAYMENT - THANK")

    summary = await get_summary(session, test_workspace.id, test_user.id, month=MONTH)
    assert summary.monthly_expenses == pytest.approx(70.0)
    assert summary.monthly_income == pytest.approx(0.0)
    spending = await _spending(session, test_workspace.id, test_user.id)
    assert list(spending.values()) == [pytest.approx(70.0)]


@pytest.mark.asyncio
async def test_card_credit_filed_under_an_income_category_is_income(
    session: AsyncSession, test_user, test_workspace,
):
    card = await _account(session, test_user.id, "Card", acc_type="credit_card")
    rewards = await _category(session, test_user.id, "Card Rewards", is_income=True)
    await _txn(session, test_user.id, card, "100", "debit")
    await _txn(session, test_user.id, card, "25", "credit", category=rewards)

    summary = await get_summary(session, test_workspace.id, test_user.id, month=MONTH)
    assert summary.monthly_income == pytest.approx(25.0)
    assert summary.monthly_expenses == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_list_footer_uses_the_same_rule(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id, "Checking")
    subscriptions = await _category(session, test_user.id, "Subscription")
    salary = await _category(session, test_user.id, "Salary", is_income=True)
    await _txn(session, test_user.id, checking, "20", "debit", category=subscriptions)
    await _txn(session, test_user.id, checking, "10", "credit", category=subscriptions)
    await _txn(session, test_user.id, checking, "1000", "credit", category=salary)
    await _txn(session, test_user.id, checking, "5", "credit")

    _, _, summary = await get_transactions(
        session, test_workspace.id, test_user.id,
        from_date=MONTH, to_date=date(2025, 3, 31), include_summary=True,
    )
    assert summary is not None
    assert float(summary["income"]) == pytest.approx(1005.0)
    assert float(summary["expense"]) == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_list_pnl_group_expenses_returns_debits_and_offsets_for_a_category(
    client: AsyncClient, auth_headers, session: AsyncSession, test_user,
):
    checking = await _account(session, test_user.id, "Checking")
    dining = await _category(session, test_user.id, "Dining & Drinks")
    debit = await _txn(session, test_user.id, checking, "120", "debit", category=dining)
    offset = await _txn(session, test_user.id, checking, "45", "credit", category=dining)
    params = {
        "category_id": str(dining.id), "from": "2025-03-01", "to": "2025-03-31",
        "user_pnl_only": "true",
    }

    expenses = await client.get(
        "/api/transactions", headers=auth_headers, params={**params, "pnl_group": "expenses"}
    )
    assert expenses.status_code == 200
    assert {item["id"] for item in expenses.json()["items"]} == {str(debit.id), str(offset.id)}
    assert expenses.json()["summary"]["expense"] == pytest.approx(75.0)

    income = await client.get(
        "/api/transactions", headers=auth_headers, params={**params, "pnl_group": "income"}
    )
    assert income.status_code == 200
    assert income.json()["items"] == []

    invalid = await client.get(
        "/api/transactions", headers=auth_headers, params={**params, "pnl_group": "debit"}
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_list_pnl_group_income_keeps_income_credits(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id, "Checking")
    salary = await _category(session, test_user.id, "Salary", is_income=True)
    dining = await _category(session, test_user.id, "Dining & Drinks")
    pay = await _txn(session, test_user.id, checking, "3000", "credit", category=salary)
    gift = await _txn(session, test_user.id, checking, "50", "credit")
    await _txn(session, test_user.id, checking, "45", "credit", category=dining)
    await _txn(session, test_user.id, checking, "120", "debit", category=dining)

    rows, total, _ = await get_transactions(
        session, test_workspace.id, test_user.id, pnl_group="income", user_pnl_only=True,
    )
    assert total == 2
    assert {row.id for row in rows} == {pay.id, gift.id}


@pytest.mark.asyncio
async def test_budget_actual_matches_category_spending(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id, "Checking")
    dining = await _category(session, test_user.id, "Dining & Drinks")
    side_gig = await _category(session, test_user.id, "Side Gig", is_income=True)
    await create_budget(
        session, test_workspace.id, test_user.id,
        BudgetCreate(category_id=dining.id, amount=Decimal("200"), month=MONTH),
    )
    await _txn(session, test_user.id, checking, "120", "debit", category=dining)
    await _txn(session, test_user.id, checking, "45", "credit", category=dining)
    # A debit in an income category is still spending, and that category's
    # income must not net it away.
    await _txn(session, test_user.id, checking, "30", "debit", category=side_gig)
    await _txn(session, test_user.id, checking, "400", "credit", category=side_gig)

    comparisons = {
        c.category_name: c
        for c in await get_budget_vs_actual(session, test_workspace.id, test_user.id, month=MONTH)
    }
    assert comparisons["Dining & Drinks"].actual_amount == Decimal("75")
    assert comparisons["Side Gig"].actual_amount == Decimal("30")
    assert (await _spending(session, test_workspace.id, test_user.id)) == {
        "Dining & Drinks": pytest.approx(75.0),
        "Side Gig": pytest.approx(30.0),
    }


@pytest.mark.asyncio
async def test_default_salary_category_is_seeded_as_income(
    session: AsyncSession, test_user, test_workspace,
):
    categories = await create_default_categories(
        session, test_user.id, lang="en", workspace_id=test_workspace.id
    )

    assert {c.name for c in categories if c.is_income} == {"Salary & Income"}


@pytest.mark.asyncio
async def test_category_api_round_trips_is_income(
    client: AsyncClient, auth_headers, test_user,
):
    created = await client.post(
        "/api/categories", headers=auth_headers,
        json={"name": "Freelance", "icon": "laptop", "color": "#16A34A", "is_income": True},
    )
    assert created.status_code == 201
    assert created.json()["is_income"] is True

    updated = await client.patch(
        f"/api/categories/{created.json()['id']}", headers=auth_headers,
        json={"is_income": False},
    )
    assert updated.status_code == 200
    assert updated.json()["is_income"] is False

    renamed = await client.patch(
        f"/api/categories/{created.json()['id']}", headers=auth_headers,
        json={"name": "Contract work"},
    )
    assert renamed.json()["is_income"] is False


_spec = importlib.util.spec_from_file_location(
    "migration_115",
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "115_category_is_income.py",
)
assert _spec is not None and _spec.loader is not None
migration_115 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration_115)


@pytest.mark.asyncio
async def test_migration_flags_income_group_members_and_salary_defaults(
    session: AsyncSession, test_user,
):
    income_group = CategoryGroup(id=uuid.uuid4(), user_id=test_user.id, name="Income")
    other_group = CategoryGroup(id=uuid.uuid4(), user_id=test_user.id, name="Outros")
    session.add_all([income_group, other_group])
    await session.flush()
    expected = {
        "Income": (income_group, True),
        "Cash & Checks": (income_group, True),
        "Salário & Renda": (other_group, True),
        "Groceries": (other_group, False),
        "Dining": (None, False),
    }
    for name, (group, _) in expected.items():
        session.add(Category(
            id=uuid.uuid4(), user_id=test_user.id, name=name,
            group_id=group.id if group else None,
        ))
    await session.flush()

    flagged = await session.run_sync(
        lambda sync_session: migration_115.backfill(sync_session.connection())
    )
    session.expire_all()

    assert flagged == 3
    rows = (await session.execute(select(Category.name, Category.is_income))).all()
    assert {name: is_income for name, is_income in rows} == {
        name: is_income for name, (_, is_income) in expected.items()
    }
