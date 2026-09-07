"""Synthetic clock/version regressions for independently applicable source revisions."""

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select

from app.models.asset import Asset
from app.models.asset_transaction import AssetTransaction
from app.models.bank_connection import BankConnection
from app.models.investment_evidence import InvestmentObservation
from app.providers.base import HoldingData, InvestmentActivity, TradeData, INCOME_AT_RECEIPT_NOTE
from app.schemas.investment_evidence import EvidenceDecision
from app.services import investment_evidence_service as service
from app.services.connection_service import _sync_holdings, _sync_trades
from tests.test_investment_evidence import observation, save, apply, wallet as wallet  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.fixture
def observation_ids():
    ids = {}

    def assign_id(mapper, connection, row):
        if row.payload["reference"] in ids:
            row.id = ids[row.payload["reference"]]

    event.listen(InvestmentObservation, "before_insert", assign_id)
    yield ids
    event.remove(InvestmentObservation, "before_insert", assign_id)


@pytest.mark.parametrize("gap", ["timezone", "date", "value", "fee"])
@pytest.mark.parametrize("persist", [False, True])
@pytest.mark.parametrize("reverse_order", [False, True])
@pytest.mark.parametrize("complete_sorts_first", [False, True])
async def test_clock_enrichment_selects_independently_eligible_revision(
    session,
    test_workspace,
    test_user,
    wallet,
    observation_ids,
    gap,
    persist,
    reverse_order,
    complete_sorts_first,
):
    complete = observation("a-complete" if complete_sorts_first else "z-complete")
    incomplete = complete.model_copy(deep=True)
    incomplete.reference = "z-incomplete" if complete_sorts_first else "a-incomplete"
    incomplete.source_local_id = complete.source_local_id = "same-scoped-execution"
    if gap == "timezone":
        incomplete.event_at = None
        incomplete.reason_codes = ["unresolved_timezone"]
        complete.timezone = "UTC"
    elif gap == "date":
        incomplete.event_date = incomplete.event_at = None
    elif gap == "value":
        incomplete.legs[0].unit_price = incomplete.legs[0].subtotal = incomplete.legs[0].total = (
            None
        )
    else:
        incomplete.legs[0].fee = None
    batch = [incomplete, complete]
    if reverse_order:
        batch.reverse()
    observation_ids.update(
        {
            item.reference: uuid.UUID(
                "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaa1"
                if item.reference.startswith("a")
                else "ffffffff-ffff-4fff-afff-fffffffffff2"
            )
            for item in batch
        }
    )
    preview = await service.preview_evidence(session, test_workspace.id, wallet.id, batch)
    reference = complete.reference
    assert [r.observation_ref for r in preview.records if r.application_status == "eligible"] == [
        reference
    ]
    if persist:
        saved = await save(session, test_workspace, test_user, wallet, batch)
        preview = saved.evidence
        reference = next(
            o.reference for o in preview.observations if o.source_reference == complete.reference
        )
        assert [
            r.observation_ref for r in preview.records if r.application_status == "eligible"
        ] == [reference]
        result = await apply(session, test_workspace, test_user, wallet, preview, reference)
    else:
        result = await service.import_evidence(
            session,
            test_workspace.id,
            test_user.id,
            wallet.id,
            batch,
            decisions=[
                EvidenceDecision(observation_ref=reference, leg_key="amount", action="apply")
            ],
            expected_revision=preview.revision,
            allow_unpriced=True,
        )
    assert result.imported == 1
    assert len(list(await session.scalars(select(AssetTransaction)))) == 1
    assert (await session.scalars(select(Asset))).one().units == 12


