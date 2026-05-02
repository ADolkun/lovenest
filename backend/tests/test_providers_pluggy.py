"""Parser tests for the Pluggy provider, focused on the
`creditCardMetadata` → `TransactionData` mapping introduced with the
installment-metadata v1 feature (issue #14).

These tests exercise `PluggyProvider.get_transactions` with an httpx
client stubbed out, so no network traffic happens.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.providers.pluggy import PluggyProvider


def _mock_httpx_client(results: list[dict]) -> MagicMock:
    """Build a MagicMock that behaves like an `httpx.AsyncClient` context
    manager whose `.get()` returns a single page of `results`."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={"results": results, "totalPages": 1})

    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


async def _fetch(txns: list[dict]):
    provider = PluggyProvider()
    fake_client = _mock_httpx_client(txns)
    with patch.object(
        PluggyProvider, "_ensure_api_key", new=AsyncMock(return_value="fake-key")
    ), patch("app.providers.pluggy.httpx.AsyncClient", return_value=fake_client):
        return await provider.get_transactions({"item_id": "i"}, "acc-ext-1")


@pytest.mark.asyncio
async def test_parser_captures_full_installment_metadata():
    """Happy path: all 4 creditCardMetadata fields flow into TransactionData."""
    result = await _fetch([
        {
            "id": "tx-1",
            "description": "AMAZON PARCELADO",
            "amount": -120.50,
            "date": "2026-04-10",
            "type": "DEBIT",
            "creditCardMetadata": {
                "installmentNumber": 3,
                "totalInstallments": 12,
                "totalAmount": 1446.00,
                "purchaseDate": "2026-02-10",
            },
        }
    ])
    assert len(result) == 1
    tx = result[0]
    assert tx.installment_number == 3
    assert tx.total_installments == 12
    assert tx.installment_total_amount == Decimal("1446.00")
    assert tx.installment_purchase_date == date(2026, 2, 10)


@pytest.mark.asyncio
async def test_parser_no_credit_card_metadata_leaves_fields_none():
    """Non-CC txns (no creditCardMetadata) get null installment fields."""
    result = await _fetch([
        {
            "id": "tx-2",
            "description": "GROCERIES",
            "amount": -30.00,
            "date": "2026-04-11",
            "type": "DEBIT",
        }
    ])
    tx = result[0]
    assert tx.installment_number is None
    assert tx.total_installments is None
    assert tx.installment_total_amount is None
    assert tx.installment_purchase_date is None


@pytest.mark.asyncio
async def test_parser_empty_credit_card_metadata():
    """`creditCardMetadata: {}` should yield all-null installment fields."""
    result = await _fetch([
        {
            "id": "tx-3",
            "description": "SINGLE CHARGE",
            "amount": -50.00,
            "date": "2026-04-11",
            "type": "DEBIT",
            "creditCardMetadata": {},
        }
    ])
    tx = result[0]
    assert tx.installment_number is None
    assert tx.total_installments is None
    assert tx.installment_total_amount is None
    assert tx.installment_purchase_date is None


@pytest.mark.asyncio
async def test_parser_null_credit_card_metadata():
    """`creditCardMetadata: null` should be handled like missing."""
    result = await _fetch([
        {
            "id": "tx-4",
            "description": "NULL META",
            "amount": -10,
            "date": "2026-04-12",
            "type": "DEBIT",
            "creditCardMetadata": None,
        }
    ])
    tx = result[0]
    assert tx.installment_number is None
    assert tx.installment_total_amount is None


@pytest.mark.asyncio
async def test_parser_invalid_installment_number_types_coerce_to_none():
    """Non-integer installmentNumber/totalInstallments must not break parsing."""
    result = await _fetch([
        {
            "id": "tx-5",
            "description": "BAD TYPES",
            "amount": -1,
            "date": "2026-04-12",
            "type": "DEBIT",
            "creditCardMetadata": {
                "installmentNumber": "3",  # string, not int
                "totalInstallments": 12.0,  # float, not int
                "totalAmount": 100,
                "purchaseDate": "2026-04-01",
            },
        }
    ])
    tx = result[0]
    assert tx.installment_number is None
    assert tx.total_installments is None
    assert tx.installment_total_amount == Decimal("100")
    assert tx.installment_purchase_date == date(2026, 4, 1)


