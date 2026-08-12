"""Tests for connection settings PATCH endpoint (Phase 2)."""
import pytest
from httpx import AsyncClient

from app.models.bank_connection import BankConnection


@pytest.mark.asyncio
async def test_update_payee_source(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    """PATCH connection settings updates payee_source."""
    resp = await client.patch(
        f"/api/connections/{test_connection.id}/settings",
        headers=auth_headers,
        json={"payee_source": "merchant"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["settings"]["payee_source"] == "merchant"


@pytest.mark.asyncio
async def test_update_import_pending(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    """PATCH connection settings updates import_pending."""
    resp = await client.patch(
        f"/api/connections/{test_connection.id}/settings",
        headers=auth_headers,
        json={"import_pending": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["settings"]["import_pending"] is False


@pytest.mark.asyncio
async def test_update_sync_assets(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    """PATCH connection settings updates sync_assets."""
    resp = await client.patch(
        f"/api/connections/{test_connection.id}/settings",
        headers=auth_headers,
        json={"sync_assets": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["settings"]["sync_assets"] is False


@pytest.mark.asyncio
async def test_update_both_settings(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    """PATCH can update both settings at once."""
    resp = await client.patch(
        f"/api/connections/{test_connection.id}/settings",
        headers=auth_headers,
        json={"payee_source": "description", "import_pending": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["settings"]["payee_source"] == "description"
    assert data["settings"]["import_pending"] is False


@pytest.mark.asyncio
async def test_partial_update_preserves_other_settings(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    """Updating one setting preserves the other."""
    # Set both first
    await client.patch(
        f"/api/connections/{test_connection.id}/settings",
        headers=auth_headers,
        json={"payee_source": "merchant", "import_pending": False},
    )

    # Update only payee_source
    resp = await client.patch(
        f"/api/connections/{test_connection.id}/settings",
        headers=auth_headers,
        json={"payee_source": "none"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["settings"]["payee_source"] == "none"
    assert data["settings"]["import_pending"] is False


@pytest.mark.asyncio
async def test_invalid_payee_source_rejected(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    """Invalid payee_source value should be rejected by validation."""
    resp = await client.patch(
        f"/api/connections/{test_connection.id}/settings",
        headers=auth_headers,
        json={"payee_source": "invalid_value"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_settings_connection_not_found(client: AsyncClient, auth_headers, test_connection):
    """PATCH on nonexistent connection returns 404."""
    resp = await client.patch(
        "/api/connections/00000000-0000-0000-0000-000000000000/settings",
        headers=auth_headers,
        json={"payee_source": "auto"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_settings_visible_in_connection_list(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    """Connection settings appear in the connection list response."""
    # Set a setting
    await client.patch(
        f"/api/connections/{test_connection.id}/settings",
        headers=auth_headers,
        json={"payee_source": "payment_data"},
    )

    # List connections
    resp = await client.get("/api/connections", headers=auth_headers)
    assert resp.status_code == 200
    conn = resp.json()[0]
    assert conn["settings"]["payee_source"] == "payment_data"


@pytest.mark.asyncio
async def test_set_account_allowlist(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    """A list of provider account ids persists and comes back on the read model."""
    resp = await client.patch(
        f"/api/connections/{test_connection.id}/settings",
        headers=auth_headers,
        json={"account_allowlist": ["acc-1", "acc-2"]},
    )
    assert resp.status_code == 200
    assert resp.json()["settings"]["account_allowlist"] == ["acc-1", "acc-2"]

    listing = await client.get("/api/connections", headers=auth_headers)
    assert listing.json()[0]["settings"]["account_allowlist"] == ["acc-1", "acc-2"]


@pytest.mark.asyncio
async def test_empty_account_allowlist_persists(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    """An empty list means "sync nothing" — it is stored, not treated as unset."""
    resp = await client.patch(
        f"/api/connections/{test_connection.id}/settings",
        headers=auth_headers,
        json={"account_allowlist": []},
    )
    assert resp.status_code == 200
    settings = resp.json()["settings"]
    assert "account_allowlist" in settings
    assert settings["account_allowlist"] == []


@pytest.mark.asyncio
async def test_omitting_account_allowlist_preserves_it(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    """Updating other settings leaves an existing allowlist untouched."""
    await client.patch(
        f"/api/connections/{test_connection.id}/settings",
        headers=auth_headers,
        json={"account_allowlist": ["acc-1"]},
    )
    resp = await client.patch(
        f"/api/connections/{test_connection.id}/settings",
        headers=auth_headers,
        json={"payee_source": "merchant"},
    )
    assert resp.status_code == 200
    settings = resp.json()["settings"]
    assert settings["account_allowlist"] == ["acc-1"]
    assert settings["payee_source"] == "merchant"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["acc-1", {"acc-1": True}, ["acc-1", 2], [None]])
async def test_invalid_account_allowlist_rejected(
    client: AsyncClient, auth_headers, test_connection: BankConnection, value
):
    """Anything that is not a list of strings is a validation error."""
    resp = await client.patch(
        f"/api/connections/{test_connection.id}/settings",
        headers=auth_headers,
        json={"account_allowlist": value},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_account_allowlist_hidden_from_non_member(
    client: AsyncClient,
    auth_headers,
    other_workspace_headers,
    test_connection: BankConnection,
):
    """A non-member of the connection's workspace gets 404, not a write."""
    resp = await client.patch(
        f"/api/connections/{test_connection.id}/settings",
        headers=other_workspace_headers,
        json={"account_allowlist": ["acc-1"]},
    )
    assert resp.status_code == 404

    read_back = await client.get("/api/connections", headers=auth_headers)
    assert "account_allowlist" not in (read_back.json()[0]["settings"] or {})
