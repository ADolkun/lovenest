"""Real PostgreSQL races for pending lifecycle ownership (never the app DB)."""
import asyncio
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import null, update

from app.models.account import Account
from app.models.transaction import Transaction
from app.models.user import User
from app.models.workspace import Workspace
from app.services import pending_transaction_service as lifecycle
from app.services.rule_engine import apply_rule_actions
from app.services.transaction_service import toggle_ignore_transaction
from tests.test_pending_transaction_service import DAY, NOW, coverage, incoming


@pytest.fixture
async def pending_pg(postgres_sessions):
    data = incoming()
    async with postgres_sessions() as session:
        user = User(id=uuid.uuid4(), email=f"synthetic-{uuid.uuid4().hex}@example.test",
                    hashed_password="synthetic-unused", is_active=True)
        session.add(user)
        await session.flush()
        workspace = Workspace(id=uuid.uuid4(), name="Synthetic", created_by_user_id=user.id,
                              default_currency="BRL")
        session.add(workspace)
        await session.flush()
        account = Account(id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id,
                          name="Synthetic", type="checking", balance=0, currency="BRL")
        session.add(account)
        await session.flush()
        tx = Transaction(id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id,
                         account_id=account.id, external_id="hold", description=data.description,
                         amount=data.amount, currency="BRL", date=DAY, type="debit", source="sync",
                         status="pending", raw_data=data.raw_data,
                         sync_metadata={"pending_missing_since": "2026-10-08T12:00:00+00:00",
                                        "pending_last_missing_at": "2026-10-08T12:00:00+00:00"})
        session.add(tx)
        await session.commit()
        return account.id, tx.id, workspace.id


@pytest.mark.parametrize("old_state", ["missing", "sql_null"])
async def test_manual_commit_wins_retirement_cas(postgres_sessions, pending_pg, old_state):
    account_id, tx_id, workspace_id = pending_pg
    if old_state == "sql_null":
        async with postgres_sessions() as setup:
            await setup.execute(update(Transaction).where(Transaction.id == tx_id).values(sync_metadata=null()))
            await setup.commit()
    ready, proceed = asyncio.Event(), asyncio.Event()
    persist = lifecycle._persist_state

    async def paused(*args):
        ready.set()
        await asyncio.wait_for(proceed.wait(), timeout=10)
        await persist(*args)

    async with postgres_sessions() as syncing:
        with patch.object(lifecycle, "_persist_state", side_effect=paused):
            task = asyncio.create_task(lifecycle.reconcile_pending(syncing, account_id, [], coverage()))
            try:
                await asyncio.wait_for(ready.wait(), timeout=10)
                async with postgres_sessions() as manual:
                    await toggle_ignore_transaction(manual, tx_id, workspace_id)
                proceed.set()
                await asyncio.wait_for(task, timeout=10)
                await syncing.commit()
            finally:
                proceed.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        tx = await syncing.get(Transaction, tx_id)
        assert tx is not None and tx.is_ignored
        assert tx.sync_metadata["pending_manual_override"] is True
        assert tx.sync_metadata["pending_ignore_owned"] is False
        assert "pending_retired_at" not in tx.sync_metadata


@pytest.mark.parametrize("claim", ["financial_edit", "ignore_rule"])
async def test_stale_manual_claim_preserves_new_retirement_audit(postgres_sessions, pending_pg, claim):
    account_id, tx_id, _ = pending_pg
    async with postgres_sessions() as manual:
        stale = await manual.get(Transaction, tx_id)
        assert stale is not None and not stale.is_ignored
        async with postgres_sessions() as syncing:
            await lifecycle.reconcile_pending(syncing, account_id, [], coverage())
            await syncing.commit()
        # The user's already-loaded object predates the retirement. Only its
        # ownership fields may replace the latest lifecycle audit at flush.
        if claim == "financial_edit":
            lifecycle.preserve_pending_override(stale)
            stale.amount = 80
        else:
            apply_rule_actions([{"op": "ignore"}], stale, category_already_set=False)
        await manual.commit()
        await manual.refresh(stale)
        assert stale.is_ignored
        assert stale.sync_metadata["pending_retired_at"] == NOW.isoformat()
        assert stale.sync_metadata["pending_manual_override"] is True
        assert stale.sync_metadata["pending_ignore_owned"] is False
        if claim == "financial_edit":
            assert stale.amount == 80


async def test_manual_unignore_keeps_audit_and_prevents_retirement(postgres_sessions, pending_pg):
    account_id, tx_id, workspace_id = pending_pg
    async with postgres_sessions() as syncing:
        await lifecycle.reconcile_pending(syncing, account_id, [], coverage())
        await syncing.commit()
    async with postgres_sessions() as manual:
        tx = await toggle_ignore_transaction(manual, tx_id, workspace_id)
        assert tx is not None and not tx.is_ignored
        assert tx.sync_metadata is not None
        assert tx.sync_metadata["pending_retired_at"] == NOW.isoformat()
    async with postgres_sessions() as syncing:
        await lifecycle.reconcile_pending(syncing, account_id, [], coverage())
        await syncing.commit()
        tx = await syncing.get(Transaction, tx_id)
        assert tx is not None and not tx.is_ignored
        assert tx.sync_metadata["pending_manual_override"] is True
