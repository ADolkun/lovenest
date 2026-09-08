"""Synthetic provider/CSV/collector evidence through owned-transfer HTTP review."""
import uuid
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.models.investment_evidence import InvestmentLeg, InvestmentObservation
from app.models.owned_transfer import InvestmentOwnedTransfer
from app.schemas.investment_evidence import EvidenceLegInput, EvidenceObservationInput
from app.services import investment_evidence_service as evidence
from tests.test_investment_evidence_integration import (
    WALLET, _api_row, _csv_bytes, _csv_row, _evidence, _rows, _sync, _upload,
    venue as venue,
)
from tests.test_onchain_history_api import (
    A, history_context as history_context, rpc_fixture,
)

PREFIX = "/api/assets/evidence"


def checked(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


async def retain_movement(
    session, asset, *, direction, reference, source, destination, quantity="3",
    when="2025-02-03T12:00:00+00:00", leg_key="ix:0", **changes,
):
    at = datetime.fromisoformat(when)
    leg = {
        "key": leg_key, "asset_id": asset.id, "asset_symbol": "SYN", "chain": "solana",
        "token_address": "native", "direction": direction, "classification": "transfer",
        "quantity": quantity, "transaction_ref": reference, "leg_ref": leg_key,
        "source_address": source, "destination_address": destination,
        "source_owner": source, "destination_owner": destination,
        "raw_units": str(int(Decimal(quantity) * 10**9)), "decimals": 9,
        "quantity_role": "principal", "fee_semantics": "none", **changes,
    }
    item = EvidenceObservationInput(
        reference=f"{reference}:{asset.id}:{leg_key}", source="csv", provider="onchain",
        source_local_id=f"{reference}:{direction}:{leg_key}", source_account_id=source if direction == "out" else destination,
        source_locator=f"synthetic/{reference}.csv", event_at=at, event_date=at.date(),
        event_time_raw=when, time_precision="second", timezone="UTC",
        observed_at=at, provider_status="completed", network_status="finalized",
        settlement_status="settled", legs=[EvidenceLegInput.model_validate(leg)],
    )
    preview = await evidence.preview_evidence(session, asset.workspace_id, asset.group_id, [item])
    saved = await evidence.import_evidence(
        session, asset.workspace_id, asset.user_id, asset.group_id, [item], expected_revision=preview.revision,
    )
    retained = next(o for o in saved.evidence.observations if o.source_local_id == item.source_local_id)
    return await session.scalar(select(InvestmentLeg).where(
        InvestmentLeg.observation_id == uuid.UUID(retained.reference), InvestmentLeg.source_leg_key == leg_key,
    ))


async def assert_ownership(v, asset, address, **changes):
    return checked(await v.client.post(f"{PREFIX}/ownership", headers=v.headers, json={
        "group_id": str(asset.group_id), "beneficial_owner": "synthetic-owner", "chain": "solana",
        "address": address, "valid_from": "2024-01-01", "reason": "Reviewed synthetic account ownership",
        **changes,
    }))


@pytest_asyncio.fixture
async def transfers(session, test_workspace, test_user, client, auth_headers):
    assets = [await make_holding(session, test_workspace.id, test_user.id, key) for key in "ABC"]
    v = SimpleNamespace(
        session=session, workspace=test_workspace, user=test_user, client=client,
        headers={**auth_headers, "X-Workspace-Id": str(test_workspace.id)},
        a=assets[0], b=assets[1], c=assets[2], ownership={}, addresses={},
    )
    v.acquisition = await add_acquisition(session, v.a)
    for asset, address in zip(assets, ["A" * 44, "B" * 44, "C" * 44]):
        v.addresses[asset.id] = address
        v.ownership[asset.id] = (await assert_ownership(v, asset, address))["id"]
    return v


async def pair(v, source, destination, *, quantity="3", reference="synthetic-transfer", when="2025-02-03T12:00:00+00:00"):
    endpoints = {"source": v.addresses[source.id], "destination": v.addresses[destination.id]}
    out = await retain_movement(v.session, source, direction="out", reference=reference, quantity=quantity, when=when, **endpoints)
    incoming = await retain_movement(v.session, destination, direction="in", reference=reference, quantity=quantity, when=when, **endpoints)
    return {
        "out_leg_id": str(out.id), "in_leg_id": str(incoming.id),
        "source_asset_id": str(source.id), "destination_asset_id": str(destination.id),
        "source_ownership_id": v.ownership[source.id], "destination_ownership_id": v.ownership[destination.id],
        "reason": "Reviewed synthetic transfer and specific source lot selection",
    }


async def transfer_preview(v, request):
    return checked(await v.client.post(f"{PREFIX}/transfers/preview", headers=v.headers, json=request))


async def select_lots(v, request, quantities=None):
    preview = await transfer_preview(v, request)
    lots = preview["available_lots"]
    if quantities is None:
        quantities = [preview["principal_quantity"]]
        assert len(lots) == 1, preview
    return {**request, "allocations": [
        {"lot_id": lot["lot_id"], "quantity": str(quantity)}
        for lot, quantity in zip(lots, quantities) if Decimal(str(quantity)) > 0
    ]}


async def confirm_transfer(v, request):
    preview = await transfer_preview(v, request)
    assert preview["can_confirm"], preview["reason_codes"]
    return checked(await v.client.post(f"{PREFIX}/transfers", headers=v.headers, json={
        **request, "expected_revision": preview["revision"],
    }))


def effect(result, asset):
    return next(row for row in result["effects"] if row["asset_id"] == str(asset.id))


async def test_exact_partial_transfer_is_idempotent_and_reversible(transfers):
    v = transfers
    request = await select_lots(v, await pair(v, v.a, v.b))
    preview = await transfer_preview(v, request)
    original = checked(await v.client.post(f"{PREFIX}/transfers", headers=v.headers, json={
        **request, "expected_revision": preview["revision"],
    }))
    replay = checked(await v.client.post(f"{PREFIX}/transfers", headers=v.headers, json={
        **request, "expected_revision": preview["revision"],
    }))
    assert replay["id"] == original["id"]
    for asset, quantity, cost in [(v.a, "7", "140"), (v.b, "3", "60")]:
        current = effect(original, asset)
        assert Decimal(current["quantity"]) == Decimal(quantity)
        assert Decimal(current["known_acquisition_cost"]) == Decimal(cost)
        assert Decimal(current["performance_basis"]) == Decimal(cost)
        assert Decimal(current["realized_gain"]) == 0
        assert current["basis_complete"]
    receipt_lot = effect(original, v.b)["lots"][0]
    assert receipt_lot["acquired"] == "2025-01-02"
    assert receipt_lot["root_transaction_id"] == str(v.acquisition.id)
    detail = checked(await v.client.get(f"{PREFIX}/transfers/{original['id']}", headers=v.headers))
    reversed_result = checked(await v.client.delete(
        f"{PREFIX}/transfers/{original['id']}", headers=v.headers, params={"expected_revision": detail["revision"]},
    ))
    assert reversed_result["status"] == "reversed"
    for asset, quantity in [(v.a, "10"), (v.b, "0")]:
        await v.session.refresh(asset)
        assert asset.units == Decimal(quantity)
    repeated = checked(await v.client.delete(
        f"{PREFIX}/transfers/{original['id']}", headers=v.headers, params={"expected_revision": detail["revision"]},
    ))
    assert repeated["id"] == original["id"] and repeated["status"] == "reversed"
    assert len(list(await v.session.scalars(select(InvestmentObservation)))) == 2


async def test_chained_return_preserves_original_lot_and_refuses_parent_reversal(transfers):
    v = transfers
    first = await confirm_transfer(v, await select_lots(v, await pair(v, v.a, v.b)))
    second = await confirm_transfer(v, await select_lots(v, await pair(
        v, v.b, v.c, quantity="2", reference="synthetic-second-hop", when="2025-02-04T12:00:00+00:00",
    )))
    third = await confirm_transfer(v, await select_lots(v, await pair(
        v, v.c, v.a, quantity="1", reference="synthetic-return", when="2025-02-05T12:00:00+00:00",
    )))
    for asset, quantity in [(v.a, "8"), (v.b, "1"), (v.c, "1")]:
        await v.session.refresh(asset)
        assert asset.units == Decimal(quantity)
    returned = next(lot for lot in effect(third, v.a)["lots"] if third["id"] in lot["lineage"])
    assert returned["root_transaction_id"] == str(v.acquisition.id)
    assert returned["acquired"] == "2025-01-02"
    assert {first["id"], second["id"], third["id"]} <= set(returned["lineage"])
    detail = checked(await v.client.get(f"{PREFIX}/transfers/{first['id']}", headers=v.headers))
    refused = await v.client.delete(f"{PREFIX}/transfers/{first['id']}", headers=v.headers, params={"expected_revision": detail["revision"]})
    assert refused.status_code == 409, refused.text
    dependent = await v.session.get(InvestmentOwnedTransfer, uuid.UUID(second["id"]))
    assert str(dependent.out_application_id) in refused.json()["detail"]["dependencies"]
    sale = checked(await v.client.post(f"/api/assets/{v.b.id}/transactions", headers=v.headers, json={
        "kind": "sell", "quantity": "1", "price": "30", "date": "2025-03-01",
    }), 201)
    assert Decimal(str(sale["realized_gain"])) == Decimal("10")
    lots = checked(await v.client.get(f"/api/assets/{v.b.id}/tax-lots", headers=v.headers))
    assert lots["sales"] and Decimal(str(lots["sales"][0]["gain"])) == Decimal("10")


@pytest.mark.parametrize("mutation", ["edit_source", "delete_source", "backdated_buy", "edit_transfer", "delete_asset"])
async def test_dependent_source_mutations_fail_without_partial_changes(transfers, mutation):
    v = transfers
    result = await confirm_transfer(v, await select_lots(v, await pair(v, v.a, v.b)))
    for asset in [v.a, v.b]:
        await v.session.refresh(asset)
    before = {asset.id: (asset.units, asset.purchase_price) for asset in [v.a, v.b]}
    if mutation == "edit_source":
        response = await v.client.patch(f"/api/assets/transactions/{v.acquisition.id}", headers=v.headers, json={"quantity": "2"})
    elif mutation == "delete_source":
        response = await v.client.delete(f"/api/assets/transactions/{v.acquisition.id}", headers=v.headers)
    elif mutation == "backdated_buy":
        response = await v.client.post(f"/api/assets/{v.a.id}/transactions", headers=v.headers, json={
            "kind": "buy", "quantity": "1", "price": "999", "date": "2025-01-01",
        })
    elif mutation == "edit_transfer":
        movement = await v.session.scalar(select(AssetTransaction).where(
            AssetTransaction.asset_id == v.b.id, AssetTransaction.kind != "buy",
        ))
        assert movement is not None
        response = await v.client.patch(f"/api/assets/transactions/{movement.id}", headers=v.headers, json={"quantity": "1"})
    else:
        response = await v.client.delete(f"/api/assets/{v.a.id}", headers=v.headers)
    assert response.status_code == 409, response.text
    decision = await v.session.get(InvestmentOwnedTransfer, uuid.UUID(result["id"]))
    assert {str(decision.id), str(decision.out_application_id), str(decision.in_application_id)} & set(response.json()["detail"]["dependencies"])
    for asset in [v.a, v.b]:
        await v.session.refresh(asset)
        assert (asset.units, asset.purchase_price) == before[asset.id]


@pytest.mark.parametrize("field,value", [
    ("chain", "ethereum"), ("token_address", "synthetic-other-mint"),
    ("token_program", "synthetic-other-program"), ("leg_ref", "ix:1"),
    ("source_address", "D" * 44), ("destination_address", "E" * 44),
    ("raw_units", "2999999999"), ("transaction_ref", None),
    ("fee_semantics", "unknown"), ("quantity_role", "balance_delta"),
])
async def test_incomplete_or_conflicting_identity_cannot_move_units(transfers, field, value):
    v = transfers
    request = await pair(v, v.a, v.b)
    leg = await v.session.get(InvestmentLeg, uuid.UUID(request["in_leg_id"]))
    observation = await v.session.get(InvestmentObservation, leg.observation_id)
    payload = {**leg.payload, field: value}
    leg.payload = payload
    observation.payload = {**observation.payload, "legs": [payload]}
    await v.session.commit()
    preview = await transfer_preview(v, request)
    assert not preview["can_confirm"]
    assert preview["status"] in {"candidate", "conflicting"}
    assert preview["reason_codes"]
    response = await v.client.post(f"{PREFIX}/transfers", headers=v.headers, json={
        **request, "expected_revision": preview["revision"],
        "allocations": [{"lot_id": preview["available_lots"][0]["lot_id"], "quantity": "3"}],
    })
    assert response.status_code in {409, 422}, response.text
    await v.session.refresh(v.a)
    await v.session.refresh(v.b)
    assert (v.a.units, v.b.units) == (Decimal("10"), Decimal("0"))


@pytest.mark.parametrize("provider,network,settlement", [
    ("completed", "confirmed", "pending"), ("completed", "processed", "settled"),
    ("failed", "finalized", "failed"), ("completed", "finalized", "unknown"),
])
async def test_provider_completion_cannot_override_missing_network_finality(transfers, provider, network, settlement):
    v = transfers
    request = await pair(v, v.a, v.b)
    leg = await v.session.get(InvestmentLeg, uuid.UUID(request["in_leg_id"]))
    observation = await v.session.get(InvestmentObservation, leg.observation_id)
    observation.payload = {
        **observation.payload, "provider_status": provider, "network_status": network,
        "settlement_status": settlement,
    }
    await v.session.commit()
    preview = await transfer_preview(v, request)
    assert not preview["can_confirm"] and preview["reason_codes"]
    assert preview["in_movement"]["provider_status"] == provider
    assert preview["in_movement"]["network_status"] == network
    assert len(list(await v.session.scalars(select(AssetTransaction)))) == 1


@pytest.mark.parametrize("field", [
    "out_leg_id", "in_leg_id", "source_asset_id", "destination_asset_id",
    "source_ownership_id", "destination_ownership_id",
])
async def test_each_foreign_workspace_reference_is_rejected(transfers, field):
    from app.models.workspace import Workspace, WorkspaceMember

    v = transfers
    request = await pair(v, v.a, v.b)
    other = Workspace(id=uuid.uuid4(), name="Other synthetic Investment", created_by_user_id=v.user.id)
    v.session.add(other)
    await v.session.flush()
    v.session.add(WorkspaceMember(workspace_id=other.id, user_id=v.user.id, role="owner"))
    await v.session.commit()
    asset = await make_holding(v.session, other.id, v.user.id, "Foreign")
    foreign = SimpleNamespace(client=v.client, headers={**v.headers, "X-Workspace-Id": str(other.id)})
    ownership = await assert_ownership(foreign, asset, "F" * 44)
    leg = await retain_movement(
        v.session, asset, direction="out", reference="synthetic-foreign",
        source="F" * 44, destination="G" * 44,
    )
    foreign_id = leg.id if "leg" in field else ownership["id"] if "ownership" in field else asset.id
    response = await v.client.post(f"{PREFIX}/transfers/preview", headers=v.headers, json={**request, field: str(foreign_id)})
    assert response.status_code == 404, response.text
    assert "Foreign" not in response.text and "synthetic-foreign" not in response.text
    await v.session.refresh(v.a)
    assert v.a.units == Decimal("10")


async def test_watch_or_different_beneficial_owner_is_not_internal(transfers):
    v = transfers
    request = await pair(v, v.a, v.b)
    foreign_owner = await assert_ownership(v, v.b, v.addresses[v.b.id], beneficial_owner="another-person")
    preview = await transfer_preview(v, {**request, "destination_ownership_id": foreign_owner["id"]})
    assert not preview["can_confirm"] and preview["reason_codes"]
    response = await v.client.post(f"{PREFIX}/transfers/preview", headers=v.headers, json={
        **request, "destination_ownership_id": str(uuid.uuid4()),
    })
    assert response.status_code == 404, response.text


async def test_viewer_can_read_but_cannot_confirm_or_revoke_ownership(transfers, viewer_auth_headers):
    v = transfers
    request = await select_lots(v, await pair(v, v.a, v.b))
    preview = await transfer_preview(v, request)
    headers = {**viewer_auth_headers, "X-Workspace-Id": str(v.workspace.id)}
    assert (await v.client.get(f"{PREFIX}/transfers", headers=headers)).status_code == 200
    assert (await v.client.post(f"{PREFIX}/transfers", headers=headers, json={
        **request, "expected_revision": preview["revision"],
    })).status_code == 403
    assert (await v.client.post(f"{PREFIX}/ownership", headers=headers, json={
        "group_id": str(v.b.group_id), "beneficial_owner": "synthetic-owner", "chain": "solana",
        "address": v.addresses[v.b.id], "reason": "Not writable",
    })).status_code == 403
    assert (await v.client.delete(f"{PREFIX}/ownership/{v.ownership[v.b.id]}", headers=headers)).status_code == 403
    assert (await v.client.get(f"{PREFIX}/transfers")).status_code == 401


async def test_source_invalidation_qualifies_prior_link_and_stale_preview(transfers):
    v = transfers
    request = await select_lots(v, await pair(v, v.a, v.b))
    prior_preview = await transfer_preview(v, request)
    result = await confirm_transfer(v, request)
    leg = await v.session.get(InvestmentLeg, uuid.UUID(request["in_leg_id"]))
    source = await v.session.get(InvestmentObservation, leg.observation_id)
    retained_payload = dict(source.payload)
    source.is_current = False
    await v.session.commit()
    detail = checked(await v.client.get(f"{PREFIX}/transfers/{result['id']}", headers=v.headers))
    assert detail["status"] == "unresolved" and detail["reason_codes"]
    received = effect(detail, v.b)
    assert not received["basis_complete"] and not received["settlement_complete"]
    assert received["performance_basis"] is None
    assert received["missing_links"]
    assert detail["revision"] != prior_preview["revision"]
    response = await v.client.post(f"{PREFIX}/transfers", headers=v.headers, json={
        **request, "expected_revision": prior_preview["revision"],
    })
    if response.status_code == 200:
        retry = response.json()
        assert retry["id"] == result["id"] and retry["status"] == "unresolved"
        assert retry["performance_basis"] is None
    else:
        assert response.status_code in {409, 422}, response.text
    await v.session.refresh(source)
    assert source.payload == retained_payload


async def apply_movement(v, asset, leg, *, allocations=None):
    request = {
        "leg_id": str(leg.id), "asset_id": str(asset.id), "ownership_id": v.ownership[asset.id],
        "allocations": allocations or [], "reason": "Reviewed synthetic quantity movement; treatment unresolved",
    }
    preview = checked(await v.client.post(f"{PREFIX}/movements/preview", headers=v.headers, json=request))
    assert preview["can_confirm"], preview
    return checked(await v.client.post(f"{PREFIX}/movements", headers=v.headers, json={
        **request, "expected_revision": preview["revision"],
    }))


async def test_known_and_unknown_receipt_parts_survive_chaining_and_sale(transfers):
    v = transfers
    await confirm_transfer(v, await select_lots(v, await pair(v, v.a, v.b, quantity="2")))
    unknown = await retain_movement(
        v.session, v.b, direction="in", reference="synthetic-unknown-acquisition",
        source="D" * 44, destination=v.addresses[v.b.id], quantity="1",
        when="2025-02-04T12:00:00+00:00", valuation_amount="999", valuation_currency="USD",
    )
    applied = await apply_movement(v, v.b, unknown)
    received = effect(applied, v.b)
    assert Decimal(received["quantity"]) == Decimal("3")
    assert Decimal(received["known_basis_quantity"]) == Decimal("2")
    assert Decimal(received["unknown_basis_quantity"]) == Decimal("1")
    assert Decimal(received["known_acquisition_cost"]) == Decimal("40")
    assert received["performance_basis"] is None and not received["basis_complete"]
    unknown_lot = next(lot for lot in received["lots"] if not lot["basis_complete"])
    assert unknown_lot["acquired"] is None and unknown_lot["acquisition_cost"] is None
    request = await pair(v, v.b, v.c, quantity="3", reference="synthetic-mixed-hop", when="2025-02-05T12:00:00+00:00")
    preview = await transfer_preview(v, request)
    selected = {**request, "allocations": [{"lot_id": lot["lot_id"], "quantity": lot["quantity"]} for lot in preview["available_lots"]]}
    moved = await confirm_transfer(v, selected)
    destination = effect(moved, v.c)
    assert Decimal(destination["quantity"]) == Decimal("3")
    assert Decimal(destination["known_acquisition_cost"]) == Decimal("40")
    assert Decimal(destination["unknown_basis_quantity"]) == Decimal("1")
    assert destination["performance_basis"] is None and destination["missing_links"]
    sold = checked(await v.client.post(f"/api/assets/{v.c.id}/transactions", headers=v.headers, json={
        "kind": "sell", "quantity": "3", "price": "30", "date": "2025-03-01",
    }), 201)
    assert sold["realized_gain"] is None
    current = checked(await v.client.get(f"{PREFIX}/transfers/{moved['id']}", headers=v.headers))
    disposition = effect(current, v.c)
    assert disposition["realized_gain"] is None
    assert Decimal(disposition["unknown_disposition_quantity"]) > 0


@pytest.mark.parametrize("payer", ["source", "other_owned", "external"])
async def test_network_fee_debits_actual_payer_once_and_never_principal_twice(transfers, payer):
    v = transfers
    payer_asset = v.a if payer == "source" else v.c
    payer_address = v.addresses[payer_asset.id] if payer != "external" else "D" * 44
    if payer == "other_owned":
        await add_acquisition(v.session, v.c, quantity="1", price="5")
    request = await pair(v, v.a, v.b)
    fee = await retain_movement(
        v.session, payer_asset, direction="out", reference="synthetic-transfer",
        source=payer_address, destination=None, quantity="0.02", leg_key="meta.fee",
        classification="fee", quantity_role="network_fee", fee_payer=payer_address, fee_semantics="separate",
    )
    request = await select_lots(v, request)
    if payer != "external":
        lots = checked(await v.client.get(f"{PREFIX}/transfers/lots", headers=v.headers, params={
            "asset_id": str(payer_asset.id), "before_leg_id": str(fee.id),
        }))["lots"]
        request["fees"] = [{
            "leg_id": str(fee.id), "asset_id": str(payer_asset.id), "ownership_id": v.ownership[payer_asset.id],
            "allocations": [{"lot_id": lots[0]["lot_id"], "quantity": "0.02"}],
            "reason": "Exact observed payer and independent network fee",
        }]
    else:
        invalid_fee = {
            "leg_id": str(fee.id), "asset_id": str(payer_asset.id), "ownership_id": v.ownership[payer_asset.id],
            "reason": "Cannot charge sponsor fee to a watched account",
        }
        denied = checked(await v.client.post(f"{PREFIX}/movements/preview", headers=v.headers, json=invalid_fee))
        assert not denied["can_confirm"]
    result = await confirm_transfer(v, request)
    repeated = checked(await v.client.post(f"{PREFIX}/transfers", headers=v.headers, json={
        **request, "expected_revision": result["revision"],
    }))
    assert repeated["id"] == result["id"]
    for asset, units in [(v.a, "6.98" if payer == "source" else "7"), (v.b, "3"), (v.c, "0.98" if payer == "other_owned" else "0")]:
        await v.session.refresh(asset)
        assert asset.units.quantize(Decimal("1e-9")) == Decimal(units)
    ledger = list(await v.session.scalars(select(AssetTransaction)))
    assert sum(tx.kind == "fee" for tx in ledger) == (payer != "external")
    assert all(tx.kind != "sell" for tx in ledger)
    assert Decimal(effect(result, v.b)["known_acquisition_cost"]) == Decimal("60")


async def test_external_reported_incident_preserves_lineage_without_internal_or_tax_treatment(transfers):
    v = transfers
    hop = await confirm_transfer(v, await select_lots(v, await pair(v, v.a, v.b)))
    outgoing = await retain_movement(
        v.session, v.b, direction="out", reference="synthetic-external-payment", quantity="2",
        source=v.addresses[v.b.id], destination="E" * 44, when="2025-02-04T12:00:00+00:00",
    )
    lots = checked(await v.client.get(f"{PREFIX}/transfers/lots", headers=v.headers, params={
        "asset_id": str(v.b.id), "before_leg_id": str(outgoing.id),
    }))["lots"]
    applied = await apply_movement(v, v.b, outgoing, allocations=[{"lot_id": lots[0]["lot_id"], "quantity": "2"}])
    groups_before = len(list(await v.session.scalars(select(AssetGroup))))
    result = checked(await v.client.post(f"{PREFIX}/incidents", headers=v.headers, json={
        "leg_id": str(outgoing.id), "allegation": "reported_scam", "source_status": "user_reported",
        "note": "Synthetic report; recipient attribution and tax treatment remain unresolved",
    }))
    assert result["tax_treatment"] == "unresolved" and result["source_status"] == "user_reported"
    assert result["leg_id"] == str(outgoing.id)
    assert len(list(await v.session.scalars(select(AssetGroup)))) == groups_before
    assert len(list(await v.session.scalars(select(InvestmentOwnedTransfer)))) == 1
    assert Decimal(effect(applied, v.b)["quantity"]) == Decimal("1")
    assert Decimal(effect(applied, v.b)["known_realized_gain"]) == 0
    assert all(tx.kind != "sell" for tx in await v.session.scalars(select(AssetTransaction)))
    current = checked(await v.client.get(f"{PREFIX}/transfers/{hop['id']}", headers=v.headers))
    refused = await v.client.delete(f"{PREFIX}/transfers/{hop['id']}", headers=v.headers, params={"expected_revision": current["revision"]})
    assert refused.status_code == 409 and applied["id"] in refused.text


async def test_partial_selection_of_two_lots_records_cost_separately_from_average(transfers):
    v = transfers
    later = await add_acquisition(v.session, v.a, quantity="2", price="50", when=date(2025, 1, 3))
    request = await pair(v, v.a, v.b)
    preview = await transfer_preview(v, request)
    assert not preview["can_confirm"] and len(preview["available_lots"]) == 2
    by_root = {lot["root_transaction_id"]: lot for lot in preview["available_lots"]}
    selected = {**request, "allocations": [
        {"lot_id": by_root[str(v.acquisition.id)]["lot_id"], "quantity": "1"},
        {"lot_id": by_root[str(later.id)]["lot_id"], "quantity": "2"},
    ]}
    result = await confirm_transfer(v, selected)
    assert Decimal(result["acquisition_cost"]) == Decimal("120")
    assert Decimal(result["performance_basis"]) == Decimal("75")
    assert Decimal(effect(result, v.a)["known_acquisition_cost"]) == Decimal("180")
    assert Decimal(effect(result, v.a)["performance_basis"]) == Decimal("225")
    assert {lot["root_transaction_id"] for lot in effect(result, v.b)["lots"]} == {str(v.acquisition.id), str(later.id)}


async def test_transfer_after_prior_sale_carries_original_lot_and_performance_cost_separately(transfers):
    v = transfers
    later = await add_acquisition(v.session, v.a, quantity="10", price="40", when=date(2025, 1, 3))
    checked(await v.client.post(f"/api/assets/{v.a.id}/transactions", headers=v.headers, json={
        "kind": "sell", "quantity": "10", "price": "60", "date": "2025-01-04",
    }), 201)
    request = await select_lots(v, await pair(v, v.a, v.b, quantity="2"))
    result = await confirm_transfer(v, request)
    assert Decimal(result["acquisition_cost"]) == Decimal("80")
    assert Decimal(result["performance_basis"]) == Decimal("60")
    assert Decimal(effect(result, v.a)["performance_basis"]) == Decimal("240")
    assert Decimal(effect(result, v.a)["known_acquisition_cost"]) == Decimal("320")
    assert effect(result, v.b)["lots"][0]["root_transaction_id"] == str(later.id)
    assert Decimal(effect(result, v.a)["realized_gain"]) == Decimal("300")
    assert Decimal(effect(result, v.b)["realized_gain"]) == Decimal("0")


async def test_fully_disposed_unknown_history_does_not_poison_a_new_known_position(transfers):
    v = transfers
    unknown = await retain_movement(
        v.session, v.b, direction="in", reference="synthetic-old-unknown", quantity="1",
        source="D" * 44, destination=v.addresses[v.b.id],
    )
    application = await apply_movement(v, v.b, unknown)
    checked(await v.client.post(f"/api/assets/{v.b.id}/transactions", headers=v.headers, json={
        "kind": "sell", "quantity": "1", "price": "30", "date": "2025-03-01",
    }), 201)
    reopened = checked(await v.client.post(f"/api/assets/{v.b.id}/transactions", headers=v.headers, json={
        "kind": "buy", "quantity": "2", "price": "5", "date": "2025-03-02",
    }), 201)
    assert Decimal(str(reopened["units"])) == Decimal("2")
    assert Decimal(str(reopened["purchase_price"])) == Decimal("10")
    assert Decimal(str(reopened["average_price"])) == Decimal("5")
    assert reopened["realized_gain"] is None
    index = checked(await v.client.get(f"{PREFIX}/transfers", headers=v.headers))
    current = next(row for row in index["applications"] if row["id"] == application["id"])
    assert Decimal(effect(current, v.b)["performance_basis"]) == Decimal("10")
    assert Decimal(effect(current, v.b)["unknown_disposition_quantity"]) == Decimal("1")


async def test_failed_principal_allows_only_its_actual_settled_fee(transfers):
    v = transfers
    request = await pair(v, v.a, v.b)
    principal = await v.session.get(InvestmentLeg, uuid.UUID(request["out_leg_id"]))
    source = await v.session.get(InvestmentObservation, principal.observation_id)
    source.payload = {**source.payload, "provider_status": "failed", "settlement_status": "failed"}
    await v.session.commit()
    preview = await transfer_preview(v, request)
    assert not preview["can_confirm"]
    fee = await retain_movement(
        v.session, v.a, direction="out", reference="synthetic-failed-transaction", quantity="0.02",
        source=v.addresses[v.a.id], destination=None, leg_key="meta.fee", classification="fee",
        quantity_role="network_fee", fee_payer=v.addresses[v.a.id], fee_semantics="separate",
    )
    fee_source = await v.session.get(InvestmentObservation, fee.observation_id)
    fee_source.payload = {**fee_source.payload, "provider_status": "failed"}
    await v.session.commit()
    lots = checked(await v.client.get(f"{PREFIX}/transfers/lots", headers=v.headers, params={
        "asset_id": str(v.a.id), "before_leg_id": str(fee.id),
    }))["lots"]
    await apply_movement(v, v.a, fee, allocations=[{"lot_id": lots[0]["lot_id"], "quantity": "0.02"}])
    await v.session.refresh(v.a)
    await v.session.refresh(v.b)
    assert (v.a.units.quantize(Decimal("1e-9")), v.b.units) == (Decimal("9.98"), Decimal("0"))
    ledger = list(await v.session.scalars(select(AssetTransaction)))
    assert sorted(tx.kind for tx in ledger) == ["buy", "fee"]


async def test_principal_plus_fee_cannot_overconsume_the_last_source_units(transfers):
    v = transfers
    request = await select_lots(v, await pair(v, v.a, v.b, quantity="10"))
    fee = await retain_movement(
        v.session, v.a, direction="out", reference="synthetic-transfer", quantity="0.02",
        source=v.addresses[v.a.id], destination=None, leg_key="meta.fee", classification="fee",
        quantity_role="network_fee", fee_payer=v.addresses[v.a.id], fee_semantics="separate",
    )
    request["fees"] = [{
        "leg_id": str(fee.id), "asset_id": str(v.a.id), "ownership_id": v.ownership[v.a.id],
        "allocations": [{"lot_id": request["allocations"][0]["lot_id"], "quantity": "0.02"}],
        "reason": "Separately observed fee exceeds the available principal plus fee inventory",
    }]
    preview = await transfer_preview(v, request)
    response = await v.client.post(f"{PREFIX}/transfers", headers=v.headers, json={**request, "expected_revision": preview["revision"]})
    assert response.status_code in {409, 422}, response.text
    await v.session.refresh(v.a)
    await v.session.refresh(v.b)
    assert (v.a.units, v.b.units) == (Decimal("10"), Decimal("0"))


async def test_preview_reports_both_prospective_holdings_without_any_write(transfers):
    v = transfers
    request = await select_lots(v, await pair(v, v.a, v.b))
    before = list(await v.session.scalars(select(AssetTransaction.id)))
    preview = await transfer_preview(v, request)
    assert Decimal(effect(preview, v.a)["quantity"]) == Decimal("7")
    assert Decimal(effect(preview, v.b)["quantity"]) == Decimal("3")
    assert Decimal(effect(preview, v.b)["known_acquisition_cost"]) == Decimal("60")
    assert list(await v.session.scalars(select(AssetTransaction.id))) == before
    assert list(await v.session.scalars(select(InvestmentOwnedTransfer))) == []
    await v.session.refresh(v.a)
    await v.session.refresh(v.b)
    assert (v.a.units, v.b.units) == (Decimal("10"), Decimal("0"))


@pytest.mark.parametrize("missing", ["provider_status", "network_status"])
async def test_null_status_is_a_visible_candidate_in_index_and_preview(transfers, missing):
    v = transfers
    request = await pair(v, v.a, v.b)
    leg = await v.session.get(InvestmentLeg, uuid.UUID(request["in_leg_id"]))
    observation = await v.session.get(InvestmentObservation, leg.observation_id)
    observation.payload = {**observation.payload, missing: None, "settlement_status": "unknown"}
    await v.session.commit()
    index = checked(await v.client.get(f"{PREFIX}/transfers", headers=v.headers))
    movement = next(item for item in index["movements"] if item["leg_id"] == str(leg.id))
    assert movement[missing] is None and movement["reason_codes"]
    preview = await transfer_preview(v, request)
    assert not preview["can_confirm"] and preview["reason_codes"]


@pytest.mark.parametrize("mode", ["orders", "evidence", "undo_orders", "undo_evidence"])
async def test_import_and_undo_cannot_bypass_recorded_allocation_dependencies(transfers, mode):
    from app.models.import_log import ImportLog
    from tests.test_investment_evidence import observation

    v = transfers
    log = ImportLog(
        workspace_id=v.workspace.id, user_id=v.user.id,
        entity="asset_evidence" if mode == "undo_evidence" else "asset_orders",
        filename="synthetic-acquisition.csv", format="csv", transaction_count=1,
    )
    v.session.add(log)
    await v.session.flush()
    v.acquisition.import_id = log.id
    await v.session.commit()
    await confirm_transfer(v, await select_lots(v, await pair(v, v.a, v.b)))
    before = set(await v.session.scalars(select(AssetTransaction.id)))
    if mode.startswith("undo"):
        response = await v.client.delete(f"/api/import-logs/{log.id}", headers=v.headers)
    elif mode == "orders":
        response = await v.client.post("/api/assets/import", headers=v.headers, json={
            "mode": "orders", "group_id": str(v.a.group_id), "allow_unpriced": True,
            "orders": [{"row": 1, "ticker": "SYN", "kind": "buy", "quantity": "1", "price": "999", "fee": "0", "date": "2025-01-01"}],
        })
    else:
        item = observation("synthetic-backdated-acquisition", quantity="1", price="999")
        item.event_date = date(2025, 1, 1)
        item.event_at = datetime.fromisoformat("2025-01-01T12:00:00+00:00")
        item.event_time_raw = item.event_at.isoformat()
        item.legs[0].asset_id = v.a.id
        item.legs[0].provider_asset_id = None
        item.legs[0].chain = "solana"
        item.legs[0].token_address = "native"
        preview = await evidence.preview_evidence(v.session, v.workspace.id, v.a.group_id, [item])
        saved = await evidence.import_evidence(v.session, v.workspace.id, v.user.id, v.a.group_id, [item], expected_revision=preview.revision)
        stored = next(o for o in saved.evidence.observations if o.source_local_id == item.source_local_id)
        response = await v.client.post(f"{PREFIX}/confirm", headers=v.headers, json={
            "group_id": str(v.a.group_id), "allow_unpriced": True, "expected_revision": saved.evidence.revision,
            "decisions": [{"observation_ref": stored.reference, "leg_key": "amount", "action": "apply"}],
        })
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["dependencies"]
    assert set(await v.session.scalars(select(AssetTransaction.id))) == before
    await v.session.refresh(v.a)
    await v.session.refresh(v.b)
    assert (v.a.units, v.b.units) == (Decimal("7"), Decimal("3"))


async def test_overlapping_source_account_names_cannot_double_apply_one_physical_movement(transfers):
    v = transfers
    first_request = await select_lots(v, await pair(v, v.a, v.b))
    first = await confirm_transfer(v, first_request)
    alias_legs = []
    for field, asset in [("out_leg_id", v.a), ("in_leg_id", v.b)]:
        original = await v.session.get(InvestmentLeg, uuid.UUID(first_request[field]))
        observed = await v.session.get(InvestmentObservation, original.observation_id)
        item = EvidenceObservationInput.model_validate({
            **observed.payload, "reference": f"synthetic-alias:{asset.id}", "source": "mapped_export",
            "source_account_id": f"alternate-export-account:{asset.id}",
            "source_local_id": f"alternate-export-row:{asset.id}", "source_locator": "synthetic/alternate-export.csv",
        })
        preview = await evidence.preview_evidence(v.session, v.workspace.id, asset.group_id, [item])
        saved = await evidence.import_evidence(v.session, v.workspace.id, v.user.id, asset.group_id, [item], expected_revision=preview.revision)
        stored = next(o for o in saved.evidence.observations if o.source_local_id == item.source_local_id)
        alias_legs.append(await v.session.scalar(select(InvestmentLeg).where(InvestmentLeg.observation_id == uuid.UUID(stored.reference))))
    alias = {**first_request, "out_leg_id": str(alias_legs[0].id), "in_leg_id": str(alias_legs[1].id)}
    preview = await transfer_preview(v, alias)
    response = await v.client.post(f"{PREFIX}/transfers", headers=v.headers, json={**alias, "expected_revision": preview["revision"]})
    if response.status_code == 200:
        assert response.json()["id"] == first["id"]
    else:
        assert response.status_code in {409, 422}, response.text
    assert len(list(await v.session.scalars(select(InvestmentOwnedTransfer)))) == 1
    await v.session.refresh(v.a)
    await v.session.refresh(v.b)
    assert (v.a.units, v.b.units) == (Decimal("7"), Decimal("3"))
    assert len(list(await v.session.scalars(select(InvestmentObservation)))) == 4


async def test_actual_collector_outputs_confirm_with_fee_and_reobserve_to_unresolved(
    session, test_workspace, test_user, client, auth_headers, monkeypatch,
):
    from app.models.account import Account
    from app.models.bank_connection import BankConnection
    from app.providers.onchain import ACCOUNT_EXTERNAL_ID

    _, payloads = rpc_fixture(monkeypatch)
    headers = {**auth_headers, "X-Workspace-Id": str(test_workspace.id)}
    v = SimpleNamespace(session=session, client=client, headers=headers, ownership={}, addresses={})
    assets, archives, groups = [], [], []
    for name in "AB":
        address = name * 44
        asset = await make_holding(session, test_workspace.id, test_user.id, name)
        group = await session.get(AssetGroup, asset.group_id)
        connection = BankConnection(
            workspace_id=test_workspace.id, user_id=test_user.id, provider="onchain",
            external_id=f"synthetic-collector-{name}", institution_name=f"Synthetic {name}",
            credentials={"addresses": [f"solana:{address}"]},
        )
        session.add(connection)
        await session.flush()
        account = Account(
            workspace_id=test_workspace.id, user_id=test_user.id, connection_id=connection.id,
            external_id=ACCOUNT_EXTERNAL_ID, name=f"Synthetic {name}", type="investment",
            balance=Decimal("19.25"), currency="USD",
        )
        session.add(account)
        await session.flush()
        group.connection_id, group.source = connection.id, "onchain"
        group.external_id = f"{connection.external_id}::{ACCOUNT_EXTERNAL_ID}"
        await session.commit()
        v.addresses[asset.id] = address
        v.ownership[asset.id] = (await assert_ownership(v, asset, address))["id"]
        saved = checked(await client.post("/api/onchain/history", headers=headers, json={
            "connection_id": str(connection.id), "chain": "solana", "address": address,
            "ownership_confirmed": True,
        }))
        assets.append(asset)
        groups.append(group)
        archives.append(saved)
    a, b = assets
    await add_acquisition(session, a, quantity="15")
    retained = list((await session.execute(select(InvestmentLeg, InvestmentObservation.group_id).join(
        InvestmentObservation, InvestmentLeg.observation_id == InvestmentObservation.id,
    ))).all())
    out = next(leg for leg, group in retained if group == a.group_id and leg.payload.get("transaction_ref") == "synthetic-send" and leg.payload.get("classification") == "transfer")
    incoming = next(leg for leg, group in retained if group == b.group_id and leg.payload.get("transaction_ref") == "synthetic-send" and leg.payload.get("classification") == "transfer")
    fee = next(leg for leg, group in retained if group == a.group_id and leg.payload.get("transaction_ref") == "synthetic-send" and leg.payload.get("classification") == "fee")
    request = {
        "out_leg_id": str(out.id), "in_leg_id": str(incoming.id), "source_asset_id": str(a.id), "destination_asset_id": str(b.id),
        "source_ownership_id": v.ownership[a.id], "destination_ownership_id": v.ownership[b.id],
        "reason": "Reviewed exact retained collector instruction and actual payer",
    }
    request = await select_lots(v, request)
    request["fees"] = [{
        "leg_id": str(fee.id), "asset_id": str(a.id), "ownership_id": v.ownership[a.id],
        "allocations": [{"lot_id": request["allocations"][0]["lot_id"], "quantity": "0.02"}],
        "reason": "Observed one network fee paid by A",
    }]
    result = await confirm_transfer(v, request)
    assert Decimal(effect(result, a)["quantity"]).quantize(Decimal("1e-9")) == Decimal("11.98")
    assert Decimal(effect(result, b)["quantity"]) == Decimal("3")
    assert Decimal(effect(result, b)["known_acquisition_cost"]) == Decimal("60")
    financial_ids = set(await session.scalars(select(AssetTransaction.id)))
    payloads["synthetic-send"]["meta"]["postBalances"][0] -= 1
    updated = checked(await client.post("/api/onchain/history", headers=headers, json={
        **archives[0]["request"], "collection_id": archives[0]["collection_id"],
        "expected_revision": archives[0]["revision"], "reobserve": True,
    }))
    assert updated["evidence"]["transactions"]["solana:synthetic-send"]["canonical_version"] is None
    detail = checked(await client.get(f"{PREFIX}/transfers/{result['id']}", headers=headers))
    assert detail["status"] == "unresolved" and detail["performance_basis"] is None
    assert set(await session.scalars(select(AssetTransaction.id))) == financial_ids
    assert all(lot["source_locator"] for lot in checked(await client.get(f"{PREFIX}/transfers", headers=headers))["movements"])


@pytest.mark.parametrize("csv_first", [False, True])
async def test_actual_provider_and_mapped_receipt_confirm_without_redebiting_synced_snapshot(venue, csv_first):
    v = venue
    source_address, destination_address = "A" * 44, "B" * 44
    withdrawal = _api_row(
        "synthetic-provider-send", kind="send", quantity="-3", amount="-999",
        created_at="2026-01-03T12:00:00Z",
        network={"chain": "solana", "hash": "synthetic-provider-transfer", "status": "finalized", "leg_ref": "ix:0", "fee_semantics": "none"},
        **{"from": {"address": source_address}, "to": {"address": destination_address}},
    )
    withdrawal["amount"].update(token_address="native", raw_units="3000000000", decimals=9)
    v.history[WALLET].append(withdrawal)
    v.accounts[0]["balance"]["amount"] = "9"
    destination = await make_holding(v.session, v.workspace.id, v.user.id, "Destination")
    csv_row = {
        "ID": "synthetic-receipt-row", "Asset": "SYN", "Date": "2026-01-03T12:00:00Z",
        "Quantity": "3", "Kind": "Receive", "Status": "completed", "Network Status": "finalized",
        "Chain": "solana", "Token Address": "native", "Transaction Hash": "synthetic-provider-transfer",
        "Leg ID": "ix:0", "Source Address": source_address, "Destination Address": destination_address,
        "Source Owner": source_address, "Destination Owner": destination_address,
        "Raw Units": "3000000000", "Decimals": "9", "Quantity Role": "principal", "Fee Semantics": "none",
    }

    async def upload_receipt():
        preview = checked(await v.client.post("/api/assets/import/preview", headers=v.headers,
            data={"mode": "evidence", "provider": "onchain", "group_id": str(destination.group_id), "source_account_id": destination_address},
            files={"file": ("synthetic-receipt.csv", _csv_bytes([csv_row]), "text/csv")},
        ))
        assert preview["errors"] == [], preview
        return checked(await v.client.post("/api/assets/import", headers=v.headers, json={
            "mode": "evidence", "group_id": str(destination.group_id), "filename": "synthetic-receipt.csv",
            "observations": preview["evidence"]["observations"], "expected_revision": preview["evidence"]["revision"],
        }))

    if csv_first:
        await upload_receipt()
    await _sync(v)
    if not csv_first:
        await upload_receipt()
    source = next(asset for asset in await _rows(v, Asset) if asset.connection_id == v.connection.id)
    assert source.units == Decimal("9")
    v.ownership = {
        source.id: (await assert_ownership(v, source, None, source_account_id=WALLET))["id"],
        destination.id: (await assert_ownership(v, destination, destination_address))["id"],
    }
    rows = list((await v.session.execute(select(InvestmentLeg, InvestmentObservation).join(
        InvestmentObservation, InvestmentLeg.observation_id == InvestmentObservation.id,
    ))).all())
    out = next(leg for leg, observation in rows if observation.payload["source_local_id"] == "synthetic-provider-send")
    incoming = next(leg for leg, observation in rows if observation.payload["source_local_id"] == "synthetic-receipt-row")
    request = await select_lots(v, {
        "out_leg_id": str(out.id), "in_leg_id": str(incoming.id), "source_asset_id": str(source.id),
        "destination_asset_id": str(destination.id), "source_ownership_id": v.ownership[source.id],
        "destination_ownership_id": v.ownership[destination.id], "reason": "Reviewed provider account withdrawal and exact mapped receipt",
    })
    result = await confirm_transfer(v, request)
    assert Decimal(effect(result, source)["quantity"]) == Decimal("9")
    assert Decimal(effect(result, destination)["quantity"]) == Decimal("3")
    assert Decimal(result["acquisition_cost"]) == Decimal("21")
    for _ in range(2):
        await _sync(v)
        await upload_receipt()
    await v.session.refresh(source)
    await v.session.refresh(destination)
    assert (source.units, destination.units) == (Decimal("9"), Decimal("3"))
    assert len(list(await v.session.scalars(select(InvestmentOwnedTransfer)))) == 1
    assert len(list(await v.session.scalars(select(AssetTransaction)))) == 3
    assert source.purchase_price == Decimal("63") and destination.purchase_price == Decimal("21")
    # A later provider snapshot can expose missing movements without rewriting its units.
    v.accounts[0]["balance"]["amount"] = "8"
    await _sync(v)
    await v.session.refresh(source)
    assert source.units == Decimal("8")
    index = checked(await v.client.get(f"{PREFIX}/transfers", headers=v.headers))
    assert Decimal(next(row for row in index["holdings"] if row["id"] == str(source.id))["units"]) == Decimal("8")
    assert Decimal(next(row for row in index["transfers"] if row["id"] == result["id"])["acquisition_cost"]) == Decimal("21")
    invalidated = await v.session.get(InvestmentObservation, out.observation_id)
    invalidated.is_current = False
    await v.session.commit()
    detail = checked(await v.client.get(f"{PREFIX}/transfers/{result['id']}", headers=v.headers))
    assert detail["status"] == "unresolved" and detail["performance_basis"] is None
    await v.session.refresh(source)
    assert source.units == Decimal("8")


@pytest.mark.parametrize("unknown_taxable", [False, True])
async def test_reportable_and_excluded_unknown_gains_have_independent_completeness(transfers, unknown_taxable):
    v = transfers
    unknown_group = await v.session.get(AssetGroup, v.b.group_id)
    known_group = await v.session.get(AssetGroup, v.a.group_id)
    unknown_group.tax_treatment = "taxable" if unknown_taxable else "roth"
    known_group.tax_treatment = "roth" if unknown_taxable else "taxable"
    await v.session.commit()
    unknown = await retain_movement(
        v.session, v.b, direction="in", reference="synthetic-tax-unknown", quantity="1",
        source="D" * 44, destination=v.addresses[v.b.id],
    )
    await apply_movement(v, v.b, unknown)
    for asset in [v.a, v.b]:
        checked(await v.client.post(f"/api/assets/{asset.id}/transactions", headers=v.headers, json={
            "kind": "sell", "quantity": "1", "price": "30", "date": "2025-03-01",
        }), 201)
    report = checked(await v.client.get("/api/assets/reportable-gain", headers=v.headers))
    if unknown_taxable:
        assert report["reportable_gain"] is None and not report["basis_complete"]
        assert report["known_reportable_gain"] == 0
        assert report["non_reportable_gain"] == 10 and report["non_reportable_basis_complete"]
    else:
        assert report["non_reportable_gain"] is None and not report["non_reportable_basis_complete"]
        assert report["known_non_reportable_gain"] == 0
        assert report["reportable_gain"] == 10 and report["basis_complete"]


async def test_two_principals_share_one_network_fee_application(transfers):
    v = transfers
    first = await pair(v, v.a, v.b, quantity="2", reference="synthetic-multileg")
    second_legs = []
    for direction, asset in [("out", v.a), ("in", v.c)]:
        second_legs.append(await retain_movement(
            v.session, asset, direction=direction, reference="synthetic-multileg", quantity="1",
            source=v.addresses[v.a.id], destination=v.addresses[v.c.id], leg_key="ix:1",
        ))
    second = {
        "out_leg_id": str(second_legs[0].id), "in_leg_id": str(second_legs[1].id),
        "source_asset_id": str(v.a.id), "destination_asset_id": str(v.c.id),
        "source_ownership_id": v.ownership[v.a.id], "destination_ownership_id": v.ownership[v.c.id],
        "reason": "Separate instruction in the same network transaction",
    }
    first, second = await select_lots(v, first), await select_lots(v, second)
    fee = await retain_movement(
        v.session, v.a, direction="out", reference="synthetic-multileg", quantity="0.02",
        source=v.addresses[v.a.id], destination=None, leg_key="meta.fee", classification="fee",
        quantity_role="network_fee", fee_payer=v.addresses[v.a.id], fee_semantics="separate",
    )
    fee_selection = {
        "leg_id": str(fee.id), "asset_id": str(v.a.id), "ownership_id": v.ownership[v.a.id],
        "allocations": [{"lot_id": first["allocations"][0]["lot_id"], "quantity": "0.02"}],
        "reason": "One transaction fee shared by two principal instructions",
    }
    first["fees"], second["fees"] = [fee_selection], [fee_selection]
    await confirm_transfer(v, first)
    await confirm_transfer(v, second)
    for asset, units in [(v.a, "6.98"), (v.b, "2"), (v.c, "1")]:
        await v.session.refresh(asset)
        assert asset.units.quantize(Decimal("1e-9")) == Decimal(units)
    fees = list(await v.session.scalars(select(AssetTransaction).where(AssetTransaction.kind == "fee")))
    assert len(fees) == 1
    assert fees[0].quantity == Decimal("0.02")
    assert len(list(await v.session.scalars(select(InvestmentOwnedTransfer)))) == 2


async def test_contradictory_fee_payer_cannot_debit_the_selected_owned_holding(transfers):
    v = transfers
    fee = await retain_movement(
        v.session, v.a, direction="out", reference="synthetic-contradictory-payer", quantity="0.02",
        source=v.addresses[v.a.id], destination=None, leg_key="meta.fee", classification="fee",
        quantity_role="network_fee", fee_payer="D" * 44, fee_semantics="separate",
    )
    lots = checked(await v.client.get(f"{PREFIX}/transfers/lots", headers=v.headers, params={
        "asset_id": str(v.a.id), "before_leg_id": str(fee.id),
    }))["lots"]
    request = {
        "leg_id": str(fee.id), "asset_id": str(v.a.id), "ownership_id": v.ownership[v.a.id],
        "allocations": [{"lot_id": lots[0]["lot_id"], "quantity": "0.02"}],
        "reason": "Contradictory imported payer cannot authorize a debit",
    }
    preview = checked(await v.client.post(f"{PREFIX}/movements/preview", headers=v.headers, json=request))
    assert not preview["can_confirm"] and preview["reason_codes"]
    response = await v.client.post(f"{PREFIX}/movements", headers=v.headers, json={**request, "expected_revision": preview["revision"]})
    assert response.status_code in {409, 422}, response.text
    await v.session.refresh(v.a)
    assert v.a.units == Decimal("10")


async def test_pooled_continuation_is_evidence_without_user_inventory_or_loss_attribution(transfers):
    v = transfers
    await confirm_transfer(v, await select_lots(v, await pair(v, v.a, v.b)))
    outgoing = await retain_movement(
        v.session, v.b, direction="out", reference="synthetic-user-external", quantity="2",
        source=v.addresses[v.b.id], destination="E" * 44, when="2025-02-04T12:00:00+00:00",
    )
    annotated = checked(await v.client.post(f"{PREFIX}/incidents", headers=v.headers, json={
        "leg_id": str(outgoing.id), "note": "Synthetic reported scam; no pooled attribution",
    }))
    pooled = await retain_movement(
        v.session, v.b, direction="out", reference="synthetic-larger-pool-payment", quantity="200",
        source="E" * 44, destination="F" * 44, when="2025-02-05T12:00:00+00:00",
    )
    preview = checked(await v.client.post(f"{PREFIX}/movements/preview", headers=v.headers, json={
        "leg_id": str(pooled.id), "asset_id": str(v.b.id), "ownership_id": v.ownership[v.b.id],
        "reason": "Observed external continuation does not establish ownership",
    }))
    assert not preview["can_confirm"]
    index = checked(await v.client.get(f"{PREFIX}/transfers", headers=v.headers))
    incident = next(row for row in index["incidents"] if row["id"] == annotated["id"])
    assert incident["leg_id"] == str(outgoing.id) and incident["tax_treatment"] == "unresolved"
    assert len(index["transfers"]) == 1 and len(index["ownership"]) == 3
    assert len(index["holdings"]) == 3
    await v.session.refresh(v.b)
    assert v.b.units == Decimal("3")


async def make_holding(session, workspace_id, user_id, name, *, units="0"):
    group = AssetGroup(
        id=uuid.uuid4(), workspace_id=workspace_id, user_id=user_id,
        name=f"Synthetic {name}", tax_treatment="taxable",
    )
    session.add(group)
    await session.flush()
    asset = Asset(
        id=uuid.uuid4(), workspace_id=workspace_id, user_id=user_id, group_id=group.id,
        name=f"Synthetic {name} coin", type="crypto", currency="USD", ticker="SYN",
        valuation_method="market_price", units=Decimal(units), last_price=Decimal("99"),
    )
    session.add(asset)
    await session.commit()
    return asset


async def add_acquisition(session, asset, *, quantity="10", price="20", when=date(2025, 1, 2)):
    from app.services.asset_transaction_service import recompute_and_cache

    tx = AssetTransaction(
        id=uuid.uuid4(), asset_id=asset.id, workspace_id=asset.workspace_id,
        kind="buy", quantity=Decimal(quantity), price=Decimal(price),
        date=when, fee=Decimal("0"), source="manual",
    )
    session.add(tx)
    await session.flush()
    await recompute_and_cache(session, asset)
    await session.commit()
    return tx


@pytest.mark.parametrize("csv_first", [False, True])
async def test_provider_and_mapped_csv_withdrawal_retain_source_without_disposal(venue, csv_first):
    v = venue
    withdrawal = _api_row(
        "synthetic-withdrawal", quantity="-3", amount="-99", kind="send",
        network={"hash": "synthetic-chain-transfer", "status": "confirmed"},
    )
    v.history[next(iter(v.history))].append(withdrawal)
    row = _csv_row("synthetic-export-withdrawal", Kind="Send", Quantity="-3", Price="", **{
        "Transaction Hash": "synthetic-chain-transfer", "Total": "99",
    })
    if csv_first:
        await _upload(v, [row])
    await _sync(v)
    if not csv_first:
        await _upload(v, [row])
    await _sync(v)
    await _upload(v, [row])
    preview = await _evidence(v)
    sources = {o["source_local_id"] for o in preview["observations"]}
    assert {"synthetic-withdrawal", "synthetic-export-withdrawal"} <= sources
    txs = await _rows(v, AssetTransaction)
    assert len(txs) == 1 and txs[0].kind == "buy"
    assert txs[0].quantity == Decimal("12")
    asset = (await _rows(v, Asset))[0]
    assert asset.units == Decimal("12")
    for observation in preview["observations"]:
        if observation["source_local_id"] in {"synthetic-withdrawal", "synthetic-export-withdrawal"}:
            assert all(leg["acquisition_basis"] is None for leg in observation["legs"])


async def test_collector_keeps_exact_principal_and_fee_evidence_without_applying_holdings(
    client, auth_headers, session, history_context, monkeypatch,
):
    connection, account, _ = history_context
    _, payloads = rpc_fixture(monkeypatch)
    response = await client.post("/api/onchain/history", headers=auth_headers, json={
        "connection_id": str(connection.id), "chain": "solana", "address": A,
        "ownership_confirmed": True,
    })
    assert response.status_code == 200, response.text
    archive = response.json()["evidence"]
    transaction = archive["transactions"]["solana:synthetic-send"]
    version = next(v for v in transaction["versions"] if v["version_id"] == transaction["canonical_version"])
    assert version["settlement"] == "settled"
    assert payloads["synthetic-send"]["meta"]["fee"] == 20_000_000
    assert {Decimal(leg["quantity"]) for leg in version["legs"]} >= {Decimal("3"), Decimal("0.02")}
    assert await session.scalar(select(AssetTransaction.id).limit(1)) is None
    assert await session.scalar(select(Asset.id).limit(1)) is None
    assert len(list(await session.scalars(select(InvestmentObservation)))) == 2
    await session.refresh(account)
    assert account.balance == Decimal("19.25")
