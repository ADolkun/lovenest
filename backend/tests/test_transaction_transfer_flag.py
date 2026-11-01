"""`is_transfer` on a transaction row is the rule the totals apply.

Upstream badges a row as a transfer whenever it carries a pair id or sits
in a `treat_as_transfer` category. Lovenest's totals disagree on two
shapes: a pair id whose second leg is gone is not a transfer, and a card
credit that is not a repayment is a refund that stays in P&L under any
category. The row flag, the Type=Transfer filter and the calendar read the
same `is_transfer` predicate `counts_as_pnl` excludes, so a row the footer
counts never reads as a transfer.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.workspace import Workspace
from app.services.transaction_calendar_service import get_transaction_calendar


def _tx(user, account, description, txn_type, *, pair_id=None, category=None, amount="100.00"):
    return Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        category_id=category.id if category else None,
        description=description,
        amount=Decimal(amount),
        currency="BRL",
        date=date.today(),
        type=txn_type,
        source="manual",
        transfer_pair_id=pair_id,
    )


@pytest_asyncio.fixture
async def rows(session: AsyncSession, test_user, test_workspace, test_account) -> dict:
    savings = Account(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name="Savings", type="savings", balance=Decimal("0"), currency="BRL",
    )
    card = Account(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name="Card", type="credit_card", balance=Decimal("0"), currency="BRL",
    )
    transfers = Category(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name="Transfers", icon="repeat", color="#0ea5e9", treat_as_transfer=True,
    )
    session.add_all([savings, card, transfers])
    await session.flush()

    pair_id = uuid.uuid4()
    by_name = {
        "pair_out": _tx(test_user, test_account, "TO SAVINGS", "debit", pair_id=pair_id),
        "pair_in": _tx(test_user, savings, "FROM CHECKING", "credit", pair_id=pair_id),
        "one_legged": _tx(test_user, test_account, "ORPHANED LEG", "debit", pair_id=uuid.uuid4()),
        "card_refund": _tx(test_user, card, "STORE RETURN", "credit", category=transfers),
        "card_payment": _tx(test_user, card, "AUTOPAY PAYMENT - THANK YOU", "credit"),
        "transfer_category": _tx(test_user, test_account, "BROKER APPLICATION", "debit", category=transfers),
        "regular": _tx(test_user, test_account, "GROCERIES", "debit"),
    }
    session.add_all(by_name.values())
    await session.commit()
    return {name: str(tx.id) for name, tx in by_name.items()}


_TRANSFERS = {"pair_out", "pair_in", "card_payment", "transfer_category"}


@pytest.mark.asyncio
async def test_list_flag_matches_what_the_totals_exclude(
    client: AsyncClient, auth_headers, rows
):
    resp = await client.get("/api/transactions?limit=500", headers=auth_headers)
    assert resp.status_code == 200
    flags = {t["id"]: t["is_transfer"] for t in resp.json()["items"]}
    assert {name: flags[tx_id] for name, tx_id in rows.items()} == {
        name: name in _TRANSFERS for name in rows
    }

    counted = await client.get(
        "/api/transactions?user_pnl_only=true&limit=500", headers=auth_headers
    )
    assert counted.status_code == 200
    counted_ids = {t["id"] for t in counted.json()["items"]}
    assert {name for name, tx_id in rows.items() if tx_id in counted_ids} == (
        set(rows) - _TRANSFERS
    )


@pytest.mark.asyncio
async def test_type_transfer_filter_lists_exactly_the_flagged_rows(
    client: AsyncClient, auth_headers, rows
):
    resp = await client.get("/api/transactions?type=transfer&limit=500", headers=auth_headers)
    assert resp.status_code == 200
    listed = {t["id"] for t in resp.json()["items"]}
    assert {name for name, tx_id in rows.items() if tx_id in listed} == _TRANSFERS
    assert all(t["is_transfer"] for t in resp.json()["items"])


@pytest.mark.asyncio
async def test_single_row_read_carries_the_flag(client: AsyncClient, auth_headers, rows):
    for name in ("one_legged", "card_refund", "card_payment", "pair_in"):
        resp = await client.get(f"/api/transactions/{rows[name]}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["is_transfer"] is (name in _TRANSFERS), name


@pytest.mark.asyncio
async def test_calendar_reads_the_same_rule(
    session: AsyncSession, test_user, test_workspace, rows
):
    calendar = await get_transaction_calendar(
        session, test_workspace.id, test_user.id, month=date.today().replace(day=1)
    )
    today = next(day for day in calendar.days if day.date == date.today())
    flags = {str(item.id): item.is_transfer for item in today.items}
    assert {name: flags[tx_id] for name, tx_id in rows.items()} == {
        name: name in _TRANSFERS for name in rows
    }


@pytest.mark.asyncio
async def test_counterpart_in_a_moved_account_is_not_named(
    client: AsyncClient, auth_headers, session: AsyncSession,
    test_user, test_workspace, test_account,
):
    elsewhere = Workspace(
        id=uuid.uuid4(), name="Elsewhere", kind="personal",
        created_by_user_id=test_user.id, default_currency="BRL", locale="pt-BR",
    )
    session.add(elsewhere)
    await session.flush()
    moved = Account(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=elsewhere.id,
        name="Secret Elsewhere", type="savings", balance=Decimal("0"), currency="BRL",
    )
    session.add(moved)
    await session.flush()
    pair_id = uuid.uuid4()
    out_leg = _tx(test_user, test_account, "TO SAVINGS", "debit", pair_id=pair_id)
    in_leg = _tx(test_user, moved, "FROM CHECKING", "credit", pair_id=pair_id)
    in_leg.workspace_id = test_workspace.id
    session.add_all([out_leg, in_leg])
    await session.commit()

    resp = await client.get("/api/transactions?limit=500", headers=auth_headers)
    assert resp.status_code == 200
    by_id = {t["id"]: t for t in resp.json()["items"]}
    assert by_id[str(out_leg.id)]["transfer_counterpart"] is None
    assert by_id[str(in_leg.id)]["transfer_counterpart"]["account_name"] == test_account.name
    assert "Secret Elsewhere" not in resp.text
