"""Core ledger and preview checks complement the separately owned API/PG tests."""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.asset_transaction import AssetTransaction
from app.models.investment_evidence import InvestmentLeg
from app.models.owned_transfer import InvestmentMovementApplication, InvestmentOwnedTransfer
from app.schemas.investment_evidence import EvidenceDecision
from app.schemas.owned_transfer import LotSelection
from app.services import investment_evidence_service as evidence
from app.services.asset_transaction_service import _recompute, _tx_to_read
from app.services.tax_lots import build_lots
from tests.test_owned_transfers_integration import (
    PREFIX, assert_ownership, checked, confirm_transfer, effect,
    pair, retain_movement, select_lots, transfer_preview, transfers as transfers,
)


def transaction(kind, quantity, price=None, *, day=1, movement=None):
    return AssetTransaction(id=uuid.uuid4(), kind=kind, quantity=Decimal(quantity),
                            price=Decimal(price) if price is not None else None, fee=Decimal(0),
                            date=date(2025, 1, day), created_at=datetime(2025, 1, day, tzinfo=timezone.utc),
                            movement=movement)


def test_non_sale_movement_and_fee_conserve_selected_basis_without_realized_gain():
    buy = transaction("buy", "10", "20")
    out = transaction("move_out", "3", day=2, movement={"allocations": [{"lot_id": str(buy.id), "quantity": "3"}]})
    fee = transaction("fee", ".02", day=2, movement={"allocations": [{"lot_id": str(buy.id), "quantity": ".02"}]})
    result = _recompute([fee, out, buy])
    assert result["units"] == Decimal("6.98")
    assert result["cost_basis"] == Decimal("139.60")
    assert result["known_acquisition_cost"] == Decimal("139.60")
    assert result["realized_gain"] == 0 and result["realized_events"] == []


def test_unknown_receipt_keeps_known_portion_and_flat_reopen_recovers_current_basis():
    known = transaction("buy", "2", "20")
    incoming = transaction("move_in", "1", day=2, movement={"performance_basis": None})
    mixed = _recompute([known, incoming])
    assert (mixed["units"], mixed["known_acquisition_cost"], mixed["unknown_basis_quantity"]) == (3, 40, 1)
    assert mixed["cost_basis"] is None
    sell = transaction("sell", "3", "30", day=3)
    new_buy = transaction("buy", "1", "7", day=4)
    reopened = _recompute([known, incoming, sell, new_buy])
    assert reopened["cost_basis"] == 7 and reopened["average_price"] == 7
    assert reopened["realized_gain"] is None and reopened["unknown_disposition_quantity"] > 0


def test_incoming_lineage_holds_original_date_and_selected_cost_not_performance_basis():
    incoming = transaction("move_in", "1", day=2, movement={"performance_basis": "55", "lots": [{
        "lot_id": "source-fragment", "root_transaction_id": str(uuid.uuid4()), "source_leg_id": None,
        "quantity": "1", "acquisition_cost": "100", "acquired": "2023-01-01", "lineage": [], "missing_links": [],
    }]})
    pos = _recompute([incoming])
    lots = build_lots([incoming], as_of=date(2025, 1, 3))
    assert pos["cost_basis"] == 55 and pos["known_acquisition_cost"] == 100
    assert lots["lots"][0]["acquired"] == date(2023, 1, 1)
    assert lots["lots"][0]["long_term"] is True


def test_unqualified_principal_cannot_supply_settled_quantity_or_a_confident_gain():
    incoming = transaction("move_in", "3", day=2, movement={"settlement_complete": False, "missing_links": ["upstream_principal_unqualified"]})
    sell = transaction("sell", "3", "30", day=3)
    result = _recompute([incoming, sell])
    assert result["units"] == 0 and result["realized_gain"] is None
    assert not result["settlement_complete"] and result["missing_links"]


def test_one_invalid_acquisition_preserves_the_other_root_in_a_mixed_receipt():
    valid, invalid = str(uuid.uuid4()), str(uuid.uuid4())
    incoming = transaction("move_in", "3", day=2, movement={
        "performance_basis": "60", "basis_complete": False,
        "invalid_root_transaction_ids": [invalid], "missing_links": ["acquisition_source_unqualified"],
        "lots": [
            {"lot_id": valid, "root_transaction_id": valid, "quantity": "2", "acquisition_cost": "40", "acquired": "2023-01-01", "lineage": []},
            {"lot_id": invalid, "root_transaction_id": invalid, "quantity": "1", "acquisition_cost": "20", "acquired": "2024-01-01", "lineage": []},
        ],
    })
    result = _recompute([incoming])
    assert result["units"] == 3 and result["settlement_complete"]
    assert result["known_acquisition_cost"] == 40 and result["unknown_basis_quantity"] == 1
    assert result["cost_basis"] is None and result["lots"][0]["acquired"] == "2023-01-01"


