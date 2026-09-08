"""Synthetic source-version, precision and provider-snapshot regressions on PostgreSQL."""
from datetime import date
from decimal import Decimal, localcontext

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.asset import Asset
from app.models.asset_transaction import AssetTransaction
from app.models.bank_connection import BankConnection
from app.models.import_log import ImportLog
from app.models.investment_evidence import InvestmentLeg, InvestmentObservation
from app.models.owned_transfer import InvestmentMovementApplication
from app.schemas.investment_evidence import EvidenceDecision, EvidenceObservationInput
from app.schemas.owned_transfer import (
    MovementConfirmRequest, MovementPreviewRequest, OwnershipCreate, TransferConfirmRequest, TransferPreviewRequest,
)
from app.services import investment_evidence_service as evidence, owned_transfer_service as service
from app.services.asset_transaction_service import recompute_and_cache
from app.services.connection_service import _ledger_reconciles
from tests.test_owned_transfers_integration import add_acquisition, retain_movement
from tests.test_owned_transfers_postgres import (
    pg_request, reviewed, transfer_pg_context as transfer_pg_context,
)


async def retain_revision(session, v, leg, **changes):
    original = await session.get(InvestmentObservation, leg.observation_id)
    revision = EvidenceObservationInput.model_validate({
        **original.payload, "reference": "synthetic-revised-observation",
        "legs": [{**leg.payload, **changes}],
    })
    preview = await evidence.preview_evidence(session, v.workspace.id, original.group_id, [revision])
    await evidence.import_evidence(session, v.workspace.id, v.user.id, original.group_id,
                                   [revision], expected_revision=preview.revision)
    return await session.scalar(select(InvestmentLeg).join(InvestmentObservation).where(
        InvestmentObservation.identity_key == original.identity_key,
        InvestmentLeg.observation_id != original.id,
        InvestmentLeg.source_leg_key == leg.source_leg_key,
    ))


@pytest.mark.parametrize("changes", [
    {"destination_address": "D" * 44, "destination_owner": "D" * 44},
    {"asset_id": None, "token_address": "D" * 44, "token_program": "spl-token"},
    {"quantity": "4", "raw_units": "4000000000"},
], ids=["endpoint", "asset", "quantity"])
async def test_conflicting_source_version_invalidates_original_and_blocks_new_movement(transfer_pg_context, changes):
    v = transfer_pg_context
    request = await reviewed(v, await pg_request(v, v.b, "synthetic-revision", "3"))
    async with v.sessions() as session:
        original = await service.confirm_transfer(session, v.workspace.id, v.user.id, request)
        leg = await session.get(InvestmentLeg, request.out_leg_id)
        revised = await retain_revision(session, v, leg, **changes)
        detail = await service.get_transfer(session, v.workspace.id, original.id)
        assert detail.status == "unresolved"
        assert "source_version_conflict" in detail.reason_codes
        assert detail.acquisition_cost is detail.performance_basis is None
        assert all(not effect.settlement_complete for effect in detail.effects)
        body = MovementPreviewRequest(
            leg_id=revised.id, asset_id=v.a.id, ownership_id=v.ownership[v.a.id],
            allocations=[{"lot_id": str(v.acquisition.id), "quantity": changes.get("quantity", "3")}],
            reason="Review conflicting source version",
        )
        preview = await service.preview_movement(session, v.workspace.id, body)
        assert not preview.can_confirm and "source_version_conflict" in preview.reason_codes
        before = list(await session.scalars(select(AssetTransaction.id).order_by(AssetTransaction.id)))
        with pytest.raises(HTTPException) as caught:
            await service.apply_movement(session, v.workspace.id, v.user.id, MovementConfirmRequest(
                **body.model_dump(), expected_revision=preview.revision,
            ))
        assert isinstance(caught.value.detail, dict)
        if "quantity" in changes:
            assert caught.value.status_code == 409 and caught.value.detail["code"] == "application_conflict"
        else:
            assert caught.value.status_code == 422
            assert "source_version_conflict" in caught.value.detail["reason_codes"]
        assert list(await session.scalars(select(AssetTransaction.id).order_by(AssetTransaction.id))) == before


