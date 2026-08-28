import uuid
from datetime import date, datetime, timezone
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
        external_id="conn-abc::acc-roth-6548",
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
            account_external_id="acc-roth-6548",
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
    mine = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Mine",
        source="simplefin",
        external_id="conn-other::acc-shared-id",
    )
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
            mine,
        ]
    )
    session.add(
        Asset(
            id=uuid.uuid4(),
            user_id=test_user.id,
            workspace_id=test_workspace.id,
            group_id=mine.id,
            name="Something",
            type="stock",
            currency="USD",
            purchase_price=Decimal("10.00"),
            account_external_id="acc-shared-id",
        )
    )
    await session.commit()

    groups = await get_groups(session, test_workspace.id, test_user.id)

    mine = next(g for g in groups if g.name == "Mine")
    assert mine.account_type is None
    assert mine.account_balance is None


@pytest.mark.asyncio
async def test_get_groups_reports_the_balance_liquid_cash_is_derived_against(
    session: AsyncSession, test_user, test_workspace
):
    account = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        external_id="acc-robinhood-9464",
        name="Robinhood Individual",
        type="investment",
        balance=Decimal("535.26"),
        currency="USD",
    )
    wallet = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Robinhood Individual",
        source="simplefin",
        external_id="conn-abc::acc-robinhood-9464",
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
            name="AMC",
            type="stock",
            currency="USD",
            purchase_price=Decimal("137.96"),
            account_external_id="acc-robinhood-9464",
        )
    )
    await session.commit()

    by_name = {g.name: g for g in await get_groups(session, test_workspace.id, test_user.id)}

    assert by_name["Robinhood Individual"].account_balance == 535.26
    # No account behind a manual wallet, so nothing to subtract from — and null
    # is not zero, which is what stops the frontend inventing cash.
    assert by_name["Manual Wallet"].account_balance is None


def _investment_account(user, workspace, external_id: str, balance: str) -> Account:
    return Account(
        id=uuid.uuid4(),
        user_id=user.id,
        workspace_id=workspace.id,
        external_id=external_id,
        name=external_id,
        type="investment",
        balance=Decimal(balance),
        currency="USD",
    )


def _held(user, workspace, group_id, name: str, account_external_id: str, **kw) -> Asset:
    return Asset(
        id=uuid.uuid4(),
        user_id=user.id,
        workspace_id=workspace.id,
        group_id=group_id,
        name=name,
        type="stock",
        currency="USD",
        purchase_price=Decimal("100.00"),
        account_external_id=account_external_id,
        **kw,
    )


@pytest.mark.asyncio
async def test_a_wallet_spanning_two_accounts_reports_neither(
    session: AsyncSession, test_user, test_workspace
):
    """An unsplit legacy wallet has no single balance to subtract from.

    Naming one of the two would invent the other account's cash.
    """
    wallet = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Mock Bank",
        source="simplefin",
        external_id="conn-abc",
    )
    session.add_all(
        [
            _investment_account(test_user, test_workspace, "acc-tod", "100.00"),
            _investment_account(test_user, test_workspace, "acc-roth", "200.00"),
            wallet,
        ]
    )
    session.add_all(
        [
            _held(test_user, test_workspace, wallet.id, "AMC", "acc-tod"),
            _held(test_user, test_workspace, wallet.id, "VOO", "acc-roth"),
        ]
    )
    await session.commit()

    mixed = next(g for g in await get_groups(session, test_workspace.id, test_user.id) if g.name == "Mock Bank")

    assert mixed.account_type is None
    assert mixed.account_balance is None


@pytest.mark.asyncio
async def test_a_sibling_account_left_archived_does_not_strand_the_wallet(
    session: AsyncSession, test_user, test_workspace
):
    """The split leaves dead rows behind on the wallet it re-keyed.

    They are not holdings — `_rollup` already ignores them — so they must not
    read as a second account and switch allocation off for good.
    """
    wallet = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Individual - TOD",
        source="simplefin",
        external_id="conn-abc::acc-tod",
    )
    session.add_all(
        [
            _investment_account(test_user, test_workspace, "acc-tod", "500.00"),
            _investment_account(test_user, test_workspace, "acc-roth", "900.00"),
            wallet,
        ]
    )
    session.add_all(
        [
            _held(test_user, test_workspace, wallet.id, "AMC", "acc-tod"),
            _held(test_user, test_workspace, wallet.id, "Gone", "acc-roth", is_archived=True),
            _held(test_user, test_workspace, wallet.id, "Sold", "acc-roth", sell_date=date(2026, 1, 5)),
        ]
    )
    await session.commit()

    tod = next(g for g in await get_groups(session, test_workspace.id, test_user.id) if g.name == "Individual - TOD")

    assert tod.account_type == "investment"
    assert tod.account_balance == 500.00


@pytest.mark.asyncio
async def test_a_synced_holding_dragged_into_a_manual_wallet_carries_no_balance(
    session: AsyncSession, test_user, test_workspace
):
    """The holding still belongs to the account; the manual wallet does not.

    Reporting the balance on both is what would count the same cash twice.
    """
    synced = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Robinhood",
        source="simplefin",
        external_id="conn-abc::acc-rh",
    )
    manual = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="US Stocks",
        source="manual",
    )
    session.add_all(
        [_investment_account(test_user, test_workspace, "acc-rh", "535.26"), synced, manual]
    )
    session.add_all(
        [
            _held(test_user, test_workspace, synced.id, "AMC", "acc-rh"),
            _held(test_user, test_workspace, manual.id, "VOO", "acc-rh"),
        ]
    )
    await session.commit()

    by_name = {g.name: g for g in await get_groups(session, test_workspace.id, test_user.id)}

    assert by_name["Robinhood"].account_balance == 535.26
    assert by_name["US Stocks"].account_type is None
    assert by_name["US Stocks"].account_balance is None


@pytest.mark.asyncio
async def test_a_sibling_connection_reusing_an_account_id_is_not_read(
    session: AsyncSession, test_user, test_workspace
):
    """`accounts.external_id` is unique per connection, not per workspace."""
    ours = BankConnection(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        provider="simplefin",
        external_id="conn-ours",
        institution_name="Ours",
        credentials={"token": "x"},
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    theirs = BankConnection(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        provider="simplefin",
        external_id="conn-theirs",
        institution_name="Theirs",
        credentials={"token": "y"},
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    session.add_all([ours, theirs])
    await session.flush()

    mine = _investment_account(test_user, test_workspace, "acc-1", "300.00")
    mine.connection_id = ours.id
    sibling = _investment_account(test_user, test_workspace, "acc-1", "999.00")
    sibling.connection_id = theirs.id
    wallet = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Ours",
        source="simplefin",
        connection_id=ours.id,
        external_id="conn-ours::acc-1",
    )
    session.add_all([mine, sibling, wallet])
    session.add(_held(test_user, test_workspace, wallet.id, "AMC", "acc-1"))
    await session.commit()

    got = next(g for g in await get_groups(session, test_workspace.id, test_user.id) if g.name == "Ours")

    assert got.account_balance == 300.00
