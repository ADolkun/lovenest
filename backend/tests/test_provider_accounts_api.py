"""Provider account discovery endpoint (issue #51)."""
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.user import User
from app.providers.base import AccountData


def _account(external_id: str, name: str = "Checking", has_holdings: bool = False) -> AccountData:
    return AccountData(
        external_id=external_id,
        name=name,
        type="checking",
        balance=Decimal("12.34"),
        currency="USD",
        has_holdings=has_holdings,
    )


def _provider(accounts: list[AccountData]) -> AsyncMock:
    provider = AsyncMock()
    provider.refresh_credentials = AsyncMock(side_effect=lambda creds: creds)
    provider.get_accounts = AsyncMock(return_value=accounts)
    return provider


async def _list_accounts(client: AsyncClient, headers: dict, connection_id, provider):
    with patch("app.services.connection_service.get_provider", return_value=provider):
        return await client.get(
            f"/api/connections/{connection_id}/provider-accounts", headers=headers
        )


async def _set_settings(
    session: AsyncSession, connection: BankConnection, settings: dict
) -> None:
    connection.settings = settings
    session.add(connection)
    await session.commit()


def _by_id(payload: list[dict]) -> dict[str, dict]:
    return {row["external_id"]: row for row in payload}


@pytest.mark.asyncio
async def test_lists_every_provider_account_with_details(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    """Every account the provider exposes is returned, with the detail needed to tell them apart."""
    provider = _provider([_account("acc-1", name="Joint Checking", has_holdings=True)])

    resp = await _list_accounts(client, auth_headers, test_connection.id, provider)

    assert resp.status_code == 200
    row = resp.json()[0]
    assert row["external_id"] == "acc-1"
    assert row["name"] == "Joint Checking"
    assert Decimal(str(row["balance"])) == Decimal("12.34")
    assert row["currency"] == "USD"
    assert row["has_holdings"] is True


@pytest.mark.asyncio
async def test_without_allowlist_everything_is_included(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    """Legacy connections sync everything, so every account reports as included."""
    provider = _provider([_account("acc-1"), _account("acc-2")])

    resp = await _list_accounts(client, auth_headers, test_connection.id, provider)

    assert [row["status"] for row in resp.json()] == ["included", "included"]


@pytest.mark.asyncio
async def test_allowlist_splits_included_from_excluded(
    client: AsyncClient,
    auth_headers,
    session: AsyncSession,
    test_connection: BankConnection,
):
    """An unlisted account sync has already seen was deliberately unchecked."""
    await _set_settings(
        session,
        test_connection,
        {"account_allowlist": ["acc-1"], "seen_account_ids": ["acc-1", "acc-2"]},
    )
    provider = _provider([_account("acc-1"), _account("acc-2")])

    resp = await _list_accounts(client, auth_headers, test_connection.id, provider)

    rows = _by_id(resp.json())
    assert rows["acc-1"]["status"] == "included"
    assert rows["acc-2"]["status"] == "excluded"


@pytest.mark.asyncio
async def test_unseen_unlisted_account_is_pending(
    client: AsyncClient,
    auth_headers,
    session: AsyncSession,
    test_connection: BankConnection,
):
    """An account that showed up after the allowlist was set is pending, not excluded."""
    await _set_settings(
        session,
        test_connection,
        {"account_allowlist": ["acc-1"], "seen_account_ids": ["acc-1"]},
    )
    provider = _provider([_account("acc-1"), _account("acc-new")])

    resp = await _list_accounts(client, auth_headers, test_connection.id, provider)

    assert _by_id(resp.json())["acc-new"]["status"] == "pending"


@pytest.mark.asyncio
async def test_already_imported_account_is_excluded_without_seen_ids(
    client: AsyncClient,
    auth_headers,
    session: AsyncSession,
    test_user: User,
    test_connection: BankConnection,
):
    """Connections that last synced before seen ids existed still know their own accounts."""
    session.add(
        Account(
            id=uuid.uuid4(),
            user_id=test_user.id,
            connection_id=test_connection.id,
            name="Old Checking",
            type="checking",
            balance=Decimal("0"),
            currency="USD",
            external_id="acc-2",
        )
    )
    await _set_settings(session, test_connection, {"account_allowlist": ["acc-1"]})
    provider = _provider([_account("acc-1"), _account("acc-2")])

    resp = await _list_accounts(client, auth_headers, test_connection.id, provider)

    assert _by_id(resp.json())["acc-2"]["status"] == "excluded"


@pytest.mark.asyncio
async def test_issues_one_provider_request(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    """SimpleFIN's per-account filter has its own rate-limit bucket — never fan out."""
    provider = _provider([_account(f"acc-{i}") for i in range(5)])

    resp = await _list_accounts(client, auth_headers, test_connection.id, provider)

    assert resp.status_code == 200
    assert provider.get_accounts.await_count == 1
    provider.get_transactions.assert_not_awaited()
    provider.get_holdings.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_failure_is_an_error_not_an_empty_list(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    """An unreachable provider must never look like "this connection has no accounts"."""
    provider = _provider([])
    provider.get_accounts = AsyncMock(side_effect=RuntimeError("provider down"))

    resp = await _list_accounts(client, auth_headers, test_connection.id, provider)

    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_missing_provider_account_stays_in_the_allowlist(
    client: AsyncClient,
    auth_headers,
    session: AsyncSession,
    test_connection: BankConnection,
):
    """A provider outage must not silently drop the user's selection."""
    await _set_settings(
        session, test_connection, {"account_allowlist": ["acc-1", "acc-gone"]}
    )
    provider = _provider([_account("acc-1")])

    resp = await _list_accounts(client, auth_headers, test_connection.id, provider)

    assert [row["external_id"] for row in resp.json()] == ["acc-1"]
    listing = await client.get("/api/connections", headers=auth_headers)
    assert listing.json()[0]["settings"]["account_allowlist"] == ["acc-1", "acc-gone"]


@pytest.mark.asyncio
async def test_non_member_gets_404(
    client: AsyncClient, other_workspace_headers, test_connection: BankConnection
):
    """Tenancy convention: a connection outside your workspace does not exist."""
    provider = _provider([_account("acc-1")])

    resp = await _list_accounts(
        client, other_workspace_headers, test_connection.id, provider
    )

    assert resp.status_code == 404