@pytest.mark.parametrize("quantity", [1.2, "NaN", "Infinity", "0", "-1"])
def test_lot_selection_refuses_inexact_nonfinite_or_nonpositive_units(quantity):
    with pytest.raises(ValueError):
        LotSelection(lot_id="synthetic-lot", quantity=quantity)


@pytest.mark.parametrize("direction,role", [("in", "principal"), ("out", "principal"), ("out", "network_fee")])
async def test_movement_preview_exposes_exact_effects_without_writes(transfers, direction, role):
    v = transfers
    address, external = v.addresses[v.a.id], "D" * 44
    leg = await retain_movement(
        v.session, v.a, direction=direction, reference="synthetic-preview", quantity="1.000000001",
        source=external if direction == "in" else address,
        destination=address if direction == "in" else external, quantity_role=role,
        fee_payer=address if role == "network_fee" else None,
    )
    before = list(await v.session.scalars(select(AssetTransaction.id)))
    request = {
        "leg_id": str(leg.id), "asset_id": str(v.a.id), "ownership_id": v.ownership[v.a.id],
        "reason": "Review synthetic movement before applying",
        "allocations": [] if direction == "in" else [{"lot_id": str(v.acquisition.id), "quantity": "1.000000001"}],
    }
    response = await v.client.post("/api/assets/evidence/movements/preview", headers=v.headers, json=request)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["can_confirm"], preview["reason_codes"]
    assert len(preview["effects"]) == 1
    effect = preview["effects"][0]
    assert effect["asset_id"] == str(v.a.id)
    assert effect["settlement_complete"] and Decimal(effect["realized_gain"]) == 0
    if direction == "in":
        assert Decimal(effect["quantity"]) == Decimal("11.000000001")
        assert Decimal(effect["known_basis_quantity"]) == 10
        assert Decimal(effect["known_acquisition_cost"]) == 200
        assert Decimal(effect["unknown_basis_quantity"]) == Decimal("1.000000001")
        assert effect["performance_basis"] is None and not effect["basis_complete"]
        assert "acquisition_missing" in effect["missing_links"]
        assert effect["lots"][-1]["acquisition_cost"] is None and effect["lots"][-1]["acquired"] is None
    else:
        assert Decimal(effect["quantity"]) == Decimal("8.999999999")
        assert Decimal(effect["known_acquisition_cost"]) == Decimal("179.999999980")
        assert Decimal(effect["performance_basis"]) == Decimal("179.999999980")
        assert Decimal(effect["unknown_basis_quantity"]) == 0 and effect["basis_complete"]
    await v.session.flush()
    assert list(await v.session.scalars(select(AssetTransaction.id))) == before
    assert list(await v.session.scalars(select(InvestmentMovementApplication))) == []
    assert list(await v.session.scalars(select(InvestmentOwnedTransfer))) == []
    await v.session.refresh(v.a)
    await v.session.refresh(leg)
    assert v.a.units == Decimal("10") and leg.asset_transaction_id is None and leg.applied_at is None


async def test_reviewed_same_day_acquisition_order_is_recorded_and_replayed(transfers):
    v = transfers
    v.acquisition.date = date(2025, 2, 3)
    await v.session.commit()
    original_created_at = v.acquisition.created_at
    request = await pair(v, v.a, v.b)
    assert "event_order_unresolved" in (await transfer_preview(v, request))["reason_codes"]
    request = await select_lots(v, {**request, "ordering_reviewed": True})
    applied = await confirm_transfer(v, request)
    assert Decimal(effect(applied, v.a)["quantity"]) == 7
    assert Decimal(effect(applied, v.b)["known_acquisition_cost"]) == 60
    outgoing = await v.session.scalar(select(AssetTransaction).where(AssetTransaction.kind == "move_out"))
    assert outgoing.movement["predecessor_transaction_ids"] == [str(v.acquisition.id)]
    assert outgoing.created_at.date() == date(2025, 2, 3)
    assert outgoing.created_at.hour == 12
    assert v.acquisition.created_at == original_created_at
    position = _recompute(list(await v.session.scalars(select(AssetTransaction).where(AssetTransaction.asset_id == v.a.id))))
    assert position["settlement_complete"] and position["units"] == 7 and position["cost_basis"] == 140
    sale = await v.client.post(f"/api/assets/{v.a.id}/transactions", headers=v.headers,
                               json={"kind": "sell", "quantity": 1, "price": 30, "date": "2025-02-04"})
    assert sale.status_code == 201, sale.text
    assert sale.json()["units"] == 6 and sale.json()["realized_gain"] == 10
    rejected = await v.client.delete(f"/api/assets/transactions/{v.acquisition.id}", headers=v.headers)
    assert rejected.status_code == 409


