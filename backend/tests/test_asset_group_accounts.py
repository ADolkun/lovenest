"""Account identity and valuation boundaries for the portfolio account view."""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_value import AssetValue
from app.models.bank_connection import BankConnection
from app.models.workspace import Workspace
from app.services import asset_group_service


def _account(user, workspace, **kwargs):
    return Account(
        id=uuid.uuid4(), user_id=user.id, workspace_id=workspace.id,
        name="Exchange", type="investment", currency="BRL", balance=Decimal("0"),
        **kwargs,
    )


async def test_manual_portfolio_link_is_explicit_and_preserves_cash(
    session, client, auth_headers, test_user, test_workspace,
):
    account = _account(test_user, test_workspace)
    session.add(account)
    await session.commit()
    response = await client.post("/api/asset-groups", headers=auth_headers, json={"name": account.name})
    assert response.status_code == 201
    group_id = response.json()["id"]
    assert response.json()["account_id"] is None  # A matching name is not identity.

    response = await client.patch(f"/api/asset-groups/{group_id}", headers=auth_headers,
                                  json={"account_id": str(account.id)})
    assert response.status_code == 200
    assert response.json()["account_id"] == str(account.id)
    assert response.json()["account_type"] == "investment"
    assert response.json()["account_balance"] is None  # Not a provider-inclusive balance.

    session.add(Asset(user_id=test_user.id, workspace_id=test_workspace.id,
                      group_id=uuid.UUID(group_id), name="Holding", type="crypto", currency="BRL",
                      purchase_price=Decimal("125")))
    await session.commit()
    groups = (await client.get("/api/asset-groups", headers=auth_headers)).json()
    assert groups[0]["current_value_primary"] == 125
    assert groups[0]["account_id"] == str(account.id)
    accounts = (await client.get("/api/accounts", headers=auth_headers)).json()
    assert accounts[0]["current_balance"] == 0

    response = await client.patch(f"/api/asset-groups/{group_id}", headers=auth_headers,
                                  json={"account_id": None})
    assert response.status_code == 200
    assert response.json()["account_id"] is None


@pytest.mark.parametrize("invalid", ["other_workspace", "missing", "closed", "checking", "connected"])
async def test_manual_portfolio_rejects_invalid_account_links(
    session, client, auth_headers, test_user, test_workspace, invalid,
):
    account = _account(test_user, test_workspace)
    if invalid == "other_workspace":
        other = Workspace(id=uuid.uuid4(), name="Other", kind="personal",
                          created_by_user_id=test_user.id)
        session.add(other)
        await session.flush()
        account.workspace_id = other.id
    elif invalid == "closed":
        account.is_closed = True
    elif invalid == "checking":
        account.type = "checking"
    elif invalid == "connected":
        connection = BankConnection(id=uuid.uuid4(), user_id=test_user.id,
                                    workspace_id=test_workspace.id, provider="coinbase",
                                    external_id="exchange", institution_name="Exchange", credentials={})
        session.add(connection)
        await session.flush()
        account.connection_id = connection.id
    if invalid != "missing":
        session.add(account)
    await session.commit()

    response = await client.post("/api/asset-groups", headers=auth_headers,
                                 json={"name": "Portfolio", "account_id": str(account.id)})
    assert response.status_code == (404 if invalid in {"missing", "other_workspace"} else 400)


async def test_an_account_can_only_be_linked_once_and_synced_wallets_cannot_be_relinked(
    session, client, auth_headers, test_user, test_workspace,
):
    account = _account(test_user, test_workspace)
    session.add(account)
    await session.commit()
    first = await client.post("/api/asset-groups", headers=auth_headers,
                              json={"name": "First", "account_id": str(account.id)})
    assert first.status_code == 201
    duplicate = await client.post("/api/asset-groups", headers=auth_headers,
                                  json={"name": "Second", "account_id": str(account.id)})
    assert duplicate.status_code == 409
    first_id = first.json()["id"]
    unchanged = await client.patch(f"/api/asset-groups/{first_id}", headers=auth_headers,
                                   json={"account_id": str(account.id), "name": "Renamed"})
    assert unchanged.status_code == 200

    synced = AssetGroup(id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
                        source="coinbase", name="Synced")
    session.add(synced)
    await session.commit()
    for link in (str(account.id), None):
        response = await client.patch(f"/api/asset-groups/{synced.id}", headers=auth_headers,
                                      json={"account_id": link})
        assert response.status_code == 400


