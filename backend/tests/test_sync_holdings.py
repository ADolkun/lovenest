"""Cover the Asset lifecycle through `_sync_holdings`.

The sync contract is a bit subtle: provider data drives creation *and*
closure of Assets, but user-set fields (sell_date, group_id) are load-
bearing and must never be overwritten. These tests pin the full matrix:
new/existing × active/withdrawn, same-day vs next-day re-syncs,
historical seeding idempotency, and sparse-field merging.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_value import AssetValue
from app.models.bank_connection import BankConnection
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.providers import register_provider
from app.providers.base import (
    AccountData,
    BankProvider,
    ConnectionData,
    HoldingData,
    TransactionData,
)
from app.services.connection_service import _sync_holdings


# ---------------------------------------------------------------------------
# Mock provider — deterministic replacement for Pluggy/Belvo in tests.
# ---------------------------------------------------------------------------


class _MockProvider(BankProvider):
    """BankProvider that returns a caller-supplied list of holdings.

    We store the list on a class-level mutable slot so each test can
    reconfigure the response without juggling singleton re-registration.
    """

    _holdings: list[HoldingData] = []
    _raise: Optional[Exception] = None

    @property
    def name(self) -> str:
        return "mock"

    async def get_oauth_url(
        self, redirect_uri: str, state: str, flow_params=None
    ) -> str:  # pragma: no cover
        return "http://mock"

    async def handle_oauth_callback(self, code: str) -> ConnectionData:  # pragma: no cover
        raise NotImplementedError

    async def get_accounts(self, credentials: dict) -> list[AccountData]:  # pragma: no cover
        return []

    async def get_transactions(
        self, credentials: dict, account_external_id: str, since=None, payee_source: str = "auto"
    ) -> list[TransactionData]:  # pragma: no cover
        return []

    async def refresh_credentials(self, credentials: dict) -> dict:  # pragma: no cover
        return credentials

    async def get_holdings(self, credentials: dict) -> list[HoldingData]:
        if _MockProvider._raise is not None:
            raise _MockProvider._raise
        return list(_MockProvider._holdings)


@pytest.fixture(autouse=True)
def _register_mock_provider():
    """Auto-register mock provider for every test in this module."""
    register_provider("mock", _MockProvider)
    _MockProvider._holdings = []
    _MockProvider._raise = None
    yield


# ---------------------------------------------------------------------------
# Fixtures specific to holdings sync.
# ---------------------------------------------------------------------------


def _connection(user_id, *, external_id: str = "item-abc", workspace_id=None) -> BankConnection:
    return BankConnection(
        id=uuid.uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        provider="mock",
        external_id=external_id,
        institution_name="Mock Bank",
        credentials={"item_id": external_id},
        status="active",
        created_at=datetime.now(timezone.utc),
    )


@pytest_asyncio.fixture
async def mock_connection(session: AsyncSession, test_user: User) -> BankConnection:
    conn = _connection(test_user.id)
    session.add(conn)
    await session.commit()
    await session.refresh(conn)
    return conn


def _holding(
    *,
    external_id: str = "h-1",
    name: str = "CDB NU",
    current_value: Decimal = Decimal("1000.00"),
    purchase_price: Optional[Decimal] = Decimal("900.00"),
    purchase_date: Optional[date] = None,
    quantity: Optional[Decimal] = Decimal("1"),
    is_withdrawn: bool = False,
    metadata: Optional[dict] = None,
    account_external_id: Optional[str] = None,
) -> HoldingData:
    return HoldingData(
        external_id=external_id,
        name=name,
        currency="BRL",
        current_value=current_value,
        quantity=quantity,
        purchase_price=purchase_price,
        purchase_date=purchase_date,
        is_withdrawn=is_withdrawn,
        metadata=metadata or {"status": "ACTIVE"},
        account_external_id=account_external_id,
    )


async def _assets_for(session: AsyncSession, user: User) -> list[Asset]:
    rows = await session.execute(select(Asset).where(Asset.user_id == user.id))
    return list(rows.scalars().all())


async def _values_for(session: AsyncSession, asset_id) -> list[AssetValue]:
    rows = await session.execute(
        select(AssetValue).where(AssetValue.asset_id == asset_id).order_by(AssetValue.date)
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_active_holding_creates_asset_wallet_and_today_value(
    session: AsyncSession, test_user: User, mock_connection: BankConnection
):
    """Happy path: one fresh ACTIVE holding → Asset + wallet + today's value."""
    today = date.today()
    _MockProvider._holdings = [
        _holding(current_value=Decimal("1234.56"), purchase_date=today - timedelta(days=10)),
    ]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()

    assets = await _assets_for(session, test_user)
    assert len(assets) == 1
    asset = assets[0]
    assert asset.external_id == "h-1"
    assert asset.source == "mock"
    assert asset.connection_id == mock_connection.id
    assert asset.sell_date is None
    assert asset.is_archived is False

    # Wallet auto-created and the asset attached.
    assert asset.group_id is not None
    wallet = await session.get(AssetGroup, asset.group_id)
    assert wallet is not None
    assert wallet.connection_id == mock_connection.id

    values = await _values_for(session, asset.id)
    # Historical seed at purchase_date + today's snapshot.
    dates = [v.date for v in values]
    assert today - timedelta(days=10) in dates
    assert today in dates


