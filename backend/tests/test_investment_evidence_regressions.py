"""Synthetic regressions for canonical application and financial semantics."""

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from tests.test_investment_evidence import observation, save, apply, wallet as wallet  # noqa: F401
from app.schemas.investment_evidence import (
    EvidenceDecision,
    EvidenceAllocation,
    EvidenceOpeningBoundary,
)
from app.models.asset import Asset
from app.models.asset_transaction import AssetTransaction
from app.models.import_log import ImportLog
from app.services import investment_evidence_service as service

pytestmark = pytest.mark.asyncio


async def attempt_apply(session, workspace, user, wallet, reference):
    """An explicitly refused corroborating/version write is a safe outcome."""
    preview = await service.preview_evidence(session, workspace.id, wallet.id)
    try:
        return await apply(session, workspace, user, wallet, preview, reference)
    except HTTPException as exc:
        assert exc.status_code in (409, 422), exc.detail
        # A refused import rolls back and expires ORM fixture instances.
        for instance in (workspace, user, wallet):
            await session.refresh(instance)
        return None


@pytest.mark.parametrize("source_first", [True, False])
@pytest.mark.parametrize("unlink_after", [True, False])
async def test_link_before_either_side_applied(
    session, test_workspace, test_user, wallet, source_first, unlink_after
):
    saved = await save(
        session,
        test_workspace,
        test_user,
        wallet,
        [observation("csv-a"), observation("api-a", source="coinbase_api")],
    )
    source = next(o for o in saved.evidence.observations if o.source == "csv")
    target = next(o for o in saved.evidence.observations if o.source == "coinbase_api")
    record = next(r for r in saved.evidence.records if r.observation_ref == source.reference)
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
                reason="Same documented purchase",
                allocations=[
                    EvidenceAllocation(leg_id=record.candidate_legs[0].leg_id, quantity="12")
                ],
            )
        ],
        saved.evidence.revision,
    )
    link_id = next(
        r for r in linked.evidence.records if r.observation_ref == source.reference
    ).link_ids[0]
    order = (
        [source.reference, target.reference]
        if source_first
        else [target.reference, source.reference]
    )
    for ref in order:
        await attempt_apply(session, test_workspace, test_user, wallet, ref)
    if unlink_after:
        preview = await service.preview_evidence(session, test_workspace.id, wallet.id)
        await service.reverse_link(session, test_workspace.id, link_id, preview.revision)
        for ref in order:
            await attempt_apply(session, test_workspace, test_user, wallet, ref)
    rows = list(await session.scalars(select(AssetTransaction)))
    assert len(rows) == 1, (
        "Same-purchase evidence must produce one application even when source application is refused"
    )
    assert sum(t.quantity for t in rows) == Decimal("12")
    assert sum(t.quantity * t.price + t.fee for t in rows) == Decimal("84")


@pytest.mark.parametrize("undo_first", [False, True])
async def test_settled_source_enrichment_after_manual_apply(
    session, test_workspace, test_user, wallet, undo_first
):
    first = observation("csv-same")
    saved = await save(session, test_workspace, test_user, wallet, [first])
    applied = await apply(session, test_workspace, test_user, wallet, saved.evidence)
    if undo_first:
        log = await session.get(ImportLog, applied.import_log_id)
        await service.undo_evidence_import(session, test_workspace.id, log)
    second = first.model_copy(deep=True)
    second.network_status = "confirmed"
    enriched = await save(session, test_workspace, test_user, wallet, [second])
    assert len(enriched.evidence.observations) == 2, "Source enrichment must remain accessible"
    for version in enriched.evidence.observations:
        await attempt_apply(session, test_workspace, test_user, wallet, version.reference)
    rows = list(await session.scalars(select(AssetTransaction)))
    assert len(rows) == (0 if undo_first else 1), (
        "A source revision must share execution application and undo identity"
    )
    assert sum((t.quantity for t in rows), Decimal("0")) == (
        Decimal("0") if undo_first else Decimal("12")
    )