@pytest.mark.parametrize("reverse_order", [False, True])
@pytest.mark.parametrize("kind", ["date_only_alias", "unknown_fee_reward"])
async def test_sync_clock_enrichment_uses_actual_writer_readiness(
    session, test_workspace, test_user, wallet, monkeypatch, observation_ids, reverse_order, kind
):
    connection = BankConnection(
        workspace_id=test_workspace.id,
        user_id=test_user.id,
        provider="coinbase",
        external_id="clock-connection",
        institution_name="Synthetic Exchange",
        credentials={},
    )
    session.add(connection)
    await session.flush()
    wallet.connection_id, wallet.source, wallet.external_id = (
        connection.id,
        "coinbase",
        "clock-connection::portfolio-A",
    )
    await session.commit()
    old = observation("z-old-clock", source="coinbase_api", execution="same-api-execution")
    old.source_local_id = "same-api-execution"
    old.event_at = None
    new = old.model_copy(deep=True)
    new.reference, new.event_at = "a-complete-clock", observation().event_at
    new.legs[0].fee = None
    if kind == "unknown_fee_reward":
        old.legs[0].fee = None
        for item in (old, new):
            item.legs[0].classification = "income"
            item.legs[0].unit_price_origin = "derived_spot"
    batch = [new, old] if reverse_order else [old, new]
    observation_ids.update(
        {
            item.reference: uuid.UUID(
                "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaa1"
                if item is new
                else "ffffffff-ffff-4fff-afff-fffffffffff2"
            )
            for item in batch
        }
    )
    holding = HoldingData(
        external_id="currency-wallet-A",
        name="Synthetic",
        currency="USD",
        ticker="SYN",
        quantity=Decimal("12"),
        current_value=Decimal("84"),
        account_external_id="portfolio-A",
        metadata={"provider_asset_id": "currency-SYN"},
    )
    trade = TradeData(
        external_id="same-api-execution",
        holding_external_id="currency-wallet-A",
        kind="buy",
        quantity=Decimal("12"),
        price=Decimal("7"),
        occurred_at=new.event_at,
        notes=INCOME_AT_RECEIPT_NOTE if kind == "unknown_fee_reward" else None,
    )
    provider = SimpleNamespace(
        get_holdings=AsyncMock(return_value=[holding]),
        get_investment_activity=AsyncMock(
            return_value=InvestmentActivity(trades=[trade], observations=batch)
        ),
    )
    monkeypatch.setattr("app.services.connection_service.get_provider", lambda _: provider)
    await _sync_holdings(session, test_user.id, connection, {}, {"portfolio-A"})
    saved = await save(session, test_workspace, test_user, wallet, batch)
    if kind == "unknown_fee_reward":
        assert all(r.application_status != "eligible" for r in saved.evidence.records)
    await _sync_trades(session, connection, {}, {"portfolio-A"})
    await session.commit()
    await _sync_trades(session, connection, {}, {"portfolio-A"})
    await session.commit()
    rows = list(await session.scalars(select(AssetTransaction)))
    assert len(rows) == 1 and rows[0].quantity == 12 and rows[0].price == 7
    if kind == "unknown_fee_reward":
        from app.services.investment_income import collect_payouts

        payouts = await collect_payouts(
            session, test_workspace.id, since=date(2025, 1, 1), until=date(2026, 1, 1)
        )
        assert len(payouts) == 1 and payouts[0].amount == 84


@pytest.mark.parametrize("persist", [False, True])
@pytest.mark.parametrize("classification", ["transfer", "income"])
async def test_compatible_nonmanual_movements_count_once(
    session, test_workspace, test_user, wallet, persist, classification
):
    first = observation("source-movement")
    first.legs[0].classification, first.legs[0].fee = classification, None
    later = first.model_copy(deep=True)
    later.reference, later.network_status = "later-movement", "confirmed"
    batch = [first, later]
    preview = (
        (await save(session, test_workspace, test_user, wallet, batch)).evidence
        if persist
        else await service.preview_evidence(session, test_workspace.id, wallet.id, batch)
    )
    assert all(record.application_status != "eligible" for record in preview.records)
    assert preview.reconciliation[0].settled_movement_quantity == 12
    assert not list(await session.scalars(select(AssetTransaction)))


