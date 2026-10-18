"""Forecast rows follow the same user-P&L rule as posted rows.

Pending and future-dated rows are classified by the SQL helpers that classify
actuals, so a credit filed under a spending category nets against it
(ADR 0011) and a linked row counts only its unlinked share (ADR 0012) in
every projected figure, not just in the posted ones.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today
from app.models.account import Account
from app.models.category import Category
from app.models.recurring_transaction import RecurringTransaction
from app.models.transaction import Transaction
from app.schemas.budget import BudgetCreate
from app.services import admin_service, reimbursement_service
from app.services.budget_service import create_budget, get_budget_vs_actual
from app.services.dashboard_service import get_spending_by_category, get_summary


async def _account(session: AsyncSession, user_id, name: str, acc_type: str = "checking") -> Account:
    account = Account(
        id=uuid.uuid4(), user_id=user_id, name=name, type=acc_type,
        balance=Decimal("0"), currency="BRL",
    )
    session.add(account)
    await session.commit()
    return account


async def _category(
    session: AsyncSession, user_id, name: str, *, is_income: bool = False,
    treat_as_transfer: bool = False,
) -> Category:
    category = Category(
        id=uuid.uuid4(), user_id=user_id, name=name, icon="tag", color="#000000",
        is_income=is_income, treat_as_transfer=treat_as_transfer,
    )
    session.add(category)
    await session.commit()
    return category


async def _txn(
    session: AsyncSession, user_id, account: Account, amount: str, txn_type: str, on: date,
    *, category: Category | None = None, status: str = "posted",
    description: str | None = None, effective_date: date | None = None,
    transfer_pair_id: uuid.UUID | None = None,
) -> Transaction:
    txn = Transaction(
        id=uuid.uuid4(), user_id=user_id, account_id=account.id,
        category_id=category.id if category else None,
        description=description or f"{txn_type} {amount}",
        amount=Decimal(amount), date=on, effective_date=effective_date or on,
        type=txn_type, source="manual", currency="BRL", status=status,
        transfer_pair_id=transfer_pair_id, created_at=datetime.now(timezone.utc),
    )
    session.add(txn)
    await session.commit()
    return txn


def _next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


async def _projected(session, workspace, user, month) -> tuple[float, float]:
    summary = await get_summary(session, workspace.id, user.id, month=month)
    return summary.projected_income_primary, summary.projected_expenses_primary


async def pending_refund(session: AsyncSession, user_id) -> tuple[date, Category]:
    """A posted 100 in Dining and a pending 30 back on it, this month."""
    today = app_today()
    checking = await _account(session, user_id, "Checking")
    dining = await _category(session, user_id, "Dining")
    await _txn(session, user_id, checking, "100", "debit", today, category=dining)
    await _txn(session, user_id, checking, "30", "credit", today, category=dining, status="pending")
    return today.replace(day=1), dining


@pytest.mark.asyncio
async def test_a_pending_refund_reduces_its_category_instead_of_adding_income(
    session: AsyncSession, test_user, test_workspace,
):
    month, dining = await pending_refund(session, test_user.id)
    await create_budget(
        session, test_workspace.id, test_user.id,
        BudgetCreate(category_id=dining.id, amount=Decimal("300"), month=month),
    )

    summary = await get_summary(session, test_workspace.id, test_user.id, month=month)
    assert (summary.monthly_income, summary.monthly_expenses) == (0, 100)
    assert (summary.projected_income, summary.projected_expenses) == (0, 70)
    assert (summary.projected_income_primary, summary.projected_expenses_primary) == (0, 70)

    breakdown = {
        row.category_name: (row.total, row.projected_total)
        for row in await get_spending_by_category(session, test_workspace.id, test_user.id, month=month)
    }
    assert breakdown == {"Dining": (100, 70)}

    budget = {
        row.category_name: row
        for row in await get_budget_vs_actual(session, test_workspace.id, test_user.id, month=month)
    }
    assert budget["Dining"].actual_amount == Decimal("100")
    assert budget["Dining"].projected_amount == Decimal("70")


@pytest.mark.asyncio
async def test_a_linked_card_charge_not_yet_due_conserves_projected_net(
    session: AsyncSession, test_user, test_workspace,
):
    await admin_service.set_app_setting(session, "credit_card_accounting_mode", "accrual")
    today = app_today()
    month = today.replace(day=1)
    bill_month = _next_month(month)
    card = await _account(session, test_user.id, "Card", "credit_card")
    checking = await _account(session, test_user.id, "Checking")
    dining = await _category(session, test_user.id, "Dining")
    refunds = await _category(session, test_user.id, "Reimbursements", is_income=True)
    charge = await _txn(
        session, test_user.id, card, "100", "debit", today, category=dining,
        effective_date=bill_month + timedelta(days=9),
    )
    repaid = await _txn(session, test_user.id, checking, "60", "credit", today, category=refunds)

    before = [await _projected(session, test_workspace, test_user, m) for m in (month, bill_month)]
    assert before == [(60, 0), (0, 100)]

    await reimbursement_service.replace_reimbursements(
        session, test_workspace.id, repaid.id, [(charge.id, Decimal("60"))],
    )

    after = [await _projected(session, test_workspace, test_user, m) for m in (month, bill_month)]
    assert after == [(0, 0), (0, pytest.approx(40))]
    assert sum(i - e for i, e in after) == pytest.approx(sum(i - e for i, e in before))
    bill_breakdown = await get_spending_by_category(
        session, test_workspace.id, test_user.id, month=bill_month,
    )
    assert [(row.category_name, row.projected_total) for row in bill_breakdown] == [
        ("Dining", pytest.approx(40))
    ]


async def _unpaired_transfer_leg(session, user, workspace) -> dict:
    checking = await _account(session, user.id, "Checking")
    return {"account": checking, "amount": "40", "txn_type": "debit",
            "transfer_pair_id": uuid.uuid4()}


async def _card_payment(session, user, workspace) -> dict:
    card = await _account(session, user.id, "Card", "credit_card")
    return {"account": card, "amount": "200", "txn_type": "credit",
            "description": "AUTOPAY PAYMENT - THANK YOU"}


async def _card_refund_in_transfer_category(session, user, workspace) -> dict:
    card = await _account(session, user.id, "Card", "credit_card")
    moves = await _category(session, user.id, "Moves", treat_as_transfer=True)
    return {"account": card, "amount": "25", "txn_type": "credit", "category": moves}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build, posted_figures",
    [
        (_unpaired_transfer_leg, (0, 40)),
        (_card_payment, (0, 0)),
        (_card_refund_in_transfer_category, (0, -25)),
    ],
    ids=["unpaired-transfer-pair-id", "card-payment", "card-refund-in-transfer-category"],
)
async def test_a_pending_row_counts_exactly_as_it_will_once_posted(
    session: AsyncSession, test_user, test_workspace, build, posted_figures,
):
    today = app_today()
    month = today.replace(day=1)
    fields = await build(session, test_user, test_workspace)
    row = await _txn(
        session, test_user.id, fields.pop("account"), fields.pop("amount"),
        fields.pop("txn_type"), today, status="pending", **fields,
    )

    pending = await get_summary(session, test_workspace.id, test_user.id, month=month)
    assert (pending.monthly_income, pending.monthly_expenses) == (0, 0)
    forecast = (pending.projected_income_primary, pending.projected_expenses_primary)

    row.status = "posted"
    await session.commit()
    posted = await get_summary(session, test_workspace.id, test_user.id, month=month)
    assert (posted.monthly_income_primary, posted.monthly_expenses_primary) == posted_figures
    assert forecast == posted_figures


@pytest.mark.asyncio
async def test_a_recurring_credit_follows_its_category(
    session: AsyncSession, test_user, test_workspace,
):
    today = app_today()
    month = today.replace(day=1)
    checking = await _account(session, test_user.id, "Checking")
    dining = await _category(session, test_user.id, "Dining")
    salary = await _category(session, test_user.id, "Salary", is_income=True)
    await _txn(session, test_user.id, checking, "100", "debit", today, category=dining)
    for category, amount in ((dining, "20"), (salary, "500")):
        session.add(RecurringTransaction(
            id=uuid.uuid4(), user_id=test_user.id, account_id=checking.id,
            category_id=category.id, description=f"{category.name} credit",
            amount=Decimal(amount), currency="BRL", type="credit", frequency="monthly",
            start_date=today, next_occurrence=today,
        ))
    await session.commit()

    summary = await get_summary(session, test_workspace.id, test_user.id, month=month)
    assert (summary.projected_income, summary.projected_expenses) == (500, 80)
    assert (summary.projected_income_primary, summary.projected_expenses_primary) == (500, 80)
    breakdown = {
        row.category_name: row.projected_total
        for row in await get_spending_by_category(session, test_workspace.id, test_user.id, month=month)
    }
    assert breakdown == {"Dining": 80}