async def test_sell_basis_does_not_erase_execution_fee(session, test_workspace, test_user, wallet):
    saved = await save(session, test_workspace, test_user, wallet, [observation("buy-before-sale")])
    await apply(session, test_workspace, test_user, wallet, saved.evidence)
    sell = observation("sale", price="10", execution="sale-execution")
    leg = sell.legs[0]
    leg.classification, leg.direction = "sell", "out"
    leg.subtotal, leg.total, leg.fee, leg.acquisition_basis = (
        Decimal("120"),
        Decimal("117"),
        Decimal("3"),
        Decimal("84"),
    )
    saved = await save(session, test_workspace, test_user, wallet, [sell])
    source = next(o for o in saved.evidence.observations if o.source_local_id == "sale")
    await apply(session, test_workspace, test_user, wallet, saved.evidence, source.reference)
    tx = (
        await session.scalars(select(AssetTransaction).where(AssetTransaction.kind == "sell"))
    ).one()
    asset = (await session.scalars(select(Asset))).one()
    assert tx.fee == Decimal("3"), "Historical acquisition basis does not contain a later sale fee"
    assert tx.quantity * tx.price - tx.fee == Decimal("117")
    assert asset.realized_gain == Decimal("33")


async def test_sell_basis_does_not_establish_unknown_sale_fee(
    session, test_workspace, test_user, wallet
):
    saved = await save(session, test_workspace, test_user, wallet, [observation("buy-before-sale")])
    await apply(session, test_workspace, test_user, wallet, saved.evidence)
    sell = observation("sale", price="10", execution="sale-execution")
    leg = sell.legs[0]
    leg.classification, leg.direction = "sell", "out"
    leg.subtotal, leg.total, leg.fee, leg.acquisition_basis = (
        Decimal("120"),
        None,
        None,
        Decimal("84"),
    )
    saved = await save(session, test_workspace, test_user, wallet, [sell])
    source = next(o for o in saved.evidence.observations if o.source_local_id == "sale")
    await attempt_apply(session, test_workspace, test_user, wallet, source.reference)
    assert (
        list(await session.scalars(select(AssetTransaction).where(AssetTransaction.kind == "sell")))
        == []
    ), "Unknown sale fee must not become zero merely because original basis is known"


async def test_linked_lot_assertions_not_added_twice_to_opening(
    session, test_workspace, test_user, wallet
):
    first = observation("remaining", source_kind="remaining_lots")
    first.legs[0].acquisition_basis = Decimal("84")
    boundary = EvidenceOpeningBoundary(
        as_of=date(2025, 2, 4),
        overlap_reviewed=True,
        assumption="Documented twelve-unit opening inventory",
    )
    preview = await service.preview_evidence(
        session, test_workspace.id, wallet.id, [first], opening_boundary=boundary
    )
    saved = await service.import_evidence(
        session,
        test_workspace.id,
        test_user.id,
        wallet.id,
        [first],
        expected_revision=preview.revision,
        opening_boundary=boundary,
    )
    await service.confirm_evidence(
        session,
        test_workspace.id,
        test_user.id,
        wallet.id,
        [
            EvidenceDecision(
                observation_ref=saved.evidence.observations[0].reference,
                leg_key="amount",
                action="apply",
            )
        ],
        saved.evidence.revision,
        opening_boundary=boundary,
        allow_unpriced=True,
    )
    second = observation("tax-corrob", source_kind="tax_workpaper")
    second.legs[0].acquisition_basis = Decimal("84")
    saved = await save(session, test_workspace, test_user, wallet, [second])
    source = next(o for o in saved.evidence.observations if o.source_local_id == "tax-corrob")
    record = next(r for r in saved.evidence.records if r.observation_ref == source.reference)
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
                reason="Tax workpaper corroborates existing opening inventory",
                allocations=[
                    EvidenceAllocation(leg_id=record.candidate_legs[0].leg_id, quantity="12")
                ],
            )
        ],
        saved.evidence.revision,
    )
    assert linked.evidence.reconciliation[0].opening_quantity == Decimal("12"), (
        "Corroborating opening records must not be added together"
    )
    assert linked.evidence.reconciliation[0].expected_closing_quantity == Decimal("12")
    assert (await session.scalars(select(Asset))).one().units == Decimal("12")
    assert len(list(await session.scalars(select(AssetTransaction)))) == 1


