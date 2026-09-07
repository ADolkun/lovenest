"""Synthetic source overlap, application, replay and authorization boundaries."""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.models.bank_connection import BankConnection
from app.models.investment_evidence import InvestmentEvent, InvestmentLeg, InvestmentObservation
from app.providers.base import HoldingData, InvestmentActivity, TradeData
from app.schemas.investment_evidence import (
    EvidenceAllocation, EvidenceDecision, EvidenceLegInput, EvidenceObservationInput,
    EvidenceOpeningBoundary,
)
from app.services import investment_evidence_service as service
from app.services.connection_service import _sync_holdings, _sync_trades


@pytest_asyncio.fixture
async def wallet(session, test_workspace, test_user):
    group = AssetGroup(workspace_id=test_workspace.id, user_id=test_user.id, name="Synthetic wallet")
    session.add(group)
    await session.commit()
    return group


def observation(ref="csv-row-Q", *, source="csv", quantity="12", price="7", execution="execution-Q", **kwargs):
    return EvidenceObservationInput(
        reference=ref, source=source, provider="coinbase", source_account_id="currency-wallet-A",
        source_local_id=ref, source_locator=f"synthetic/{ref}",
        event_date=date(2025, 2, 3), time_precision="second",
        event_time_raw="2025-02-03T10:11:12+00:00",
        event_at=datetime(2025, 2, 3, 10, 11, 12, tzinfo=timezone.utc),
        settlement_status="settled", provider_status="completed",
        account_external_id="portfolio-A", holding_external_id="currency-wallet-A",
        legs=[EvidenceLegInput(
            key="amount", asset_symbol="SYN", provider_asset_id="currency-SYN",
            classification="buy", direction="in", quantity=quantity, unit_price=price,
            subtotal=str(Decimal(quantity) * Decimal(price)), total=str(Decimal(quantity) * Decimal(price)),
            fee="0", fee_currency="USD", valuation_currency="USD", execution_id=execution,
            execution_currency="USD", unit_price_origin="reported",
        )], **kwargs,
    )


async def save(session, workspace, user, wallet, observations):
    preview = await service.preview_evidence(session, workspace.id, wallet.id, observations)
    return await service.import_evidence(
        session, workspace.id, user.id, wallet.id, observations,
        expected_revision=preview.revision,
    )


async def apply(session, workspace, user, wallet, preview, reference=None):
    ref = reference or preview.observations[0].reference
    decision = EvidenceDecision(observation_ref=ref, leg_key="amount", action="apply")
    return await service.confirm_evidence(
        session, workspace.id, user.id, wallet.id, [decision], preview.revision, allow_unpriced=True,
    )


@pytest.mark.parametrize("bad", ["bad", "NaN", "Infinity", "-1", 0.1])
def test_invalid_source_and_allocation_amounts_are_validation_errors(bad):
    with pytest.raises(ValidationError):
        EvidenceLegInput(key="amount", quantity=bad)
    with pytest.raises(ValidationError):
        EvidenceAllocation(leg_id=uuid.uuid4(), quantity=bad)


def test_source_totals_and_aggregates_do_not_round_at_decimal_default_precision():
    item = observation()
    leg = item.legs[0]
    value = Decimal("84.00000000000000000000000000001")
    leg.unit_price, leg.subtotal, leg.total, leg.fee = None, value, value, Decimal("0")
    assert service._conflicts(item, leg) == []
    assert service._sum_exact([value, Decimal("0")]) == value
    assert service._product_exact(value, Decimal("1")) == value
    with pytest.raises(ValidationError, match="supported 128-digit"):
        EvidenceLegInput(key="amount", quantity="1e1000")


@pytest.mark.asyncio
async def test_exact_observation_roundtrip_and_preview_is_read_only(session, test_workspace, test_user, wallet):
    item = observation(quantity="0.1234567890123456789012345678")
    preview = await service.preview_evidence(session, test_workspace.id, wallet.id, [item])
    assert list(await session.scalars(select(InvestmentObservation))) == []
    assert list(await session.scalars(select(AssetTransaction))) == []
    result = await save(session, test_workspace, test_user, wallet, [item])
    assert result.retained == 1
    assert result.evidence.observations[0].legs[0].quantity == item.legs[0].quantity
    assert result.evidence.observations[0].source_local_id == "csv-row-Q"
    assert preview.target.workspace_id == test_workspace.id
    replay = await save(session, test_workspace, test_user, wallet, [item])
    assert replay.retained == 0


