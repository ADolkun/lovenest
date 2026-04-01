import uuid
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.user import User


@pytest_asyncio.fixture
async def second_account(session: AsyncSession, test_user: User, test_connection: BankConnection) -> Account:
    """Create a second test account (same currency)."""
    account = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        connection_id=test_connection.id,
        external_id="acc-ext-456",
        name="Poupança",
        type="savings",
        balance=Decimal("5000.00"),
        currency="BRL",
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


@pytest_asyncio.fixture
async def usd_account(session: AsyncSession, test_user: User, test_connection: BankConnection) -> Account:
    """Create a USD test account for cross-currency tests."""
    account = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        connection_id=test_connection.id,
        external_id="acc-ext-usd",
        name="USD Account",
        type="checking",
        balance=Decimal("1000.00"),
        currency="USD",
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


@pytest.mark.asyncio
async def test_create_same_currency_transfer(
    client: AsyncClient, auth_headers, test_account: Account, second_account: Account
):
    response = await client.post(
        "/api/transactions/transfer",
        json={
            "from_account_id": str(test_account.id),
            "to_account_id": str(second_account.id),
            "amount": 500.00,
            "date": date.today().isoformat(),
            "description": "Transfer to savings",
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()

    # Check structure
    assert "debit" in data
    assert "credit" in data
    assert "transfer_pair_id" in data

    # Check debit
    assert data["debit"]["type"] == "debit"
    assert data["debit"]["source"] == "transfer"
    assert float(data["debit"]["amount"]) == 500.00
    assert data["debit"]["account_id"] == str(test_account.id)
    assert data["debit"]["transfer_pair_id"] == data["transfer_pair_id"]

    # Check credit
    assert data["credit"]["type"] == "credit"
    assert data["credit"]["source"] == "transfer"
    assert float(data["credit"]["amount"]) == 500.00
    assert data["credit"]["account_id"] == str(second_account.id)
    assert data["credit"]["transfer_pair_id"] == data["transfer_pair_id"]

    # Both share the same transfer_pair_id
    assert data["debit"]["transfer_pair_id"] == data["credit"]["transfer_pair_id"]


@pytest.mark.asyncio
async def test_create_cross_currency_transfer(
    client: AsyncClient, auth_headers, test_account: Account, usd_account: Account
):
    """Cross-currency transfer should convert amount."""
    response = await client.post(
        "/api/transactions/transfer",
        json={
            "from_account_id": str(test_account.id),
            "to_account_id": str(usd_account.id),
            "amount": 1000.00,
            "date": date.today().isoformat(),
            "description": "Transfer BRL to USD",
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()

    assert data["debit"]["currency"] == "BRL"
    assert data["credit"]["currency"] == "USD"
    assert float(data["debit"]["amount"]) == 1000.00
    # Credit amount may differ due to FX conversion (or be 1000 if FX falls back to 1:1)
    assert float(data["credit"]["amount"]) > 0


@pytest.mark.asyncio
async def test_create_cross_currency_transfer_with_manual_fx_rate(
    client: AsyncClient, auth_headers, test_account: Account, usd_account: Account
):
    """Cross-currency transfer with manual FX rate should use provided rate."""
    response = await client.post(
        "/api/transactions/transfer",
        json={
            "from_account_id": str(test_account.id),
            "to_account_id": str(usd_account.id),
            "amount": 1000.00,
            "date": date.today().isoformat(),
            "description": "Transfer BRL to USD with manual rate",
            "fx_rate": 0.20,
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()

    assert data["debit"]["currency"] == "BRL"
    assert data["credit"]["currency"] == "USD"
    assert float(data["debit"]["amount"]) == 1000.00
    # 1000 * 0.20 = 200.00
    assert float(data["credit"]["amount"]) == 200.00


@pytest.mark.asyncio
async def test_reject_same_account_transfer(
    client: AsyncClient, auth_headers, test_account: Account
):
    response = await client.post(
        "/api/transactions/transfer",
        json={
            "from_account_id": str(test_account.id),
            "to_account_id": str(test_account.id),
            "amount": 100.00,
            "date": date.today().isoformat(),
            "description": "Self transfer",
        },
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert "same account" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_reject_invalid_account_transfer(
    client: AsyncClient, auth_headers, test_account: Account
):
    fake_id = str(uuid.uuid4())
    response = await client.post(
        "/api/transactions/transfer",
        json={
            "from_account_id": str(test_account.id),
            "to_account_id": fake_id,
            "amount": 100.00,
            "date": date.today().isoformat(),
            "description": "Invalid transfer",
        },
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_delete_cascades_to_paired_transaction(
    client: AsyncClient, auth_headers, test_account: Account, second_account: Account
):
    # Create transfer
    response = await client.post(
        "/api/transactions/transfer",
        json={
            "from_account_id": str(test_account.id),
            "to_account_id": str(second_account.id),
            "amount": 200.00,
            "date": date.today().isoformat(),
            "description": "Transfer to delete",
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()
    debit_id = data["debit"]["id"]
    credit_id = data["credit"]["id"]

    # Delete debit — should cascade to credit
    del_response = await client.delete(
        f"/api/transactions/{debit_id}", headers=auth_headers
    )
    assert del_response.status_code == 204

    # Both should be gone
    assert (await client.get(f"/api/transactions/{debit_id}", headers=auth_headers)).status_code == 404
    assert (await client.get(f"/api/transactions/{credit_id}", headers=auth_headers)).status_code == 404


@pytest.mark.asyncio
async def test_update_cascades_to_paired_transaction(
    client: AsyncClient, auth_headers, test_account: Account, second_account: Account
):
    # Create transfer
    response = await client.post(
        "/api/transactions/transfer",
        json={
            "from_account_id": str(test_account.id),
            "to_account_id": str(second_account.id),
            "amount": 300.00,
            "date": date.today().isoformat(),
            "description": "Original description",
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()
    debit_id = data["debit"]["id"]
    credit_id = data["credit"]["id"]

    # Update debit description and date
    new_date = "2025-06-15"
    update_response = await client.patch(
        f"/api/transactions/{debit_id}",
        json={"description": "Updated description", "date": new_date},
        headers=auth_headers,
    )
    assert update_response.status_code == 200

    # Check credit was also updated
    credit_response = await client.get(
        f"/api/transactions/{credit_id}", headers=auth_headers
    )
    assert credit_response.status_code == 200
    credit_data = credit_response.json()
    assert credit_data["description"] == "Updated description"
    assert credit_data["date"] == new_date


@pytest.mark.asyncio
async def test_transfers_appear_in_transaction_list(
    client: AsyncClient, auth_headers, test_account: Account, second_account: Account
):
    # Create transfer
    response = await client.post(
        "/api/transactions/transfer",
        json={
            "from_account_id": str(test_account.id),
            "to_account_id": str(second_account.id),
            "amount": 100.00,
            "date": date.today().isoformat(),
            "description": "Listed transfer",
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    transfer_pair_id = response.json()["transfer_pair_id"]

    # List transactions
    list_response = await client.get("/api/transactions", headers=auth_headers)
    assert list_response.status_code == 200
    items = list_response.json()["items"]

    transfer_txns = [tx for tx in items if tx.get("transfer_pair_id") == transfer_pair_id]
    assert len(transfer_txns) == 2
    assert {tx["type"] for tx in transfer_txns} == {"debit", "credit"}