@pytest.mark.parametrize("treatment", ["taxable", "roth", "traditional", "hsa", "other"])
async def test_manually_reviewed_income_reaches_income_report(
    session, test_workspace, test_user, wallet, treatment
):
    from app.providers.base import INCOME_AT_RECEIPT_NOTE
    from app.services.investment_income import collect_payouts, reportable_income

    wallet.tax_treatment = treatment
    await session.commit()
    item = observation("reward")
    item.legs[0].classification = "income"
    saved = await save(session, test_workspace, test_user, wallet, [item])
    await apply(session, test_workspace, test_user, wallet, saved.evidence)
    payouts = await collect_payouts(
        session, test_workspace.id, since=date(2025, 1, 1), until=date(2025, 12, 31)
    )
    tx = (await session.scalars(select(AssetTransaction))).one()
    assert INCOME_AT_RECEIPT_NOTE in tx.notes
    assert len(payouts) == 1 and payouts[0].amount == Decimal("84")
    result = await reportable_income(
        session, test_workspace.id, start=date(2025, 1, 1), end=date(2026, 1, 1)
    )
    assert result["other_ordinary_income"] == (84.0 if treatment == "taxable" else 0.0)
    assert result["non_reportable_income"] == (0.0 if treatment == "taxable" else 84.0)


@pytest.mark.parametrize("reported", ["price", "basis", "both", "subtotal"])
@pytest.mark.parametrize("fee", ["0", "3"])
async def test_option_contract_preview_equals_persisted_cost(
    session, test_workspace, test_user, wallet, reported, fee
):
    item = observation("option", quantity="1", price="2")
    leg = item.legs[0]
    leg.asset_symbol, leg.provider_asset_id = "SYN261218C00100000", None
    leg.fee = Decimal(fee)
    leg.subtotal, leg.total = Decimal("200"), Decimal("200") + leg.fee
    leg.unit_price = Decimal("2") if reported in {"price", "both"} else None
    leg.acquisition_basis = leg.total if reported in {"basis", "both"} else None
    saved = await save(session, test_workspace, test_user, wallet, [item])
    assert saved.evidence.records[0].application_status == "eligible"
    assert saved.evidence.records[0].effects.basis_delta == leg.total
    await apply(session, test_workspace, test_user, wallet, saved.evidence)
    asset = (await session.scalars(select(Asset))).one()
    tx = (await session.scalars(select(AssetTransaction))).one()
    assert asset.type == "option" and asset.purchase_price == leg.total
    assert (tx.quantity * tx.price * 100 + tx.fee).quantize(Decimal("0.01")) == leg.total
    # SQLite NUMERIC round-trips through binary float; compare quoted cents.
    assert tx.price.quantize(Decimal("0.01")) == (
        leg.total / 100 if reported in {"basis", "both"} else Decimal("2")
    )


