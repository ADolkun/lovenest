"""A reimbursement is booked against the expense it repays (ADR 0012).

The linked amount leaves both rows in their own months and categories, so the
reporting figures are checked month by month and against one another: the
dashboard card, the category breakdown, the list footer and the budget bar.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.credit_card_bill import CreditCardBill
from app.models.transaction import Transaction
from app.models.transaction_reimbursement import TransactionReimbursement
from app.models.workspace import Workspace
from app.schemas.budget import BudgetCreate
from app.schemas.category import CategoryUpdate
from app.schemas.group import GroupCreate, GroupMemberCreate
from app.schemas.transaction import TransactionUpdate
from app.schemas.transaction_split import TransactionSplitInput, TransactionSplitsInput
from app.services import (
    category_service,
    group_service,
    reimbursement_service,
    split_service,
    transaction_service,
)
from app.services.budget_service import create_budget, get_budget_vs_actual
from app.services.connection_service import _sync_bill_finance_charges
from app.services.dashboard_service import get_spending_by_category, get_summary
from app.services.transfer_detection_service import detect_transfer_pairs

SEPT = date(2025, 9, 1)
OCT = date(2025, 10, 1)


async def _account(
    session: AsyncSession, user_id, name: str = "Checking", acc_type: str = "checking",
) -> Account:
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
    session: AsyncSession, user_id, account: Account, amount: str, txn_type: str, on: date,
    *, category: Category | None = None, description: str | None = None,
    currency: str = "BRL", status: str = "posted", source: str = "manual",
    workspace_id=None,
) -> Transaction:
    txn = Transaction(
        id=uuid.uuid4(), user_id=user_id, account_id=account.id,
        category_id=category.id if category else None,
        description=description or f"{txn_type} {amount} {on}",
        amount=Decimal(amount), date=on, type=txn_type, source=source,
        currency=currency, status=status, created_at=datetime.now(timezone.utc),
        **({"workspace_id": workspace_id} if workspace_id else {}),
    )
    session.add(txn)
    await session.commit()
    return txn


async def _link(session, workspace_id, credit: Transaction, *allocations):
    return await reimbursement_service.replace_reimbursements(
        session, workspace_id, credit.id,
        [(expense.id, Decimal(amount)) for expense, amount in allocations],
    )


async def _spending(session, workspace, user, month) -> dict[str | None, float]:
    rows = await get_spending_by_category(session, workspace.id, user.id, month=month)
    return {row.category_name: row.total for row in rows}


async def _net(session, workspace, user, month) -> tuple[float, float]:
    summary = await get_summary(session, workspace.id, user.id, month=month)
    return summary.monthly_income_primary, summary.monthly_expenses_primary


def _month_end(month: date) -> date:
    return (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


async def _footer(session, workspace, user, month) -> tuple[float, float]:
    _, _, summary = await transaction_service.get_transactions(
        session, workspace.id, user.id,
        from_date=month, to_date=_month_end(month), include_summary=True,
    )
    assert summary is not None
    return float(summary["income"]), float(summary["expense"])


# --- reporting ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_linked_expense_leaves_its_own_month_and_net_is_conserved(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    dining = await _category(session, test_user.id, "Dining")
    trip = await _txn(session, test_user.id, checking, "100", "debit", date(2025, 9, 12), category=dining)
    await _txn(session, test_user.id, checking, "40", "debit", date(2025, 10, 3), category=dining)
    repaid = await _txn(session, test_user.id, checking, "100", "credit", date(2025, 10, 8), category=dining)

    before = [await _net(session, test_workspace, test_user, m) for m in (SEPT, OCT)]
    assert before == [(pytest.approx(0), pytest.approx(100)), (pytest.approx(0), pytest.approx(-60))]

    await _link(session, test_workspace.id, repaid, (trip, "100"))

    assert await _spending(session, test_workspace, test_user, SEPT) == {}
    assert await _spending(session, test_workspace, test_user, OCT) == {"Dining": pytest.approx(40)}
    after = [await _net(session, test_workspace, test_user, m) for m in (SEPT, OCT)]
    assert after == [(pytest.approx(0), pytest.approx(0)), (pytest.approx(0), pytest.approx(40))]
    assert sum(i - e for i, e in after) == pytest.approx(sum(i - e for i, e in before))
    for month, figures in zip((SEPT, OCT), after):
        assert await _footer(session, test_workspace, test_user, month) == (
            pytest.approx(figures[0]), pytest.approx(figures[1]),
        )


@pytest.mark.asyncio
async def test_partial_link_leaves_the_remainder_to_the_category_rule(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    dining = await _category(session, test_user.id, "Dining")
    meals = await _txn(session, test_user.id, checking, "117.09", "debit", date(2025, 9, 20), category=dining)
    await _txn(session, test_user.id, checking, "200", "debit", date(2025, 10, 2), category=dining)
    credit = await _txn(session, test_user.id, checking, "259", "credit", date(2025, 10, 9), category=dining)

    await _link(session, test_workspace.id, credit, (meals, "117.09"))

    assert await _spending(session, test_workspace, test_user, SEPT) == {}
    # The unlinked 141.91 is still money back on Dining, in the month it posted.
    assert await _spending(session, test_workspace, test_user, OCT) == {"Dining": pytest.approx(58.09)}
    assert await _net(session, test_workspace, test_user, OCT) == (pytest.approx(0), pytest.approx(58.09))
    assert await _footer(session, test_workspace, test_user, OCT) == (pytest.approx(0), pytest.approx(58.09))


@pytest.mark.asyncio
async def test_partial_link_from_an_income_category_leaves_the_remainder_as_income(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    travel = await _category(session, test_user.id, "Travel")
    payroll = await _category(session, test_user.id, "Employer", is_income=True)
    meals = await _txn(session, test_user.id, checking, "117.09", "debit", date(2025, 9, 20), category=travel)
    credit = await _txn(session, test_user.id, checking, "259", "credit", date(2025, 10, 9), category=payroll)

    await _link(session, test_workspace.id, credit, (meals, "117.09"))

    assert await _net(session, test_workspace, test_user, SEPT) == (pytest.approx(0), pytest.approx(0))
    assert await _net(session, test_workspace, test_user, OCT) == (pytest.approx(141.91), pytest.approx(0))
    assert await _footer(session, test_workspace, test_user, OCT) == (pytest.approx(141.91), pytest.approx(0))


@pytest.mark.asyncio
async def test_fully_linked_credit_in_an_income_category_leaves_income(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    travel = await _category(session, test_user.id, "Travel")
    payroll = await _category(session, test_user.id, "Employer", is_income=True)
    hotel = await _txn(session, test_user.id, checking, "100", "debit", date(2025, 9, 15), category=travel)
    credit = await _txn(session, test_user.id, checking, "100", "credit", date(2025, 10, 1), category=payroll)
    assert await _net(session, test_workspace, test_user, OCT) == (pytest.approx(100), pytest.approx(0))

    await _link(session, test_workspace.id, credit, (hotel, "100"))

    assert await _net(session, test_workspace, test_user, SEPT) == (pytest.approx(0), pytest.approx(0))
    assert await _net(session, test_workspace, test_user, OCT) == (pytest.approx(0), pytest.approx(0))


@pytest.mark.asyncio
async def test_one_credit_spread_across_four_monthly_expenses(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    subscriptions = await _category(session, test_user.id, "Subscriptions")
    months = [date(2025, m, 1) for m in (7, 8, 9, 10)]
    plans = [
        await _txn(session, test_user.id, checking, "40", "debit", m.replace(day=5), category=subscriptions)
        for m in months
    ]
    zelle = await _txn(
        session, test_user.id, checking, "100", "credit", date(2025, 10, 6),
        category=subscriptions, description="ZELLE FROM SISTER",
    )

    await _link(session, test_workspace.id, zelle, *[(plan, "25") for plan in plans])

    for month in months:
        assert await _spending(session, test_workspace, test_user, month) == {
            "Subscriptions": pytest.approx(15)
        }
        assert await _footer(session, test_workspace, test_user, month) == (
            pytest.approx(0), pytest.approx(15),
        )


@pytest.mark.asyncio
async def test_budget_actual_follows_the_link(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    dining = await _category(session, test_user.id, "Dining")
    await create_budget(
        session, test_workspace.id, test_user.id,
        BudgetCreate(category_id=dining.id, amount=Decimal("300"), month=SEPT),
    )
    meals = await _txn(session, test_user.id, checking, "150", "debit", date(2025, 9, 9), category=dining)
    credit = await _txn(session, test_user.id, checking, "90", "credit", date(2025, 10, 2), category=dining)

    await _link(session, test_workspace.id, credit, (meals, "90"))

    comparisons = {
        c.category_name: c
        for c in await get_budget_vs_actual(session, test_workspace.id, test_user.id, month=SEPT)
    }
    assert comparisons["Dining"].actual_amount == pytest.approx(Decimal("60"))


# --- API ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_replaces_the_whole_set_and_reads_from_either_side(
    client: AsyncClient, auth_headers, session: AsyncSession, test_user,
):
    checking = await _account(session, test_user.id)
    dining = await _category(session, test_user.id, "Dining")
    a = await _txn(session, test_user.id, checking, "50", "debit", date(2025, 9, 1), category=dining, description="Lunch A")
    b = await _txn(session, test_user.id, checking, "50", "debit", date(2025, 9, 2), description="Lunch B")
    c = await _txn(session, test_user.id, checking, "50", "debit", date(2025, 9, 3), description="Lunch C")
    credit = await _txn(session, test_user.id, checking, "100", "credit", date(2025, 9, 30), description="ACME")
    url = f"/api/transactions/{credit.id}/reimbursements"

    first = await client.put(url, headers=auth_headers, json={"allocations": [
        {"expense_id": str(a.id), "amount": 30}, {"expense_id": str(b.id), "amount": 20},
    ]})
    assert first.status_code == 200, first.text
    by_counterpart = {link["counterpart_id"]: link for link in first.json()}
    assert by_counterpart[str(a.id)] == {
        "id": by_counterpart[str(a.id)]["id"], "counterpart_id": str(a.id),
        "counterpart_description": "Lunch A", "counterpart_date": "2025-09-01",
        "counterpart_category_name": "Dining", "amount": 30.0, "currency": "BRL",
        "in_effect": True,
    }
    b_link_id = by_counterpart[str(b.id)]["id"]

    second = await client.put(url, headers=auth_headers, json={"allocations": [
        {"expense_id": str(b.id), "amount": 25}, {"expense_id": str(c.id), "amount": 10.5},
    ]})
    assert second.status_code == 200, second.text
    assert {(x["counterpart_id"], x["amount"]) for x in second.json()} == {
        (str(b.id), 25.0), (str(c.id), 10.5),
    }
    assert next(x["id"] for x in second.json() if x["counterpart_id"] == str(b.id)) == b_link_id

    from_expense = await client.get(f"/api/transactions/{b.id}/reimbursements", headers=auth_headers)
    assert from_expense.status_code == 200
    assert [(x["counterpart_id"], x["counterpart_description"], x["amount"]) for x in from_expense.json()] == [
        (str(credit.id), "ACME", 25.0)
    ]

    listed = await client.get(
        "/api/transactions", headers=auth_headers, params={"from": "2025-09-01", "to": "2025-09-30"},
    )
    links = {item["id"]: item["reimbursement_links"] for item in listed.json()["items"]}
    assert links[str(a.id)] == []
    assert [x["counterpart_id"] for x in links[str(b.id)]] == [str(credit.id)]
    assert {x["counterpart_id"] for x in links[str(credit.id)]} == {str(b.id), str(c.id)}

    single = await client.get(f"/api/transactions/{credit.id}", headers=auth_headers)
    assert len(single.json()["reimbursement_links"]) == 2

    cleared = await client.put(url, headers=auth_headers, json={"allocations": []})
    assert cleared.status_code == 200
    assert cleared.json() == []
    assert (await session.execute(select(TransactionReimbursement))).scalars().all() == []


@pytest.mark.asyncio
async def test_delete_from_either_side(
    client: AsyncClient, auth_headers, session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    meal = await _txn(session, test_user.id, checking, "50", "debit", date(2025, 9, 1))
    taxi = await _txn(session, test_user.id, checking, "20", "debit", date(2025, 9, 2))
    other = await _txn(session, test_user.id, checking, "5", "debit", date(2025, 9, 3))
    credit = await _txn(session, test_user.id, checking, "70", "credit", date(2025, 9, 30))
    links = await _link(session, test_workspace.id, credit, (meal, "50"), (taxi, "20"))
    meal_link = next(x["id"] for x in links if x["counterpart_id"] == meal.id)
    taxi_link = next(x["id"] for x in links if x["counterpart_id"] == taxi.id)

    unrelated = await client.delete(
        f"/api/transactions/{other.id}/reimbursements/{meal_link}", headers=auth_headers
    )
    assert unrelated.status_code == 404
    from_expense = await client.delete(
        f"/api/transactions/{meal.id}/reimbursements/{meal_link}", headers=auth_headers
    )
    assert from_expense.status_code == 204
    from_credit = await client.delete(
        f"/api/transactions/{credit.id}/reimbursements/{taxi_link}", headers=auth_headers
    )
    assert from_credit.status_code == 204
    session.expire_all()
    assert (await session.execute(select(TransactionReimbursement))).scalars().all() == []


@pytest.mark.asyncio
async def test_other_workspace_cannot_read_or_link(
    client: AsyncClient, auth_headers, other_workspace_headers, session: AsyncSession, test_user,
):
    checking = await _account(session, test_user.id)
    credit = await _txn(session, test_user.id, checking, "70", "credit", date(2025, 9, 30))
    elsewhere = Workspace(
        id=uuid.uuid4(), name="Elsewhere", kind="personal",
        created_by_user_id=test_user.id, default_currency="BRL", locale="en",
    )
    session.add(elsewhere)
    await session.commit()
    foreign = await _txn(
        session, test_user.id, checking, "10", "debit", date(2025, 9, 1), workspace_id=elsewhere.id,
    )

    for method in ("get", "put"):
        kwargs = {"json": {"allocations": []}} if method == "put" else {}
        response = await getattr(client, method)(
            f"/api/transactions/{credit.id}/reimbursements", headers=other_workspace_headers, **kwargs
        )
        assert response.status_code == 404
    response = await client.put(
        f"/api/transactions/{credit.id}/reimbursements", headers=auth_headers,
        json={"allocations": [{"expense_id": str(foreign.id), "amount": 10}]},
    )
    assert response.status_code == 400
    assert "no longer exists" in response.json()["detail"]


@pytest.mark.asyncio
async def test_eligibility_rules(
    client: AsyncClient, auth_headers, session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    card = await _account(session, test_user.id, "Card", acc_type="credit_card")
    savings = await _account(session, test_user.id, "Savings")
    day = date(2025, 9, 10)
    credit = await _txn(session, test_user.id, checking, "100", "credit", date(2025, 9, 30))
    expense = await _txn(session, test_user.id, checking, "60", "debit", day, description="Dinner")

    async def put(credit_id, *allocations):
        return await client.put(
            f"/api/transactions/{credit_id}/reimbursements", headers=auth_headers,
            json={"allocations": [{"expense_id": str(e), "amount": a} for e, a in allocations]},
        )

    async def refused(response, fragment):
        assert response.status_code == 400, response.text
        assert fragment in response.json()["detail"]

    await refused(await put(expense.id), "Only a credit")
    other_credit = await _txn(session, test_user.id, checking, "5", "credit", day)
    await refused(await put(credit.id, (other_credit.id, 5)), "is not an expense")
    await refused(await put(credit.id, (credit.id, 5)), "can't reimburse itself")
    await refused(await put(credit.id, (expense.id, 0)), "greater than zero")
    await refused(await put(credit.id, (expense.id, -3)), "greater than zero")
    await refused(await put(credit.id, (expense.id, 5), (expense.id, 6)), "only once")

    euros = await _txn(session, test_user.id, checking, "10", "debit", day, currency="EUR", description="Paris")
    await refused(await put(credit.id, (euros.id, 5)), "same currency")

    pending = await _txn(session, test_user.id, checking, "10", "debit", day, status="pending", description="Held")
    await refused(await put(credit.id, (pending.id, 5)), '"Held" can\'t be reimbursed: it is still pending')
    future = await _txn(session, test_user.id, checking, "10", "debit", date.today() + timedelta(days=5))
    await refused(await put(credit.id, (future.id, 5)), "dated in the future")
    settlement = await _txn(session, test_user.id, checking, "10", "debit", day, source="settlement")
    await refused(await put(credit.id, (settlement.id, 5)), "group settlement")
    opening = await _txn(session, test_user.id, checking, "10", "debit", day, source="opening_balance")
    await refused(await put(credit.id, (opening.id, 5)), "opening balance")

    leg_out = await _txn(session, test_user.id, checking, "10", "debit", day)
    leg_in = await _txn(session, test_user.id, savings, "10", "credit", day)
    await transaction_service.link_existing_as_transfer(session, test_workspace.id, [leg_out.id, leg_in.id])
    await refused(await put(credit.id, (leg_out.id, 5)), "part of a transfer")
    await refused(await put(leg_in.id, (expense.id, 5)), "This credit can't be linked as a reimbursement: it is part of a transfer")

    card_payment = await _txn(
        session, test_user.id, card, "300", "credit", day, description="AUTOMATIC PAYMENT - THANK",
    )
    await refused(await put(card_payment.id, (expense.id, 5)), "credit card payment")
    pending_credit = await _txn(session, test_user.id, checking, "10", "credit", day, status="pending")
    await refused(await put(pending_credit.id, (expense.id, 5)), "This credit can't be linked")

    shared = await _txn(session, test_user.id, checking, "90", "debit", day, description="Group dinner")
    group = await group_service.create_group(session, test_workspace.id, test_user.id, GroupCreate(name="Friends"))
    member = await group_service.create_member(session, group.id, test_workspace.id, GroupMemberCreate(name="Ana"))
    assert member is not None
    await split_service.replace_splits(
        session, shared,
        TransactionSplitsInput(share_type="equal", splits=[TransactionSplitInput(group_member_id=member.id)]),
        test_user.id,
    )
    await session.commit()
    await refused(await put(credit.id, (shared.id, 5)), "Use a group settlement")

    await refused(await put(credit.id, (expense.id, 60), (leg_out.id, 0.01)), "part of a transfer")
    big = await _txn(session, test_user.id, checking, "500", "debit", day)
    await refused(await put(credit.id, (big.id, 100.01)), "add up to 100.01 BRL, more than this credit's 100.00 BRL")

    first = await _txn(session, test_user.id, checking, "80", "credit", day)
    assert (await put(first.id, (expense.id, 50))).status_code == 200
    await refused(await put(credit.id, (expense.id, 20)), "has 10.00 BRL left to reimburse")
    assert (await put(credit.id, (expense.id, 10))).status_code == 200
    await refused(await put(credit.id, (expense.id, 11)), "has 10.00 BRL left")
    other = await _txn(session, test_user.id, checking, "3", "credit", day)
    await refused(await put(other.id, (expense.id, 1)), "already fully reimbursed")

    missing = await client.put(
        f"/api/transactions/{uuid.uuid4()}/reimbursements", headers=auth_headers, json={"allocations": []},
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_candidates_rank_by_category_then_date_and_report_remaining(
    client: AsyncClient, auth_headers, session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    dining = await _category(session, test_user.id, "Dining")
    travel = await _category(session, test_user.id, "Travel")
    credit_day = date(2025, 10, 5)
    credit = await _txn(session, test_user.id, checking, "100", "credit", credit_day, category=dining)
    earlier = await _txn(session, test_user.id, checking, "100", "debit", date(2025, 9, 20), category=dining, description="Bistro")
    nearest = await _txn(session, test_user.id, checking, "40", "debit", date(2025, 10, 1), category=dining)
    after = await _txn(session, test_user.id, checking, "30", "debit", date(2025, 10, 10), category=dining)
    other_category = await _txn(session, test_user.id, checking, "70", "debit", date(2025, 10, 4), category=travel)
    old = await _txn(session, test_user.id, checking, "20", "debit", date(2025, 1, 2), category=dining, description="Old bistro tab")
    too_late = await _txn(session, test_user.id, checking, "20", "debit", date(2025, 10, 25), category=dining)
    await _txn(session, test_user.id, checking, "20", "debit", date(2025, 10, 1), category=dining, currency="EUR")
    await _txn(session, test_user.id, checking, "20", "debit", date(2025, 10, 1), category=dining, status="pending")
    claimed = await _txn(session, test_user.id, checking, "15", "debit", date(2025, 10, 2), category=dining)
    rival = await _txn(session, test_user.id, checking, "45", "credit", date(2025, 10, 3))
    await _link(session, test_workspace.id, rival, (earlier, "30"), (claimed, "15"))
    await _link(session, test_workspace.id, credit, (nearest, "10"))

    url = f"/api/transactions/{credit.id}/reimbursement-candidates"
    response = await client.get(url, headers=auth_headers)
    assert response.status_code == 200, response.text
    rows = [(c["transaction"]["id"], c["remaining"], c["allocated_here"]) for c in response.json()]
    assert rows == [
        (str(nearest.id), 40.0, 10.0),
        (str(earlier.id), 70.0, 0.0),
        (str(after.id), 30.0, 0.0),
        (str(other_category.id), 70.0, 0.0),
    ]
    assert str(too_late.id) not in {r[0] for r in rows}
    linked_row = next(c for c in response.json() if c["transaction"]["id"] == str(nearest.id))
    assert [x["counterpart_id"] for x in linked_row["transaction"]["reimbursement_links"]] == [str(credit.id)]

    searched = await client.get(url, headers=auth_headers, params={"q": "bistro"})
    assert [c["transaction"]["id"] for c in searched.json()] == [str(earlier.id), str(old.id)]

    limited = await client.get(url, headers=auth_headers, params={"limit": 1})
    assert len(limited.json()) == 1

    on_debit = await client.get(f"/api/transactions/{earlier.id}/reimbursement-candidates", headers=auth_headers)
    assert on_debit.status_code == 400


# --- a link applies only while both sides count ------------------------------


@dataclass
class _Repaid:
    """A September subscription repaid by an October Zelle, as in the probe."""

    expense: Transaction
    credit: Transaction


async def _repaid_subscription(
    session: AsyncSession, user, workspace, *, credit_on_card: bool = False,
) -> _Repaid:
    spending = await _account(session, user.id, "Spending")
    receiving = await _account(
        session, user.id, "Card" if credit_on_card else "Checking",
        acc_type="credit_card" if credit_on_card else "checking",
    )
    subscriptions = await _category(session, user.id, "Subscriptions")
    family = await _category(session, user.id, "Family")
    await create_budget(
        session, workspace.id, user.id,
        BudgetCreate(category_id=subscriptions.id, amount=Decimal("300"), month=SEPT),
    )
    expense = await _txn(
        session, user.id, spending, "100", "debit", date(2025, 9, 5),
        category=subscriptions, description="STREAMING PLAN",
    )
    credit = await _txn(
        session, user.id, receiving, "100", "credit", date(2025, 10, 6),
        category=family, description="ZELLE FROM SISTER",
    )
    return _Repaid(expense, credit)


async def _figures(session, workspace, user) -> list:
    """Every reporting figure the link can move, for both months."""
    figures: list = []
    for month in (SEPT, OCT):
        figures.append((
            await _net(session, workspace, user, month),
            await _footer(session, workspace, user, month),
            await _spending(session, workspace, user, month),
        ))
    figures.append({
        c.category_name: c.actual_amount
        for c in await get_budget_vs_actual(session, workspace.id, user.id, month=SEPT)
    })
    return figures


def _net_cash_flow(figures: list) -> float:
    return sum(income - expenses for (income, expenses), _, _ in figures[:2])


async def _transfer_category(session, workspace, user, row):
    moved = await _category(session, user.id, "Moved money")
    moved.treat_as_transfer = True
    await session.commit()
    await transaction_service.update_transaction(
        session, row.id, workspace.id, user.id, TransactionUpdate(category_id=moved.id),
    )


async def _transfer_category_in_bulk(session, workspace, user, row):
    moved = await _category(session, user.id, "Moved money")
    moved.treat_as_transfer = True
    await session.commit()
    await transaction_service.bulk_update_category(session, workspace.id, [row.id], moved.id)


async def _ignored_category(session, workspace, user, row):
    hidden = await _category(session, user.id, "Not mine")
    hidden.is_ignored = True
    await session.commit()
    await transaction_service.update_transaction(
        session, row.id, workspace.id, user.id, TransactionUpdate(category_id=hidden.id),
    )


async def _category_turned_into_transfers(session, workspace, user, row):
    await category_service.update_category(
        session, row.category_id, workspace.id, CategoryUpdate(treat_as_transfer=True),
    )


async def _category_turned_ignored(session, workspace, user, row):
    await category_service.update_category(
        session, row.category_id, workspace.id, CategoryUpdate(is_ignored=True),
    )


async def _ignored(session, workspace, user, row):
    await transaction_service.toggle_ignore_transaction(session, row.id, workspace.id)


async def _excluded_from_pnl(session, workspace, user, row):
    await transaction_service.update_transaction(
        session, row.id, workspace.id, user.id, TransactionUpdate(exclude_from_pnl=True),
    )


async def _card_payment_description(session, workspace, user, row):
    marker = "CHASE CREDIT CRD AUTOPAY 4410" if row.type == "debit" else "AUTOMATIC PAYMENT - THANK"
    await transaction_service.update_transaction(
        session, row.id, workspace.id, user.id, TransactionUpdate(description=marker),
    )


async def _settlement(session, workspace, user, row):
    await session.execute(
        update(Transaction).where(Transaction.id == row.id).values(source="settlement")
    )
    await session.commit()


async def _account_closed(session, workspace, user, row):
    await session.execute(
        update(Account).where(Account.id == row.account_id).values(is_closed=True)
    )
    await session.commit()


_LEAVING_USER_PNL = [
    _transfer_category,
    _transfer_category_in_bulk,
    _ignored_category,
    _category_turned_into_transfers,
    _category_turned_ignored,
    _ignored,
    _excluded_from_pnl,
    _card_payment_description,
    _settlement,
]


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["expense", "credit"])
@pytest.mark.parametrize("leave", _LEAVING_USER_PNL, ids=lambda f: f.__name__.strip("_"))
async def test_a_side_leaving_user_pnl_leaves_every_figure_as_if_unlinked(
    session: AsyncSession, test_user, test_workspace, side, leave,
):
    repaid = await _repaid_subscription(
        session, test_user, test_workspace,
        credit_on_card=leave is _card_payment_description and side == "credit",
    )
    await _link(session, test_workspace.id, repaid.credit, (repaid.expense, "100"))
    assert _net_cash_flow(await _figures(session, test_workspace, test_user)) == pytest.approx(0)

    await leave(session, test_workspace, test_user, getattr(repaid, side))
    linked = await _figures(session, test_workspace, test_user)
    links = await reimbursement_service.links_for_transactions(
        session, test_workspace.id, [repaid.credit.id, repaid.expense.id]
    )
    assert [link["in_effect"] for row_links in links.values() for link in row_links] == [False, False]

    await _link(session, test_workspace.id, repaid.credit)
    unlinked = await _figures(session, test_workspace, test_user)
    assert linked == unlinked
    assert _net_cash_flow(linked) == pytest.approx(_net_cash_flow(unlinked))


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["expense", "credit"])
async def test_closing_either_account_keeps_the_link_in_effect(
    session: AsyncSession, test_user, test_workspace, side,
):
    repaid = await _repaid_subscription(session, test_user, test_workspace)
    await _link(session, test_workspace.id, repaid.credit, (repaid.expense, "100"))
    linked = await _figures(session, test_workspace, test_user)

    await _account_closed(session, test_workspace, test_user, getattr(repaid, side))

    assert await _figures(session, test_workspace, test_user) == linked
    links = await reimbursement_service.links_for_transactions(
        session, test_workspace.id, [repaid.credit.id, repaid.expense.id]
    )
    assert [link["in_effect"] for row_links in links.values() for link in row_links] == [True, True]


@pytest.mark.asyncio
async def test_the_probe_a_zelle_moved_to_a_transfer_category_keeps_net_at_minus_100(
    session: AsyncSession, test_user, test_workspace,
):
    repaid = await _repaid_subscription(session, test_user, test_workspace)
    await _link(session, test_workspace.id, repaid.credit, (repaid.expense, "100"))
    await _transfer_category(session, test_workspace, test_user, repaid.credit)

    assert await _net(session, test_workspace, test_user, SEPT) == (pytest.approx(0), pytest.approx(100))
    assert await _net(session, test_workspace, test_user, OCT) == (pytest.approx(0), pytest.approx(0))


@pytest.mark.asyncio
async def test_a_link_comes_back_into_effect_when_its_side_counts_again(
    session: AsyncSession, test_user, test_workspace,
):
    repaid = await _repaid_subscription(session, test_user, test_workspace)
    await _link(session, test_workspace.id, repaid.credit, (repaid.expense, "60"))
    in_effect = await _figures(session, test_workspace, test_user)

    await _ignored(session, test_workspace, test_user, repaid.credit)
    assert await _spending(session, test_workspace, test_user, SEPT) == {"Subscriptions": pytest.approx(100)}
    await _ignored(session, test_workspace, test_user, repaid.credit)

    assert await _figures(session, test_workspace, test_user) == in_effect
    assert await _spending(session, test_workspace, test_user, SEPT) == {"Subscriptions": pytest.approx(40)}


@pytest.mark.asyncio
async def test_only_the_links_whose_other_side_left_go_inert(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    subscriptions = await _category(session, test_user.id, "Subscriptions")
    plan = await _txn(session, test_user.id, checking, "40", "debit", date(2025, 9, 5), category=subscriptions)
    sister = await _txn(session, test_user.id, checking, "10", "credit", date(2025, 10, 6), category=subscriptions)
    brother = await _txn(session, test_user.id, checking, "10", "credit", date(2025, 10, 7), category=subscriptions)
    await _link(session, test_workspace.id, sister, (plan, "10"))
    await _link(session, test_workspace.id, brother, (plan, "10"))

    await _ignored(session, test_workspace, test_user, brother)

    assert await _spending(session, test_workspace, test_user, SEPT) == {"Subscriptions": pytest.approx(30)}
    assert await _spending(session, test_workspace, test_user, OCT) == {}
    assert await _net(session, test_workspace, test_user, SEPT) == (pytest.approx(0), pytest.approx(30))
    assert await _net(session, test_workspace, test_user, OCT) == (pytest.approx(0), pytest.approx(0))


@pytest.mark.asyncio
async def test_rows_out_of_user_pnl_cannot_be_newly_linked(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    closed = await _account(session, test_user.id, "Old card")
    moved = await _category(session, test_user.id, "Moved money")
    moved.treat_as_transfer = True
    hidden = await _category(session, test_user.id, "Not mine")
    hidden.is_ignored = True
    closed.is_closed = True
    await session.commit()
    day = date(2025, 9, 10)
    credit = await _txn(session, test_user.id, checking, "100", "credit", date(2025, 9, 30))
    ignored = await _txn(session, test_user.id, checking, "10", "debit", day, description="Ignored")
    ignored.is_ignored = True
    excluded = await _txn(session, test_user.id, checking, "10", "debit", day, description="Excluded")
    excluded.exclude_from_pnl = True
    await session.commit()
    in_transfers = await _txn(session, test_user.id, checking, "10", "debit", day, category=moved, description="Moved")
    in_hidden = await _txn(session, test_user.id, checking, "10", "debit", day, category=hidden, description="Hidden")
    on_closed = await _txn(session, test_user.id, closed, "10", "debit", day, description="Closed")
    fine = await _txn(session, test_user.id, checking, "10", "debit", day, description="Fine")

    for row, reason in [
        (ignored, '"Ignored" can\'t be reimbursed: it is ignored.'),
        (excluded, '"Excluded" can\'t be reimbursed: it is excluded from income and expenses.'),
        (in_transfers, '"Moved" can\'t be reimbursed: it is filed under a category left out'),
        (in_hidden, '"Hidden" can\'t be reimbursed: it is filed under a category left out'),
    ]:
        with pytest.raises(ValueError, match=reason):
            await _link(session, test_workspace.id, credit, (row, "5"))

    # A closed account's history still counts in period reports, so its rows
    # can be linked like any other.
    candidates = await reimbursement_service.reimbursement_candidates(session, test_workspace.id, credit.id)
    assert {tx.id for tx, _, _ in candidates} == {fine.id, on_closed.id}
    zelle = await _txn(session, test_user.id, closed, "10", "credit", day)
    links = await _link(session, test_workspace.id, zelle, (fine, "5"))
    assert [link["in_effect"] for link in links] == [True]


@pytest.mark.asyncio
async def test_a_link_made_before_its_side_left_stays_editable(
    session: AsyncSession, test_user, test_workspace,
):
    repaid = await _repaid_subscription(session, test_user, test_workspace)
    other = await _txn(
        session, test_user.id, await _account(session, test_user.id, "Other"), "30", "debit",
        date(2025, 9, 20), description="Other plan",
    )
    await _link(session, test_workspace.id, repaid.credit, (repaid.expense, "60"))
    await _ignored(session, test_workspace, test_user, repaid.expense)

    links = await _link(session, test_workspace.id, repaid.credit, (repaid.expense, "70"), (other, "30"))
    assert {(link["counterpart_id"], link["amount"], link["in_effect"]) for link in links} == {
        (repaid.expense.id, 70.0, False), (other.id, 30.0, True),
    }


@pytest.mark.asyncio
async def test_candidates_list_what_this_credit_already_repays_first(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    dining = await _category(session, test_user.id, "Dining")
    travel = await _category(session, test_user.id, "Travel")
    credit = await _txn(session, test_user.id, checking, "100", "credit", date(2025, 10, 5), category=dining)
    nearest = await _txn(session, test_user.id, checking, "40", "debit", date(2025, 10, 4), category=dining)
    linked_far = await _txn(session, test_user.id, checking, "30", "debit", date(2025, 6, 1), category=travel)
    linked_after = await _txn(session, test_user.id, checking, "20", "debit", date(2025, 10, 9), category=dining)
    await _link(session, test_workspace.id, credit, (linked_far, "10"), (linked_after, "5"))

    candidates = await reimbursement_service.reimbursement_candidates(session, test_workspace.id, credit.id)
    assert [(tx.id, here) for tx, _, here in candidates] == [
        (linked_after.id, Decimal("5")),
        (linked_far.id, Decimal("10")),
        (nearest.id, Decimal("0")),
    ]


@pytest.mark.asyncio
async def test_a_sync_moving_an_account_takes_its_links_along(
    session: AsyncSession, test_user, test_workspace,
):
    from tests.test_sync_account_allowlist import (
        _account as _provider_account,
        _fake_provider,
        _make_connection,
        _sync,
    )
    from app.providers.base import TransactionData

    day = date.today() - timedelta(days=2)

    def synced(external_id: str, amount: str, txn_type: str) -> TransactionData:
        return TransactionData(
            external_id=external_id, description=external_id.upper(), amount=Decimal(amount),
            date=day, type=txn_type, currency="BRL",
        )

    connection = await _make_connection(session, test_user.id, test_workspace.id)
    provider = _fake_provider(
        [_provider_account("acc-1")],
        {"acc-1": [synced("gym", "50", "debit"), synced("refund", "20", "credit")]},
    )
    await _sync(session, connection, test_workspace, test_user, provider)
    rows = {
        tx.external_id: tx
        for tx in (await session.execute(select(Transaction).where(Transaction.source == "sync"))).scalars()
    }
    gym, refund = rows["gym"], rows["refund"]
    cash = await _account(session, test_user.id, "Cash")
    handout = await _txn(session, test_user.id, cash, "15", "credit", day, workspace_id=test_workspace.id)
    await _link(session, test_workspace.id, refund, (gym, "20"))
    await _link(session, test_workspace.id, handout, (gym, "15"))

    shared = Workspace(
        id=uuid.uuid4(), name="Shared", kind="personal",
        created_by_user_id=test_user.id, default_currency="BRL", locale="en",
    )
    session.add(shared)
    connection.workspace_id = shared.id
    await session.commit()
    await _sync(session, connection, shared, test_user, provider)
    await session.commit()

    remaining = (await session.execute(
        select(TransactionReimbursement.reimbursement_id, TransactionReimbursement.workspace_id)
    )).all()
    assert remaining == [(refund.id, shared.id)]
    assert [link["counterpart_id"] for link in await reimbursement_service.get_reimbursements(
        session, shared.id, gym.id
    )] == [refund.id]


@pytest.mark.asyncio
async def test_a_sync_moving_a_connection_keeps_links_between_its_own_accounts(
    session: AsyncSession, test_user, test_workspace,
):
    from tests.test_sync_account_allowlist import (
        _account as _provider_account,
        _fake_provider,
        _make_connection,
        _sync,
    )
    from app.providers.base import AccountData, TransactionData

    day = date.today() - timedelta(days=2)

    def synced(external_id: str, amount: str, txn_type: str) -> TransactionData:
        return TransactionData(
            external_id=external_id, description=external_id.upper(), amount=Decimal(amount),
            date=day, type=txn_type, currency="BRL",
        )

    card = AccountData(
        external_id="card", name="Card", type="credit_card", balance=Decimal("80"), currency="BRL",
    )
    connection = await _make_connection(session, test_user.id, test_workspace.id)
    provider = _fake_provider(
        [card, _provider_account("checking")],
        {"card": [synced("charge", "80", "debit")], "checking": [synced("zelle", "50", "credit")]},
    )
    await _sync(session, connection, test_workspace, test_user, provider)
    rows = {
        tx.external_id: tx
        for tx in (await session.execute(select(Transaction).where(Transaction.source == "sync"))).scalars()
    }
    charge, zelle = rows["charge"], rows["zelle"]
    cash = await _account(session, test_user.id, "Cash")
    handout = await _txn(session, test_user.id, cash, "15", "credit", day, workspace_id=test_workspace.id)
    await _link(session, test_workspace.id, zelle, (charge, "50"))
    await _link(session, test_workspace.id, handout, (charge, "15"))

    shared = Workspace(
        id=uuid.uuid4(), name="Shared", kind="personal",
        created_by_user_id=test_user.id, default_currency="BRL", locale="en",
    )
    session.add(shared)
    connection.workspace_id = shared.id
    await session.commit()
    await _sync(session, connection, shared, test_user, provider)
    await session.commit()

    remaining = (await session.execute(
        select(
            TransactionReimbursement.reimbursement_id,
            TransactionReimbursement.expense_id,
            TransactionReimbursement.workspace_id,
        )
    )).all()
    assert remaining == [(zelle.id, charge.id, shared.id)]


# --- cross-feature guards ----------------------------------------------------


@pytest.mark.asyncio
async def test_linked_rows_cannot_be_split_or_turned_into_transfers(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    savings = await _account(session, test_user.id, "Savings")
    expense = await _txn(session, test_user.id, checking, "60", "debit", date(2025, 9, 10))
    credit = await _txn(session, test_user.id, checking, "60", "credit", date(2025, 9, 20))
    incoming = await _txn(session, test_user.id, savings, "60", "credit", date(2025, 9, 10))
    await _link(session, test_workspace.id, credit, (expense, "60"))
    group = await group_service.create_group(session, test_workspace.id, test_user.id, GroupCreate(name="Trip"))
    member = await group_service.create_member(session, group.id, test_workspace.id, GroupMemberCreate(name="Bo"))
    assert member is not None
    payload = TransactionSplitsInput(
        share_type="equal", splits=[TransactionSplitInput(group_member_id=member.id)]
    )

    with pytest.raises(ValueError, match="before splitting it with a group"):
        await split_service.replace_splits(session, expense, payload, test_user.id)
    with pytest.raises(ValueError, match="before splitting it with a group"):
        await transaction_service.update_transaction(
            session, credit.id, test_workspace.id, test_user.id, TransactionUpdate(splits=payload),
        )
    result = await transaction_service.bulk_add_to_group(
        session, test_workspace.id, test_user.id, [expense.id, credit.id], group.id,
    )
    assert result == {"updated": 0, "skipped": 2}

    with pytest.raises(ValueError, match="before marking it as a transfer"):
        await transaction_service.link_existing_as_transfer(
            session, test_workspace.id, [expense.id, incoming.id]
        )
    with pytest.raises(ValueError, match="before marking it as a transfer"):
        await transaction_service.create_transfer_counterpart(
            session, test_workspace.id, test_user.id, expense.id, savings.id,
        )
    assert await transaction_service.get_transfer_candidates(session, test_workspace.id, expense.id) == []
    candidates = await transaction_service.get_transfer_candidates(session, test_workspace.id, incoming.id)
    assert expense.id not in {tx.id for tx in candidates}


@pytest.mark.asyncio
async def test_transfer_detection_skips_linked_rows(
    session: AsyncSession, test_user, test_workspace,
):
    a = await _account(session, test_user.id, "A")
    b = await _account(session, test_user.id, "B")
    day = date(2025, 9, 10)
    out = await _txn(session, test_user.id, a, "500", "debit", day, description="Transfer debit 500 REF50000")
    into = await _txn(session, test_user.id, b, "500", "credit", day, description="Transfer credit 500 REF50000")
    repay = await _txn(session, test_user.id, a, "500", "credit", date(2025, 9, 20))
    await _link(session, test_workspace.id, repay, (out, "500"))

    assert await detect_transfer_pairs(session, test_workspace.id) == 0
    await session.refresh(into)
    assert into.transfer_pair_id is None


@pytest.mark.asyncio
async def test_transfer_detection_rechecks_links_made_after_it_read_the_pool(
    session: AsyncSession, test_user, test_workspace, monkeypatch,
):
    from app.services import transfer_detection_service

    a = await _account(session, test_user.id, "A")
    b = await _account(session, test_user.id, "B")
    day = date(2025, 9, 10)
    out = await _txn(session, test_user.id, a, "500", "debit", day, description="Transfer debit 500 REF50000")
    into = await _txn(session, test_user.id, b, "500", "credit", day, description="Transfer credit 500 REF50000")
    repay = await _txn(session, test_user.id, a, "500", "credit", date(2025, 9, 20))
    read_pool = transfer_detection_service._pool

    async def pool_then_link(*args, **kwargs):
        pool = await read_pool(*args, **kwargs)
        session.add(TransactionReimbursement(
            workspace_id=test_workspace.id, reimbursement_id=repay.id, expense_id=out.id,
            amount=Decimal("500"),
        ))
        return pool

    monkeypatch.setattr(transfer_detection_service, "_pool", pool_then_link)

    assert await detect_transfer_pairs(session, test_workspace.id) == 0
    assert out.transfer_pair_id is None and into.transfer_pair_id is None


@pytest.mark.asyncio
async def test_edits_that_would_break_links_are_refused(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    groceries = await _category(session, test_user.id, "Groceries")
    expense = await _txn(session, test_user.id, checking, "60", "debit", date(2025, 9, 10))
    credit = await _txn(session, test_user.id, checking, "100", "credit", date(2025, 9, 20))
    await _link(session, test_workspace.id, credit, (expense, "45"))

    async def update(tx, **fields):
        return await transaction_service.update_transaction(
            session, tx.id, test_workspace.id, test_user.id, TransactionUpdate(**fields),
        )

    for tx, fields, message in [
        (expense, {"amount": Decimal("44.99")}, "add up to 45.00 BRL"),
        (credit, {"amount": Decimal("40")}, "can't go below that"),
        (expense, {"type": "credit"}, "its type can't change"),
        (credit, {"currency": "USD"}, "its currency can't change"),
        (expense, {"status": "pending"}, "can't go back to pending"),
        (credit, {"date": date.today() + timedelta(days=3)}, "future date"),
    ]:
        with pytest.raises(ValueError, match=message):
            await update(tx, **fields)

    updated = await update(expense, amount=Decimal("45"), category_id=groceries.id, description="Market")
    assert updated is not None and updated.amount == Decimal("45")
    assert (await update(credit, date=date(2025, 9, 25), notes="Reimbursed")) is not None


@pytest.mark.asyncio
async def test_sync_shrinks_links_when_a_charge_amount_drops(
    session: AsyncSession, test_user, test_workspace,
):
    card = await _account(session, test_user.id, "Card", acc_type="credit_card")
    checking = await _account(session, test_user.id)
    bill = CreditCardBill(
        id=uuid.uuid4(), user_id=test_user.id, account_id=card.id,
        external_id="bill-1", due_date=date(2025, 9, 15),
        total_amount=Decimal("100"), currency="BRL",
    )
    session.add(bill)
    await session.commit()
    charge = Transaction(
        id=uuid.uuid4(), user_id=test_user.id, account_id=card.id,
        external_id="bill_charge:bill-1:c1", description="IOF", amount=Decimal("100"),
        currency="BRL", date=date(2025, 9, 5), effective_date=bill.due_date,
        type="debit", source="sync", status="posted", bill_id=bill.id,
    )
    session.add(charge)
    await session.commit()
    first = await _txn(session, test_user.id, checking, "70", "credit", date(2025, 9, 20))
    second = await _txn(session, test_user.id, checking, "50", "credit", date(2025, 9, 21))
    await _link(session, test_workspace.id, first, (charge, "60"))
    await _link(session, test_workspace.id, second, (charge, "40"))

    await _sync_bill_finance_charges(
        session, test_user.id, card, bill,
        [{"id": "c1", "amount": "50", "type": "IOF", "additionalInfo": "IOF"}],
    )
    await session.commit()

    totals = await reimbursement_service.linked_totals(session, [charge.id, first.id, second.id])
    assert totals == {charge.id: Decimal("50"), first.id: Decimal("30"), second.id: Decimal("20")}

    await _sync_bill_finance_charges(
        session, test_user.id, card, bill,
        [{"id": "c1", "amount": "0.01", "type": "IOF", "additionalInfo": "IOF"}],
    )
    await session.commit()
    remaining = (await session.execute(select(TransactionReimbursement))).scalars().all()
    assert sum(link.amount for link in remaining) <= Decimal("0.01")


@pytest.mark.asyncio
async def test_fit_links_leaves_links_that_still_fit(
    session: AsyncSession, test_user, test_workspace,
):
    checking = await _account(session, test_user.id)
    expense = await _txn(session, test_user.id, checking, "100", "debit", date(2025, 9, 10))
    credit = await _txn(session, test_user.id, checking, "100", "credit", date(2025, 9, 20))
    await _link(session, test_workspace.id, credit, (expense, "60"))

    expense.amount = Decimal("80")
    await reimbursement_service.fit_links_to_amount(session, expense)
    await session.commit()

    assert (await reimbursement_service.linked_totals(session, [expense.id])) == {expense.id: Decimal("60")}


# --- PostgreSQL: cascade and the reporting SQL --------------------------------


async def _pg_user(session: AsyncSession):
    from app.models.user import User
    from app.services.workspace_service import create_personal_workspace_for_user

    user = User(
        id=uuid.uuid4(), email=f"reimb-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", is_active=True, is_superuser=False, is_verified=True,
        preferences={"language": "en", "currency_display": "BRL"},
    )
    session.add(user)
    await session.commit()
    workspace = await create_personal_workspace_for_user(session, user)
    await session.commit()
    return user, workspace


@pytest.mark.asyncio
async def test_postgres_cascade_and_reporting(postgres_sessions):
    async with postgres_sessions() as session:
        user, workspace = await _pg_user(session)
        checking = await _account(session, user.id)
        dining = await _category(session, user.id, "Dining")
        trip = await _txn(session, user.id, checking, "117.09", "debit", date(2025, 9, 12), category=dining)
        other = await _txn(session, user.id, checking, "20", "debit", date(2025, 9, 13), category=dining)
        credit = await _txn(session, user.id, checking, "259", "credit", date(2025, 10, 8), category=dining)
        await _link(session, workspace.id, credit, (trip, "117.09"), (other, "20"))

        assert await _spending(session, workspace, user, SEPT) == {}
        assert await _net(session, workspace, user, OCT) == (pytest.approx(0), pytest.approx(-121.91))
        assert await _footer(session, workspace, user, OCT) == (pytest.approx(0), pytest.approx(-121.91))
        budget = await create_budget(
            session, workspace.id, user.id,
            BudgetCreate(category_id=dining.id, amount=Decimal("300"), month=SEPT),
        )
        assert budget is not None
        actual = {
            c.category_name: c.actual_amount
            for c in await get_budget_vs_actual(session, workspace.id, user.id, month=SEPT)
        }
        assert actual["Dining"] == pytest.approx(Decimal("0"))

        later = await _txn(session, user.id, checking, "30", "debit", date(2025, 10, 1), category=dining)
        candidates = await reimbursement_service.reimbursement_candidates(
            session, workspace.id, credit.id
        )
        assert [(tx.id, remaining, here) for tx, remaining, here in candidates] == [
            (other.id, Decimal("20"), Decimal("20")),
            (trip.id, Decimal("117.09"), Decimal("117.09")),
            (later.id, Decimal("30"), Decimal("0")),
        ]
        trip_id, credit_id = trip.id, credit.id
        with pytest.raises(ValueError, match="can't go below that"):
            await transaction_service.update_transaction(
                session, trip_id, workspace.id, user.id, TransactionUpdate(amount=Decimal("100")),
            )
        await session.rollback()

        trip = await session.get(Transaction, trip_id)
        assert trip is not None
        await session.delete(trip)
        await session.commit()
        assert await reimbursement_service.linked_totals(session, [credit_id]) == {credit_id: Decimal("20")}

        await session.execute(delete(Transaction).where(Transaction.id == credit_id))
        await session.commit()
        assert (await session.execute(select(TransactionReimbursement))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leave, side",
    [
        (_transfer_category, "credit"),
        (_category_turned_ignored, "expense"),
        (_card_payment_description, "expense"),
        (_settlement, "credit"),
    ],
    ids=lambda value: value if isinstance(value, str) else value.__name__.strip("_"),
)
async def test_postgres_a_side_leaving_user_pnl_leaves_every_figure_as_if_unlinked(
    postgres_sessions, leave, side,
):
    async with postgres_sessions() as session:
        user, workspace = await _pg_user(session)
        repaid = await _repaid_subscription(session, user, workspace)
        await _link(session, workspace.id, repaid.credit, (repaid.expense, "100"))

        await leave(session, workspace, user, getattr(repaid, side))
        linked = await _figures(session, workspace, user)
        await _link(session, workspace.id, repaid.credit)

        assert linked == await _figures(session, workspace, user)


@pytest.mark.asyncio
async def test_postgres_splitting_a_row_holds_it_against_a_concurrent_link(postgres_sessions):
    async with postgres_sessions() as splitter, postgres_sessions() as linker:
        user, workspace = await _pg_user(splitter)
        checking = await _account(splitter, user.id)
        expense = await _txn(splitter, user.id, checking, "60", "debit", date(2025, 9, 10))
        credit = await _txn(splitter, user.id, checking, "60", "credit", date(2025, 9, 20))
        group = await group_service.create_group(splitter, workspace.id, user.id, GroupCreate(name="Trip"))
        member = await group_service.create_member(splitter, group.id, workspace.id, GroupMemberCreate(name="Bo"))
        assert member is not None
        await split_service.replace_splits(
            splitter, expense,
            TransactionSplitsInput(share_type="equal", splits=[TransactionSplitInput(group_member_id=member.id)]),
            user.id,
        )

        await linker.execute(text("SET lock_timeout = '300ms'"))
        with pytest.raises(DBAPIError, match="lock timeout"):
            await _link(linker, workspace.id, credit, (expense, "60"))
        await linker.rollback()
        await splitter.commit()

        with pytest.raises(ValueError, match="Use a group settlement"):
            await _link(linker, workspace.id, credit, (expense, "60"))


@pytest.mark.asyncio
async def test_postgres_rehoming_links_after_an_account_moves_workspace(postgres_sessions):
    async with postgres_sessions() as session:
        user, workspace = await _pg_user(session)
        moving = await _account(session, user.id, "Moving")
        staying = await _account(session, user.id, "Staying")
        day = date(2025, 9, 10)
        gym = await _txn(session, user.id, moving, "50", "debit", day, source="sync")
        refund = await _txn(session, user.id, moving, "20", "credit", day, source="sync")
        handout = await _txn(session, user.id, staying, "15", "credit", day)
        await _link(session, workspace.id, refund, (gym, "20"))
        await _link(session, workspace.id, handout, (gym, "15"))
        elsewhere = Workspace(
            id=uuid.uuid4(), name="Elsewhere", kind="personal",
            created_by_user_id=user.id, default_currency="BRL", locale="en",
        )
        session.add(elsewhere)
        await session.commit()

        await session.execute(
            update(Transaction).where(Transaction.account_id == moving.id).values(workspace_id=elsewhere.id)
        )
        await reimbursement_service.rehome_links_for_accounts(session, [moving.id])
        await session.commit()

        remaining = (await session.execute(
            select(TransactionReimbursement.reimbursement_id, TransactionReimbursement.workspace_id)
        )).all()
        assert remaining == [(refund.id, elsewhere.id)]
