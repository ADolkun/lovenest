"""Reviewing accounts before the first import on connect (issue #53)."""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.providers.base import AccountData, ConnectionData, TransactionData
from app.services.connection_service import handle_oauth_callback, sync_connection


def _account(external_id: str, name: str = "Checking") -> AccountData:
    return AccountData(
        external_id=external_id,
        name=name,
        type="checking",
        balance=Decimal("100"),
        currency="USD",
    )


def _transaction(external_id: str) -> TransactionData:
    return TransactionData(
        external_id=external_id,
        description=external_id.upper(),
        amount=Decimal("10"),
        date=date.today(),
        type="debit",
        currency="USD",
    )


def _provider(accounts: list[AccountData]) -> AsyncMock:
    provider = AsyncMock()
    provider.refresh_credentials = AsyncMock(return_value={"token": "t"})
    provider.get_institution_logo = AsyncMock(return_value=None)
    provider.get_accounts = AsyncMock(return_value=accounts)
    provider.get_holdings = AsyncMock(return_value=[])
    provider.get_bills = AsyncMock(return_value=[])
    provider.get_transactions = AsyncMock(
        side_effect=lambda credentials, account_external_id, since=None, payee_source="auto": [
            _transaction(f"tx-{account_external_id}")
        ]
    )
    provider.handle_oauth_callback = AsyncMock(return_value=ConnectionData(
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        institution_name="Review Bank",
        credentials={"token": "x"},
        accounts=accounts,
    ))
    return provider


async def _connect(session, workspace, user, provider, **kwargs) -> BankConnection:
    with patch("app.services.connection_service.get_provider", return_value=provider), \
         patch("app.services.connection_service.detect_transfer_pairs", new_callable=AsyncMock), \
         patch("app.services.connection_service.stamp_primary_amount", new_callable=AsyncMock), \
         patch("app.services.connection_service.apply_rules_to_transaction", new_callable=AsyncMock):
        return await handle_oauth_callback(
            session, workspace.id, user.id, "code", provider_name="test", **kwargs
        )


async def _sync(session, connection, workspace, user, provider):
    with patch("app.services.connection_service.get_provider", return_value=provider), \
         patch("app.services.connection_service.detect_transfer_pairs", new_callable=AsyncMock), \
         patch("app.services.connection_service.stamp_primary_amount", new_callable=AsyncMock), \
         patch("app.services.connection_service.apply_rules_to_transaction", new_callable=AsyncMock):
        return await sync_connection(session, connection.id, workspace.id, user.id)


async def _external_ids(session: AsyncSession, connection: BankConnection) -> list[str]:
    rows = await session.execute(
        select(Account.external_id).where(Account.connection_id == connection.id)
    )
    return sorted(str(external_id) for external_id in rows.scalars().all())


@pytest.mark.asyncio
async def test_review_first_connect_imports_nothing(
    session: AsyncSession, test_user, test_workspace
):
    """The point of the choice: unwanted accounts are never created even once."""
    provider = _provider([_account("acc-1"), _account("acc-2", "Savings")])

    connection = await _connect(
        session, test_workspace, test_user, provider, account_allowlist=[]
    )

    assert (connection.settings or {})["account_allowlist"] == []
    assert await _external_ids(session, connection) == []


@pytest.mark.asyncio
async def test_review_first_connect_leaves_every_account_pending(
    client, auth_headers, session: AsyncSession, test_user, test_workspace
):
    """The user has been shown nothing yet, so nothing counts as deliberately excluded."""
    accounts = [_account("acc-1"), _account("acc-2", "Savings")]
    connection = await _connect(
        session, test_workspace, test_user, _provider(accounts), account_allowlist=[]
    )

    with patch(
        "app.services.connection_service.get_provider", return_value=_provider(accounts)
    ):
        resp = await client.get(
            f"/api/connections/{connection.id}/provider-accounts", headers=auth_headers
        )

    assert resp.status_code == 200
    assert [row["status"] for row in resp.json()] == ["pending", "pending"]


@pytest.mark.asyncio
async def test_first_sync_imports_exactly_the_reviewed_selection(
    client, auth_headers, session: AsyncSession, test_user, test_workspace
):
    accounts = [_account("acc-1"), _account("acc-2", "Savings")]
    connection = await _connect(
        session, test_workspace, test_user, _provider(accounts), account_allowlist=[]
    )

    resp = await client.patch(
        f"/api/connections/{connection.id}/settings",
        headers=auth_headers,
        json={"account_allowlist": ["acc-2"]},
    )
    assert resp.status_code == 200
    await session.refresh(connection)

    await _sync(session, connection, test_workspace, test_user, _provider(accounts))

    assert await _external_ids(session, connection) == ["acc-2"]


@pytest.mark.asyncio
async def test_connect_without_review_imports_everything(
    session: AsyncSession, test_user, test_workspace
):
    """Not taking the choice leaves the connection on the legacy default."""
    provider = _provider([_account("acc-1"), _account("acc-2", "Savings")])

    connection = await _connect(session, test_workspace, test_user, provider)

    assert "account_allowlist" not in (connection.settings or {})
    assert await _external_ids(session, connection) == ["acc-1", "acc-2"]


@pytest.mark.asyncio
async def test_provider_that_cannot_enumerate_falls_back_to_legacy(
    session: AsyncSession, test_user, test_workspace
):
    """An empty allowlist plus an empty account list would sync nothing forever."""
    provider = _provider([])

    connection = await _connect(
        session, test_workspace, test_user, provider, account_allowlist=[]
    )

    assert "account_allowlist" not in (connection.settings or {})

    await _sync(
        session, connection, test_workspace, test_user, _provider([_account("acc-1")])
    )

    assert await _external_ids(session, connection) == ["acc-1"]


@pytest.mark.asyncio
async def test_reconnect_does_not_reset_the_allowlist(
    session: AsyncSession, test_user, test_workspace
):
    """Re-authorising is not a re-review — the stored selection stands."""
    connection = BankConnection(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        provider="test",
        external_id="ext-reconnect",
        institution_name="Review Bank",
        credentials={"token": "old"},
        status="active",
        settings={"account_allowlist": ["acc-1"], "reviewed_account_ids": ["acc-1", "acc-2"]},
        created_at=datetime.now(timezone.utc),
    )
    session.add(connection)
    await session.commit()

    reconnected = await _connect(
        session,
        test_workspace,
        test_user,
        _provider([_account("acc-1"), _account("acc-2", "Savings")]),
        account_allowlist=[],
        reconnect_connection_id=connection.id,
    )

    assert (reconnected.settings or {})["account_allowlist"] == ["acc-1"]
    assert (reconnected.settings or {})["reviewed_account_ids"] == ["acc-1", "acc-2"]