@pytest.mark.parametrize("source_first", [True, False])
@pytest.mark.parametrize("unlink_first", [True, False])
async def test_multifill_corroboration_never_owns_allocated_inventory(
    session, test_workspace, test_user, wallet, source_first, unlink_first
):
    saved = await save(
        session,
        test_workspace,
        test_user,
        wallet,
        [
            observation("csv-order"),
            observation("api-fill-a", source="coinbase_api", quantity="6", execution="fill-a"),
            observation("api-fill-b", source="coinbase_api", quantity="6", execution="fill-b"),
        ],
    )
    source = next(o for o in saved.evidence.observations if o.source == "csv")
    targets = [o for o in saved.evidence.observations if o.source != "csv"]
    from app.models.investment_evidence import InvestmentLeg

    legs = list(await session.scalars(select(InvestmentLeg)))
    allocations = [
        EvidenceAllocation(leg_id=leg.id, quantity="6")
        for leg in legs
        if str(leg.observation_id) in {o.reference for o in targets}
    ]
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
                allocations=allocations,
                reason="Documented synthetic order with two fills",
            )
        ],
        saved.evidence.revision,
    )
    if unlink_first:
        for link_id in next(
            r for r in linked.evidence.records if r.observation_ref == source.reference
        ).link_ids:
            preview = await service.preview_evidence(session, test_workspace.id, wallet.id)
            await service.reverse_link(session, test_workspace.id, link_id, preview.revision)
    order = [source, *targets] if source_first else [*targets, source]
    for item in order:
        await attempt_apply(session, test_workspace, test_user, wallet, item.reference)
    rows = list(await session.scalars(select(AssetTransaction)))
    assert len(rows) == 2 and sum(tx.quantity for tx in rows) == 12
    assert sum(tx.quantity * tx.price + tx.fee for tx in rows) == 84


async def test_unknown_version_cannot_bridge_conflicting_execution_facts(
    session, test_workspace, test_user, wallet
):
    unknown = observation("same-source")
    unknown.legs[0].unit_price = unknown.legs[0].subtotal = unknown.legs[0].total = None
    left, right = observation("same-source"), observation("same-source", price="8")
    saved = await save(session, test_workspace, test_user, wallet, [unknown])
    saved = await save(session, test_workspace, test_user, wallet, [left])
    saved = await save(session, test_workspace, test_user, wallet, [right])
    assert all("source_version" in r.conflicting_fields for r in saved.evidence.records)
    for source in saved.evidence.observations:
        await attempt_apply(session, test_workspace, test_user, wallet, source.reference)
    assert not list(await session.scalars(select(AssetTransaction)))


async def test_compatible_unapplied_versions_count_once_and_apply_once(
    session, test_workspace, test_user, wallet
):
    first = observation("source-version")
    second = first.model_copy(deep=True)
    second.network_status = "confirmed"
    saved = await save(session, test_workspace, test_user, wallet, [first])
    saved = await save(session, test_workspace, test_user, wallet, [second])
    assert sum(r.application_status == "eligible" for r in saved.evidence.records) == 1
    assert saved.evidence.reconciliation[0].settled_movement_quantity == 12
    for source in saved.evidence.observations:
        await attempt_apply(session, test_workspace, test_user, wallet, source.reference)
    assert len(list(await session.scalars(select(AssetTransaction)))) == 1