@pytest.mark.asyncio
async def test_apply_replay_link_and_unlink_never_duplicate_economics(session, test_workspace, test_user, wallet):
    first = await save(session, test_workspace, test_user, wallet, [observation()])
    applied = await apply(session, test_workspace, test_user, wallet, first.evidence)
    assert applied.imported == 1
    original = (await session.scalars(select(AssetTransaction))).one()
    assert original.quantity == Decimal("12") and original.price == Decimal("7")
    second = await save(session, test_workspace, test_user, wallet, [observation("api-trade-A", source="coinbase_api", execution="execution-A")])
    candidate = next(r for r in second.evidence.records if r.application_status != "already_applied")
    assert candidate.match_status == "candidate" and candidate.effects.ledger_rows == 0
    decision = EvidenceDecision(
        observation_ref=candidate.observation_ref, leg_key="amount", action="link",
        allocations=[EvidenceAllocation(leg_id=candidate.candidate_legs[0].leg_id, quantity="12")],
        reason="Synthetic source order documents the same purchase",
    )
    linked = await service.confirm_evidence(session, test_workspace.id, test_user.id, wallet.id, [decision], second.evidence.revision)
    assert linked.imported == 0 and linked.linked == 1
    replay = await service.confirm_evidence(session, test_workspace.id, test_user.id, wallet.id, [decision], second.evidence.revision)
    assert replay.imported == 0
    record = next(r for r in linked.evidence.records if r.observation_ref == candidate.observation_ref)
    reversed_preview = await service.reverse_link(session, test_workspace.id, record.link_ids[0], linked.evidence.revision)
    reversed_record = next(r for r in reversed_preview.records if r.observation_ref == candidate.observation_ref)
    assert reversed_record.application_status == "already_applied"
    assert list(await session.scalars(select(AssetTransaction))) == [original]
    assert len(list(await session.scalars(select(InvestmentObservation)))) == 2


@pytest.mark.asyncio
async def test_distinct_verified_executions_survive_identical_fingerprints(session, test_workspace, test_user, wallet):
    result = await save(session, test_workspace, test_user, wallet, [
        observation("row-A", execution="execution-A"), observation("row-B", execution="execution-B"),
    ])
    assert all(r.application_status == "eligible" for r in result.evidence.records)
    first = await apply(session, test_workspace, test_user, wallet, result.evidence, result.evidence.observations[0].reference)
    await apply(session, test_workspace, test_user, wallet, first.evidence, first.evidence.observations[1].reference)
    assert len(list(await session.scalars(select(AssetTransaction)))) == 2


@pytest.mark.asyncio
async def test_missing_identity_and_mismatched_money_stay_reviewable(session, test_workspace, test_user, wallet):
    first = observation("anonymous-A", execution=None)
    second = observation("anonymous-B", execution=None)
    first.source_local_id = second.source_local_id = None
    result = await save(session, test_workspace, test_user, wallet, [first, second])
    assert all(r.match_status == "candidate" for r in result.evidence.records)
    mismatch = observation("bad-total", quantity="10", price="8.2")
    mismatch.legs[0].total, mismatch.legs[0].fee = Decimal("84"), Decimal("3")
    preview = await service.preview_evidence(session, test_workspace.id, wallet.id, [mismatch])
    record = next(r for r in preview.records if r.observation_ref == "bad-total")
    assert "total" in record.conflicting_fields
    assert record.application_status == "blocked"
    assert list(await session.scalars(select(AssetTransaction))) == []


@pytest.mark.asyncio
async def test_group_and_asset_scope_and_stale_review_fail_closed(session, test_workspace, test_user, wallet):
    with pytest.raises(HTTPException) as outside:
        await service.preview_evidence(session, uuid.uuid4(), wallet.id, [observation()])
    assert outside.value.status_code == 404
    foreign = observation()
    foreign.legs[0].asset_id = uuid.uuid4()
    with pytest.raises(HTTPException) as outside_asset:
        await service.preview_evidence(session, test_workspace.id, wallet.id, [foreign])
    assert outside_asset.value.status_code == 404
    first = await save(session, test_workspace, test_user, wallet, [observation()])
    await save(session, test_workspace, test_user, wallet, [observation("independent", quantity="3", execution="distinct")])
    with pytest.raises(HTTPException) as stale:
        await apply(session, test_workspace, test_user, wallet, first.evidence)
    assert stale.value.status_code == 409
    assert list(await session.scalars(select(AssetTransaction))) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("csv_first", [False, True])