@pytest.mark.asyncio
async def test_new_withdrawn_holding_is_skipped_entirely(
    session: AsyncSession, test_user: User, mock_connection: BankConnection
):
    """A holding that's already TOTAL_WITHDRAWAL on first sight should not
    create a zero-balance Asset — there's no meaningful history to show."""
    _MockProvider._holdings = [
        _holding(external_id="dead-1", current_value=Decimal("0"), is_withdrawn=True),
    ]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()

    assert await _assets_for(session, test_user) == []


@pytest.mark.asyncio
async def test_withdrawn_existing_asset_gets_sell_date(
    session: AsyncSession, test_user: User, mock_connection: BankConnection
):
    """Position was ACTIVE on a prior sync, now TOTAL_WITHDRAWAL. We set
    sell_date to today, leave historical AssetValues intact, and do not
    append a fresh zero value."""
    today = date.today()

    # First sync: active.
    _MockProvider._holdings = [_holding(external_id="h-1", current_value=Decimal("500"))]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()
    [asset] = await _assets_for(session, test_user)
    values_before = await _values_for(session, asset.id)
    assert asset.sell_date is None
    assert len(values_before) >= 1

    # Second sync: withdrawn.
    _MockProvider._holdings = [
        _holding(external_id="h-1", current_value=Decimal("0"), is_withdrawn=True)
    ]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()
    await session.refresh(asset)

    assert asset.sell_date == today
    values_after = await _values_for(session, asset.id)
    # Historical values preserved; no zero appended.
    assert len(values_after) == len(values_before)
    assert all(v.amount != Decimal("0") for v in values_after)


@pytest.mark.asyncio
async def test_withdrawn_existing_does_not_overwrite_user_sell_date(
    session: AsyncSession, test_user: User, mock_connection: BankConnection
):
    """If the user manually marked sell_date (e.g. they know the exact
    redemption date), a later provider-reported withdrawal must not clobber it."""
    user_sell_date = date.today() - timedelta(days=30)

    asset = Asset(
        id=uuid.uuid4(),
        user_id=test_user.id,
        connection_id=mock_connection.id,
        source="mock",
        external_id="h-1",
        name="CDB NU",
        type="investment",
        currency="BRL",
        sell_date=user_sell_date,
    )
    session.add(asset)
    await session.commit()

    _MockProvider._holdings = [
        _holding(external_id="h-1", current_value=Decimal("0"), is_withdrawn=True)
    ]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()
    await session.refresh(asset)

    assert asset.sell_date == user_sell_date