async def test_opening_versions_aliases_and_changed_boundary(
    session, test_workspace, test_user, wallet
):
    first = observation("opening-owner", source_kind="remaining_lots")
    boundary = EvidenceOpeningBoundary(
        as_of=date(2025, 2, 4), overlap_reviewed=True, assumption="Synthetic reviewed opening"
    )
    preview = await service.preview_evidence(
        session, test_workspace.id, wallet.id, [first], opening_boundary=boundary
    )
    assert preview.reconciliation[0].opening_quantity is None
    saved = await service.import_evidence(
        session,
        test_workspace.id,
        test_user.id,
        wallet.id,
        [first],
        expected_revision=preview.revision,
        opening_boundary=boundary,
    )
    applied = await service.confirm_evidence(
        session,
        test_workspace.id,
        test_user.id,
        wallet.id,
        [
            EvidenceDecision(
                observation_ref=saved.evidence.observations[0].reference,
                leg_key="amount",
                action="apply",
            )
        ],
        saved.evidence.revision,
        opening_boundary=boundary,
        allow_unpriced=True,
    )
    enriched = first.model_copy(deep=True)
    enriched.network_status = "confirmed"
    await save(session, test_workspace, test_user, wallet, [enriched])
    alias = observation("opening-alias", source_kind="tax_workpaper")
    alias.legs[0].provider_asset_id = None
    saved = await save(session, test_workspace, test_user, wallet, [alias])
    source = next(o for o in saved.evidence.observations if o.source_local_id == "opening-alias")
    rec = next(r for r in saved.evidence.records if r.observation_ref == source.reference)
    owner = next(
        c
        for c in rec.candidate_legs
        if c.source_refs[0].observation_ref == applied.evidence.observations[0].reference
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
                allocations=[EvidenceAllocation(leg_id=owner.leg_id, quantity="12")],
                reason="Same synthetic opening inventory",
            )
        ],
        saved.evidence.revision,
    )
    assert len(linked.evidence.reconciliation) == 1
    assert linked.evidence.reconciliation[0].opening_quantity == 12
    changed = boundary.model_copy(update={"as_of": date(2025, 3, 4)})
    preview = await service.preview_evidence(
        session, test_workspace.id, wallet.id, opening_boundary=changed
    )
    assert preview.reconciliation[0].opening_quantity is None
    assert "opening_balance_unknown" in preview.reconciliation[0].missing_coverage
    await service.undo_evidence_import(
        session, test_workspace.id, await session.get(ImportLog, linked.import_log_id)
    )
    await service.undo_evidence_import(
        session, test_workspace.id, await session.get(ImportLog, applied.import_log_id)
    )
    preview = await service.preview_evidence(session, test_workspace.id, wallet.id)
    assert preview.reconciliation[0].opening_quantity is None


async def test_http_history_owns_apply_separately_and_refreshes_after_undo(
    session, test_workspace, test_user, wallet, client, auth_headers
):
    headers = {**auth_headers, "X-Workspace-Id": str(test_workspace.id)}
    saved = await save(session, test_workspace, test_user, wallet, [observation()])
    applied = await apply(session, test_workspace, test_user, wallet, saved.evidence)
    logs = (await client.get("/api/import-logs", headers=headers)).json()
    assert next(log for log in logs if log["id"] == str(saved.import_log_id))["evidence"] == {
        "observations": 1,
        "applications": 0,
        "links": 0,
    }
    application = next(log for log in logs if log["id"] == str(applied.import_log_id))
    assert application["evidence"] == {"observations": 0, "applications": 1, "links": 0}
    assert application["transaction_count"] == 1 and application["filename"].startswith("Apply:")
    assert (
        await client.delete(f"/api/import-logs/{saved.import_log_id}", headers=headers)
    ).status_code == 204
    assert len(list(await session.scalars(select(AssetTransaction)))) == 1
    assert (
        await client.delete(f"/api/import-logs/{applied.import_log_id}", headers=headers)
    ).status_code == 204
    assert not list(await session.scalars(select(AssetTransaction)))
    logs = (await client.get("/api/import-logs", headers=headers)).json()
    assert (
        next(log for log in logs if log["id"] == str(applied.import_log_id))["evidence"][
            "applications"
        ]
        == 0
    )
    response = await client.get(
        "/api/assets/evidence", headers=headers, params={"group_id": str(wallet.id)}
    )
    assert response.json()["records"][0]["reason_codes"] == ["application_reversed"]