@pytest.mark.parametrize("writer,next_writer", [("economic", "movement"), ("movement", "movement"), ("movement", "economic")])
@pytest.mark.parametrize("reverse_first", [False, True])
async def test_conflicting_source_cannot_bypass_either_writer_or_reversal(transfer_pg_context, writer, next_writer, reverse_first):
    v = transfer_pg_context
    async with v.sessions() as session:
        asset = await session.get(Asset, v.b.id)
        asset.external_metadata = {"evidence_asset_identity": {"chain": "solana", "token_address": "native"}}
        await session.commit()
        leg = await retain_movement(
            session, asset, direction="in", reference="synthetic-writer-owner",
            source="E" * 44, destination=v.addresses[v.b.id],
            classification="buy" if writer == "economic" else "transfer",
            unit_price="20", unit_price_origin="reported", execution_currency="USD", fee="0", fee_currency="USD",
        )
        if writer == "economic":
            preview = await evidence.preview_evidence(session, v.workspace.id, asset.group_id)
            applied = await evidence.confirm_evidence(
                session, v.workspace.id, v.user.id, asset.group_id,
                [EvidenceDecision(observation_ref=str(leg.observation_id), leg_key=leg.source_leg_key, action="apply")],
                preview.revision, allow_unpriced=True,
            )
            assert applied.imported == 1
            if reverse_first:
                log = await session.get(ImportLog, applied.import_log_id)
                await evidence.undo_evidence_import(session, v.workspace.id, log)
                await session.commit()
        else:
            body = MovementPreviewRequest(leg_id=leg.id, asset_id=asset.id, ownership_id=v.ownership[asset.id], reason="Initial receipt")
            preview = await service.preview_movement(session, v.workspace.id, body)
            assert preview.can_confirm, preview.reason_codes
            applied = await service.apply_movement(session, v.workspace.id, v.user.id, MovementConfirmRequest(
                **body.model_dump(), expected_revision=preview.revision,
            ))
            if reverse_first:
                await service.reverse_movement(session, v.workspace.id, applied.id, applied.revision)
        revised = await retain_revision(session, v, leg, source_address="F" * 44, source_owner="F" * 44,
                                         classification="buy" if next_writer == "economic" else "transfer")
        group_id = asset.group_id
        before_rows = list(await session.scalars(select(AssetTransaction.id).order_by(AssetTransaction.id)))
        pointers = select(InvestmentLeg.id, InvestmentLeg.asset_transaction_id, InvestmentLeg.applied_at).order_by(InvestmentLeg.id)
        before_pointers = list((await session.execute(pointers)).all())
        if next_writer == "movement":
            body = MovementPreviewRequest(leg_id=revised.id, asset_id=v.b.id, ownership_id=v.ownership[v.b.id],
                                          reason="Conflicting alias of the same source", ordering_reviewed=True)
            preview = await service.preview_movement(session, v.workspace.id, body)
            assert not preview.can_confirm and "source_version_conflict" in preview.reason_codes
            with pytest.raises(HTTPException) as caught:
                await service.apply_movement(session, v.workspace.id, v.user.id, MovementConfirmRequest(
                    **body.model_dump(), expected_revision=preview.revision,
                ))
            assert isinstance(caught.value.detail, dict)
            assert "source_version_conflict" in caught.value.detail["reason_codes"]
        else:
            preview = await evidence.preview_evidence(session, v.workspace.id, group_id)
            record = next(row for row in preview.records if row.observation_ref == str(revised.observation_id))
            assert record.application_status == "blocked" and "source_version" in record.conflicting_fields
            with pytest.raises(HTTPException) as caught:
                await evidence.confirm_evidence(session, v.workspace.id, v.user.id, group_id,
                    [EvidenceDecision(observation_ref=str(revised.observation_id), leg_key=revised.source_leg_key, action="apply")],
                    preview.revision, allow_unpriced=True)
        assert caught.value.status_code == 422
        assert list(await session.scalars(select(AssetTransaction.id).order_by(AssetTransaction.id))) == before_rows
        assert list((await session.execute(pointers)).all()) == before_pointers