@pytest.mark.asyncio
async def test_user_sold_active_on_provider_stops_value_updates(
    session: AsyncSession, test_user: User, mock_connection: BankConnection
):
    """User marked the asset sold but the provider still reports it active.
    Respect the user: no new AssetValues should be appended, so dashboards
    don't keep moving for a position the user considers closed."""
    user_sell_date = date.today() - timedelta(days=5)

    asset = Asset(
        id=uuid.uuid4(),
        user_id=test_user.id,
        connection_id=mock_connection.id,
        source="mock",
        external_id="h-1",
        name="CDB NU",
        type="investment",
        currency="BRL",
        sell_date=user_sell_date,
    )
    session.add(asset)
    await session.commit()

    _MockProvider._holdings = [_holding(external_id="h-1", current_value=Decimal("999.99"))]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()
    await session.refresh(asset)

    assert asset.sell_date == user_sell_date  # untouched
    values = await _values_for(session, asset.id)
    assert values == []  # no new values recorded


@pytest.mark.asyncio
async def test_same_day_resync_updates_asset_value_in_place(
    session: AsyncSession, test_user: User, mock_connection: BankConnection
):
    """Running sync twice in one day must not duplicate the day's AssetValue —
    the second pass updates the amount in the existing row."""
    _MockProvider._holdings = [_holding(external_id="h-1", current_value=Decimal("100"))]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()
    [asset] = await _assets_for(session, test_user)

    _MockProvider._holdings = [_holding(external_id="h-1", current_value=Decimal("110"))]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()

    values = await _values_for(session, asset.id)
    today = date.today()
    today_rows = [v for v in values if v.date == today]
    assert len(today_rows) == 1
    assert today_rows[0].amount == Decimal("110")


@pytest.mark.asyncio
async def test_sparse_fields_are_preserved_on_later_sync(
    session: AsyncSession, test_user: User, mock_connection: BankConnection
):
    """Pluggy returns purchase_price/purchase_date/units on some syncs and
    null on others. Earlier non-null values must not be clobbered by later
    nulls — otherwise the user loses provenance data permanently."""
    _MockProvider._holdings = [
        _holding(
            external_id="h-1",
            current_value=Decimal("500"),
            purchase_price=Decimal("450"),
            quantity=Decimal("10"),
            purchase_date=date.today() - timedelta(days=100),
        )
    ]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()
    [asset] = await _assets_for(session, test_user)
    assert asset.purchase_price == Decimal("450")
    assert asset.units == Decimal("10")

    _MockProvider._holdings = [
        _holding(
            external_id="h-1",
            current_value=Decimal("520"),
            purchase_price=None,
            quantity=None,
            purchase_date=None,
        )
    ]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()
    await session.refresh(asset)

    assert asset.purchase_price == Decimal("450")
    assert asset.units == Decimal("10")


@pytest.mark.asyncio
async def test_historical_seed_is_idempotent(
    session: AsyncSession, test_user: User, mock_connection: BankConnection
):
    """Historical seed at purchase_date should be inserted exactly once —
    running sync 3× still leaves a single row at that date."""
    purchase = date.today() - timedelta(days=60)
    _MockProvider._holdings = [
        _holding(
            external_id="h-1",
            current_value=Decimal("1000"),
            purchase_price=Decimal("900"),
            purchase_date=purchase,
        )
    ]

    assert mock_connection.credentials is not None
    for _ in range(3):
        await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
        await session.commit()

    [asset] = await _assets_for(session, test_user)
    purchase_rows = [v for v in await _values_for(session, asset.id) if v.date == purchase]
    assert len(purchase_rows) == 1
    assert purchase_rows[0].amount == Decimal("900")