async def test_http_history_refuses_undo_of_inventory_needed_by_sale(
    session, test_workspace, test_user, wallet, client, auth_headers
):
    saved = await save(session, test_workspace, test_user, wallet, [observation()])
    bought = await apply(session, test_workspace, test_user, wallet, saved.evidence)
    sale = observation("sale", execution="sale-execution")
    sale.legs[0].direction, sale.legs[0].classification = "out", "sell"
    saved = await save(session, test_workspace, test_user, wallet, [sale])
    source = next(o for o in saved.evidence.observations if o.source_local_id == "sale")
    await apply(session, test_workspace, test_user, wallet, saved.evidence, source.reference)
    headers = {**auth_headers, "X-Workspace-Id": str(test_workspace.id)}
    response = await client.delete(f"/api/import-logs/{bought.import_log_id}", headers=headers)
    assert response.status_code in (409, 422), response.text
    assert len(list(await session.scalars(select(AssetTransaction)))) == 2
    logs = (await client.get("/api/import-logs", headers=headers)).json()
    assert (
        next(log for log in logs if log["id"] == str(bought.import_log_id))["evidence"][
            "applications"
        ]
        == 1
    )


async def test_existing_option_by_asset_id_uses_holding_multiplier(
    session, test_workspace, test_user, wallet
):
    first = observation("option-a", quantity="1", price="2")
    first.legs[0].asset_symbol, first.legs[0].provider_asset_id = "SYN261218C00100000", None
    first.legs[0].subtotal = first.legs[0].total = Decimal("200")
    saved = await save(session, test_workspace, test_user, wallet, [first])
    await apply(session, test_workspace, test_user, wallet, saved.evidence)
    asset = (await session.scalars(select(Asset))).one()
    second = observation("option-b", quantity="1", price="2", execution="other-option-execution")
    leg = second.legs[0]
    leg.asset_symbol, leg.provider_asset_id, leg.asset_id = None, None, asset.id
    leg.subtotal = leg.total = leg.acquisition_basis = Decimal("200")
    saved = await save(session, test_workspace, test_user, wallet, [second])
    source = next(o for o in saved.evidence.observations if o.source_local_id == "option-b")
    record = next(r for r in saved.evidence.records if r.observation_ref == source.reference)
    assert record.application_status == "eligible" and record.effects.basis_delta == 200
    await apply(session, test_workspace, test_user, wallet, saved.evidence, source.reference)
    assert asset.purchase_price == 400 and asset.units == 2


@pytest.mark.parametrize("reverse_order", [False, True])
async def test_unqualified_same_ticker_snapshot_does_not_acquire_token_identity(
    session, test_workspace, wallet, reverse_order
):
    first = observation("chain-a", source_kind="balance_snapshot")
    first.legs[0].chain, first.legs[0].token_address = "chain-a", "synthetic-token-a"
    second = observation("chain-b", source_kind="balance_snapshot")
    second.legs[0].chain, second.legs[0].token_address = "chain-b", "synthetic-token-b"
    generic = observation("unqualified", source_kind="balance_snapshot")
    rows = [first, generic, second]
    preview = await service.preview_evidence(
        session, test_workspace.id, wallet.id, rows[::-1] if reverse_order else rows
    )
    assert len(preview.reconciliation) == 3
    assert {row.chain for row in preview.reconciliation} == {None, "chain-a", "chain-b"}