async def test_compatible_source_enrichment_preserves_confirmed_transfer(transfer_pg_context):
    v = transfer_pg_context
    request = await reviewed(v, await pg_request(v, v.b, "synthetic-compatible", "3"))
    async with v.sessions() as session:
        original = await service.confirm_transfer(session, v.workspace.id, v.user.id, request)
        leg = await session.get(InvestmentLeg, request.out_leg_id)
        revised = await retain_revision(session, v, leg, provider_asset_id="synthetic-native-asset")
        index = await service.list_transfers(session, v.workspace.id)
        assert "source_version_conflict" not in next(row for row in index.movements if row.leg_id == revised.id).reason_codes
        detail = await service.get_transfer(session, v.workspace.id, original.id)
        assert detail.status == "confirmed" and detail.acquisition_cost == Decimal("60")
        assert (await service.confirm_transfer(session, v.workspace.id, v.user.id, request)).id == original.id
        assert len(list(await session.scalars(select(InvestmentMovementApplication)))) == 2
        assert len(list(await session.scalars(select(AssetTransaction)))) == 3


@pytest.mark.parametrize("quantity", ["10000000000.000000000000000001", "12345678901234567890.123456789012345678"])
@pytest.mark.parametrize("transfer", [False, True], ids=["standalone", "transfer"])
async def test_supported_exact_quantity_previews_and_confirms_without_decimal_rounding(transfer_pg_context, quantity, transfer):
    v = transfer_pg_context
    async with v.sessions() as session:
        source = await session.get(Asset, v.a.id)
        acquisition = await session.get(AssetTransaction, v.acquisition.id)
        acquisition.quantity, acquisition.price = Decimal("90000000000000000000"), Decimal("0")
        source.units, source.last_price = acquisition.quantity, Decimal("0")
        await session.commit()
        with localcontext(prec=128):
            raw_units = str(int(Decimal(quantity) * 10**18))
            remaining = acquisition.quantity - Decimal(quantity)
        outgoing = await retain_movement(session, source, direction="out", reference="synthetic-exact-principal",
            source=v.addresses[v.a.id], destination=v.addresses[v.b.id], quantity=quantity, raw_units=raw_units, decimals=18)
        allocation = [{"lot_id": str(v.acquisition.id), "quantity": quantity}]
        if transfer:
            destination = await session.get(Asset, v.b.id)
            destination.last_price = Decimal("0")
            await session.commit()
            incoming = await retain_movement(session, destination, direction="in", reference="synthetic-exact-principal",
                source=v.addresses[v.a.id], destination=v.addresses[v.b.id], quantity=quantity, raw_units=raw_units, decimals=18)
            body = TransferPreviewRequest(out_leg_id=outgoing.id, in_leg_id=incoming.id, source_asset_id=v.a.id,
                destination_asset_id=v.b.id, source_ownership_id=v.ownership[v.a.id], destination_ownership_id=v.ownership[v.b.id],
                allocations=allocation, reason="Exact reviewed principal")
            preview = await service.preview_transfer(session, v.workspace.id, body)
            assert preview.can_confirm, preview.reason_codes
            result = await service.confirm_transfer(session, v.workspace.id, v.user.id, TransferConfirmRequest(
                **body.model_dump(), expected_revision=preview.revision,
            ))
            assert result.principal_quantity == Decimal(quantity)
            assert next(effect for effect in result.effects if effect.asset_id == v.b.id).quantity == Decimal(quantity)
        else:
            body = MovementPreviewRequest(leg_id=outgoing.id, asset_id=v.a.id, ownership_id=v.ownership[v.a.id],
                                          allocations=allocation, reason="Exact reviewed principal")
            preview = await service.preview_movement(session, v.workspace.id, body)
            assert preview.can_confirm, preview.reason_codes
            result = await service.apply_movement(session, v.workspace.id, v.user.id, MovementConfirmRequest(
                **body.model_dump(), expected_revision=preview.revision,
            ))
        assert next(effect for effect in result.effects if effect.asset_id == v.a.id).quantity == remaining
        rows = list(await session.scalars(select(AssetTransaction).where(AssetTransaction.kind == "move_out")))
        assert len(rows) == 1 and rows[0].quantity == Decimal(quantity)
        if transfer:
            acquisition = await session.get(AssetTransaction, v.acquisition.id)
            acquisition.fee = Decimal("1")
            await session.commit()
            detail = await service.get_transfer(session, v.workspace.id, result.id)
            assert detail.status == "unresolved"
            assert detail.unknown_basis_quantity == Decimal(quantity)