@pytest.mark.asyncio
async def test_parser_malformed_purchase_date_falls_back_to_none():
    """Invalid purchaseDate strings should not raise — silently drop."""
    result = await _fetch([
        {
            "id": "tx-6",
            "description": "BAD DATE",
            "amount": -1,
            "date": "2026-04-12",
            "type": "DEBIT",
            "creditCardMetadata": {
                "installmentNumber": 1,
                "totalInstallments": 2,
                "totalAmount": 2,
                "purchaseDate": "not-a-date",
            },
        }
    ])
    tx = result[0]
    assert tx.installment_purchase_date is None
    assert tx.installment_number == 1
    assert tx.total_installments == 2


@pytest.mark.asyncio
async def test_parser_purchase_date_with_time_suffix():
    """ISO datetime strings (with time) should be truncated to date cleanly."""
    result = await _fetch([
        {
            "id": "tx-7",
            "description": "WITH TIME",
            "amount": -1,
            "date": "2026-04-12",
            "type": "DEBIT",
            "creditCardMetadata": {
                "installmentNumber": 1,
                "totalInstallments": 1,
                "totalAmount": 10,
                "purchaseDate": "2026-01-15T12:34:56.000Z",
            },
        }
    ])
    tx = result[0]
    assert tx.installment_purchase_date == date(2026, 1, 15)


@pytest.mark.asyncio
async def test_parser_negative_total_amount_is_stored_as_absolute():
    """Pluggy may report negative totalAmount for debits; we store absolute."""
    result = await _fetch([
        {
            "id": "tx-8",
            "description": "NEG TOTAL",
            "amount": -10,
            "date": "2026-04-12",
            "type": "DEBIT",
            "creditCardMetadata": {
                "installmentNumber": 2,
                "totalInstallments": 6,
                "totalAmount": -600.00,
                "purchaseDate": "2026-01-01",
            },
        }
    ])
    tx = result[0]
    assert tx.installment_total_amount == Decimal("600.00")


@pytest.mark.asyncio
async def test_parser_captures_bill_external_id():
    """`creditCardMetadata.billId` flows into TransactionData.bill_external_id —
    the sync layer resolves it to a credit_card_bills FK (issue #92)."""
    result = await _fetch([
        {
            "id": "tx-bill-1",
            "description": "RESTAURANT",
            "amount": -50.00,
            "date": "2026-04-10",
            "type": "DEBIT",
            "creditCardMetadata": {"billId": "bill-abc-123"},
        }
    ])
    assert result[0].bill_external_id == "bill-abc-123"


@pytest.mark.asyncio
async def test_parser_no_bill_id_leaves_field_none():
    result = await _fetch([
        {
            "id": "tx-no-bill",
            "description": "X",
            "amount": -10,
            "date": "2026-04-10",
            "type": "DEBIT",
            "creditCardMetadata": {"installmentNumber": 1, "totalInstallments": 1},
        }
    ])
    assert result[0].bill_external_id is None


@pytest.mark.asyncio
async def test_parser_bill_id_coerced_to_string():
    """Defensive: providers may emit numeric bill ids; column is String(255)."""
    result = await _fetch([
        {
            "id": "tx-num-bill",
            "description": "X",
            "amount": -10,
            "date": "2026-04-10",
            "type": "DEBIT",
            "creditCardMetadata": {"billId": 999},
        }
    ])
    assert result[0].bill_external_id == "999"


@pytest.mark.asyncio
async def test_parser_missing_purchase_date_only():
    """Some connectors omit purchaseDate even when counts are present."""
    result = await _fetch([
        {
            "id": "tx-9",
            "description": "NO PURCHASE DATE",
            "amount": -25,
            "date": "2026-04-12",
            "type": "DEBIT",
            "creditCardMetadata": {
                "installmentNumber": 4,
                "totalInstallments": 10,
                "totalAmount": 250,
            },
        }
    ])
    tx = result[0]
    assert tx.installment_number == 4
    assert tx.total_installments == 10
    assert tx.installment_total_amount == Decimal("250")
    assert tx.installment_purchase_date is None