async def test_full_holdings_and_api_sync_is_safe_in_either_order(session, test_workspace, test_user, wallet, monkeypatch, csv_first):
    connection = BankConnection(
        workspace_id=test_workspace.id, user_id=test_user.id, provider="coinbase",
        external_id="connection-A", institution_name="Synthetic Exchange", credentials={"synthetic": True},
    )
    session.add(connection)
    await session.flush()
    wallet.connection_id, wallet.source, wallet.external_id = connection.id, "coinbase", "connection-A::portfolio-A"
    await session.commit()
    api = observation("api-trade-A", source="coinbase_api", execution="api-trade-A")
    holding = HoldingData(
        external_id="currency-wallet-A", name="Synthetic", currency="USD", ticker="SYN",
        quantity=Decimal("12"), current_value=Decimal("84"), account_external_id="portfolio-A",
        metadata={"provider_asset_id": "currency-SYN"},
    )
    trade = TradeData(
        external_id="api-trade-A", holding_external_id="currency-wallet-A", kind="buy",
        quantity=Decimal("12"), price=Decimal("7"), occurred_at=api.event_at,
    )
    provider = SimpleNamespace(
        get_holdings=AsyncMock(return_value=[holding]),
        get_investment_activity=AsyncMock(return_value=InvestmentActivity(trades=[trade], observations=[api])),
    )
    monkeypatch.setattr("app.services.connection_service.get_provider", lambda _: provider)
    if csv_first:
        saved = await save(session, test_workspace, test_user, wallet, [observation()])
        await apply(session, test_workspace, test_user, wallet, saved.evidence)
    await _sync_holdings(session, test_user.id, connection, {}, {"portfolio-A"})
    await _sync_trades(session, connection, {}, {"portfolio-A"})
    await session.commit()
    if not csv_first:
        await save(session, test_workspace, test_user, wallet, [observation()])
    assert len(list(await session.scalars(select(Asset)))) == 1
    assert len(list(await session.scalars(select(AssetTransaction)))) == 1
    preview = await service.preview_evidence(session, test_workspace.id, wallet.id)
    pending = next(r for r in preview.records if r.application_status != "already_applied")
    assert pending.match_status == "candidate"
    decision = EvidenceDecision(
        observation_ref=pending.observation_ref, leg_key="amount", action="link",
        allocations=[EvidenceAllocation(leg_id=pending.candidate_legs[0].leg_id, quantity="12")],
        reason="Reviewed documented synthetic purchase",
    )
    await service.confirm_evidence(session, test_workspace.id, test_user.id, wallet.id, [decision], preview.revision)
    await _sync_holdings(session, test_user.id, connection, {}, {"portfolio-A"})
    await _sync_trades(session, connection, {}, {"portfolio-A"})
    await session.commit()
    assert len(list(await session.scalars(select(AssetTransaction)))) == 1
    assert (await session.scalars(select(Asset))).one().units == Decimal("12")


@pytest.mark.asyncio
async def test_shared_order_groups_two_conversion_legs_without_dropping_either(session, test_workspace, test_user, wallet):
    incoming = observation("conversion-in", source="coinbase_api", order_ref="order-synthetic")
    outgoing = observation("conversion-out", source="coinbase_api", order_ref="order-synthetic")
    outgoing.legs[0].asset_symbol = "SECOND"
    outgoing.legs[0].provider_asset_id = "currency-SECOND"
    outgoing.legs[0].direction, outgoing.legs[0].classification = "out", "sell"
    result = await save(session, test_workspace, test_user, wallet, [incoming, outgoing])
    assert result.retained == 2
    assert len(list(await session.scalars(select(InvestmentEvent)))) == 1
    assert len(list(await session.scalars(select(InvestmentLeg)))) == 2


@pytest.mark.asyncio
async def test_status_progression_retains_both_observations_and_can_apply_settled_version(session, test_workspace, test_user, wallet):
    pending = observation("status-version", source="coinbase_api")
    pending.provider_status, pending.network_status, pending.settlement_status = "completed", "unconfirmed", "pending"
    await save(session, test_workspace, test_user, wallet, [pending])
    settled = observation("status-version", source="coinbase_api")
    settled.network_status = "confirmed"
    result = await save(session, test_workspace, test_user, wallet, [settled])
    eligible = [record for record in result.evidence.records if record.application_status == "eligible"]
    assert len(eligible) == 1
    await apply(session, test_workspace, test_user, wallet, result.evidence, eligible[0].observation_ref)
    assert len(list(await session.scalars(select(InvestmentObservation)))) == 2
    assert len(list(await session.scalars(select(AssetTransaction)))) == 1