@pytest.mark.parametrize("dust", ["-0.000000000000000001", "0.000000000000000001"])
@pytest.mark.parametrize("sell", [False, True], ids=["buys", "buys-and-sells"])
async def test_ordinary_provider_trades_keep_dust_tolerant_basis(transfer_pg_context, dust, sell):
    v = transfer_pg_context
    async with v.sessions() as session:
        asset = await session.get(Asset, v.a.id)
        connection = BankConnection(workspace_id=v.workspace.id, user_id=v.user.id, provider="coinbase",
                                    external_id="synthetic-dust", institution_name="Synthetic", credentials={})
        session.add(connection)
        if sell:
            session.add(AssetTransaction(workspace_id=v.workspace.id, asset_id=asset.id, kind="sell",
                                         quantity=Decimal("1"), price=Decimal("30"), fee=Decimal("0"), date=date(2025, 1, 3)))
        await session.flush()
        quantity = Decimal("9" if sell else "10")
        asset.connection_id, asset.units = connection.id, quantity + Decimal(dust)
        await session.commit()
        assert await _ledger_reconciles(session, asset)
        await recompute_and_cache(session, asset)
        await session.commit()
        await session.refresh(asset)
        assert asset.units == quantity
        assert asset.purchase_price == quantity * Decimal("20")
        assert asset.average_price == Decimal("20")
        assert asset.realized_gain == Decimal("10" if sell else "0")


@pytest.mark.parametrize("settled,snapshot", [
    (True, "7"), (True, "6.999999999999999999"), (True, "7.000000000000000001"), (False, "10"),
], ids=["exact", "negative-dust", "positive-dust", "unqualified-exact-replay"])
async def test_movement_provider_snapshot_still_requires_exact_qualified_replay(transfer_pg_context, settled, snapshot):
    v = transfer_pg_context
    request = await reviewed(v, await pg_request(v, v.b, "synthetic-snapshot", "3"))
    async with v.sessions() as session:
        await service.confirm_transfer(session, v.workspace.id, v.user.id, request)
        asset = await session.get(Asset, v.a.id)
        connection = BankConnection(workspace_id=v.workspace.id, user_id=v.user.id, provider="coinbase",
                                    external_id="synthetic-snapshot", institution_name="Synthetic", credentials={})
        session.add(connection)
        await session.flush()
        asset.connection_id, asset.units = connection.id, Decimal(snapshot)
        if not settled:
            leg = await session.get(InvestmentLeg, request.out_leg_id)
            observation = await session.get(InvestmentObservation, leg.observation_id)
            observation.payload = {**observation.payload, "network_status": "pending"}
        await session.commit()
        assert await _ledger_reconciles(session, asset) is settled
        await recompute_and_cache(session, asset)
        await session.commit()
        await session.refresh(asset)
        assert asset.units == Decimal(snapshot)
        if settled and snapshot == "7":
            assert asset.purchase_price == Decimal("140") and asset.average_price == Decimal("20")
        else:
            assert asset.purchase_price is asset.average_price is asset.realized_gain is None


