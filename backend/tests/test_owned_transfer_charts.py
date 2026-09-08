"""The real movement ledger reaches both chart readers without hiding source values."""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.asset_value import AssetValue
from app.models.bank_connection import BankConnection
from app.models.investment_evidence import InvestmentLeg, InvestmentObservation
from app.services.asset_service import _load_asset_native_values, get_asset_value_trend
from tests.test_owned_transfers_integration import (
    confirm_transfer, pair, select_lots, transfers as transfers,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_state", ["settled", "unknown_basis", "unconfirmed", "provider_mismatch"])
async def test_both_history_readers_qualify_movements_and_preserve_source_values(transfers, source_state):
    v = transfers
    request = await select_lots(v, await pair(v, v.a, v.b))
    await confirm_transfer(v, request)
    v.session.add(AssetValue(
        asset_id=v.b.id, date=date(2025, 2, 3), amount=Decimal("495"), price=Decimal("99"),
    ))
    if source_state == "unknown_basis":
        # A changed acquisition invalidates its cost without invalidating settled units.
        v.acquisition.price = Decimal("30")
    elif source_state == "unconfirmed":
        leg = await v.session.get(InvestmentLeg, uuid.UUID(request["in_leg_id"]))
        observation = await v.session.get(InvestmentObservation, leg.observation_id)
        observation.payload = {**observation.payload, "network_status": "unconfirmed"}
    elif source_state == "provider_mismatch":
        connection = BankConnection(
            workspace_id=v.workspace.id, user_id=v.user.id, provider="onchain",
            external_id="synthetic-chart-provider", institution_name="Synthetic chart source", credentials={},
        )
        v.session.add(connection)
        await v.session.flush()
        v.b.connection_id = connection.id
        v.b.units = Decimal("5")
    await v.session.commit()

    expected = 495.0 if source_state in {"unconfirmed", "provider_mismatch"} else 297.0
    individual = await get_asset_value_trend(v.session, v.b.id, v.workspace.id)
    bulk = await _load_asset_native_values(v.session, [v.b])
    assert individual is not None
    assert {row["date"]: row["amount"] for row in individual}["2025-02-03"] == expected
    assert dict(bulk[str(v.b.id)])[date(2025, 2, 3)] == expected
    assert bulk[str(v.b.id)] == [(date.fromisoformat(row["date"]), row["amount"]) for row in individual]