async def test_saved_source_family_keeps_its_original_connection_namespace(
    session, test_workspace, test_user, wallet
):
    original = observation("same-source")
    await save(session, test_workspace, test_user, wallet, [original])
    connection = BankConnection(
        workspace_id=test_workspace.id,
        user_id=test_user.id,
        provider="coinbase",
        external_id="new-connection",
        institution_name="Synthetic Exchange",
        credentials={},
    )
    session.add(connection)
    await session.flush()
    wallet.connection_id = connection.id
    await session.commit()
    saved = await save(session, test_workspace, test_user, wallet, [original])
    assert len(saved.evidence.observations) == 2
    assert all(record.application_status == "eligible" for record in saved.evidence.records)
    assert saved.evidence.reconciliation[0].settled_movement_quantity == 24


async def test_incompatible_source_family_movements_remain_unresolved(
    session, test_workspace, test_user, wallet
):
    unknown = observation("movement-source")
    unknown.legs[0].classification = "transfer"
    unknown.legs[0].quantity = None
    unknown.legs[0].subtotal = unknown.legs[0].total = None
    first = unknown.model_copy(deep=True)
    first.reference, first.legs[0].quantity = "first-value", Decimal("12")
    other = unknown.model_copy(deep=True)
    other.reference, other.legs[0].quantity = "other-value", Decimal("13")
    preview = await service.preview_evidence(
        session, test_workspace.id, wallet.id, [unknown, first, other]
    )
    assert all("source_version" in record.conflicting_fields for record in preview.records)
    assert preview.reconciliation[0].settled_movement_quantity == 0
    assert "unresolved_movements" in preview.reconciliation[0].missing_coverage


@pytest.mark.parametrize("reverse_order", [False, True])
async def test_compatible_transfer_identity_enrichment_and_sparse_link_target_share_scope(
    session, test_workspace, test_user, wallet, reverse_order
):
    from app.schemas.investment_evidence import EvidenceAllocation

    sparse = observation("sparse-execution")
    sparse.legs[0].classification = "transfer"
    sparse.legs[0].provider_asset_id = None
    complete = sparse.model_copy(deep=True)
    complete.reference = "complete-execution"
    complete.legs[0].provider_asset_id = "currency-SYN"
    batch = [complete, sparse] if reverse_order else [sparse, complete]
    preview = await service.preview_evidence(session, test_workspace.id, wallet.id, batch)
    assert len(preview.reconciliation) == 1
    assert preview.reconciliation[0].provider_asset_id == "currency-SYN"
    assert preview.reconciliation[0].settled_movement_quantity == 12
    assert sparse.legs[0].provider_asset_id is None
    saved = await save(session, test_workspace, test_user, wallet, batch)
    support = sparse.model_copy(deep=True)
    support.reference, support.source_local_id, support.source = (
        "support-row",
        "support-row",
        "other_csv",
    )
    saved = await save(session, test_workspace, test_user, wallet, [support])
    source = next(o for o in saved.evidence.observations if o.source_local_id == "support-row")
    record = next(r for r in saved.evidence.records if r.observation_ref == source.reference)
    sparse_ref = next(
        o.reference for o in saved.evidence.observations if o.source_reference == sparse.reference
    )
    target = next(
        candidate
        for candidate in record.candidate_legs
        if candidate.source_refs[0].observation_ref == sparse_ref
    )
    linked = await service.confirm_evidence(
        session,
        test_workspace.id,
        test_user.id,
        wallet.id,
        [
            EvidenceDecision(
                observation_ref=source.reference,
                leg_key="amount",
                action="link",
                allocations=[EvidenceAllocation(leg_id=target.leg_id, quantity="12")],
                reason="Synthetic source corroborates the same execution",
            )
        ],
        saved.evidence.revision,
    )
    assert len(linked.evidence.reconciliation) == 1
    assert linked.evidence.reconciliation[0].settled_movement_quantity == 12
    assert linked.evidence.reconciliation[0].provider_asset_id == "currency-SYN"
    assert (
        next(o for o in linked.evidence.observations if o.reference == sparse_ref)
        .legs[0]
        .provider_asset_id
        is None
    )


