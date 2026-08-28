"""Migration 087, and the sync behavior it exists to make safe.

The fork keyed a per-account wallet by the account's own external id; upstream
v0.14.4 keys it "{connection}::{account}" and tells the two shapes apart by the
"::". Every test here starts from the fork-era shape, runs the migration, and
then drives a real `_sync_holdings` — because the damage the migration prevents
only shows up on the far side of a sync.
"""

import importlib.util
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.bank_connection import BankConnection
from app.providers.base import HoldingData
from app.services.asset_group_service import get_groups
from app.services.connection_service import _sync_holdings, _wallet_external_id

_spec = importlib.util.spec_from_file_location(
    "migration_087",
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "087_rekey_fork_era_wallets.py",
)
assert _spec is not None and _spec.loader is not None
migration_087 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration_087)

CONNECTION_KEY = "item-abc"


async def _run_migration(session: AsyncSession) -> int:
    await session.flush()
    return await session.run_sync(
        lambda sync_session: migration_087.rekey(sync_session.connection())
    )


async def _connection(session, user, workspace) -> BankConnection:
    conn = BankConnection(
        id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id,
        provider="test", external_id=CONNECTION_KEY, institution_name="First Bank",
        credentials={"token": "t"}, status="active",
        created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.flush()
    return conn


def _account(user, workspace, conn, external_id, name) -> Account:
    return Account(
        id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id,
        connection_id=conn.id, external_id=external_id, name=name,
        type="investment", balance=Decimal("0"), currency="USD",
    )


def _fork_era_wallet(user, workspace, conn, external_id, name, tax_treatment) -> AssetGroup:
    """A wallet as the fork's own `_wallet_for_account` wrote it: bare key."""
    return AssetGroup(
        id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, name=name,
        source="test", connection_id=conn.id, external_id=external_id,
        tax_treatment=tax_treatment,
    )


def _holding_row(user, workspace, conn, group_id, external_id, account_external_id) -> Asset:
    return Asset(
        id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id,
        connection_id=conn.id, source="test", external_id=external_id,
        name=external_id, type="investment", currency="USD",
        valuation_method="manual", group_id=group_id,
        account_external_id=account_external_id,
    )


def _payload(external_id, account_external_id) -> HoldingData:
    return HoldingData(
        external_id=external_id, name=external_id, currency="USD",
        current_value=Decimal("10"), account_external_id=account_external_id,
        account_name=f"Acct {account_external_id}",
    )


async def _sync(session, user, conn, holdings):
    provider = AsyncMock()
    provider.get_holdings.return_value = holdings
    with patch("app.services.connection_service.get_provider", return_value=provider):
        await _sync_holdings(session, user.id, conn, {"token": "t"})
    await session.commit()


async def _wallets_by_name(session, workspace) -> dict[str, AssetGroup]:
    # populate_existing: the migration writes through the raw connection, and
    # the session is built with expire_on_commit=False, so an already-loaded
    # wallet would otherwise keep reporting its pre-migration key.
    rows = await session.execute(
        select(AssetGroup)
        .where(AssetGroup.workspace_id == workspace.id)
        .execution_options(populate_existing=True)
    )
    return {g.name: g for g in rows.scalars().all()}


async def _two_retirement_wallets(session, test_user, test_workspace):
    conn = await _connection(session, test_user, test_workspace)
    session.add_all([
        _account(test_user, test_workspace, conn, "acc-1", "Roth"),
        _account(test_user, test_workspace, conn, "acc-2", "401k"),
        _account(test_user, test_workspace, conn, "acc-3", "Brokerage"),
    ])
    roth = _fork_era_wallet(test_user, test_workspace, conn, "acc-1", "Roth IRA", "roth")
    plan = _fork_era_wallet(test_user, test_workspace, conn, "acc-2", "401k", "traditional")
    session.add_all([roth, plan])
    await session.flush()
    session.add_all([
        _holding_row(test_user, test_workspace, conn, roth.id, "h-1", "acc-1"),
        _holding_row(test_user, test_workspace, conn, plan.id, "h-2", "acc-2"),
    ])
    await session.commit()
    return conn


@pytest.mark.asyncio
async def test_a_new_account_does_not_take_over_another_wallet(
    session: AsyncSession, test_user, test_workspace
):
    """Without the re-key, the 401(k) wallet is handed to the new account.

    Upstream's last-resort "exactly one unclaimed candidate" rule fires when a
    payload names an account no wallet matches, and which wallet it takes
    depends on the order the provider happened to list the accounts in.
    """
    conn = await _two_retirement_wallets(session, test_user, test_workspace)
    assert await _run_migration(session) == 2

    # The new account sits between the two known ones — the order that trips it.
    await _sync(session, test_user, conn, [
        _payload("h-1", "acc-1"), _payload("h-3", "acc-3"), _payload("h-2", "acc-2"),
    ])

    wallets = await _wallets_by_name(session, test_workspace)
    assert wallets["401k"].external_id == f"{CONNECTION_KEY}::acc-2"
    assert wallets["401k"].tax_treatment == "traditional"
    assert wallets["Roth IRA"].external_id == f"{CONNECTION_KEY}::acc-1"
    assert wallets["Roth IRA"].tax_treatment == "roth"
    held = await session.execute(
        select(Asset.external_id).where(Asset.group_id == wallets["401k"].id)
    )
    assert list(held.scalars().all()) == ["h-2"]


@pytest.mark.asyncio
async def test_a_payload_that_drops_attribution_does_not_collapse_the_wallets(
    session: AsyncSession, test_user, test_workspace
):
    """One degraded poll must not cost the user every wallet they set up.

    `hint_lost` is what refuses to drain a per-account wallet into the
    connection default, and it only recognizes one by the "::" in its key.
    """
    conn = await _two_retirement_wallets(session, test_user, test_workspace)
    await _run_migration(session)

    await _sync(session, test_user, conn, [_payload("h-1", None), _payload("h-2", None)])

    wallets = await _wallets_by_name(session, test_workspace)
    assert wallets["Roth IRA"].tax_treatment == "roth"
    assert wallets["401k"].tax_treatment == "traditional"
    # Surviving empty is not surviving: the holdings have to stay put, or the
    # next poll reaps what it drained.
    for name, holding in (("Roth IRA", "h-1"), ("401k", "h-2")):
        held = await session.execute(
            select(Asset.external_id).where(Asset.group_id == wallets[name].id)
        )
        assert list(held.scalars().all()) == [holding]


@pytest.mark.asyncio
async def test_the_connection_level_and_manual_wallets_are_left_alone(
    session: AsyncSession, test_user, test_workspace
):
    """Only an account-keyed wallet is re-keyed.

    The connection's own key is already the shape upstream writes for holdings
    it cannot attribute, and a manual wallet was never sync's to key.
    """
    conn = await _connection(session, test_user, test_workspace)
    unattributed = _fork_era_wallet(
        test_user, test_workspace, conn, CONNECTION_KEY, "First Bank", "taxable"
    )
    manual = AssetGroup(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        name="US Stocks", source="manual", external_id="acc-1",
    )
    # The account exists, so only `source` keeps the manual wallet out.
    session.add(_account(test_user, test_workspace, conn, "acc-1", "Brokerage"))
    session.add_all([unattributed, manual])
    await session.commit()

    assert await _run_migration(session) == 0
    await session.commit()

    wallets = await _wallets_by_name(session, test_workspace)
    assert wallets["First Bank"].external_id == CONNECTION_KEY
    assert wallets["US Stocks"].external_id == "acc-1"


@pytest.mark.asyncio
async def test_a_key_already_claimed_is_left_bare_rather_than_failing(
    session: AsyncSession, test_user, test_workspace
):
    """A half-migrated database must still boot.

    Re-keying onto a taken key trips the unique index, which would fail the
    migration on every start; the bare row is recoverable, that is not.
    """
    conn = await _connection(session, test_user, test_workspace)
    session.add_all([
        _account(test_user, test_workspace, conn, "acc-1", "Brokerage"),
        _fork_era_wallet(test_user, test_workspace, conn, "acc-1", "Bare", "roth"),
        _fork_era_wallet(
            test_user, test_workspace, conn, f"{CONNECTION_KEY}::acc-1", "Already", "taxable"
        ),
    ])
    await session.commit()

    assert await _run_migration(session) == 0
    await session.commit()

    wallets = await _wallets_by_name(session, test_workspace)
    assert wallets["Bare"].external_id == "acc-1"


@pytest.mark.asyncio
async def test_an_orphaned_wallet_is_adopted_rather_than_stranded(
    session: AsyncSession, test_user, test_workspace
):
    """A wallet a disconnect orphaned still has to survive the split.

    The migration cannot reach it — `connection_id` went NULL, so there is no
    connection left to build the key from — and re-keying its live sibling is
    what tells the sync it is "past the legacy era". Without the exact bare-key
    rule the orphan is locked out of adoption: its holdings move into a fresh
    `taxable` wallet, and the tax character the user set goes with them.
    """
    conn = await _two_retirement_wallets(session, test_user, test_workspace)
    wallets = await _wallets_by_name(session, test_workspace)
    orphan = wallets["401k"]
    orphan.connection_id = None
    await session.execute(
        update(Asset).where(Asset.group_id == orphan.id).values(connection_id=None)
    )
    await session.commit()

    # Only the live sibling can be re-keyed; that is what flips the gate.
    assert await _run_migration(session) == 1
    await session.commit()

    await _sync(session, test_user, conn, [
        _payload("h-1", "acc-1"), _payload("h-2", "acc-2"),
    ])

    wallets = await _wallets_by_name(session, test_workspace)
    assert len(wallets) == 2
    assert wallets["401k"].external_id == f"{CONNECTION_KEY}::acc-2"
    assert wallets["401k"].tax_treatment == "traditional"
    held = await session.execute(
        select(Asset.external_id).where(Asset.group_id == wallets["401k"].id)
    )
    assert list(held.scalars().all()) == ["h-2"]


@pytest.mark.asyncio
async def test_a_rotated_connection_key_is_not_read_as_an_account_id(
    session: AsyncSession, test_user, test_workspace
):
    """A reconnect rewrites `bank_connections.external_id` in place.

    The connection-level wallet stays on the previous key, which is not an
    account id — keying it "{new}::{old}" would name an account that never
    existed and no later sync could adopt it. Asking `accounts` is what tells
    the two apart; the sync then adopts the wallet on its own.
    """
    conn = await _connection(session, test_user, test_workspace)
    session.add_all([
        _account(test_user, test_workspace, conn, "acc-1", "Brokerage"),
        _fork_era_wallet(
            test_user, test_workspace, conn, CONNECTION_KEY, "First Bank", "roth"
        ),
    ])
    await session.flush()
    conn.external_id = "item-reconnected"
    await session.commit()

    assert await _run_migration(session) == 0
    await session.commit()

    await _sync(session, test_user, conn, [_payload("h-1", "acc-1")])

    wallets = await _wallets_by_name(session, test_workspace)
    assert len(wallets) == 1
    adopted = next(iter(wallets.values()))
    assert adopted.external_id == "item-reconnected::acc-1"
    assert adopted.tax_treatment == "roth"


@pytest.mark.asyncio
async def test_a_wallet_whose_account_is_keyed_like_its_connection_is_rekeyed(
    session: AsyncSession, test_user, test_workspace
):
    """The crypto shape: one account per connection, sharing its external id.

    Coinbase names the connection and its single investment account after the
    same portfolio uuid, so the fork-era wallet key is indistinguishable from a
    connection-level one by comparison alone. An account owns that key, so it
    is re-keyed like any other.
    """
    conn = await _connection(session, test_user, test_workspace)
    session.add(_account(test_user, test_workspace, conn, CONNECTION_KEY, "Coinbase"))
    wallet = _fork_era_wallet(
        test_user, test_workspace, conn, CONNECTION_KEY, "Coinbase", "taxable"
    )
    session.add(wallet)
    await session.commit()

    assert await _run_migration(session) == 1
    await session.commit()

    wallets = await _wallets_by_name(session, test_workspace)
    assert wallets["Coinbase"].external_id == f"{CONNECTION_KEY}::{CONNECTION_KEY}"


@pytest.mark.asyncio
async def test_a_wallet_split_across_accounts_hands_on_no_tax_treatment(
    session: AsyncSession, test_user, test_workspace
):
    """Adoption must not give one account a setting made for several.

    A connection-level wallet held every account's holdings at once, so its tax
    character was never a statement about the account that adopts the row — and
    which account that is comes down to payload order (issue #76).
    """
    conn = await _connection(session, test_user, test_workspace)
    shared = _fork_era_wallet(
        test_user, test_workspace, conn, CONNECTION_KEY, "First Bank", "roth"
    )
    session.add_all([
        _account(test_user, test_workspace, conn, "acc-1", "Roth"),
        _account(test_user, test_workspace, conn, "acc-2", "Brokerage"),
        shared,
    ])
    await session.flush()
    session.add_all([
        _holding_row(test_user, test_workspace, conn, shared.id, "h-1", None),
        _holding_row(test_user, test_workspace, conn, shared.id, "h-2", None),
    ])
    await session.commit()

    await _sync(session, test_user, conn, [
        _payload("h-1", "acc-1"), _payload("h-2", "acc-2"),
    ])

    wallets = await _wallets_by_name(session, test_workspace)
    adopted = [g for g in wallets.values() if g.external_id == f"{CONNECTION_KEY}::acc-1"]
    assert len(adopted) == 1
    assert adopted[0].tax_treatment == "taxable"


@pytest.mark.asyncio
async def test_a_payload_that_drops_attribution_keeps_the_account_join(
    session: AsyncSession, test_user, test_workspace
):
    """Liquid Cash and allocation are read through the holding's attribution.

    A provider that stops naming the account for one poll must not erase it:
    the wallet would report no account type and no balance until the next
    healthy payload, and the UI shows that as an empty allocation bucket and no
    Liquid Cash rather than as an error.
    """
    conn = await _two_retirement_wallets(session, test_user, test_workspace)
    assert await _run_migration(session) == 2
    await session.commit()

    await _sync(session, test_user, conn, [_payload("h-1", "acc-1")])
    await _sync(session, test_user, conn, [_payload("h-1", None)])

    groups = await get_groups(session, test_workspace.id, test_user.id)
    roth = next(g for g in groups if g.name == "Roth IRA")
    assert roth.account_type == "investment"
    assert roth.account_balance is not None


@pytest.mark.asyncio
async def test_another_connections_orphan_wallet_is_not_reaped(
    session: AsyncSession, test_user, test_workspace
):
    """Orphans ride along so their assets can be re-filed, not to be deleted.

    A connection removed long ago can leave a wallet the user keeps on purpose;
    the sync of an unrelated connection knows nothing about it.
    """
    conn = await _connection(session, test_user, test_workspace)
    session.add(_account(test_user, test_workspace, conn, "acc-1", "Brokerage"))
    orphan = _fork_era_wallet(
        test_user, test_workspace, conn, "acc-old", "Old Broker", "taxable"
    )
    session.add(orphan)
    await session.flush()
    orphan.connection_id = None
    await session.commit()
    orphan_id = orphan.id

    await _sync(session, test_user, conn, [_payload("h-1", "acc-1")])

    assert await session.get(AssetGroup, orphan_id) is not None


@pytest.mark.asyncio
async def test_the_downgrade_restores_the_bare_key_and_skips_a_taken_one(
    session: AsyncSession, test_user, test_workspace
):
    """The upgrade leaves a wallet bare on collision; the downgrade must too.

    Stripping the twin onto that same key trips the unique index, and
    transactional DDL takes the whole downgrade with it.
    """
    conn = await _connection(session, test_user, test_workspace)
    session.add_all([
        _account(test_user, test_workspace, conn, "acc-1", "Roth"),
        _account(test_user, test_workspace, conn, "acc-2", "Brokerage"),
        _fork_era_wallet(
            test_user, test_workspace, conn, f"{CONNECTION_KEY}::acc-1", "Roth IRA", "roth"
        ),
        _fork_era_wallet(
            test_user, test_workspace, conn, f"{CONNECTION_KEY}::acc-2", "Twin", "taxable"
        ),
        _fork_era_wallet(test_user, test_workspace, conn, "acc-2", "Bare", "taxable"),
    ])
    await session.commit()

    moved = await session.run_sync(
        lambda sync_session: migration_087.unkey(sync_session.connection())
    )
    await session.commit()

    assert moved == 1
    wallets = await _wallets_by_name(session, test_workspace)
    assert wallets["Roth IRA"].external_id == "acc-1"
    assert wallets["Twin"].external_id == f"{CONNECTION_KEY}::acc-2"
    assert wallets["Bare"].external_id == "acc-2"


def test_the_migrations_frozen_key_helper_still_matches_the_runtime():
    """The copy is deliberate, so nothing fails when the two drift.

    A migration writing keys the sync cannot match would re-split every wallet.
    """
    long_account = "a" * 250
    for connection_key, account_key in [
        (CONNECTION_KEY, "acc-1"),
        (CONNECTION_KEY, long_account),
        ("c" * 200, "d" * 200),
        (CONNECTION_KEY, ""),
        (CONNECTION_KEY, None),
    ]:
        assert migration_087._wallet_external_id(
            connection_key, account_key
        ) == _wallet_external_id(connection_key, account_key)