@pytest.mark.parametrize("reversed,legacy_key", [(False, False), (True, False), (False, True), (True, True)])
async def test_physical_receipt_and_tombstone_cannot_credit_another_group(transfers, reversed, legacy_key):
    v = transfers
    first = await confirm_transfer(v, await select_lots(v, await pair(v, v.a, v.b)))
    if reversed:
        checked(await v.client.delete(f"{PREFIX}/transfers/{first['id']}", headers=v.headers,
                                      params={"expected_revision": first["revision"]}))
    if legacy_key:
        for row in await v.session.scalars(select(InvestmentMovementApplication)):
            row.application_key = f"old-account-scope-{row.id}"
        await v.session.commit()
    alias_ownership = await assert_ownership(v, v.c, v.addresses[v.b.id])
    alias = await retain_movement(v.session, v.c, direction="in", reference="synthetic-transfer",
                                 source=v.addresses[v.a.id], destination=v.addresses[v.b.id])
    request = {"leg_id": str(alias.id), "asset_id": str(v.c.id), "ownership_id": alias_ownership["id"],
               "reason": "Duplicate receipt account mapping"}
    preview = checked(await v.client.post(f"{PREFIX}/movements/preview", headers=v.headers, json=request))
    assert not preview["can_confirm"]
    result = await v.client.post(f"{PREFIX}/movements", headers=v.headers,
                                 json={**request, "expected_revision": preview["revision"]})
    assert result.status_code == 409, result.text
    await v.session.refresh(v.c)
    assert v.c.units == 0
    assert len(list(await v.session.scalars(select(InvestmentMovementApplication)))) == 2


async def test_full_external_send_keeps_consumed_lot_provenance_after_reversal(transfers):
    v = transfers
    hop = await confirm_transfer(v, await select_lots(v, await pair(v, v.a, v.b)))
    outgoing = await retain_movement(v.session, v.b, direction="out", reference="synthetic-full-payment", quantity="3",
                                     source=v.addresses[v.b.id], destination="E" * 44, when="2025-02-04T12:00:00+00:00")
    lots = checked(await v.client.get(f"{PREFIX}/transfers/lots", headers=v.headers,
                                     params={"asset_id": str(v.b.id), "before_leg_id": str(outgoing.id)}))["lots"]
    request = {"leg_id": str(outgoing.id), "asset_id": str(v.b.id), "ownership_id": v.ownership[v.b.id],
               "allocations": [{"lot_id": lots[0]["lot_id"], "quantity": "3"}], "reason": "Reviewed synthetic full payment"}
    preview = checked(await v.client.post(f"{PREFIX}/movements/preview", headers=v.headers, json=request))
    applied = checked(await v.client.post(f"{PREFIX}/movements", headers=v.headers,
                                         json={**request, "expected_revision": preview["revision"]}))
    assert effect(applied, v.b)["lots"] == [] and Decimal(effect(applied, v.b)["quantity"]) == 0
    assert applied["selected_lots"] == preview["selected_lots"]
    consumed = applied["selected_lots"][0]
    assert consumed["root_transaction_id"] == str(v.acquisition.id)
    assert consumed["lineage"] == [hop["id"]] and consumed["acquired"] == "2025-01-02"
    assert Decimal(consumed["quantity"]) == 3 and Decimal(consumed["acquisition_cost"]) == 60
    reversed_result = checked(await v.client.delete(f"{PREFIX}/movements/{applied['id']}", headers=v.headers,
                                                     params={"expected_revision": applied["revision"]}))
    assert reversed_result["selected_lots"] == applied["selected_lots"]