async def test_provider_account_identity_uses_attribution_and_blocks_manual_duplicate(
    session, client, auth_headers, test_user, test_workspace,
):
    # A legacy/orphan provider wallet can still resolve a manually retained account.
    account = _account(test_user, test_workspace, external_id="provider-account")
    group = AssetGroup(id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
                       source="simplefin", name="Renamed wallet")
    session.add_all([account, group])
    await session.flush()
    session.add(Asset(user_id=test_user.id, workspace_id=test_workspace.id, group_id=group.id,
                      name="Holding", type="investment", currency="BRL", purchase_price=Decimal("10"),
                      account_external_id=account.external_id))
    await session.commit()
    read = await asset_group_service.get_group(session, group.id, test_workspace.id, test_user.id)
    assert read is not None
    assert read.account_id == account.id
    duplicate = await client.post("/api/asset-groups", headers=auth_headers,
                                  json={"name": "Duplicate", "account_id": str(account.id)})
    assert duplicate.status_code == 409


async def test_database_prevents_two_portfolios_linking_the_same_account(
    session, test_user, test_workspace,
):
    account = _account(test_user, test_workspace)
    session.add(account)
    await session.flush()
    for name in ("First", "Second"):
        session.add(AssetGroup(user_id=test_user.id, workspace_id=test_workspace.id,
                               name=name, source="manual", account_id=account.id))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


@pytest.mark.parametrize("change", [{"is_closed": True}, {"type": "checking"}])
async def test_saved_link_survives_account_lifecycle_changes(
    session, client, auth_headers, test_user, test_workspace, change,
):
    account = _account(test_user, test_workspace)
    session.add(account)
    await session.commit()
    created = await client.post("/api/asset-groups", headers=auth_headers,
                                json={"name": "Portfolio", "account_id": str(account.id)})
    assert created.status_code == 201
    for key, value in change.items():
        setattr(account, key, value)
    await session.commit()

    groups = (await client.get("/api/asset-groups", headers=auth_headers)).json()
    assert groups[0]["account_id"] == str(account.id)
    assert groups[0]["account_balance"] is None
    unlinked = await client.patch(f"/api/asset-groups/{created.json()['id']}",
                                  headers=auth_headers, json={"account_id": None})
    assert unlinked.status_code == 200
    assert unlinked.json()["account_id"] is None


async def test_unknown_and_zero_portfolio_values_are_distinct(
    session, test_user, test_workspace,
):
    for name, price, missing in (("Unknown", None, 1), ("Zero", Decimal("0"), 0)):
        group = AssetGroup(id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
                           name=name, source="manual")
        session.add(group)
        await session.flush()
        session.add(Asset(user_id=test_user.id, workspace_id=test_workspace.id, group_id=group.id,
                          name="Holding", type="crypto", currency="BRL",
                          valuation_method="market_price", units=Decimal("2"), last_price=price))
        await session.flush()
        read = await asset_group_service.get_group(session, group.id, test_workspace.id, test_user.id)
        assert read is not None
        assert read.current_value_primary == 0
        assert read.asset_count == 1
        assert read.unvalued_count == missing


async def test_portfolio_uses_current_quotes_and_reports_unvalued_holdings(
    session, client, auth_headers, test_user, test_workspace, monkeypatch,
):
    group = AssetGroup(id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
                       source="manual", name="Valuations")
    session.add(group)
    await session.flush()
    current = Asset(id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
                    group_id=group.id, name="Quoted", type="crypto", currency="BRL",
                    valuation_method="market_price", units=Decimal("2"), last_price=Decimal("30"))
    unknown = Asset(user_id=test_user.id, workspace_id=test_workspace.id, group_id=group.id,
                    name="Unpriced", type="crypto", currency="BRL", valuation_method="market_price",
                    units=Decimal("2"), purchase_price=Decimal("999"))
    option = Asset(user_id=test_user.id, workspace_id=test_workspace.id, group_id=group.id,
                   name="Option", type="option", currency="USD", valuation_method="market_price",
                   units=Decimal("1"), last_price=Decimal("2"))
    session.add_all([current, unknown, option])
    await session.flush()
    session.add(AssetValue(asset_id=current.id, workspace_id=test_workspace.id,
                           amount=Decimal("5"), date=date(2020, 1, 1), source="market_price"))
    await session.commit()

    async def convert(session, amount, from_currency, to_currency, **kwargs):
        assert (from_currency, to_currency) == ("USD", "BRL")
        assert isinstance(amount, Decimal)
        return amount * Decimal("5"), Decimal("5")

    monkeypatch.setattr(asset_group_service, "convert", convert)
    result = (await client.get("/api/asset-groups", headers=auth_headers)).json()[0]
    assert result["current_value_primary"] == 1060  # 2 * 30 + 1 * 2 * 100 * 5.
    assert result["currency"] is None  # A mixed native sum has no denomination.
    assert result["asset_count"] == 3
    assert result["unvalued_count"] == 1
