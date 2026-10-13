"""Group split shares follow the same income-or-offset rule as their parent.

ADR 0011. A refund on a shared expense nets against spending, and only the
user's part of it: the owner's figures subtract the others' shares of the
refund from the netting, and a linked member's figures net only their own.
Also pins that a category whose money back outweighs its spending stays in
the breakdown instead of vanishing.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.group import Group, GroupMember
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.schemas.budget import BudgetCreate
from app.services._query_filters import owner_split_offset_pnl
from app.services.budget_service import create_budget, get_budget_vs_actual
from app.services.dashboard_service import (
    get_monthly_trend,
    get_spending_by_category,
    get_summary,
)

MONTH = date(2025, 3, 1)
NEXT_MONTH = date(2025, 4, 1)
DAY = date(2025, 3, 10)


async def _account(session: AsyncSession, user_id, name: str = "Checking") -> Account:
    account = Account(
        id=uuid.uuid4(), user_id=user_id, name=name, type="checking",
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
    category: Category | None = None, *, status: str = "posted",
) -> Transaction:
    txn = Transaction(
        id=uuid.uuid4(), user_id=user_id, account_id=account.id,
        category_id=category.id if category else None,
        description=f"{txn_type} {amount}", amount=Decimal(amount),
        amount_primary=Decimal(amount), date=DAY, type=txn_type, source="manual",
        currency="BRL", status=status, created_at=datetime.now(timezone.utc),
    )
    session.add(txn)
    await session.commit()
    return txn


async def _group(session: AsyncSession, owner_id) -> Group:
    group = Group(id=uuid.uuid4(), user_id=owner_id, name="Trip", default_currency="BRL")
    session.add(group)
    await session.flush()
    return group


async def _member(
    session: AsyncSession, group: Group, name: str, *,
    is_self: bool = False, linked_user_id=None,
) -> GroupMember:
    member = GroupMember(
        id=uuid.uuid4(), group_id=group.id, name=name,
        is_self=is_self, linked_user_id=linked_user_id,
    )
    session.add(member)
    await session.flush()
    return member


async def _split(session: AsyncSession, txn: Transaction, member: GroupMember, share: str) -> None:
    session.add(TransactionSplit(
        id=uuid.uuid4(), transaction_id=txn.id, group_member_id=member.id,
        share_amount=Decimal(share), share_type="exact",
    ))
    await session.commit()


async def _other_user(session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(), email="friend@example.com", hashed_password="x",
        is_active=True, is_verified=True, preferences={"currency_display": "BRL"},
    )
    session.add(user)
    await session.flush()
    workspace = Workspace(
        id=uuid.uuid4(), name="Friend", kind="personal",
        created_by_user_id=user.id, default_currency="BRL",
    )
    session.add(workspace)
    await session.flush()
    session.add(WorkspaceMember(
        id=uuid.uuid4(), workspace_id=workspace.id, user_id=user.id, role="owner",
    ))
    await session.commit()
    return user


async def _spending(session, workspace_id, user_id) -> dict[str, float]:
    rows = await get_spending_by_category(session, workspace_id, user_id, month=MONTH)
    return {row.category_name: row.total for row in rows}


async def _budget_row(session, workspace_id, user_id, category: Category, month: date):
    rows = await get_budget_vs_actual(session, workspace_id, user_id, month=month)
    return next(row for row in rows if row.category_id == category.id)


async def _register_to_char(session: AsyncSession) -> None:
    """The monthly trend buckets with Postgres `to_char`; SQLite needs a stand-in."""
    raw = await session.connection()

    def _install(sync_conn):
        sync_conn.connection.dbapi_connection.create_function(
            "to_char", 2, lambda value, _fmt: None if value is None else str(value)[:7],
        )

    await raw.run_sync(_install)


async def _dining_with_more_back_than_spent_and_a_viewer_share(
    session: AsyncSession, user_id,
) -> Category:
    """The user's own Dining rows net to -30; their share of a friend's dinner
    filed under the same category is 40."""
    checking = await _account(session, user_id)
    dining = await _category(session, user_id, "Dining")
    await _txn(session, user_id, checking, "50", "debit", dining)
    await _txn(session, user_id, checking, "80", "credit", dining)

    owner = await _other_user(session)
    owner_checking = await _account(session, owner.id, "Friend checking")
    group = await _group(session, owner.id)
    await _member(session, group, "Owner", is_self=True, linked_user_id=owner.id)
    viewer = await _member(session, group, "Viewer", linked_user_id=user_id)
    dinner = await _txn(session, owner.id, owner_checking, "80", "debit", dining)
    await _split(session, dinner, viewer, "40")
    return dining


@pytest.mark.asyncio
async def test_owner_nets_only_their_share_of_a_shared_refund(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    dining = await _category(session, test_user.id, "Dining")
    group = await _group(session, test_user.id)
    await _member(session, group, "Me", is_self=True, linked_user_id=test_user.id)
    friend = await _member(session, group, "Friend")

    dinner = await _txn(session, test_user.id, checking, "100", "debit", dining)
    await _split(session, dinner, friend, "50")
    refund = await _txn(session, test_user.id, checking, "40", "credit", dining)
    await _split(session, refund, friend, "20")
    await create_budget(
        session, test_workspace.id, test_user.id,
        BudgetCreate(category_id=dining.id, amount=Decimal("200"), month=MONTH),
    )

    # Owner's share: 50 of the dinner less 20 of the refund.
    assert await owner_split_offset_pnl(
        session, test_user.id, MONTH, NEXT_MONTH, workspace_id=test_workspace.id,
    ) == (pytest.approx(0.0), pytest.approx(30.0))
    summary = await get_summary(session, test_workspace.id, test_user.id, month=MONTH)
    assert summary.monthly_expenses == pytest.approx(30.0)
    assert summary.monthly_income == pytest.approx(0.0)
    assert summary.monthly_expenses_primary == pytest.approx(30.0)
    assert summary.monthly_income_primary == pytest.approx(0.0)
    assert await _spending(session, test_workspace.id, test_user.id) == {
        "Dining": pytest.approx(30.0)
    }
    budget = next(
        row for row in await get_budget_vs_actual(
            session, test_workspace.id, test_user.id, month=MONTH,
        )
        if row.category_id == dining.id
    )
    assert budget.actual_amount == Decimal("30")


@pytest.mark.asyncio
async def test_shared_refund_on_unshared_spend_still_counts_toward_owner(
    session: AsyncSession, test_user, test_workspace,
):
    """The category nets to zero before split offsets, so it must not be
    dropped before the friend's half of the refund is taken back out."""
    checking = await _account(session, test_user.id)
    hotel = await _category(session, test_user.id, "Hotel")
    group = await _group(session, test_user.id)
    await _member(session, group, "Me", is_self=True, linked_user_id=test_user.id)
    friend = await _member(session, group, "Friend")

    await _txn(session, test_user.id, checking, "100", "debit", hotel)
    refund = await _txn(session, test_user.id, checking, "100", "credit", hotel)
    await _split(session, refund, friend, "50")

    assert await _spending(session, test_workspace.id, test_user.id) == {
        "Hotel": pytest.approx(50.0)
    }
    summary = await get_summary(session, test_workspace.id, test_user.id, month=MONTH)
    assert summary.monthly_expenses == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_shared_income_credit_still_reduces_income(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    salary = await _category(session, test_user.id, "Salary", is_income=True)
    group = await _group(session, test_user.id)
    await _member(session, group, "Me", is_self=True, linked_user_id=test_user.id)
    friend = await _member(session, group, "Friend")

    pay = await _txn(session, test_user.id, checking, "1000", "credit", salary)
    await _split(session, pay, friend, "400")

    summary = await get_summary(session, test_workspace.id, test_user.id, month=MONTH)
    assert summary.monthly_income == pytest.approx(600.0)
    assert summary.monthly_expenses == pytest.approx(0.0)
    assert await _spending(session, test_workspace.id, test_user.id) == {}


@pytest.mark.asyncio
async def test_viewer_nets_their_share_of_a_refunded_shared_expense(
    session: AsyncSession, test_user, test_workspace,
):
    owner = await _other_user(session)
    checking = await _account(session, owner.id)
    dining = await _category(session, owner.id, "Dining")
    group = await _group(session, owner.id)
    await _member(session, group, "Owner", is_self=True, linked_user_id=owner.id)
    viewer = await _member(session, group, "Viewer", linked_user_id=test_user.id)

    dinner = await _txn(session, owner.id, checking, "100", "debit", dining)
    await _split(session, dinner, viewer, "50")
    refund = await _txn(session, owner.id, checking, "40", "credit", dining)
    await _split(session, refund, viewer, "20")

    summary = await get_summary(session, test_workspace.id, test_user.id, month=MONTH)
    assert summary.monthly_expenses == pytest.approx(30.0)
    assert summary.monthly_income == pytest.approx(0.0)
    assert summary.monthly_expenses_primary == pytest.approx(30.0)
    assert summary.monthly_income_primary == pytest.approx(0.0)
    assert await _spending(session, test_workspace.id, test_user.id) == {
        "Dining": pytest.approx(30.0)
    }


@pytest.mark.asyncio
async def test_category_with_more_money_back_than_spend_stays_visible(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    dining = await _category(session, test_user.id, "Dining")
    groceries = await _category(session, test_user.id, "Groceries")
    gym = await _category(session, test_user.id, "Gym")
    await _txn(session, test_user.id, checking, "50", "debit", dining)
    await _txn(session, test_user.id, checking, "80", "credit", dining)
    await _txn(session, test_user.id, checking, "100", "debit", groceries)
    await _txn(session, test_user.id, checking, "20", "debit", gym)
    await _txn(session, test_user.id, checking, "20", "credit", gym)

    rows = await get_spending_by_category(session, test_workspace.id, test_user.id, month=MONTH)

    by_name = {row.category_name: row for row in rows}
    assert set(by_name) == {"Groceries", "Dining"}
    assert by_name["Dining"].total == pytest.approx(-30.0)
    assert by_name["Dining"].projected_total == pytest.approx(-30.0)
    assert by_name["Dining"].percentage == 0
    assert by_name["Groceries"].percentage == pytest.approx(100.0)
    summary = await get_summary(session, test_workspace.id, test_user.id, month=MONTH)
    assert sum(row.total for row in rows) == pytest.approx(summary.monthly_expenses)


@pytest.mark.asyncio
async def test_budget_keeps_owner_share_of_a_refund_that_nets_the_category_to_zero(
    session: AsyncSession, test_user, test_workspace,
):
    """The owner's own rows net Hotel to zero; the friend's half of the refund
    is spend left with the owner, as this month and as the previous month."""
    checking = await _account(session, test_user.id)
    hotel = await _category(session, test_user.id, "Hotel")
    group = await _group(session, test_user.id)
    await _member(session, group, "Me", is_self=True, linked_user_id=test_user.id)
    friend = await _member(session, group, "Friend")
    await _txn(session, test_user.id, checking, "100", "debit", hotel)
    refund = await _txn(session, test_user.id, checking, "100", "credit", hotel)
    await _split(session, refund, friend, "50")

    current = await _budget_row(session, test_workspace.id, test_user.id, hotel, MONTH)
    assert current.actual_amount == Decimal("50")
    assert current.projected_amount == Decimal("50")
    following = await _budget_row(session, test_workspace.id, test_user.id, hotel, NEXT_MONTH)
    assert following.prev_month_amount == Decimal("50")
    assert following.projected_prev_month_amount == Decimal("50")


@pytest.mark.asyncio
async def test_viewer_share_nets_against_a_month_with_more_back_than_spent(
    session: AsyncSession, test_user, test_workspace,
):
    dining = await _dining_with_more_back_than_spent_and_a_viewer_share(session, test_user.id)

    current = await _budget_row(session, test_workspace.id, test_user.id, dining, MONTH)
    assert current.actual_amount == Decimal("10")
    assert current.projected_amount == Decimal("10")
    following = await _budget_row(session, test_workspace.id, test_user.id, dining, NEXT_MONTH)
    assert following.prev_month_amount == Decimal("10")
    assert following.projected_prev_month_amount == Decimal("10")

    summary = await get_summary(session, test_workspace.id, test_user.id, month=MONTH)
    assert summary.monthly_expenses == pytest.approx(10.0)
    await _register_to_char(session)
    trend = await get_monthly_trend(session, test_workspace.id, test_user.id)
    assert {t.month: t.expenses for t in trend} == {"2025-03": pytest.approx(10.0)}


@pytest.mark.asyncio
async def test_budget_projection_nets_pending_spend_against_money_back(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    dining = await _category(session, test_user.id, "Dining")
    await _txn(session, test_user.id, checking, "40", "debit", dining)
    await _txn(session, test_user.id, checking, "100", "credit", dining)
    await _txn(session, test_user.id, checking, "30", "debit", dining, status="pending")
    await create_budget(
        session, test_workspace.id, test_user.id,
        BudgetCreate(category_id=dining.id, amount=Decimal("200"), month=MONTH),
    )

    row = await _budget_row(session, test_workspace.id, test_user.id, dining, MONTH)
    assert row.actual_amount == Decimal("0")
    assert row.projected_amount == Decimal("0")
    assert row.percentage_used == 0


@pytest.mark.asyncio
async def test_monthly_trend_keeps_a_month_with_more_back_than_spent_signed(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    dining = await _category(session, test_user.id, "Dining")
    await _txn(session, test_user.id, checking, "50", "debit", dining)
    await _txn(session, test_user.id, checking, "110", "credit", dining)
    await _register_to_char(session)

    trend = await get_monthly_trend(session, test_workspace.id, test_user.id)

    assert {t.month: t.expenses for t in trend} == {"2025-03": pytest.approx(-60.0)}
    summary = await get_summary(session, test_workspace.id, test_user.id, month=MONTH)
    assert summary.monthly_expenses == pytest.approx(-60.0)