@pytest.mark.asyncio
async def test_historical_seed_respects_prior_manual_value(
    session: AsyncSession, test_user: User, mock_connection: BankConnection
):
    """If a manual AssetValue already exists at purchase_date, the seed must
    not overwrite it. Users' numbers always win over provider defaults."""
    purchase = date.today() - timedelta(days=60)
    asset = Asset(
        id=uuid.uuid4(),
        user_id=test_user.id,
        connection_id=mock_connection.id,
        source="mock",
        external_id="h-1",
        name="CDB NU",
        type="investment",
        currency="BRL",
    )
    session.add(asset)
    session.add(
        AssetValue(
            asset_id=asset.id,
            amount=Decimal("777"),
            date=purchase,
            source="manual",
        )
    )
    await session.commit()

    _MockProvider._holdings = [
        _holding(
            external_id="h-1",
            current_value=Decimal("1000"),
            purchase_price=Decimal("900"),
            purchase_date=purchase,
        )
    ]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()

    rows = [v for v in await _values_for(session, asset.id) if v.date == purchase]
    assert len(rows) == 1
    assert rows[0].amount == Decimal("777")
    assert rows[0].source == "manual"


@pytest.mark.asyncio
async def test_disappeared_holding_gets_archived(
    session: AsyncSession, test_user: User, mock_connection: BankConnection
):
    """If a previously-synced holding is no longer in the provider response
    at all (e.g. broker removed it), archive the Asset so it stops cluttering
    the UI — but don't delete it, history matters."""
    _MockProvider._holdings = [
        _holding(external_id="h-1", current_value=Decimal("500")),
        _holding(external_id="h-2", current_value=Decimal("200")),
    ]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()

    # Only h-1 comes back on the next sync. h-2 must be archived.
    _MockProvider._holdings = [_holding(external_id="h-1", current_value=Decimal("510"))]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()

    assets = {a.external_id: a for a in await _assets_for(session, test_user)}
    assert assets["h-1"].is_archived is False
    assert assets["h-2"].is_archived is True


@pytest.mark.asyncio
async def test_returned_holding_is_unarchived_after_reconnect(
    session: AsyncSession, test_user: User, mock_connection: BankConnection
):
    """A holding archived on unlink should become active again when reconnected."""
    _MockProvider._holdings = [_holding(external_id="h-1", current_value=Decimal("500"))]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()

    # Next sync does not include h-1 -> archived.
    _MockProvider._holdings = []

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()

    assets = {a.external_id: a for a in await _assets_for(session, test_user)}
    assert assets["h-1"].is_archived is True

    # Simulate unlink/reconnect: old connection disappears and a new one syncs.
    await session.delete(mock_connection)
    await session.commit()

    reconnected = BankConnection(
        id=uuid.uuid4(),
        user_id=test_user.id,
        provider="mock",
        external_id="item-reconnected",
        institution_name="Mock Bank",
        credentials={"item_id": "item-reconnected"},
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    session.add(reconnected)
    await session.commit()

    # Later sync includes h-1 again -> should unarchive.
    _MockProvider._holdings = [_holding(external_id="h-1", current_value=Decimal("525"))]

    assert reconnected.credentials is not None
    await _sync_holdings(session, test_user.id, reconnected, reconnected.credentials)
    await session.commit()

    assets = {a.external_id: a for a in await _assets_for(session, test_user)}
    assert assets["h-1"].is_archived is False


@pytest.mark.asyncio
async def test_user_archived_holding_stays_archived_on_same_connection_sync(
    session: AsyncSession, test_user: User, mock_connection: BankConnection
):
    """Sync must not override a user archive decision on the same connection."""
    _MockProvider._holdings = [_holding(external_id="h-1", current_value=Decimal("500"))]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()

    assets = {a.external_id: a for a in await _assets_for(session, test_user)}
    assets["h-1"].is_archived = True
    await session.commit()

    _MockProvider._holdings = [_holding(external_id="h-1", current_value=Decimal("510"))]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()

    assets = {a.external_id: a for a in await _assets_for(session, test_user)}
    assert assets["h-1"].is_archived is True


@pytest.mark.asyncio
async def test_user_moved_asset_to_custom_wallet_not_overridden(
    session: AsyncSession, test_user: User, mock_connection: BankConnection
):
    """If the user moves a synced asset to a custom wallet, later syncs must
    not drag it back to the provider's default wallet."""
    _MockProvider._holdings = [_holding(external_id="h-1", current_value=Decimal("500"))]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()
    [asset] = await _assets_for(session, test_user)
    default_wallet_id = asset.group_id

    # Simulate user moving to a custom wallet.
    custom_wallet = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        name="US Stocks",
        icon="briefcase",
        color="#000",
        position=5,
        source="manual",
    )
    session.add(custom_wallet)
    await session.commit()
    asset.group_id = custom_wallet.id
    await session.commit()

    _MockProvider._holdings = [_holding(external_id="h-1", current_value=Decimal("520"))]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()
    await session.refresh(asset)

    assert asset.group_id == custom_wallet.id
    assert asset.group_id != default_wallet_id


