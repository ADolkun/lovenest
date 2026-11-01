import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_value import AssetValue
from app.models.bank_connection import BankConnection
from app.models.fx_rate import FxRate
from app.models.workspace import Workspace
from app.services.asset_group_service import get_group, get_groups


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
    # The account is in USD: with no rate into a BRL primary its balance is unknown.
    test_user.preferences = {**(test_user.preferences or {}), "currency_display": "USD"}
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


@pytest.mark.asyncio
async def test_an_account_balance_no_rate_converts_is_unknown_rather_than_1_to_1(
    session: AsyncSession, test_user, test_workspace
):
    """Liquid Cash is this balance less the holdings; a balance taken at a
    fake 1:1 rate would derive cash out of a currency mismatch."""
    account = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        external_id="acc-xts",
        name="Test Broker",
        type="investment",
        balance=Decimal("535.26"),
        currency="XTS",
    )
    wallet = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Test Broker",
        source="simplefin",
        external_id="conn-abc::acc-xts",
    )
    session.add_all([account, wallet])
    session.add(
        Asset(
            id=uuid.uuid4(),
            user_id=test_user.id,
            workspace_id=test_workspace.id,
            group_id=wallet.id,
            name="Fund",
            type="stock",
            currency="XTS",
            purchase_price=Decimal("137.96"),
            account_external_id="acc-xts",
        )
    )
    await session.commit()

    read = next(g for g in await get_groups(session, test_workspace.id, test_user.id) if g.name == "Test Broker")

    assert read.account_type == "investment"
    assert read.account_balance is None
    assert read.unconverted_count == 1


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
    test_user.preferences = {**(test_user.preferences or {}), "currency_display": "USD"}
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
    test_user.preferences = {**(test_user.preferences or {}), "currency_display": "USD"}
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
    test_user.preferences = {**(test_user.preferences or {}), "currency_display": "USD"}
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


@pytest.mark.asyncio
async def test_rollup_keeps_sub_cent_market_prices_in_primary_currency(
    session: AsyncSession, test_user, test_workspace
):
    """issue #622: a wallet holding low-priced foreign-currency assets must
    not roll up to 0.0 in the user's primary currency. Each converted amount
    keeps six decimals inside the sum; the read boundary still displays the
    total rounded to two. So 30 coins at 0.002087 USD x 0.92 (0.0576 EUR)
    must surface as 0.06, not the 0.0 the per-asset two-decimal quantization
    produced."""
    test_user.preferences = {**(test_user.preferences or {}), "currency_display": "EUR"}
    session.add(
        FxRate(
            base_currency="USD",
            quote_currency="EUR",
            date=date.today(),
            rate=Decimal("0.9200000000"),
            source="test",
        )
    )
    wallet = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Coin Wallet",
    )
    session.add(wallet)
    await session.commit()

    assets = [
        Asset(
            id=uuid.uuid4(),
            user_id=test_user.id,
            workspace_id=test_workspace.id,
            group_id=wallet.id,
            name=f"Coin {i}",
            type="investment",
            currency="USD",
            units=Decimal("1"),
            valuation_method="market_price",
            last_price=Decimal("0.002087"),
            ticker="COIN-USD",
            position=i,
        )
        for i in range(30)
    ]
    session.add_all(assets)
    await session.commit()
    # The rollup reads the latest AssetValue; the quote sync writes one per price.
    session.add_all(
        AssetValue(
            id=uuid.uuid4(),
            asset_id=asset.id,
            workspace_id=test_workspace.id,
            amount=Decimal("0.002087"),
            date=date.today(),
            source="sync",
        )
        for asset in assets
    )
    await session.commit()

    read = await get_group(session, wallet.id, test_workspace.id, test_user.id)

    assert read is not None
    assert read.asset_count == 30
    assert read.current_value == pytest.approx(0.06)
    assert read.current_value_primary == pytest.approx(0.06)


@pytest.mark.asyncio
async def test_rollup_excludes_assets_without_fx_rate(
    session: AsyncSession, test_user, test_workspace
):
    """issue #574: a wallet holding a foreign-currency asset without a
    resolvable FX rate must not add the raw amount to the primary-currency
    total at a fake 1:1 rate. The asset still counts toward the wallet size;
    its primary contribution is 0. A sibling asset whose currency has a rate
    still converts."""
    test_user.preferences = {**(test_user.preferences or {}), "currency_display": "USD"}
    session.add(
        FxRate(
            id=uuid.uuid4(),
            base_currency="USD",
            quote_currency="EUR",
            date=date.today(),
            rate=Decimal("0.9200000000"),
            source="test",
        )
    )
    wallet = AssetGroup(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Mixed Wallet",
    )
    session.add(wallet)
    await session.commit()

    eur_asset = Asset(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        group_id=wallet.id,
        name="Fund",
        type="investment",
        currency="EUR",
        units=Decimal("1000"),
        valuation_method="manual",
        purchase_price=Decimal("1000"),
        position=0,
    )
    gbp_asset = Asset(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        group_id=wallet.id,
        name="Cash",
        type="cash",
        currency="GBP",
        units=Decimal("500"),
        valuation_method="manual",
        purchase_price=Decimal("500"),
        position=1,
    )
    session.add_all([eur_asset, gbp_asset])
    await session.commit()
    session.add_all([
        AssetValue(
            id=uuid.uuid4(),
            asset_id=eur_asset.id,
            workspace_id=test_workspace.id,
            amount=Decimal("1000"),
            date=date.today(),
            source="manual",
        ),
        AssetValue(
            id=uuid.uuid4(),
            asset_id=gbp_asset.id,
            workspace_id=test_workspace.id,
            amount=Decimal("500"),
            date=date.today(),
            source="manual",
        ),
    ])
    await session.commit()

    read = await get_group(session, wallet.id, test_workspace.id, test_user.id)

    # 1000 EUR -> 1086.956522 USD at 1/0.92, shown at two decimals. The 500
    # GBP is left out of the primary total instead of being added as 500.0.
    assert read is not None
    assert read.asset_count == 2
    assert read.current_value_primary == pytest.approx(1086.96, abs=0.005)
    # Left out, but not silently: the GBP holding is counted as unconverted,
    # which is not the same as having no value.
    assert read.unconverted_count == 1
    assert read.unvalued_count == 0
