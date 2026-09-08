"""Synthetic producer -> persisted observation -> account/wallet API contracts."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_value import AssetValue
from app.models.bank_connection import BankConnection
from app.models.workspace import Workspace
from app.providers.base import AccountData, HoldingData
from app.providers.onchain import OnChainProvider
from app.providers.coinbase import CoinbaseProvider
from app.providers.simplefin import SimpleFinProvider
from app.schemas.account import AccountCreate
from app.services import account_service, asset_group_service
from app.services.connection_service import (
    _record_balance_observation, _upsert_asset_from_holding, _upsert_asset_value_for_today,
)

T0 = datetime(2024, 2, 3, 12, tzinfo=timezone.utc)


async def _portfolio(session, user, workspace, *, balance="120", values=("70", "20")):
    raw = {
        "id": "synthetic-account", "name": "Synthetic investment", "currency": "BRL",
        "balance": balance, "balance-date": int(T0.timestamp()),
        "holdings": [{"id": f"synthetic-holding-{i}", "market_value": value}
                     for i, value in enumerate(values)],
    }
    _, [data] = SimpleFinProvider._parse_accounts({"accounts": [raw]})
    connection = BankConnection(
        id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, provider="simplefin",
        external_id=str(uuid.uuid4()), institution_name="Synthetic investment", credentials={},
        settings={}, status="active", last_sync_at=T0,
    )
    _record_balance_observation(connection, [data], SimpleFinProvider(),
                                persisted_account_ids={data.external_id})
    account = Account(
        id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, connection_id=connection.id,
        external_id=data.external_id, name=data.name, type="investment", currency=data.currency,
        balance=data.balance,
    )
    group = AssetGroup(
        id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, name="Synthetic wallet",
        source="simplefin", connection_id=connection.id,
    )
    session.add_all([connection, account, group])
    await session.flush()
    holdings = []
    for i, value in enumerate(values):
        # Independent quote fixture at the exact declared source boundary.
        # SimpleFIN's holding `created` is deliberately never used as quote age.
        asset = Asset(
            id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id, group_id=group.id,
            name=f"Synthetic holding {i}", type="stock" if i == 0 else "other",
            ticker="SYNTH" if i == 0 else None, source="simplefin", connection_id=connection.id,
            account_external_id=data.external_id, currency="BRL", units=Decimal("1"),
            valuation_method="market_price", last_price=Decimal(value) if value is not None else None,
            last_price_at=T0,
        )
        session.add(asset)
        if i > 0 and value is not None:
            holding = HoldingData(
                external_id=f"synthetic-{group.id}-{i}", name=asset.name, currency="BRL",
                current_value=Decimal(value), quantity=Decimal("1"),
                account_external_id=data.external_id,
                metadata={"valuation_observation": {
                    "observed_at": T0.isoformat(), "amount": value, "currency": "BRL",
                }},
            )
            asset.valuation_method = "manual"
            asset.last_price = None
            asset.last_price_at = None
            await session.flush()
            await _upsert_asset_from_holding(
                session, asset, holding, user.id, connection.id, "simplefin", workspace.id,
            )
            await _upsert_asset_value_for_today(session, asset, holding.current_value, T0.date())
        holdings.append(asset)
    await session.commit()
    return account, group, connection, holdings


async def test_same_contract_from_account_list_detail_and_wallet(
    session, client, auth_headers, test_user, test_workspace,
):
    account, group, _, _ = await _portfolio(session, test_user, test_workspace)
    accounts = (await client.get("/api/accounts", headers=auth_headers)).json()
    detail = (await client.get(f"/api/accounts/{account.id}", headers=auth_headers)).json()
    wallets = (await client.get("/api/asset-groups", headers=auth_headers)).json()
    explanation = accounts[0]["balance_explanation"]
    assert explanation == detail["balance_explanation"]
    wallet_explanation = wallets[0]["balance_explanation"]
    assert wallet_explanation == {**explanation, "wallet_id": str(group.id)}
    assert explanation["basis"] == "reported_account_total"
    assert explanation["amount"] == 120
    assert explanation["holdings_value"] == 90
    assert explanation["residual_cash"] == 30
    assert explanation["reconciliation"] == "residual_derived"
    assert [(h["ticker"], h["value"]) for h in explanation["holdings"]] == [("SYNTH", 70), (None, 20)]
    assert explanation["observation_basis"] == "source"
    await session.refresh(account)
    assert account.balance == Decimal("120")


@pytest.mark.parametrize("balance,values,expected,coverage", [
    ("0", ("0", "0"), 0, "complete"),
    ("120", (None, None), None, "unavailable"),
    ("120", ("70", None), None, "partial"),
    ("0", ("70", "20"), 0, "complete"),
])
async def test_unknown_zero_and_nonnegative_residual(
    session, test_user, test_workspace, balance, values, expected, coverage,
):
    account, group, _, _ = await _portfolio(session, test_user, test_workspace, balance=balance, values=values)
    read = await asset_group_service.get_group(session, group.id, test_workspace.id, test_user.id)
    assert read is not None
    explanation = read.balance_explanation
    assert explanation is not None
    assert explanation.residual_cash == expected
    assert explanation.holdings_coverage == coverage
    if values == (None, None):
        assert explanation.holdings_value is None
        assert "missing_holding_value" in explanation.reason_codes
    if balance == "0" and values == ("70", "20"):
        assert explanation.reconciliation == "difference"
        assert explanation.difference == -90
        assert "balance_difference" in explanation.reason_codes
    assert account.balance == Decimal(balance)


@pytest.mark.parametrize("change,reason", [
    ("time", "observation_time_mismatch"),
    ("unknown_time", "observation_time_unknown"),
    ("currency", "currency_mismatch"),
    ("split", "account_scope_mismatch"),
    ("legacy", "coverage_unknown"),
])
async def test_incompatible_scopes_never_derive_cash(
    session, test_user, test_workspace, change, reason,
):
    _, group, connection, holdings = await _portfolio(session, test_user, test_workspace)
    if change == "time":
        holdings[0].last_price_at = T0 + timedelta(seconds=1)
    elif change == "unknown_time":
        holdings[0].last_price_at = None
    elif change == "currency":
        holdings[0].currency = "USD"
    elif change == "split":
        other = AssetGroup(id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
                           name="Other scope", source="simplefin", connection_id=connection.id)
        session.add(other)
        holdings[1].group_id = other.id
    else:
        connection.settings = {}
    await session.commit()
    read = await asset_group_service.get_group(session, group.id, test_workspace.id, test_user.id)
    assert read is not None and read.balance_explanation is not None
    assert read.balance_explanation.residual_cash is None
    assert read.balance_explanation.reconciliation == "not_comparable"
    assert reason in read.balance_explanation.reason_codes


async def test_manual_ledger_list_detail_and_link_remain_separate(
    session, client, auth_headers, test_user, test_workspace,
):
    account = await account_service.create_account(session, test_workspace.id, test_user.id,
                                                  AccountCreate(name="Cash ledger", type="investment", balance=45, currency="BRL"))
    group = (await client.post("/api/asset-groups", headers=auth_headers,
                               json={"name": "Manual holdings", "account_id": str(account.id)})).json()
    accounts = (await client.get("/api/accounts", headers=auth_headers)).json()
    detail = (await client.get(f"/api/accounts/{account.id}", headers=auth_headers)).json()
    for read in (accounts[0], detail, group):
        explanation = read["balance_explanation"]
        assert explanation["amount"] == 45
        assert explanation["basis"] == "cash_ledger"
        assert explanation["residual_cash"] is None
        assert explanation["reconciliation"] == "separate_cash_ledger"
    assert group["account_balance"] is None


async def test_reused_provider_id_does_not_include_sibling_connection(
    session, test_user, test_workspace,
):
    first, group, _, _ = await _portfolio(session, test_user, test_workspace)
    second, _, _, _ = await _portfolio(session, test_user, test_workspace, balance="800", values=("600", "100"))
    read = await asset_group_service.get_group(session, group.id, test_workspace.id, test_user.id)
    assert read is not None and read.balance_explanation is not None
    assert read.balance_explanation.account_id == first.id != second.id
    assert read.balance_explanation.amount == 120
    assert read.balance_explanation.holdings_value == 90
    assert read.balance_explanation.residual_cash == 30


async def test_retained_onchain_value_keeps_original_collection_time(
    session, test_user, test_workspace,
):
    account, group, connection, _ = await _portfolio(session, test_user, test_workspace)
    connection.provider = "onchain"
    provider = OnChainProvider()
    data = AccountData(external_id=account.external_id, name="Synthetic", type="investment",
                       balance=account.balance, currency="BRL")
    provider.holdings_observation = provider.account_holdings_observation = {
        "observed_at": T0.isoformat(), "complete": True, "reasons": [], "unreadable_count": 0,
    }
    _record_balance_observation(connection, [data], provider, persisted_account_ids={data.external_id})
    t1 = T0 + timedelta(hours=1)
    provider.holdings_observation = provider.account_holdings_observation = {
        "observed_at": t1.isoformat(), "complete": False,
        "reasons": ["token_rate_limited"], "unreadable_count": 1,
    }
    data.balance = None
    _record_balance_observation(connection, [data], provider, persisted_account_ids=set())
    connection.status = "sync_error"
    await session.commit()
    read = await asset_group_service.get_group(session, group.id, test_workspace.id, test_user.id)
    assert read is not None
    detail = read.balance_explanation
    assert detail is not None
    assert detail.amount == 120
    assert detail.observed_at == T0
    assert detail.observation_basis == "collection"
    assert detail.refresh_observed_at == t1
    assert detail.coverage == "partial"
    assert "token_rate_limited" in detail.reason_codes
    assert detail.residual_cash is None
    assert detail.reconciliation == "shared_derivation"


@pytest.mark.parametrize("raw", [None, "invalid", ""])
async def test_unusable_provider_balance_does_not_certify_default_zero(
    session, test_user, test_workspace, raw,
):
    account, _, _, _ = await _portfolio(session, test_user, test_workspace, balance=raw, values=())
    [read] = await account_service.get_accounts(session, test_workspace.id, account_id=account.id)
    assert read["balance_explanation"].amount is None
    assert read["balance_explanation"].residual_cash is None


@pytest.mark.parametrize("balance", ["120", "121"])
async def test_account_write_without_holdings_cannot_reuse_old_reconciliation(
    session, test_user, test_workspace, balance,
):
    account, group, connection, _ = await _portfolio(session, test_user, test_workspace)
    account.balance = Decimal(balance)
    data = AccountData(external_id=account.external_id, name="Synthetic", type="investment",
                       balance=account.balance, currency="BRL")
    _record_balance_observation(connection, [data], SimpleFinProvider(), persisted_account_ids=set())
    await session.commit()
    read = await asset_group_service.get_group(session, group.id, test_workspace.id, test_user.id)
    assert read is not None and read.balance_explanation is not None
    detail = read.balance_explanation
    assert detail.amount == float(balance)
    assert detail.residual_cash is None
    assert detail.reconciliation == "not_comparable"
    if balance == "121":
        assert detail.observed_at is None
        assert "account_observation_changed" in detail.reason_codes
    else:
        assert "holdings_refresh_unconfirmed" in detail.reason_codes


async def test_source_clock_does_not_follow_a_changed_holding_value(
    session, test_user, test_workspace,
):
    _, group, _, holdings = await _portfolio(session, test_user, test_workspace)
    latest = (await session.execute(select(AssetValue).where(
        AssetValue.asset_id == holdings[1].id,
    ))).scalar_one()
    latest.amount = Decimal("21")
    await session.commit()
    read = await asset_group_service.get_group(session, group.id, test_workspace.id, test_user.id)
    assert read is not None and read.balance_explanation is not None
    detail = read.balance_explanation
    assert detail.holdings[1].value == 21
    assert detail.holdings[1].observation_basis == "recorded"
    assert detail.residual_cash is None


async def test_connected_credit_card_explanation_preserves_display_sign(
    session, client, auth_headers, test_user, test_workspace,
):
    account, _, _, _ = await _portfolio(session, test_user, test_workspace, values=())
    account.type = "credit_card"
    await session.commit()
    read = (await client.get(f"/api/accounts/{account.id}", headers=auth_headers)).json()
    assert read["current_balance"] == read["balance_explanation"]["amount"] == -120


async def test_coinbase_all_unpriced_reports_omitted_values_not_known_zero(monkeypatch):
    provider = CoinbaseProvider()
    monkeypatch.setattr(provider, "_walk_accounts", AsyncMock(return_value=[{
        "id": "synthetic-crypto", "currency": {"code": "SYNTH", "type": "crypto"},
        "balance": {"amount": "5", "currency": "SYNTH"},
    }]))
    monkeypatch.setattr(provider, "_usd_prices", AsyncMock(return_value={}))
    [account] = await provider.get_accounts({})
    assert account.balance_metadata is not None
    assert account.balance_metadata["value_available"] is False
    assert account.balance_metadata["coverage"] == "partial"
    assert account.balance_metadata["omitted_count"] == 1


@pytest.mark.parametrize("timestamp", [None, True, "invalid", 0, -1, 10**100])
def test_missing_or_invalid_source_epoch_is_unknown(timestamp):
    _, [account] = SimpleFinProvider._parse_accounts({"accounts": [{
        "id": "synthetic", "balance": "0", "currency": "BRL", "balance-date": timestamp,
    }]})
    assert account.balance_metadata is not None
    assert account.balance_metadata["observed_at"] is None
    assert account.balance_metadata["value_available"] is True


async def test_cross_workspace_same_names_and_external_ids_do_not_leak(
    session, test_user, test_workspace,
):
    first, group, _, _ = await _portfolio(session, test_user, test_workspace)
    other = Workspace(id=uuid.uuid4(), name="Other synthetic workspace", kind="personal",
                      created_by_user_id=test_user.id)
    session.add(other)
    await session.flush()
    second, _, _, _ = await _portfolio(session, test_user, other, balance="800", values=("600", "100"))
    [read] = await account_service.get_accounts(session, test_workspace.id)
    assert read["id"] == first.id != second.id
    assert read["balance_explanation"].amount == 120
    assert all(h.value not in {600, 100} for h in read["balance_explanation"].holdings)
    assert await asset_group_service.get_group(session, group.id, other.id, test_user.id) is None


@pytest.mark.parametrize("observation", [None, {}, {
    "observed_at": "2024-02-03T12:00:00", "amount": "20", "currency": "BRL",
}, {
    "observed_at": T0.isoformat(), "amount": "20", "currency": "USD",
}])
async def test_unproved_non_ticker_source_clock_stays_unknown(
    session, test_user, test_workspace, observation,
):
    _, group, _, holdings = await _portfolio(session, test_user, test_workspace)
    holdings[1].external_metadata = {"valuation_observation": observation}
    await session.commit()
    read = await asset_group_service.get_group(session, group.id, test_workspace.id, test_user.id)
    assert read is not None and read.balance_explanation is not None
    detail = read.balance_explanation
    assert detail.holdings[1].observation_basis == "recorded"
    assert detail.holdings[1].value_date is None
    assert detail.reconciliation == "not_comparable"
    assert detail.residual_cash is None


async def test_database_sync_clock_is_serialized_as_utc(
    session, client, auth_headers, test_user, test_workspace,
):
    account, _, connection, _ = await _portfolio(session, test_user, test_workspace)
    connection.last_sync_at = T0.replace(tzinfo=None)
    await session.commit()
    response = (await client.get(f"/api/accounts/{account.id}", headers=auth_headers)).json()
    assert response["balance_explanation"]["last_successful_sync_at"] == "2024-02-03T12:00:00Z"


async def test_unknown_source_currency_is_not_the_provider_fallback(
    session, test_user, test_workspace,
):
    account, _, connection, _ = await _portfolio(session, test_user, test_workspace, values=())
    _, [data] = SimpleFinProvider._parse_accounts({"accounts": [{
        "id": account.external_id, "balance": "120", "currency": "invalid",
        "balance-date": int(T0.timestamp()), "holdings": [],
    }]})
    account.currency = data.currency
    _record_balance_observation(connection, [data], SimpleFinProvider(),
                                persisted_account_ids={data.external_id})
    await session.commit()
    [read] = await account_service.get_accounts(session, test_workspace.id)
    assert read["balance_explanation"].currency is None
    assert "currency_unknown" in read["balance_explanation"].reason_codes
    assert "currency_mismatch" not in read["balance_explanation"].reason_codes
    assert read["balance_explanation"].residual_cash is None


@pytest.mark.parametrize('sync_assets', [False, True])
@pytest.mark.parametrize('source_balance,expected_amount', [(None, None), ('', None), ('invalid', None), ('0', 0), ('120', 120)])
async def test_real_sync_invalid_balance_without_successful_holdings(session, test_user, test_workspace, sync_assets, source_balance, expected_amount):
    from app.services.connection_service import sync_connection
    account, _, connection, _ = await _portfolio(session, test_user, test_workspace, values=())
    connection.credentials = {'synthetic': 'credential'}
    connection.settings = {'sync_assets': sync_assets}
    await session.commit()
    _, [data] = SimpleFinProvider._parse_accounts({'accounts': [{
        'id': account.external_id, 'balance': source_balance, 'currency': 'BRL', 'holdings': [],
    }]})
    provider = SimpleFinProvider()
    provider.refresh_credentials = AsyncMock(return_value={'synthetic': 'credential'})
    provider.get_institution_logo = AsyncMock(return_value=None)
    provider.get_accounts = AsyncMock(return_value=[data])
    provider.get_transactions = AsyncMock(return_value=[])
    provider.get_holdings = AsyncMock(side_effect=RuntimeError('Synthetic holdings failure'))
    provider.get_trades = AsyncMock(return_value=[])
    saved_account_id, saved_workspace_id = account.id, test_workspace.id
    with patch('app.services.connection_service.get_provider', return_value=provider):
        await sync_connection(session, connection.id, test_workspace.id, test_user.id)
    [read] = await account_service.get_accounts(session, saved_workspace_id, account_id=saved_account_id)
    detail = read['balance_explanation']
    assert detail.amount == expected_amount
    assert detail.residual_cash is None


@pytest.mark.parametrize('source_balance', ['-120', '120', '0'])
async def test_real_simplefin_card_observation_binding(session, test_user, test_workspace, source_balance):
    from app.services.connection_service import sync_connection
    account, _, connection, _ = await _portfolio(session, test_user, test_workspace, values=())
    account.type = 'credit_card'
    connection.credentials = {'synthetic': 'credential'}
    connection.settings = {'sync_assets': True}
    await session.commit()
    _, [data] = SimpleFinProvider._parse_accounts({'accounts': [{
        'id': account.external_id, 'balance': source_balance, 'currency': 'BRL',
        'holdings': [], 'balance-date': int(T0.timestamp()),
    }]})
    provider = SimpleFinProvider()
    provider.refresh_credentials = AsyncMock(return_value={'synthetic': 'credential'})
    provider.get_institution_logo = AsyncMock(return_value=None)
    provider.get_accounts = AsyncMock(return_value=[data])
    provider.get_transactions = AsyncMock(return_value=[])
    provider.get_holdings = AsyncMock(return_value=[])
    provider.get_trades = AsyncMock(return_value=[])
    with patch('app.services.connection_service.get_provider', return_value=provider):
        await sync_connection(session, connection.id, test_workspace.id, test_user.id)
    [read] = await account_service.get_accounts(session, test_workspace.id, account_id=account.id)
    detail = read['balance_explanation']
    assert detail.amount == float(source_balance)
    assert detail.observed_at == T0
    assert 'account_observation_changed' not in detail.reason_codes


@pytest.mark.parametrize('sync_assets', [False, True])
@pytest.mark.parametrize('recovered_balance', ['0', '120'])
async def test_account_validity_recovers_without_confirming_holdings(
    session, test_user, test_workspace, sync_assets, recovered_balance,
):
    from app.services.connection_service import sync_connection
    account, _, connection, _ = await _portfolio(session, test_user, test_workspace, values=())
    connection.credentials = {'synthetic': 'credential'}
    connection.settings = {'sync_assets': sync_assets}
    await session.commit()
    account_id, external_id = account.id, account.external_id
    connection_id, workspace_id, user_id = connection.id, test_workspace.id, test_user.id
    provider = SimpleFinProvider()
    provider.refresh_credentials = AsyncMock(return_value={'synthetic': 'credential'})
    provider.get_institution_logo = AsyncMock(return_value=None)
    provider.get_transactions = AsyncMock(return_value=[])
    provider.get_holdings = AsyncMock(side_effect=RuntimeError('Synthetic holdings failure'))
    provider.get_trades = AsyncMock(return_value=[])
    with patch('app.services.connection_service.get_provider', return_value=provider):
        for balance, moment, expected in [(None, T0, None), (recovered_balance, T0 + timedelta(days=1), float(recovered_balance))]:
            _, [data] = SimpleFinProvider._parse_accounts({'accounts': [{
                'id': external_id, 'balance': balance, 'currency': 'BRL',
                'holdings': [], 'balance-date': int(moment.timestamp()),
            }]})
            provider.get_accounts = AsyncMock(return_value=[data])
            await sync_connection(session, connection_id, workspace_id, user_id)
            [read] = await account_service.get_accounts(session, workspace_id, account_id=account_id)
            detail = read['balance_explanation']
            assert detail.amount == expected
            assert detail.observed_at == moment
            assert detail.residual_cash is None
            assert 'holdings_refresh_unconfirmed' in detail.reason_codes
            if expected is not None:
                assert 'account_balance_unavailable' not in detail.reason_codes


@pytest.mark.parametrize('balance', ['120', '130'])
async def test_rejected_current_currency_survives_retained_snapshot_binding(
    session, test_user, test_workspace, balance,
):
    from app.services.connection_service import sync_connection
    account, _, connection, _ = await _portfolio(session, test_user, test_workspace, values=())
    connection.credentials = {'synthetic': 'credential'}
    original = connection.settings['account_balance_observations'][account.external_id]
    connection.settings = {**connection.settings, 'sync_assets': False}
    account.currency = 'USD'
    connection.settings = {**connection.settings, 'account_balance_observations': {
        account.external_id: {**original, 'currency': 'USD'},
    }}
    await session.commit()
    account_id, external_id = account.id, account.external_id
    connection_id, workspace_id, user_id = connection.id, test_workspace.id, test_user.id
    provider = SimpleFinProvider()
    provider.refresh_credentials = AsyncMock(return_value={'synthetic': 'credential'})
    provider.get_institution_logo = AsyncMock(return_value=None)
    provider.get_transactions = AsyncMock(return_value=[])
    with patch('app.services.connection_service.get_provider', return_value=provider):
        for currency in ('invalid', 'USD'):
            _, [data] = SimpleFinProvider._parse_accounts({'accounts': [{
                'id': external_id, 'balance': balance, 'currency': currency,
                'holdings': [], 'balance-date': int((T0 + timedelta(days=1)).timestamp()),
            }]})
            provider.get_accounts = AsyncMock(return_value=[data])
            await sync_connection(session, connection_id, workspace_id, user_id)
            [read] = await account_service.get_accounts(session, workspace_id, account_id=account_id)
            detail = read['balance_explanation']
            assert detail.amount == float(balance)
            assert detail.currency == (None if currency == 'invalid' else 'USD')
            assert ('currency_unknown' in detail.reason_codes) == (currency == 'invalid')
            assert detail.residual_cash is None
            if balance == '120':
                assert detail.observed_at == T0


async def test_asset_api_clocks_match_explanation_instants(
    session, client, auth_headers, test_user, test_workspace,
):
    _, group, _, holdings = await _portfolio(session, test_user, test_workspace)
    holdings[0].last_price_at = T0.replace(tzinfo=None)
    recorded = await session.scalar(select(AssetValue).where(AssetValue.asset_id == holdings[1].id))
    assert recorded is not None
    recorded.recorded_at = T0.replace(tzinfo=None)
    await session.commit()
    response = await client.get('/api/assets', headers=auth_headers)
    assert response.status_code == 200
    by_id = {item['id']: item for item in response.json()}
    assert datetime.fromisoformat(by_id[str(holdings[0].id)]['last_price_at']) == T0
    assert datetime.fromisoformat(by_id[str(holdings[1].id)]['value_updated_at']) == T0
    read = await asset_group_service.get_group(session, group.id, test_workspace.id, test_user.id)
    assert read is not None and read.balance_explanation is not None
    assert read.balance_explanation.holdings[0].observed_at == T0