@pytest.mark.asyncio
async def test_unknown_fee_is_not_zero_and_total_basis_is_not_charged_twice(session, test_workspace, test_user, wallet):
    item = observation()
    item.legs[0].fee = None
    preview = await service.preview_evidence(session, test_workspace.id, wallet.id, [item])
    assert preview.records[0].application_status == "blocked"
    assert "fee_assumption_required" in preview.records[0].reason_codes
    item.legs[0].acquisition_basis = Decimal("84")
    result = await save(session, test_workspace, test_user, wallet, [item])
    assert result.evidence.records[0].effects.basis_delta == Decimal("84")
    applied = await apply(session, test_workspace, test_user, wallet, result.evidence)
    assert applied.imported == 1
    tx = (await session.scalars(select(AssetTransaction))).one()
    assert tx.price * tx.quantity + tx.fee == Decimal("84")
    assert applied.evidence.observations[0].legs[0].fee is None


@pytest.mark.asyncio
async def test_undo_removes_only_owned_application_and_retains_source_replay_marker(session, test_workspace, test_user, wallet):
    from app.models.import_log import ImportLog
    result = await save(session, test_workspace, test_user, wallet, [observation()])
    applied = await apply(session, test_workspace, test_user, wallet, result.evidence)
    log = await session.get(ImportLog, applied.import_log_id)
    await service.undo_evidence_import(session, test_workspace.id, log)
    assert list(await session.scalars(select(AssetTransaction))) == []
    assert len(list(await session.scalars(select(InvestmentObservation)))) == 1
    replay = await save(session, test_workspace, test_user, wallet, [observation()])
    assert replay.evidence.records[0].application_status == "blocked"
    assert "application_reversed" in replay.evidence.records[0].reason_codes


@pytest.mark.asyncio
async def test_reviewed_opening_boundary_allows_later_distinct_acquisitions(session, test_workspace, test_user, wallet):
    opening = observation("opening-lot", source_kind="remaining_lots")
    opening.legs[0].acquisition_basis = Decimal("84")
    boundary = EvidenceOpeningBoundary(as_of=date(2025, 2, 4), overlap_reviewed=True, assumption="Synthetic opening inventory")
    preview = await service.preview_evidence(session, test_workspace.id, wallet.id, [opening], opening_boundary=boundary)
    saved = await service.import_evidence(
        session, test_workspace.id, test_user.id, wallet.id, [opening],
        expected_revision=preview.revision, opening_boundary=boundary,
    )
    applied = await service.confirm_evidence(session, test_workspace.id, test_user.id, wallet.id, [
        EvidenceDecision(observation_ref=saved.evidence.observations[0].reference, leg_key="amount", action="apply"),
    ], saved.evidence.revision, opening_boundary=boundary, allow_unpriced=True)
    assert applied.imported == 1
    later = observation("later-acquisition", quantity="3", execution="later-execution")
    later.event_date, later.event_at = date(2025, 3, 3), datetime(2025, 3, 3, tzinfo=timezone.utc)
    later.event_time_raw = later.event_at.isoformat()
    preview = await service.preview_evidence(session, test_workspace.id, wallet.id, [later])
    record = next(r for r in preview.records if r.observation_ref == "later-acquisition")
    assert record.application_status == "eligible"


@pytest.mark.asyncio
async def test_unlink_uses_the_same_opening_boundary_revision_as_get(session, test_workspace, test_user, wallet, client, auth_headers):
    result = await save(session, test_workspace, test_user, wallet, [observation()])
    await apply(session, test_workspace, test_user, wallet, result.evidence)
    result = await save(session, test_workspace, test_user, wallet, [observation("api-A", source="coinbase_api")])
    record = next(r for r in result.evidence.records if r.application_status != "already_applied")
    linked = await service.confirm_evidence(session, test_workspace.id, test_user.id, wallet.id, [
        EvidenceDecision(
            observation_ref=record.observation_ref, leg_key="amount", action="link",
            allocations=[EvidenceAllocation(leg_id=record.candidate_legs[0].leg_id, quantity="12")],
            reason="Synthetic documented purchase",
        ),
    ], result.evidence.revision)
    link_id = next(r for r in linked.evidence.records if r.observation_ref == record.observation_ref).link_ids[0]
    headers = {**auth_headers, "X-Workspace-Id": str(test_workspace.id)}
    boundary = {"opening_as_of": "2026-01-01", "opening_assumption": "Synthetic opening boundary", "overlap_reviewed": "true"}
    preview = await client.get("/api/assets/evidence", headers=headers, params={"group_id": str(wallet.id), **boundary})
    assert preview.status_code == 200
    response = await client.delete(f"/api/assets/evidence/links/{link_id}", headers=headers, params={
        "expected_revision": preview.json()["revision"], **boundary,
    })
    assert response.status_code == 200, response.text
    assert len(list(await session.scalars(select(AssetTransaction)))) == 1