async def test_account_only_owned_fee_requires_separate_recorded_application(transfers):
    v = transfers
    index = checked(await v.client.get(f"{PREFIX}/transfers", headers=v.headers))
    checked(await v.client.delete(f"{PREFIX}/ownership/{v.ownership[v.a.id]}", headers=v.headers,
                                 params={"expected_revision": index["revision"]}))
    account_owner = await assert_ownership(v, v.a, None, source_account_id=v.addresses[v.a.id])
    v.ownership[v.a.id] = account_owner["id"]
    request = await pair(v, v.a, v.b)
    fee = await retain_movement(v.session, v.a, direction="out", reference="synthetic-transfer",
                               source=v.addresses[v.a.id], destination=None, quantity="0.02", leg_key="meta.fee",
                               classification="fee", quantity_role="network_fee", fee_payer=v.addresses[v.a.id], fee_semantics="separate")
    request = await select_lots(v, request)
    preview = await transfer_preview(v, request)
    assert not preview["can_confirm"] and "owned_fee_application_required" in preview["reason_codes"]
    request["fees"] = [{"leg_id": str(fee.id), "asset_id": str(v.a.id), "ownership_id": account_owner["id"],
                        "allocations": [{"lot_id": str(v.acquisition.id), "quantity": "0.02"}], "reason": "Reviewed owned payer fee"}]
    applied = await confirm_transfer(v, request)
    assert Decimal(effect(applied, v.a)["quantity"]) == Decimal("6.98")


@pytest.mark.parametrize("direction,role", [("in", "principal"), ("out", "principal"), ("out", "network_fee")])
async def test_standalone_movement_cannot_corrupt_option_contract_replay(transfers, direction, role):
    v = transfers
    v.a.type = "option"
    await v.session.commit()
    leg = await retain_movement(v.session, v.a, direction=direction, reference="synthetic-option-movement",
                                source=v.addresses[v.a.id], destination=v.addresses[v.a.id], quantity="0.02",
                                quantity_role=role, fee_payer=v.addresses[v.a.id], fee_semantics="separate")
    request = {"leg_id": str(leg.id), "asset_id": str(v.a.id), "ownership_id": v.ownership[v.a.id],
               "allocations": [] if direction == "in" else [{"lot_id": str(v.acquisition.id), "quantity": "0.02"}],
               "reason": "Wrong holding type"}
    preview = checked(await v.client.post(f"{PREFIX}/movements/preview", headers=v.headers, json=request))
    assert not preview["can_confirm"] and "unsupported_transfer_asset" in preview["reason_codes"]
    result = await v.client.post(f"{PREFIX}/movements", headers=v.headers,
                                json={**request, "expected_revision": preview["revision"]})
    assert result.status_code == 422
    assert list(await v.session.scalars(select(InvestmentMovementApplication))) == []


@pytest.mark.parametrize("kind", ["move_in", "move_out", "fee"])
def test_movement_transaction_read_preserves_exact_atomic_quantity(kind):
    tx = transaction(kind, "3.000000000000000001", movement={"leg_id": str(uuid.uuid4())})
    tx.asset_id, tx.source = uuid.uuid4(), "evidence"
    read = _tx_to_read(tx).model_dump(mode="json")
    assert read["quantity_exact"] == "3.000000000000000001"
    assert read["price"] is None


@pytest.mark.parametrize("classification", ["buy", "sell", "income", "lot", "unknown"])
async def test_nonmovement_classification_cannot_use_quantity_writer(transfers, classification):
    v = transfers
    leg = await retain_movement(v.session, v.b, direction="in", reference="synthetic-wrong-classification",
                                source="E" * 44, destination=v.addresses[v.b.id], classification=classification)
    request = {"leg_id": str(leg.id), "asset_id": str(v.b.id), "ownership_id": v.ownership[v.b.id],
               "ordering_reviewed": True, "reason": "Review classification boundary"}
    preview = checked(await v.client.post(f"{PREFIX}/movements/preview", headers=v.headers, json=request))
    assert not preview["can_confirm"] and "unsupported_movement_classification" in preview["reason_codes"]
    response = await v.client.post(f"{PREFIX}/movements", headers=v.headers,
                                   json={**request, "expected_revision": preview["revision"]})
    assert response.status_code == 422
    await v.session.refresh(leg)
    assert leg.asset_transaction_id is None and leg.applied_at is None