@pytest.mark.parametrize("mode", ["batch_versions", "corroborating", "undone"])
async def test_sync_writer_obeys_canonical_version_and_link_barrier(
    session, test_workspace, test_user, wallet, monkeypatch, mode
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.models.bank_connection import BankConnection
    from app.providers.base import HoldingData, InvestmentActivity, TradeData
    from app.services.connection_service import _sync_holdings, _sync_trades

    connection = BankConnection(
        workspace_id=test_workspace.id,
        user_id=test_user.id,
        provider="coinbase",
        external_id="connection-A",
        institution_name="Synthetic Exchange",
        credentials={},
    )
    session.add(connection)
    await session.flush()
    wallet.connection_id, wallet.source, wallet.external_id = (
        connection.id,
        "coinbase",
        "connection-A::portfolio-A",
    )
    await session.commit()
    first = observation("api-execution", source="coinbase_api", execution="api-execution")
    second = first.model_copy(deep=True)
    second.reference, second.network_status = "later-retrieval", "confirmed"
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
        external_id="api-execution",
        holding_external_id="currency-wallet-A",
        kind="buy",
        quantity=Decimal("12"),
        price=Decimal("7"),
        occurred_at=first.event_at,
    )
    provider = SimpleNamespace(
        get_holdings=AsyncMock(return_value=[holding]),
        get_investment_activity=AsyncMock(
            return_value=InvestmentActivity(trades=[trade], observations=[first, second])
        ),
    )
    monkeypatch.setattr("app.services.connection_service.get_provider", lambda _: provider)
    if mode == "corroborating":
        saved = await save(
            session, test_workspace, test_user, wallet, [first, observation("csv-canonical")]
        )
        source = next(o for o in saved.evidence.observations if o.source == "coinbase_api")
        rec = next(r for r in saved.evidence.records if r.observation_ref == source.reference)
        await service.confirm_evidence(
            session,
            test_workspace.id,
            test_user.id,
            wallet.id,
            [
                EvidenceDecision(
                    observation_ref=source.reference,
                    leg_key="amount",
                    action="link",
                    allocations=[
                        EvidenceAllocation(leg_id=rec.candidate_legs[0].leg_id, quantity="12")
                    ],
                    reason="Canonical synthetic CSV execution",
                )
            ],
            saved.evidence.revision,
        )
    if mode == "undone":
        saved = await save(session, test_workspace, test_user, wallet, [first])
        applied = await apply(session, test_workspace, test_user, wallet, saved.evidence)
        await service.undo_evidence_import(
            session, test_workspace.id, await session.get(ImportLog, applied.import_log_id)
        )
    await _sync_holdings(session, test_user.id, connection, {}, {"portfolio-A"})
    await _sync_trades(session, connection, {}, {"portfolio-A"})
    await session.commit()
    assert len(list(await session.scalars(select(AssetTransaction)))) == (
        1 if mode == "batch_versions" else 0
    )
    await _sync_trades(session, connection, {}, {"portfolio-A"})
    await session.commit()
    assert len(list(await session.scalars(select(AssetTransaction)))) == (
        1 if mode == "batch_versions" else 0
    )


async def test_competing_applied_opening_boundaries_are_unknown(session, test_workspace, wallet):
    from types import SimpleNamespace
    import uuid
    from app.schemas.investment_evidence import EvidenceRecord

    inputs, records, legs, events = {}, [], [], []
    for index, as_of in enumerate(("2025-02-04", "2025-03-04")):
        source = observation(f"opening-{index}", source_kind="remaining_lots")
        inputs[source.reference] = source
        event_id = uuid.uuid4()
        events.append(
            SimpleNamespace(
                id=event_id,
                opening_boundary={
                    "as_of": as_of,
                    "overlap_reviewed": True,
                    "assumption": "Synthetic reviewed boundary",
                },
            )
        )
        legs.append(
            SimpleNamespace(
                id=uuid.uuid4(),
                observation_id=source.reference,
                source_leg_key="amount",
                event_id=event_id,
                asset_transaction_id=uuid.uuid4(),
                payload=source.legs[0].model_dump(mode="json"),
            )
        )
        records.append(
            EvidenceRecord(
                observation_ref=source.reference,
                leg_key="amount",
                match_status="unmatched",
                application_status="already_applied",
            )
        )
    for boundary in (None, EvidenceOpeningBoundary.model_validate(events[0].opening_boundary)):
        result = service._reconciliation(inputs, records, legs, events, boundary, {(r.observation_ref, r.leg_key): (r.observation_ref, r.leg_key) for r in records})
        assert result[0].opening_quantity is None and result[0].expected_closing_quantity is None
        assert "competing_opening_boundaries" in result[0].missing_coverage

