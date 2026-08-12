"""Per-connection account allowlist enforcement during sync (issue #49)."""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.bank_connection import BankConnection
from app.models.transaction import Transaction
from app.providers.base import AccountData, ConnectionData, HoldingData, TransactionData
from app.services.connection_service import handle_oauth_callback, sync_connection


async def _make_connection(
    session: AsyncSession,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    settings: dict | None = None,
    name: str = "Allowlist Bank",
) -> BankConnection:
    conn = BankConnection(
        id=uuid.uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        provider="test",
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        institution_name=name,
        credentials={"token": "fake"},
        status="active",
        settings=settings,
        created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.commit()
    await session.refresh(conn)
    return conn


def _account(external_id: str, name: str = "Checking") -> AccountData:
    return AccountData(
        external_id=external_id,
        name=name,
        type="checking",
        balance=Decimal("100"),
        currency="BRL",
    )


def _transaction(external_id: str) -> TransactionData:
    return TransactionData(
        external_id=external_id,
        description=external_id.upper(),
        amount=Decimal("10"),
        date=date.today(),
        type="debit",
        currency="BRL",
    )


def _holding(external_id: str, account_external_id: str | None) -> HoldingData:
    return HoldingData(
        external_id=external_id,
        name=f"Fund {external_id}",
        currency="BRL",
        current_value=Decimal("1000"),
        account_external_id=account_external_id,
    )


def _fake_provider(
    accounts: list[AccountData],
    transactions: dict[str, list[TransactionData]] | None = None,
    holdings: list[HoldingData] | None = None,
) -> AsyncMock:
    by_account = transactions or {}
    provider = AsyncMock()
    provider.refresh_credentials = AsyncMock(return_value={"token": "t"})
    provider.get_institution_logo = AsyncMock(return_value=None)
    provider.get_accounts = AsyncMock(return_value=accounts)
    provider.get_holdings = AsyncMock(return_value=list(holdings or []))
    provider.get_bills = AsyncMock(return_value=[])

    async def _get_transactions(
        credentials, account_external_id, since=None, payee_source="auto"
    ):
        return list(by_account.get(account_external_id, []))

    provider.get_transactions = AsyncMock(side_effect=_get_transactions)
    return provider


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


def _settings(connection: BankConnection) -> dict:
    return connection.settings or {}


@pytest.mark.asyncio
async def test_absent_allowlist_syncs_every_account(
    session: AsyncSession, test_user, test_workspace
):
    """Legacy behavior: with no allowlist, everything the provider returns lands."""
    conn = await _make_connection(session, test_user.id, test_workspace.id)
    provider = _fake_provider(
        [_account("acc-1"), _account("acc-2", "Savings")],
        transactions={"acc-1": [_transaction("tx-1")], "acc-2": [_transaction("tx-2")]},
        holdings=[_holding("h-1", "acc-1"), _holding("h-2", None)],
    )

    result, _ = await _sync(session, conn, test_workspace, test_user, provider)

    assert result.status == "active"
    assert await _external_ids(session, conn) == ["acc-1", "acc-2"]
    descriptions = (
        await session.execute(select(Transaction.external_id).where(Transaction.source == "sync"))
    ).scalars().all()
    assert set(descriptions) == {"tx-1", "tx-2"}
    assets = (await session.execute(select(Asset.external_id))).scalars().all()
    assert set(assets) == {"h-1", "h-2"}


@pytest.mark.asyncio
async def test_allowlist_creates_only_listed_accounts(
    session: AsyncSession, test_user, test_workspace
):
    conn = await _make_connection(
        session, test_user.id, test_workspace.id, settings={"account_allowlist": ["acc-1"]}
    )
    provider = _fake_provider(
        [_account("acc-1"), _account("acc-2", "Savings")],
        transactions={"acc-1": [_transaction("tx-1")], "acc-2": [_transaction("tx-2")]},
    )

    await _sync(session, conn, test_workspace, test_user, provider)

    assert await _external_ids(session, conn) == ["acc-1"]
    tx_ids = (
        await session.execute(select(Transaction.external_id).where(Transaction.source == "sync"))
    ).scalars().all()
    assert set(tx_ids) == {"tx-1"}


@pytest.mark.asyncio
async def test_empty_allowlist_syncs_nothing_without_error(
    session: AsyncSession, test_user, test_workspace
):
    """Present-and-empty is a valid state distinct from absent, not a failure."""
    conn = await _make_connection(
        session, test_user.id, test_workspace.id, settings={"account_allowlist": []}
    )
    provider = _fake_provider(
        [_account("acc-1")], transactions={"acc-1": [_transaction("tx-1")]}
    )

    result, _ = await _sync(session, conn, test_workspace, test_user, provider)

    assert result.status == "active"
    assert await _external_ids(session, conn) == []


@pytest.mark.asyncio
async def test_excluded_account_holdings_are_not_synced(
    session: AsyncSession, test_user, test_workspace
):
    """Exclusion beats the asset-sync toggle: holdings follow their account."""
    conn = await _make_connection(
        session, test_user.id, test_workspace.id,
        settings={"account_allowlist": ["acc-1"], "sync_assets": True},
    )
    provider = _fake_provider(
        [_account("acc-1"), _account("acc-2", "Brokerage")],
        holdings=[_holding("h-1", "acc-1"), _holding("h-2", "acc-2")],
    )

    await _sync(session, conn, test_workspace, test_user, provider)

    assets = (await session.execute(select(Asset.external_id))).scalars().all()
    assert assets == ["h-1"]


@pytest.mark.asyncio
async def test_unattributable_holdings_are_denied_when_allowlist_configured(
    session: AsyncSession, test_user, test_workspace
):
    """A holding with no account id cannot be shown to belong to an allowed account."""
    conn = await _make_connection(
        session, test_user.id, test_workspace.id, settings={"account_allowlist": ["acc-1"]}
    )
    provider = _fake_provider(
        [_account("acc-1")], holdings=[_holding("h-item-level", None)]
    )

    await _sync(session, conn, test_workspace, test_user, provider)

    assets = (await session.execute(select(Asset))).scalars().all()
    assert assets == []


@pytest.mark.asyncio
async def test_resync_never_resurrects_an_excluded_account(
    session: AsyncSession, test_user, test_workspace
):
    conn = await _make_connection(
        session, test_user.id, test_workspace.id, settings={"account_allowlist": ["acc-1"]}
    )
    provider = _fake_provider([_account("acc-1"), _account("acc-2", "Savings")])

    await _sync(session, conn, test_workspace, test_user, provider)
    await _sync(session, conn, test_workspace, test_user, provider)

    assert await _external_ids(session, conn) == ["acc-1"]


@pytest.mark.asyncio
async def test_excluding_an_account_leaves_imported_data_untouched(
    session: AsyncSession, test_user, test_workspace
):
    conn = await _make_connection(session, test_user.id, test_workspace.id)
    provider = _fake_provider(
        [_account("acc-1"), _account("acc-2", "Brokerage")],
        transactions={"acc-2": [_transaction("tx-2")]},
        holdings=[_holding("h-2", "acc-2")],
    )
    await _sync(session, conn, test_workspace, test_user, provider)

    conn.settings = {**(conn.settings or {}), "account_allowlist": ["acc-1"]}
    await session.commit()

    await _sync(session, conn, test_workspace, test_user, provider)

    assert await _external_ids(session, conn) == ["acc-1", "acc-2"]
    tx = (
        await session.execute(select(Transaction).where(Transaction.external_id == "tx-2"))
    ).scalar_one()
    assert tx.description == "TX-2"
    asset = (
        await session.execute(select(Asset).where(Asset.external_id == "h-2"))
    ).scalar_one()
    assert asset.is_archived is False


@pytest.mark.asyncio
async def test_excluded_account_transactions_survive_phantom_cleanup(
    session: AsyncSession, test_user, test_workspace
):
    """The one sync step that deletes rows must not reach an excluded account."""
    conn = await _make_connection(session, test_user.id, test_workspace.id)
    provider = _fake_provider([_account("acc-1"), _account("acc-2", "Savings")])
    await _sync(session, conn, test_workspace, test_user, provider)

    excluded_id = (
        await session.execute(select(Account.id).where(Account.external_id == "acc-2"))
    ).scalar_one()
    paired = Transaction(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        account_id=excluded_id, external_id="paid-1", description="RENT PAYMENT",
        amount=Decimal("500"), currency="BRL", date=date(2026, 6, 1),
        type="debit", source="sync", status="posted",
    )
    phantom = Transaction(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        account_id=excluded_id, external_id="paid-2", description="RENT PAYMENT",
        amount=Decimal("500"), currency="BRL", date=date(2026, 6, 2),
        type="debit", source="sync", status="posted",
    )
    session.add_all([paired, phantom])
    await session.flush()
    paired.transfer_pair_id = phantom.id
    conn.settings = {**(conn.settings or {}), "account_allowlist": ["acc-1"]}
    await session.commit()

    await _sync(session, conn, test_workspace, test_user, provider)

    surviving = (
        await session.execute(
            select(Transaction.external_id).where(Transaction.account_id == excluded_id)
        )
    ).scalars().all()
    assert {"paid-1", "paid-2"} <= set(surviving)


@pytest.mark.asyncio
async def test_two_connections_import_only_their_own_selection(
    session: AsyncSession, test_user, test_workspace
):
    """Same provider account list, different allowlists, independent outcomes."""
    accounts = [_account("acc-1"), _account("acc-2", "Savings")]
    first = await _make_connection(
        session, test_user.id, test_workspace.id,
        settings={"account_allowlist": ["acc-1"]}, name="First",
    )
    second = await _make_connection(
        session, test_user.id, test_workspace.id,
        settings={"account_allowlist": ["acc-2"]}, name="Second",
    )

    await _sync(session, first, test_workspace, test_user, _fake_provider(accounts))
    await _sync(session, second, test_workspace, test_user, _fake_provider(accounts))

    assert await _external_ids(session, first) == ["acc-1"]
    assert await _external_ids(session, second) == ["acc-2"]


@pytest.mark.asyncio
async def test_sync_records_the_provider_account_ids_it_saw(
    session: AsyncSession, test_user, test_workspace
):
    """Recorded before filtering, so a later review can tell new from unchecked."""
    conn = await _make_connection(
        session, test_user.id, test_workspace.id, settings={"account_allowlist": ["acc-1"]}
    )
    provider = _fake_provider([_account("acc-1"), _account("acc-2", "Savings")])

    result, _ = await _sync(session, conn, test_workspace, test_user, provider)

    assert _settings(result)["seen_account_ids"] == ["acc-1", "acc-2"]
    assert _settings(result)["account_allowlist"] == ["acc-1"]


@pytest.mark.asyncio
async def test_allowlist_survives_reconnect_and_applies_on_next_sync(
    session: AsyncSession, test_user, test_workspace
):
    conn = await _make_connection(
        session, test_user.id, test_workspace.id, settings={"account_allowlist": ["acc-1"]}
    )
    reconnect_provider = AsyncMock()
    reconnect_provider.handle_oauth_callback = AsyncMock(return_value=ConnectionData(
        external_id="reconnected-ext",
        institution_name="Allowlist Bank",
        credentials={"token": "fresh"},
        accounts=[],
    ))

    with patch("app.services.connection_service.get_provider", return_value=reconnect_provider):
        reconnected = await handle_oauth_callback(
            session, test_workspace.id, test_user.id, "fresh-token",
            provider_name="test", reconnect_connection_id=conn.id,
        )

    assert _settings(reconnected)["account_allowlist"] == ["acc-1"]

    provider = _fake_provider([_account("acc-1"), _account("acc-2", "Savings")])
    await _sync(session, reconnected, test_workspace, test_user, provider)

    assert await _external_ids(session, conn) == ["acc-1"]


@pytest.mark.asyncio
async def test_first_connect_honors_an_allowlist_chosen_in_the_connect_flow(
    session: AsyncSession, test_user, test_workspace
):
    """The very first import is filtered too — not just later syncs."""
    provider = _fake_provider([])
    provider.handle_oauth_callback = AsyncMock(return_value=ConnectionData(
        external_id="ext-first-connect",
        institution_name="First Connect Bank",
        credentials={"token": "x"},
        accounts=[_account("acc-1"), _account("acc-2", "Savings")],
    ))
    provider.get_holdings = AsyncMock(return_value=[
        _holding("h-1", "acc-1"), _holding("h-2", "acc-2"),
    ])

    with patch("app.services.connection_service.get_provider", return_value=provider), \
         patch(
             "app.services.connection_service.oauth_state.consume_state",
             new_callable=AsyncMock,
             return_value={
                 "user_id": str(test_user.id),
                 "workspace_id": str(test_workspace.id),
                 "provider": "test",
                 "flow_params": {"account_allowlist": ["acc-1"]},
             },
         ), \
         patch("app.services.connection_service.detect_transfer_pairs", new_callable=AsyncMock), \
         patch("app.services.connection_service.stamp_primary_amount", new_callable=AsyncMock), \
         patch("app.services.connection_service.apply_rules_to_transaction", new_callable=AsyncMock):
        connection = await handle_oauth_callback(
            session, test_workspace.id, test_user.id, "code", state="stored-state",
        )

    assert _settings(connection)["account_allowlist"] == ["acc-1"]
    assert _settings(connection)["seen_account_ids"] == ["acc-1", "acc-2"]
    assert _settings(connection)["flow_params"] == {}
    assert await _external_ids(session, connection) == ["acc-1"]
    assets = (await session.execute(select(Asset.external_id))).scalars().all()
    assert assets == ["h-1"]


@pytest.mark.asyncio
async def test_allowlist_saved_through_the_api_is_enforced_by_the_next_sync(
    client, auth_headers, session: AsyncSession, test_user, test_workspace
):
    """The settings write path and the sync filter meet: saving a selection changes the import."""
    connection = await _make_connection(session, test_user.id, test_workspace.id)

    resp = await client.patch(
        f"/api/connections/{connection.id}/settings",
        headers=auth_headers,
        json={"account_allowlist": ["acc-1"]},
    )
    assert resp.status_code == 200
    await session.refresh(connection)

    provider = _fake_provider([_account("acc-1"), _account("acc-2")])
    await _sync(session, connection, test_workspace, test_user, provider)

    assert await _external_ids(session, connection) == ["acc-1"]