@pytest.mark.asyncio
async def test_provider_error_is_swallowed_without_side_effects(
    session: AsyncSession, test_user: User, mock_connection: BankConnection
):
    """/investments errors must not crash the account/transaction sync that
    just succeeded — silent failure here is intentional."""
    _MockProvider._raise = RuntimeError("provider 500")

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    # No assets, no exception.
    assert await _assets_for(session, test_user) == []


@pytest.mark.asyncio
async def test_next_day_sync_appends_new_asset_value(
    session: AsyncSession, test_user: User, mock_connection: BankConnection
):
    """Value history accumulates: a pre-existing yesterday row must remain
    alongside today's new row."""
    yesterday = date.today() - timedelta(days=1)
    asset = Asset(
        id=uuid.uuid4(),
        user_id=test_user.id,
        connection_id=mock_connection.id,
        source="mock",
        external_id="h-1",
        name="CDB NU",
        type="investment",
        currency="BRL",
    )
    session.add(asset)
    session.add(AssetValue(asset_id=asset.id, amount=Decimal("100"), date=yesterday, source="sync"))
    await session.commit()

    _MockProvider._holdings = [_holding(external_id="h-1", current_value=Decimal("105"))]

    assert mock_connection.credentials is not None
    await _sync_holdings(session, test_user.id, mock_connection, mock_connection.credentials)
    await session.commit()

    rows = await _values_for(session, asset.id)
    assert len(rows) == 2
    assert rows[0].date == yesterday and rows[0].amount == Decimal("100")
    assert rows[1].date == date.today() and rows[1].amount == Decimal("105")


# ---------------------------------------------------------------------------
# Workspace isolation (#75)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def second_workspace(session: AsyncSession, test_user: User) -> Workspace:
    ws = Workspace(
        id=uuid.uuid4(),
        name="Casa",
        kind="household",
        created_by_user_id=test_user.id,
        default_currency="BRL",
        locale="pt-BR",
    )
    session.add(ws)
    await session.flush()
    session.add(
        WorkspaceMember(id=uuid.uuid4(), workspace_id=ws.id, user_id=test_user.id, role="owner")
    )
    await session.commit()
    return ws


