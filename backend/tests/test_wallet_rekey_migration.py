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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.bank_connection import BankConnection
from app.providers.base import HoldingData
from app.services.connection_service import _sync_holdings

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
    rows = await session.execute(
        select(AssetGroup).where(AssetGroup.workspace_id == workspace.id)
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
