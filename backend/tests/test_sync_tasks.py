import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models.bank_connection import BankConnection
from app.tasks.sync_tasks import _sync_one


@pytest.mark.asyncio
async def test_scheduled_sync_triggers_provider_refresh(session, test_user, test_workspace):
    conn = BankConnection(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        provider="test",
        external_id="provider-conn",
        institution_name="Test Bank",
        credentials={"token": "fake"},
        status="active",
        last_sync_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.commit()

    def session_maker():
        return session

    with patch(
        "app.tasks.sync_tasks.connection_service.sync_connection",
        new_callable=AsyncMock,
    ) as sync_connection:
        await _sync_one(session_maker, conn.id, test_user.id)

    sync_connection.assert_awaited_once_with(
        session,
        conn.id,
        test_workspace.id,
        test_user.id,
        trigger_provider_refresh=True,
    )