@pytest.mark.asyncio
async def test_sync_never_touches_the_other_workspace_wallet(
    session: AsyncSession,
    test_user: User,
    mock_connection: BankConnection,
    second_workspace: Workspace,
):
    """One user, two workspaces, one provider external_id.

    The real setup: the same SimpleFIN member linked once per workspace, so
    both connections carry an identical (user_id, source, external_id). Syncing
    the second workspace must build its own wallet, not adopt the first's.
    """
    # A wallet already exists in the first workspace for this external_id.
    personal_wallet = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=mock_connection.workspace_id,
        name="Mock Bank",
        source="mock",
        connection_id=mock_connection.id,
        external_id="item-abc",
    )
    session.add(personal_wallet)

    # Second connection: same user, same provider item, other workspace.
    other_conn = _connection(test_user.id, workspace_id=second_workspace.id)
    session.add(other_conn)
    await session.commit()

    _MockProvider._holdings = [_holding(external_id="h-1", current_value=Decimal("500"))]
    assert other_conn.credentials is not None
    await _sync_holdings(session, test_user.id, other_conn, other_conn.credentials)
    await session.commit()

    await session.refresh(personal_wallet)
    assert personal_wallet.connection_id == mock_connection.id
    assert personal_wallet.workspace_id == mock_connection.workspace_id

    assets = await _assets_for(session, test_user)
    assert len(assets) == 1
    asset = assets[0]
    assert asset.workspace_id == second_workspace.id
    assert asset.group_id is not None and asset.group_id != personal_wallet.id
    wallet = await session.get(AssetGroup, asset.group_id)
    assert wallet is not None and wallet.workspace_id == second_workspace.id
    # Names and positions are per-workspace: neither the " 2" suffix nor the
    # position counter may disclose the other workspace's wallet count.
    assert wallet.name == "Mock Bank"
    assert wallet.position == 0


@pytest.mark.asyncio
async def test_sync_does_not_adopt_the_other_workspace_orphan_asset(
    session: AsyncSession,
    test_user: User,
    mock_connection: BankConnection,
    second_workspace: Workspace,
):
    """An asset orphaned by a disconnect in one workspace stays there."""
    orphan = Asset(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=mock_connection.workspace_id,
        connection_id=None,
        source="mock",
        external_id="h-1",
        name="CDB NU",
        type="investment",
        currency="BRL",
        is_archived=True,
    )
    session.add(orphan)
    other_conn = _connection(
        test_user.id, external_id="item-other", workspace_id=second_workspace.id
    )
    session.add(other_conn)
    await session.commit()

    _MockProvider._holdings = [_holding(external_id="h-1", current_value=Decimal("500"))]
    assert other_conn.credentials is not None
    await _sync_holdings(session, test_user.id, other_conn, other_conn.credentials)
    await session.commit()

    await session.refresh(orphan)
    assert orphan.connection_id is None
    assert orphan.is_archived is True

    assets = await _assets_for(session, test_user)
    assert len(assets) == 2
    fresh = [a for a in assets if a.id != orphan.id][0]
    assert fresh.workspace_id == second_workspace.id
    assert fresh.connection_id == other_conn.id


# ---------------------------------------------------------------------------
# One wallet per provider account (#76)
# ---------------------------------------------------------------------------


def _account_row(connection: BankConnection, user: User, external_id: str, name: str) -> Account:
    return Account(
        id=uuid.uuid4(),
        user_id=user.id,
        workspace_id=connection.workspace_id,
        connection_id=connection.id,
        external_id=external_id,
        name=name,
        type="investment",
        balance=Decimal("0"),
        currency="USD",
    )


@pytest_asyncio.fixture
async def brokerage_connection(
    session: AsyncSession, test_user: User, test_workspace: Workspace
) -> BankConnection:
    """A connection whose provider attributes holdings to four accounts."""
    conn = _connection(test_user.id, workspace_id=test_workspace.id)
    session.add(conn)
    session.add_all(
        [
            _account_row(conn, test_user, "acc-tod", "Individual - TOD (5044)"),
            _account_row(conn, test_user, "acc-roth", "ROTH IRA (6548)"),
        ]
    )
    await session.commit()
    await session.refresh(conn)
    return conn


async def _wallet_of(session: AsyncSession, asset: Asset) -> AssetGroup:
    assert asset.group_id is not None
    wallet = await session.get(AssetGroup, asset.group_id)
    assert wallet is not None
    return wallet