async def test_external_sponsor_fee_cannot_become_owned_through_account_metadata(transfer_pg_context):
    v = transfer_pg_context
    request = await reviewed(v, await pg_request(v, v.b, "synthetic-sponsored", "3"))
    async with v.sessions() as session:
        account = await service.create_ownership(session, v.workspace.id, v.user.id, OwnershipCreate(
            group_id=v.a.group_id, beneficial_owner="synthetic-owner", chain="solana",
            source_account_id=v.addresses[v.a.id], reason="Reviewed source account ownership",
        ))
        fee = await retain_movement(session, v.a, direction="unknown", reference="synthetic-sponsored",
            source="E" * 44, destination=None, quantity="0.02", leg_key="meta.fee", classification="fee",
            quantity_role="network_fee", fee_payer="E" * 44, fee_semantics="separate")
        observation = await session.get(InvestmentObservation, fee.observation_id)
        item = EvidenceObservationInput.model_validate({
            **observation.payload, "reference": "synthetic-sponsored-account", "source_account_id": v.addresses[v.a.id],
        })
        preview = await evidence.preview_evidence(session, v.workspace.id, v.a.group_id, [item])
        await evidence.import_evidence(session, v.workspace.id, v.user.id, v.a.group_id,
                                       [item], expected_revision=preview.revision)
        body = TransferPreviewRequest.model_validate({
            **request.model_dump(exclude={"expected_revision"}), "source_ownership_id": account.id,
        })
        preview = await service.preview_transfer(session, v.workspace.id, body)
        assert preview.can_confirm, preview.reason_codes
        assert preview.fee_movements and all(row.fee_payer == "E" * 44 for row in preview.fee_movements)
        result = await service.confirm_transfer(session, v.workspace.id, v.user.id, TransferConfirmRequest(
            **body.model_dump(), expected_revision=preview.revision,
        ))
        assert next(effect for effect in result.effects if effect.asset_id == v.a.id).quantity == Decimal("7")
        assert list(await session.scalars(select(AssetTransaction.id).where(AssetTransaction.kind == "fee"))) == []
        retained = await service.list_transfers(session, v.workspace.id)
        sponsor = next(row for row in retained.movements if row.classification == "fee" and row.leg_id != fee.id)
        movement = MovementPreviewRequest(leg_id=sponsor.leg_id, asset_id=v.a.id, ownership_id=account.id,
            allocations=[{"lot_id": str(v.acquisition.id), "quantity": "0.02"}], reason="Reject unsupported sponsor debit")
        preview = await service.preview_movement(session, v.workspace.id, movement)
        assert not preview.can_confirm and "ownership_effect_unknown" in preview.reason_codes
        with pytest.raises(HTTPException) as caught:
            await service.apply_movement(session, v.workspace.id, v.user.id, MovementConfirmRequest(
                **movement.model_dump(), expected_revision=preview.revision,
            ))
        assert caught.value.status_code == 422
        assert list(await session.scalars(select(AssetTransaction.id).where(AssetTransaction.kind == "fee"))) == []


async def test_transfer_detail_preserves_exact_partial_known_cost_subtotal(transfer_pg_context):
    v = transfer_pg_context
    known_cost = Decimal("12345678901.123456789012345678")
    async with v.sessions() as session:
        first = await session.get(AssetTransaction, v.acquisition.id)
        first.quantity, first.price = Decimal("1"), known_cost
        source = await session.get(Asset, v.a.id)
        second = await add_acquisition(session, source, quantity="1", price="1", when=date(2025, 1, 3))
        second_id = second.id
    body = TransferPreviewRequest(**await pg_request(v, v.b, "synthetic-partial-cost", "2"), allocations=[
        {"lot_id": str(v.acquisition.id), "quantity": "1"}, {"lot_id": str(second_id), "quantity": "1"},
    ])
    async with v.sessions() as session:
        preview = await service.preview_transfer(session, v.workspace.id, body)
        assert preview.can_confirm, preview.reason_codes
        applied = await service.confirm_transfer(session, v.workspace.id, v.user.id, TransferConfirmRequest(
            **body.model_dump(), expected_revision=preview.revision,
        ))
        second = await session.get(AssetTransaction, second_id)
        second.fee = Decimal("1")
        await session.commit()
        detail = await service.get_transfer(session, v.workspace.id, applied.id)
        assert detail.status == "unresolved" and detail.acquisition_cost is None
        assert detail.known_acquisition_cost == known_cost
        assert detail.unknown_basis_quantity == Decimal("1")