@pytest.mark.parametrize('fee', [Decimal('3'), None])
async def test_absent_derived_sale_price_does_not_swallow_fee(session, test_workspace, test_user, wallet, fee):
    saved = await save(session, test_workspace, test_user, wallet, [observation('buy')])
    await apply(session, test_workspace, test_user, wallet, saved.evidence)
    sale = observation('sale', execution='sale-execution')
    leg = sale.legs[0]
    leg.classification, leg.direction = 'sell', 'out'
    leg.unit_price, leg.unit_price_origin = None, 'derived_execution'
    leg.subtotal, leg.total, leg.fee, leg.acquisition_basis = Decimal('120'), None, fee, Decimal('84')
    saved = await save(session, test_workspace, test_user, wallet, [sale])
    source = next(o for o in saved.evidence.observations if o.source_local_id == 'sale')
    await attempt_apply(session, test_workspace, test_user, wallet, source.reference)
    sales = list(await session.scalars(select(AssetTransaction).where(AssetTransaction.kind == 'sell')))
    if fee is None:
        assert not sales
    else:
        assert sales[0].fee == 3
        assert (await session.scalars(select(Asset))).one().realized_gain == 33


@pytest.mark.parametrize('basis', ['87', '88', None])
async def test_income_gross_excludes_acquisition_fee(session, test_workspace, test_user, wallet, basis):
    from app.services.investment_income import collect_payouts
    income = observation('income-with-fee')
    leg = income.legs[0]
    leg.classification, leg.fee = 'income', Decimal('3')
    leg.total, leg.acquisition_basis = Decimal('87'), Decimal(basis) if basis else None
    saved = await save(session, test_workspace, test_user, wallet, [income])
    await attempt_apply(session, test_workspace, test_user, wallet, saved.evidence.observations[0].reference)
    if basis == '88':
        assert not list(await session.scalars(select(AssetTransaction)))
    else:
        tx = (await session.scalars(select(AssetTransaction))).one()
        assert tx.price == 7 and tx.fee == 3
        assert (await session.scalars(select(Asset))).one().purchase_price == 87
        payouts = await collect_payouts(session, test_workspace.id, since=date(2025, 1, 1), until=date(2026, 1, 1))
        assert len(payouts) == 1 and payouts[0].amount == 84

async def test_option_income_is_retained_without_incompatible_report_units(session, test_workspace, test_user, wallet):
    income = observation('option-income', quantity='1', price='2')
    income.legs[0].classification = 'income'
    income.legs[0].asset_symbol, income.legs[0].provider_asset_id = 'SYN261218C00100000', None
    income.legs[0].subtotal = income.legs[0].total = Decimal('200')
    saved = await save(session, test_workspace, test_user, wallet, [income])
    assert 'unsupported_option_income' in saved.evidence.records[0].conflicting_fields
    await attempt_apply(session, test_workspace, test_user, wallet, saved.evidence.observations[0].reference)
    assert not list(await session.scalars(select(AssetTransaction)))


async def test_secondary_income_label_does_not_create_reportable_opening_income(session, test_workspace, test_user, wallet):
    from app.services.investment_income import collect_payouts
    opening = observation('opening-income-label', source_kind='remaining_lots')
    opening.legs[0].classification = 'income'
    boundary = EvidenceOpeningBoundary(as_of=date(2025, 2, 4), overlap_reviewed=True, assumption='Synthetic remaining inventory, not a current income receipt')
    preview = await service.preview_evidence(session, test_workspace.id, wallet.id, [opening], opening_boundary=boundary)
    saved = await service.import_evidence(session, test_workspace.id, test_user.id, wallet.id, [opening], expected_revision=preview.revision, opening_boundary=boundary)
    await service.confirm_evidence(session, test_workspace.id, test_user.id, wallet.id, [EvidenceDecision(observation_ref=saved.evidence.observations[0].reference, leg_key='amount', action='apply')], saved.evidence.revision, opening_boundary=boundary, allow_unpriced=True)
    assert (await session.scalars(select(Asset))).one().purchase_price == 84
    assert not await collect_payouts(session, test_workspace.id, since=date(2025, 1, 1), until=date(2026, 1, 1))