@pytest.mark.asyncio
async def test_holdings_land_in_a_wallet_per_provider_account(
    session: AsyncSession, test_user: User, brokerage_connection: BankConnection
):
    """Two accounts, three holdings → two wallets named after the accounts.

    Same ticker in two accounts is two Holdings with different basis, so they
    must not collapse into one wallet.
    """
    _MockProvider._holdings = [
        _holding(external_id="h-1", account_external_id="acc-tod"),
        _holding(external_id="h-2", account_external_id="acc-roth"),
        _holding(external_id="h-3", account_external_id="acc-tod"),
    ]

    assert brokerage_connection.credentials is not None
    await _sync_holdings(
        session, test_user.id, brokerage_connection, brokerage_connection.credentials
    )
    await session.commit()

    assets = {a.external_id: a for a in await _assets_for(session, test_user)}
    assert assets["h-1"].group_id == assets["h-3"].group_id
    assert assets["h-1"].group_id != assets["h-2"].group_id

    tod = await _wallet_of(session, assets["h-1"])
    roth = await _wallet_of(session, assets["h-2"])
    assert tod.name == "Individual - TOD (5044)"
    assert roth.name == "ROTH IRA (6548)"
    assert tod.external_id == "acc-tod" and roth.external_id == "acc-roth"
    # Tax character is user-set: no provider reports it, so a wallet named
    # "ROTH IRA" is still born taxable.
    assert roth.tax_treatment == "taxable"

    assert assets["h-1"].account_external_id == "acc-tod"
    assert assets["h-2"].account_external_id == "acc-roth"


@pytest.mark.asyncio
async def test_unattributed_holdings_keep_a_connection_level_wallet(
    session: AsyncSession, test_user: User, brokerage_connection: BankConnection
):
    """Unset account attribution means *unattributable*, not *none*.

    Pluggy reports investments at item level, so those holdings stay in the
    connection's own wallet instead of being forced into an account's.
    """
    _MockProvider._holdings = [
        _holding(external_id="h-1", account_external_id=None),
        _holding(external_id="h-2", account_external_id="acc-tod"),
    ]

    assert brokerage_connection.credentials is not None
    await _sync_holdings(
        session, test_user.id, brokerage_connection, brokerage_connection.credentials
    )
    await session.commit()

    assets = {a.external_id: a for a in await _assets_for(session, test_user)}
    unattributed = await _wallet_of(session, assets["h-1"])
    assert unattributed.external_id == brokerage_connection.external_id
    assert unattributed.name == "Mock Bank"
    assert assets["h-1"].account_external_id is None
    assert assets["h-2"].group_id != assets["h-1"].group_id


@pytest.mark.asyncio
async def test_existing_connection_wallet_splits_by_account(
    session: AsyncSession, test_user: User, brokerage_connection: BankConnection
):
    """The pre-#76 wallet spanning every account splits on the next sync."""
    legacy = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=brokerage_connection.workspace_id,
        name="Mock Bank",
        source="mock",
        connection_id=brokerage_connection.id,
        external_id=brokerage_connection.external_id,
    )
    session.add(legacy)
    await session.flush()
    session.add_all(
        [
            Asset(
                id=uuid.uuid4(),
                user_id=test_user.id,
                workspace_id=brokerage_connection.workspace_id,
                connection_id=brokerage_connection.id,
                source="mock",
                external_id=ext,
                name=f"Holding {ext}",
                type="investment",
                currency="USD",
                group_id=legacy.id,
            )
            for ext in ("h-1", "h-2")
        ]
    )
    await session.commit()

    _MockProvider._holdings = [
        _holding(external_id="h-1", account_external_id="acc-tod"),
        _holding(external_id="h-2", account_external_id="acc-roth"),
    ]
    assert brokerage_connection.credentials is not None
    await _sync_holdings(
        session, test_user.id, brokerage_connection, brokerage_connection.credentials
    )
    await session.commit()

    assets = {a.external_id: a for a in await _assets_for(session, test_user)}
    assert assets["h-1"].group_id != legacy.id
    assert assets["h-2"].group_id != legacy.id
    assert (await _wallet_of(session, assets["h-1"])).external_id == "acc-tod"
    assert (await _wallet_of(session, assets["h-2"])).external_id == "acc-roth"