@pytest.mark.parametrize("undo,alias", [(False, False), (True, False), (False, True), (True, True)])
async def test_economic_application_or_undo_cannot_be_reapplied_as_movement(transfers, undo, alias):
    v = transfers
    v.b.external_metadata = {"evidence_asset_identity": {"chain": "solana", "token_address": "native"}}
    await v.session.commit()
    leg = await retain_movement(v.session, v.b, direction="in", reference="synthetic-acquisition-principal",
                                source="E" * 44, destination=v.addresses[v.b.id], classification="buy", unit_price="20",
                                unit_price_origin="reported", execution_currency="USD", fee="0", fee_currency="USD")
    source = await evidence.preview_evidence(v.session, v.workspace.id, v.b.group_id)
    applied = await evidence.confirm_evidence(v.session, v.workspace.id, v.user.id, v.b.group_id,
        [EvidenceDecision(observation_ref=str(leg.observation_id), leg_key=leg.source_leg_key, action="apply")],
        source.revision, allow_unpriced=True)
    assert applied.imported == 1
    if undo:
        response = await v.client.delete(f"/api/import-logs/{applied.import_log_id}", headers=v.headers)
        assert response.status_code in {200, 204}, response.text
    asset, ownership = v.b, v.ownership[v.b.id]
    if alias:
        asset = v.c
        ownership = (await assert_ownership(v, v.c, v.addresses[v.b.id]))["id"]
        leg = await retain_movement(v.session, v.c, direction="in", reference="synthetic-acquisition-principal",
                                     source="E" * 44, destination=v.addresses[v.b.id])
    before_rows = list(await v.session.scalars(select(AssetTransaction.id)))
    before_pointers = list((await v.session.execute(select(InvestmentLeg.id, InvestmentLeg.asset_transaction_id, InvestmentLeg.applied_at))).all())
    request = {"leg_id": str(leg.id), "asset_id": str(asset.id), "ownership_id": ownership,
               "ordering_reviewed": True, "reason": "Already represented by the economic writer"}
    preview = checked(await v.client.post(f"{PREFIX}/movements/preview", headers=v.headers, json=request))
    assert not preview["can_confirm"] and "canonical_application_owned" in preview["reason_codes"]
    response = await v.client.post(f"{PREFIX}/movements", headers=v.headers,
                                   json={**request, "expected_revision": preview["revision"]})
    assert response.status_code == 422
    assert list(await v.session.scalars(select(AssetTransaction.id))) == before_rows
    assert list((await v.session.execute(select(InvestmentLeg.id, InvestmentLeg.asset_transaction_id, InvestmentLeg.applied_at))).all()) == before_pointers
    assert list(await v.session.scalars(select(InvestmentMovementApplication))) == []


@pytest.mark.parametrize("reversed", [False, True])
async def test_movement_or_tombstone_blocks_economic_alias_in_another_group(transfers, reversed):
    v = transfers
    v.c.external_metadata = {"evidence_asset_identity": {"chain": "solana", "token_address": "native"}}
    await v.session.commit()
    receipt = await retain_movement(v.session, v.b, direction="in", reference="synthetic-existing-movement",
                                    source="E" * 44, destination=v.addresses[v.b.id])
    request = {"leg_id": str(receipt.id), "asset_id": str(v.b.id), "ownership_id": v.ownership[v.b.id],
               "reason": "Review receipt without acquisition assertion"}
    preview = checked(await v.client.post(f"{PREFIX}/movements/preview", headers=v.headers, json=request))
    applied = checked(await v.client.post(f"{PREFIX}/movements", headers=v.headers,
                                         json={**request, "expected_revision": preview["revision"]}))
    if reversed:
        checked(await v.client.delete(f"{PREFIX}/movements/{applied['id']}", headers=v.headers,
                                     params={"expected_revision": applied["revision"]}))
    alias = await retain_movement(v.session, v.c, direction="in", reference="synthetic-existing-movement",
                                  source="E" * 44, destination=v.addresses[v.b.id], classification="buy", unit_price="20",
                                  unit_price_origin="reported", execution_currency="USD", fee="0", fee_currency="USD")
    before_rows = list(await v.session.scalars(select(AssetTransaction.id)))
    source = await evidence.preview_evidence(v.session, v.workspace.id, v.c.group_id)
    record = next(row for row in source.records if row.observation_ref == str(alias.observation_id))
    assert record.application_status == "blocked" and "canonical_movement_application" in record.reason_codes
    with pytest.raises(HTTPException) as caught:
        await evidence.confirm_evidence(v.session, v.workspace.id, v.user.id, v.c.group_id,
            [EvidenceDecision(observation_ref=str(alias.observation_id), leg_key=alias.source_leg_key, action="apply")],
            source.revision, allow_unpriced=True)
    assert caught.value.status_code == 422
    assert list(await v.session.scalars(select(AssetTransaction.id))) == before_rows
    await v.session.refresh(alias)
    assert alias.asset_transaction_id is None and alias.applied_at is None
