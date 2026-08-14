import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.bank_connection import BankConnection
from app.models.workspace import Workspace
from app.services.asset_group_service import get_groups


@pytest.mark.asyncio
async def test_get_groups_hides_empty_synced_wallets(
    session: AsyncSession, test_user, test_workspace
):
    active_connection = BankConnection(
        id=uuid.uuid4(),
        user_id=test_user.id,
        provider="pluggy",
        external_id="item-active",
        institution_name="Active Bank",
        credentials={"token": "x"},
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    session.add(active_connection)

    manual_group = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        name="Manual Wallet",
        source="manual",
    )
    orphan_synced_group = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        name="MeuPluggy",
        source="pluggy",
        connection_id=None,
    )
    active_synced_group = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        name="Connected Wallet",
        source="pluggy",
        connection_id=active_connection.id,
    )
    session.add_all([manual_group, orphan_synced_group, active_synced_group])
    await session.commit()

    groups = await get_groups(session, test_workspace.id, test_user.id)
    names = {g.name for g in groups}

    assert "Manual Wallet" in names
    assert "Connected Wallet" not in names
    assert "MeuPluggy" not in names


@pytest.mark.asyncio
async def test_get_groups_reports_the_account_type_a_wallet_mirrors(
    session: AsyncSession, test_user, test_workspace
):
    account = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        external_id="acc-roth-6548",
        name="ROTH IRA",
        type="investment",
        balance=Decimal("0"),
        currency="USD",
    )
    wallet = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="ROTH IRA",
        source="simplefin",
        external_id="acc-roth-6548",
    )
    manual = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Manual Wallet",
        source="manual",
    )
    session.add_all([account, wallet, manual])
    session.add(
        Asset(
            id=uuid.uuid4(),
            user_id=test_user.id,
            workspace_id=test_workspace.id,
            group_id=wallet.id,
            name="Vanguard S&P 500",
            type="etf",
            currency="USD",
            purchase_price=Decimal("100.00"),
        )
    )
    await session.commit()

    by_name = {g.name: g for g in await get_groups(session, test_workspace.id, test_user.id)}

    assert by_name["ROTH IRA"].account_type == "investment"
    assert by_name["Manual Wallet"].account_type is None


@pytest.mark.asyncio
async def test_account_type_does_not_leak_across_workspaces(
    session: AsyncSession, test_user, test_workspace
):
    other_workspace = Workspace(
        id=uuid.uuid4(),
        created_by_user_id=test_user.id,
        name="Other",
    )
    session.add(other_workspace)
    await session.flush()
    session.add_all(
        [
            Account(
                id=uuid.uuid4(),
                user_id=test_user.id,
                workspace_id=other_workspace.id,
                external_id="acc-shared-id",
                name="Elsewhere",
                type="checking",
                balance=Decimal("0"),
                currency="USD",
            ),
            AssetGroup(
                id=uuid.uuid4(),
                user_id=test_user.id,
                workspace_id=test_workspace.id,
                name="Mine",
                source="manual",
                external_id="acc-shared-id",
            ),
        ]
    )
    await session.commit()

    groups = await get_groups(session, test_workspace.id, test_user.id)

    assert next(g for g in groups if g.name == "Mine").account_type is None