@pytest.mark.asyncio
async def test_holding_follows_the_account_the_provider_moves_it_to(
    session: AsyncSession, test_user: User, brokerage_connection: BankConnection
):
    """Attribution is the provider's to state, and to restate.

    A position transferred between two of the same broker's accounts changes
    tax character, so the wallet has to follow it — the deliberate cost being
    that a re-file by hand between two synced wallets does not survive a sync.
    """
    _MockProvider._holdings = [_holding(external_id="h-1", account_external_id="acc-tod")]
    assert brokerage_connection.credentials is not None
    await _sync_holdings(
        session, test_user.id, brokerage_connection, brokerage_connection.credentials
    )
    await session.commit()
    [asset] = await _assets_for(session, test_user)
    assert (await _wallet_of(session, asset)).external_id == "acc-tod"

    _MockProvider._holdings = [_holding(external_id="h-1", account_external_id="acc-roth")]
    await _sync_holdings(
        session, test_user.id, brokerage_connection, brokerage_connection.credentials
    )
    await session.commit()
    await session.refresh(asset)

    assert asset.account_external_id == "acc-roth"
    assert (await _wallet_of(session, asset)).external_id == "acc-roth"


@pytest.mark.asyncio
async def test_allowlist_gates_which_accounts_holdings_sync_from(
    session: AsyncSession, test_user: User, brokerage_connection: BankConnection
):
    """The allowlist reaches holdings, not just accounts.

    `_syncable_accounts` hands down the surviving account ids and holdings
    filter on them, so excluding an account stops its positions importing too.
    """
    _MockProvider._holdings = [
        _holding(external_id="h-tod", account_external_id="acc-tod"),
        _holding(external_id="h-roth", account_external_id="acc-roth"),
    ]

    assert brokerage_connection.credentials is not None
    await _sync_holdings(
        session,
        test_user.id,
        brokerage_connection,
        brokerage_connection.credentials,
        synced_account_ids={"acc-tod"},
    )
    await session.commit()

    assets = await _assets_for(session, test_user)
    assert [a.external_id for a in assets] == ["h-tod"]


@pytest.mark.asyncio
async def test_excluding_an_account_later_does_not_archive_what_it_already_imported(
    session: AsyncSession, test_user: User, brokerage_connection: BankConnection
):
    """Excluding stops future syncs of an account; it never deletes history.

    The archive sweep drops anything the provider stopped reporting, so a
    holding filtered out by the allowlist has to be held back from it.
    """
    _MockProvider._holdings = [
        _holding(external_id="h-tod", account_external_id="acc-tod"),
        _holding(external_id="h-roth", account_external_id="acc-roth"),
    ]
    assert brokerage_connection.credentials is not None
    await _sync_holdings(
        session, test_user.id, brokerage_connection, brokerage_connection.credentials
    )
    await session.commit()

    await _sync_holdings(
        session,
        test_user.id,
        brokerage_connection,
        brokerage_connection.credentials,
        synced_account_ids={"acc-tod"},
    )
    await session.commit()

    assets = {a.external_id: a for a in await _assets_for(session, test_user)}
    assert assets["h-roth"].is_archived is False


@pytest.mark.asyncio
async def test_unattributable_holdings_do_not_sync_under_an_allowlist(
    session: AsyncSession, test_user: User, brokerage_connection: BankConnection
):
    """Deny by default: a holding the provider cannot place in an account
    cannot be shown to belong to an allowed one."""
    _MockProvider._holdings = [_holding(external_id="h-1", account_external_id=None)]

    assert brokerage_connection.credentials is not None
    await _sync_holdings(
        session,
        test_user.id,
        brokerage_connection,
        brokerage_connection.credentials,
        synced_account_ids={"acc-tod"},
    )
    await session.commit()

    assert await _assets_for(session, test_user) == []