@pytest.mark.parametrize("support", ["active", "reversed", "none", "conflicting", "other_leg"])
async def test_reviewed_old_alias_support_follows_actual_writer_and_http_undo(
    session, test_workspace, test_user, wallet, client, auth_headers, support
):
    from app.models.investment_evidence import InvestmentLeg, InvestmentObservationLink
    from app.schemas.investment_evidence import EvidenceAllocation

    target = observation("api-target", source="coinbase_api", execution="api-execution")
    target.legs[0].fee, target.legs[0].total = None, Decimal("87")
    if support == "other_leg":
        target.legs.append(target.legs[0].model_copy(update={"key": "independent-leg"}))
    items = [target]
    if support != "none":
        corroborator = observation("csv-support", execution="csv-execution")
        corroborator.legs[0].fee, corroborator.legs[0].total = Decimal("3"), Decimal("87")
        items.append(corroborator)
    saved = await save(session, test_workspace, test_user, wallet, items)
    source_ref = link_id = target_id = None
    if support != "none":
        source_ref = next(
            o.reference for o in saved.evidence.observations if o.source_local_id == "csv-support"
        )
        target_ref = next(
            o.reference for o in saved.evidence.observations if o.source == "coinbase_api"
        )
        target_key = "independent-leg" if support == "other_leg" else "amount"
        target_id = await session.scalar(
            select(InvestmentLeg.id).where(
                InvestmentLeg.observation_id == uuid.UUID(target_ref),
                InvestmentLeg.source_leg_key == target_key,
            )
        )
        linked = await service.confirm_evidence(
            session,
            test_workspace.id,
            test_user.id,
            wallet.id,
            [
                EvidenceDecision(
                    observation_ref=source_ref,
                    leg_key="amount",
                    action="link",
                    allocations=[EvidenceAllocation(leg_id=target_id, quantity="12")],
                    reason="Reviewed independent support for this synthetic execution leg",
                )
            ],
            saved.evidence.revision,
        )
        link_id = next(
            r for r in linked.evidence.records if r.observation_ref == source_ref
        ).link_ids[0]
    enriched = target.model_copy(deep=True)
    enriched.reference, enriched.legs[0].fee = "api-fee-supplied", Decimal("3")
    saved = await save(session, test_workspace, test_user, wallet, [enriched])
    writer = next(
        o.reference for o in saved.evidence.observations if o.source_reference == enriched.reference
    )
    applied = await apply(session, test_workspace, test_user, wallet, saved.evidence, writer)
    if source_ref:
        status = next(
            r.application_status
            for r in applied.evidence.records
            if r.observation_ref == source_ref
        )
        assert status == ("blocked" if support == "other_leg" else "already_applied")
    if support == "reversed":
        await service.reverse_link(session, test_workspace.id, link_id, applied.evidence.revision)
    if support == "conflicting":
        conflict = enriched.model_copy(deep=True)
        conflict.reference = "api-conflicting-fee"
        conflict.legs[0].fee, conflict.legs[0].total = Decimal("4"), Decimal("88")
        saved = await save(session, test_workspace, test_user, wallet, [conflict])
        assert any(
            "source_version" in record.conflicting_fields for record in saved.evidence.records
        )
    headers = {**auth_headers, "X-Workspace-Id": str(test_workspace.id)}
    response = await client.delete(f"/api/import-logs/{applied.import_log_id}", headers=headers)
    assert response.status_code == (409 if support == "conflicting" else 204), response.text
    rows = list(await session.scalars(select(AssetTransaction)))
    if support in {"active", "conflicting"}:
        assert len(rows) == 1 and rows[0].quantity == 12 and rows[0].fee == 3
        assert rows[0].import_id == (applied.import_log_id if support == "conflicting" else None)
    else:
        assert not rows
        preview = await service.preview_evidence(session, test_workspace.id, wallet.id)
        assert "application_reversed" in next(
            r.reason_codes
            for r in preview.records
            if r.observation_ref == writer and r.leg_key == "amount"
        )
    if link_id:
        link = await session.get(InvestmentObservationLink, link_id)
        assert link.leg_id == target_id
        assert (link.reversed_at is not None) == (support == "reversed")
    preview = await service.preview_evidence(session, test_workspace.id, wallet.id)
    assert (
        next(o for o in preview.observations if o.source_reference == target.reference).legs[0].fee
        is None
    )
