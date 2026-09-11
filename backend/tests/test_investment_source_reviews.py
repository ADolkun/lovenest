"""Synthetic source semantics, canonical correction, replay and refusal boundaries."""
import copy
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

from app.models.asset import Asset
from app.models.asset_value import AssetValue
from app.models.bank_connection import BankConnection
from app.models.asset_transaction import AssetTransaction
from app.models.investment_evidence import InvestmentLeg, InvestmentObservation, InvestmentSourceReview
from app.schemas.asset import AssetTransactionCreate, AssetTransactionUpdate
from app.schemas.investment_evidence import EvidenceAllocation, EvidenceDecision
from app.schemas.investment_source_review import SourceReviewConfirm, SourceReviewRequest, SourceSemantics
from app.services import asset_transaction_service as ledger
from app.services import investment_evidence_service as evidence
from app.services import investment_source_review_service as service
from tests.test_investment_evidence import apply, observation, save, wallet as wallet_fixture

wallet = wallet_fixture


async def confirm(session, workspace_id, request):
    preview = await service.preview_review(session, workspace_id, request)
    assert preview.supported, preview.blockers
    data = SourceReviewConfirm(request=request, expected_revision=preview.revision, preview_digest=preview.preview_digest)
    return preview, data


@pytest_asyncio.fixture
async def correction_case(session, test_workspace, test_user, wallet, request):
    options = getattr(request, "param", {})
    connection = None
    if options.get("provider"):
        connection = BankConnection(workspace_id=test_workspace.id, user_id=test_user.id, provider="coinbase", external_id="synthetic-connection", institution_name="Synthetic exchange", credentials={}, status="active")
        session.add(connection)
        await session.flush()
        wallet.connection_id = connection.id
        await session.commit()
    original = observation("api-original", source="coinbase_api")
    if "programs" in options:
        original.legs[0].chain, original.legs[0].token_address = "solana", "synthetic-mint"
        original.legs[0].token_program = options["programs"][0]
    first = await save(session, test_workspace, test_user, wallet, [original])
    await apply(session, test_workspace, test_user, wallet, first.evidence)
    if connection:
        holding = (await session.scalars(select(Asset))).one()
        holding.connection_id = connection.id
        holding.units = Decimal(options.get("reported_quantity", "12"))
        holding.current_value = Decimal("999")
        holding.last_price = Decimal("55")
        await session.commit()
    target_id = (await session.scalars(select(InvestmentLeg))).one().id
    if "programs" in options:
        holding = (await session.scalars(select(Asset))).one()
        holding.external_metadata = {"evidence_asset_identity": {
            "provider_asset_id": "currency-SYN", "chain": "solana", "token_address": "synthetic-mint",
            "token_program": options["programs"][2],
        }}
        await session.commit()
    if options.get("alias"):
        alias = original.model_copy(deep=True)
        alias.source, alias.source_local_id = "csv", "canonical-alias"
        alias.legs[0].token_program = None
        saved = await save(session, test_workspace, test_user, wallet, [alias])
        alias_ref = next(row.reference for row in saved.evidence.observations if row.source_local_id == "canonical-alias")
        await evidence.confirm_evidence(session, test_workspace.id, test_user.id, wallet.id, [EvidenceDecision(
            observation_ref=alias_ref, leg_key="amount", action="link", reason="Synthetic canonical alias",
            allocations=[EvidenceAllocation(leg_id=target_id)],
        )], saved.evidence.revision)
        target_id = (await session.scalars(select(InvestmentLeg).where(InvestmentLeg.observation_id == uuid.UUID(alias_ref)))).one().id
    source = observation("csv-source", source="csv", quantity=options.get("quantity", "12"))
    if "programs" in options:
        source.legs[0].chain, source.legs[0].token_address = "solana", "synthetic-mint"
        source.legs[0].token_program = options["programs"][1]
    source.event_at += timedelta(seconds=10)
    if options.get("next_day"):
        source.event_date += timedelta(days=1)
        source.event_at += timedelta(days=1)
    source.event_time_raw = source.event_at.isoformat()
    source.legs[0].unit_price = source.legs[0].subtotal = source.legs[0].fee = None
    source.legs[0].total = Decimal(options.get("total", "120"))
    source.legs[0].unit_price_origin = "unknown"
    if options.get("pending"):
        source.settlement_status = "pending"
    result = await save(session, test_workspace, test_user, wallet, [source])
    sources = list((await session.scalars(select(InvestmentObservation))).all())
    legs = list((await session.scalars(select(InvestmentLeg))).all())
    tx = (await session.scalars(select(AssetTransaction))).one()
    source_row = next(row for row in sources if row.payload["source_local_id"] == "csv-source")
    target_row = next(row for row in sources if row.payload["source"] == "coinbase_api")
    request = SourceReviewRequest(
        group_id=wallet.id, request_key="association", action="associate", reason="Synthetic export documents the same acquisition",
        source_leg_id=next(row.id for row in legs if row.observation_id == source_row.id),
        target_leg_id=target_id,
        source_semantics=SourceSemantics(clock_role="posted", amount_field="total", amount_meaning="fee_inclusive_total", decimal_places=2),
        target_semantics=SourceSemantics(clock_role="execution", amount_field="unit_price", amount_meaning="valuation"),
        same_execution_reviewed=True,
    )
    return {"workspace": test_workspace, "user": test_user, "wallet": wallet, "tx": tx,
            "source": source, "source_row": source_row, "target_row": target_row, "request": request,
            "evidence": result.evidence, "sources_before": {row.id: copy.deepcopy(row.payload) for row in sources}}


async def associate(session, case):
    _, data = await confirm(session, case["workspace"].id, case["request"])
    package = await service.confirm_review(session, case["workspace"].id, case["user"].id, data)
    return package.reviews[-1].id


def correction(case, association_id, **changes):
    return SourceReviewRequest(group_id=case["wallet"].id, request_key=changes.pop("request_key", "correction"),
                              action=changes.pop("action", "correct"), reason="Use the documented acquisition total", review_id=association_id, **changes)


@pytest.mark.asyncio
async def test_documentary_association_keeps_conflicts_and_zero_financial_effect(session, correction_case):
    c = correction_case
    before = service._image(c["tx"])
    preview, data = await confirm(session, c["workspace"].id, c["request"])
    assert list(await session.scalars(select(InvestmentSourceReview))) == []
    assert preview.effects["units_delta"] == "0"
    assert preview.sources[0]["leg"]["total"] == "120" and preview.sources[0]["leg"]["fee"] is None
    assert preview.sources[0]["semantics"]["clock_role"] == "posted"
    await service.confirm_review(session, c["workspace"].id, c["user"].id, data)
    assert service._image(c["tx"]) == before
    view = await evidence.preview_evidence(session, c["workspace"].id, c["wallet"].id)
    record = next(row for row in view.records if row.observation_ref == str(c["source_row"].id))
    assert record.match_status == "conflicting" and "event_at" in record.conflicting_fields
    assert record.application_status == "blocked"
    with pytest.raises(HTTPException) as error:
        await evidence.confirm_evidence(session, c["workspace"].id, c["user"].id, c["wallet"].id, [EvidenceDecision(
            observation_ref=str(c["source_row"].id), leg_key="amount", action="link", reason="No equality assumed",
            allocations=[EvidenceAllocation(leg_id=c["request"].target_leg_id)],
        )], view.revision)
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_correct_preview_cancel_apply_replay_reverse_and_tombstone(session, correction_case):
    c = correction_case
    association_id = await associate(session, c)
    request = correction(c, association_id)
    preview, data = await confirm(session, c["workspace"].id, request)
    assert preview.effects["cost_before"] == "84.000000000000000000000000000000000000"
    assert Decimal(preview.effects["cost_after"]) == 120
    assert Decimal(preview.effects["cost_delta"]) == 36
    assert preview.effects["original_source_fee"] is None
    assert preview.effects["fee_treatment"] == "included_no_additional_fee"
    assert c["tx"].price == 7  # Preview/cancel do not write.
    package = await service.confirm_review(session, c["workspace"].id, c["user"].id, data)
    assert c["tx"].price == 10 and c["tx"].quantity == 12 and c["tx"].fee == 0
    assert len(list(await session.scalars(select(AssetTransaction)))) == 1
    assert len((await service.confirm_review(session, c["workspace"].id, c["user"].id, data)).reviews) == 2
    current = await evidence.preview_evidence(session, c["workspace"].id, c["wallet"].id)
    source_record = next(row for row in current.records if row.observation_ref == str(c["source_row"].id))
    assert source_record.application_status == "already_applied"
    result = await evidence.confirm_evidence(session, c["workspace"].id, c["user"].id, c["wallet"].id, [
        EvidenceDecision(observation_ref=str(c["source_row"].id), leg_key="amount", action="apply"),
    ], current.revision)
    assert result.imported == 0
    reverse = correction(c, package.reviews[-1].id, action="reverse", request_key="reverse")
    reverse_preview, reverse_data = await confirm(session, c["workspace"].id, reverse)
    assert Decimal(reverse_preview.effects["cost_delta"]) == -36
    await service.confirm_review(session, c["workspace"].id, c["user"].id, reverse_data)
    assert c["tx"].price == 7
    assert len((await service.confirm_review(session, c["workspace"].id, c["user"].id, reverse_data)).reviews) == 3
    replay = await save(session, c["workspace"], c["user"], c["wallet"], [c["source"]])
    assert replay.retained == 0
    assert next(row for row in replay.evidence.records if row.observation_ref == str(c["source_row"].id)).application_status == "already_applied"
    assert {row.id: row.payload for row in await session.scalars(select(InvestmentObservation))} == c["sources_before"]


@pytest.mark.asyncio
async def test_active_correction_blocks_edit_undo_but_later_sale_blocks_reversal(session, correction_case):
    c = correction_case
    association_id = await associate(session, c)
    _, data = await confirm(session, c["workspace"].id, correction(c, association_id))
    package = await service.confirm_review(session, c["workspace"].id, c["user"].id, data)
    with pytest.raises(HTTPException, match="Reverse the active source correction"):
        await ledger.update_transaction(session, c["tx"].id, c["workspace"].id, AssetTransactionUpdate(price=Decimal(9)))
    with pytest.raises(HTTPException, match="Reverse the active source correction"):
        await ledger.delete_transaction(session, c["tx"].id, c["workspace"].id)
    await ledger.add_transaction(session, c["tx"].asset_id, c["workspace"].id, AssetTransactionCreate(
        kind="sell", quantity=Decimal(1), price=Decimal(15), date=c["tx"].date + timedelta(days=1), fee=Decimal(0),
    ))
    preview = await service.preview_review(session, c["workspace"].id, correction(c, package.reviews[-1].id, action="reverse", request_key="reverse"))
    assert not preview.supported and "dependent_disposal_or_movement" in preview.blockers


@pytest.mark.asyncio
async def test_request_key_collision_and_stale_preview_fail_without_a_write(session, correction_case):
    c = correction_case
    workspace_id, user_id = c["workspace"].id, c["user"].id
    association_id = await associate(session, c)
    request = correction(c, association_id)
    _, data = await confirm(session, c["workspace"].id, request)
    c["tx"].notes = "Synthetic independent edit"
    await session.commit()
    with pytest.raises(HTTPException) as stale:
        await service.confirm_review(session, c["workspace"].id, c["user"].id, data)
    assert stale.value.status_code == 409
    _, collision = await confirm(session, workspace_id, c["request"].model_copy(update={"reason": "Different review"}))
    with pytest.raises(HTTPException) as duplicate:
        await service.confirm_review(session, workspace_id, user_id, collision)
    assert duplicate.value.status_code == 409
    assert len(list(await session.scalars(select(InvestmentSourceReview)))) == 1


@pytest.mark.parametrize("change,blocker", [
    ({"field": "valuation_amount", "meaning": "valuation"}, "supported_execution_amount_required"),
    ({"field": "unit_price", "meaning": "execution_unit_price"}, "reported_execution_price_required"),
    ({"field": "subtotal", "meaning": "execution_subtotal"}, "source_fee_unknown"),
    ({"same_execution": False}, "same_execution_source_review_required"),
])
@pytest.mark.asyncio
async def test_unsupported_meanings_remain_associations(session, correction_case, change, blocker):
    c = correction_case
    if "field" in change:
        c["request"].source_semantics = SourceSemantics(clock_role="posted", amount_field=change["field"], amount_meaning=change["meaning"])
    if "same_execution" in change:
        c["request"].same_execution_reviewed = change["same_execution"]
    association_id = await associate(session, c)
    preview = await service.preview_review(session, c["workspace"].id, correction(c, association_id))
    assert not preview.supported and blocker in preview.blockers
    assert c["tx"].price == 7


@pytest.mark.asyncio
async def test_http_roles_scope_exact_export_and_changed_preview(session, correction_case, client, auth_headers, viewer_auth_headers):
    c = correction_case
    wid, gid = c["workspace"].id, c["wallet"].id
    request = c["request"].model_dump(mode="json")
    preview = await client.post("/api/assets/evidence/source-reviews/preview", json=request, headers=auth_headers)
    assert preview.status_code == 200, preview.text
    body = {"request": request, "expected_revision": preview.json()["revision"], "preview_digest": preview.json()["preview_digest"]}
    denied = await client.post("/api/assets/evidence/source-reviews/confirm", json=body, headers=viewer_auth_headers)
    assert denied.status_code == 403
    other = await client.get("/api/assets/evidence/source-reviews", params={"group_id": str(uuid.uuid4())}, headers=auth_headers)
    assert other.status_code == 404
    saved = await client.post("/api/assets/evidence/source-reviews/confirm", json=body, headers=auth_headers)
    assert saved.status_code == 200, saved.text
    exported = await client.get("/api/assets/evidence/source-reviews/export", params={"group_id": str(gid), "expected_revision": saved.json()["revision"]}, headers=viewer_auth_headers)
    assert exported.status_code == 200
    assert exported.json()["reviews"][0]["payload"]["sources"][0]["leg"]["fee"] is None
    assert exported.json()["target"]["workspace_id"] == str(wid)


@pytest.mark.parametrize("correction_case", [{"quantity": "10"}], indirect=True)
@pytest.mark.asyncio
async def test_quantity_correction_lots_and_timeline_use_current_entry_with_original_sources(session, correction_case, client, auth_headers):
    c = correction_case
    aid = await associate(session, c)
    preview, data = await confirm(session, c["workspace"].id, correction(c, aid))
    assert Decimal(preview.effects["units_delta"]) == -2
    await service.confirm_review(session, c["workspace"].id, c["user"].id, data)
    from app.services.tax_lots import asset_tax_lots
    lots = await asset_tax_lots(session, c["tx"].asset_id, c["workspace"].id)
    assert lots is not None
    assert sum(Decimal(str(row["quantity"])) for row in lots["lots"]) == 10
    response = await client.get("/api/assets/timeline", params={"group_id": str(c["wallet"].id)}, headers=auth_headers)
    assert response.status_code == 200, response.text
    current = [leg for event in response.json()["events"] for leg in event["legs"] if leg["applied_entry"]]
    assert len(current) == 1
    assert Decimal(current[0]["applied_entry"]["quantity"]) == 10
    assert Decimal(current[0]["applied_entry"]["price"]) == 12
    assert current[0]["quantity"] == "12"  # Still the original source fact.
    source_url = next(source["detail_url"] for event in response.json()["events"] for source in event["sources"] if source["source_id"] == str(c["target_row"].id))
    detail = (await client.get(source_url, headers=auth_headers)).json()
    assert Decimal(detail["applied_entry"]["quantity"]) == 10
    assert detail["observation"]["legs"][0]["quantity"] == "12"
    assert {row["request"]["action"] for row in detail["source_reviews"]} == {"associate", "correct"}


@pytest.mark.parametrize("correction_case,blocker", [
    ({"quantity": "7"}, "ledger_scale_loss"),
    ({"total": "120000000000000"}, "holding_cache_range_exceeded"),
    ({"next_day": True}, "cross_date_correction_unsupported"),
    ({"pending": True}, "source_settlement_or_qualification_unresolved"),
], indirect=["correction_case"])
@pytest.mark.asyncio
async def test_unsupported_clock_settlement_and_exact_representation(session, correction_case, blocker):
    c = correction_case
    aid = await associate(session, c)
    preview = await service.preview_review(session, c["workspace"].id, correction(c, aid))
    assert not preview.supported and blocker in preview.blockers
    assert c["tx"].quantity == 12 and c["tx"].price == 7


@pytest.mark.parametrize("correction_case", [{"provider": True}, {"provider": True, "reported_quantity": "5"}], indirect=True)
@pytest.mark.asyncio
async def test_provider_correction_never_recomputes_snapshot_or_chart(session, correction_case):
    c = correction_case
    asset = await session.get(Asset, c["tx"].asset_id)
    before = {key: getattr(asset, key) for key in ("units", "current_value", "last_price", "last_price_at", "balance_updated_at") if hasattr(asset, key)}
    values_before = [(row.id, row.amount, row.price) for row in await session.scalars(select(AssetValue))]
    aid = await associate(session, c)
    preview, data = await confirm(session, c["workspace"].id, correction(c, aid))
    await service.confirm_review(session, c["workspace"].id, c["user"].id, data)
    await session.refresh(asset)
    assert {key: getattr(asset, key) for key in before} == before
    assert [(row.id, row.amount, row.price) for row in await session.scalars(select(AssetValue))] == values_before
    assert preview.effects["provider_snapshot_preserved"] is True
    assert asset.purchase_price == (120 if asset.units == 12 else None)


@pytest.mark.asyncio
async def test_generic_undo_cannot_remove_active_correction_and_revocation_cannot_free_source(session, correction_case):
    from app.models.import_log import ImportLog
    c = correction_case
    aid = await associate(session, c)
    _, data = await confirm(session, c["workspace"].id, correction(c, aid))
    await service.confirm_review(session, c["workspace"].id, c["user"].id, data)
    log = await session.get(ImportLog, c["tx"].import_id)
    with pytest.raises(HTTPException, match="Reverse the active source correction"):
        await evidence.undo_evidence_import(session, c["workspace"].id, log)
    request = correction(c, aid, action="revoke", request_key="revocation")
    _, revocation = await confirm(session, c["workspace"].id, request)
    await service.confirm_review(session, c["workspace"].id, c["user"].id, revocation)
    view = await evidence.preview_evidence(session, c["workspace"].id, c["wallet"].id)
    record = next(row for row in view.records if row.observation_ref == str(c["source_row"].id))
    assert record.application_status == "already_applied"


@pytest.mark.asyncio
async def test_independent_applied_owner_and_recovery_dependencies_are_blocked(session, correction_case):
    from app.models.recovery_evidence import InvestmentRecoveryEntry
    c = correction_case
    aid = await associate(session, c)
    entry = InvestmentRecoveryEntry(workspace_id=c["workspace"].id, group_id=c["wallet"].id,
        observation_id=c["target_row"].id, leg_id=c["request"].target_leg_id,
        entry_key="synthetic-recovery", fingerprint="synthetic", payload={"role": "disposition"})
    session.add(entry)
    await session.commit()
    preview = await service.preview_review(session, c["workspace"].id, correction(c, aid))
    assert "retained_recovery_dependency" in preview.blockers
    extra = AssetTransaction(workspace_id=c["workspace"].id, asset_id=c["tx"].asset_id, kind="buy",
                             quantity=Decimal(12), price=Decimal(10), fee=Decimal(0), date=c["tx"].date)
    session.add(extra)
    await session.flush()
    leg = await session.get(InvestmentLeg, c["request"].source_leg_id)
    leg.asset_transaction_id, leg.applied_at = extra.id, c["tx"].created_at
    await session.commit()
    preview = await service.preview_review(session, c["workspace"].id, correction(c, aid))
    assert "independently_applied_source_requires_separate_correction" in preview.blockers


@pytest.mark.parametrize("correction_case", [{"programs": ("program-a", "program-b", "program-a")}], indirect=True)
@pytest.mark.asyncio
async def test_program_conflict_rejects_association_and_retained_legacy_review(session, correction_case):
    c = correction_case
    wid, uid, gid = c["workspace"].id, c["user"].id, c["wallet"].id
    before = await service._load(session, wid, gid)
    for confirm_request in (False, True):
        with pytest.raises(HTTPException, match="token program") as error:
            if confirm_request:
                await service.confirm_review(session, wid, uid, SourceReviewConfirm(request=c["request"], expected_revision=before["revision"], preview_digest="legacy"))
            else:
                await service.preview_review(session, wid, c["request"])
        assert error.value.status_code == 422
    after = await service._load(session, wid, gid)
    assert after["revision"] == before["revision"]
    # Seed an association retained by the old implementation, without calling the guarded writer.
    sources = []
    for leg_id, semantics in ((c["request"].source_leg_id, c["request"].source_semantics),
                              (c["request"].target_leg_id, c["request"].target_semantics)):
        row, observation, leg, item = service._facts(after, leg_id)
        sources.append({"observation_id": str(row.id), "identity_key": row.identity_key,
                        "fingerprint": row.fingerprint, "leg_id": str(leg.id), "leg_key": item.key,
                        "observation": observation.model_dump(mode="json"), "leg": item.model_dump(mode="json"),
                        "semantics": semantics.model_dump(mode="json")})
    retained = InvestmentSourceReview(workspace_id=wid, group_id=gid, request_key="legacy-association",
        fingerprint=evidence._digest(c["request"].model_dump(mode="json")), created_by=uid,
        payload={"request": c["request"].model_dump(mode="json"), "sources": sources,
                 "effects": {"ledger_rows_added": 0, "units_delta": "0", "cost_delta": "0", "financial_ownership_changed": False}})
    session.add(retained)
    await session.commit()
    request = correction(c, retained.id)
    retained_revision = (await service._load(session, wid, gid))["revision"]
    for confirm_request in (False, True):
        with pytest.raises(HTTPException, match="token program"):
            if confirm_request:
                await service.confirm_review(session, wid, uid, SourceReviewConfirm(request=request, expected_revision="legacy", preview_digest="legacy"))
            else:
                await service.preview_review(session, wid, request)
    state = await service._load(session, wid, gid)
    assert state["revision"] == retained_revision and len(state["reviews"]) == 1
    assert state["transactions"][0].price == 7
    assert {row.id: row.payload for row in state["observations"]} == c["sources_before"]


@pytest.mark.parametrize("correction_case", [
    {"programs": ("program-a", "program-a", "program-b")},
    {"programs": ("program-a", None, "program-b")},
    {"programs": ("program-a", "program-b", None), "alias": True},
    {"programs": ("program-a", None, "program-b"), "alias": True},
], indirect=True)
@pytest.mark.asyncio
async def test_correction_checks_all_known_programs_without_financial_or_audit_writes(session, correction_case):
    c = correction_case
    wid, uid, gid = c["workspace"].id, c["user"].id, c["wallet"].id
    aid = await associate(session, c)
    before = await service._load(session, wid, gid)
    request = correction(c, aid)
    preview = await service.preview_review(session, wid, request)
    assert not preview.supported and "token_program_conflict" in preview.blockers
    with pytest.raises(HTTPException) as error:
        await service.confirm_review(session, wid, uid, SourceReviewConfirm(request=request, expected_revision=preview.revision, preview_digest=preview.preview_digest))
    assert error.value.status_code == 422
    after = await service._load(session, wid, gid)
    assert after["revision"] == before["revision"]  # All financial, evidence and audit state is unchanged.


@pytest.mark.parametrize("correction_case", [{"programs": programs} for programs in [
    ("program-a", "program-a", "program-a"), ("program-a", None, "program-a"),
    (None, "program-a", None), (None, None, None),
]], indirect=True)
@pytest.mark.asyncio
async def test_matching_or_missing_programs_allow_correction_replay_and_exact_reversal(session, correction_case):
    c = correction_case
    wid, uid = c["workspace"].id, c["user"].id
    before = service._image(c["tx"])
    aid = await associate(session, c)
    _, data = await confirm(session, wid, correction(c, aid))
    package = await service.confirm_review(session, wid, uid, data)
    assert c["tx"].price == 10
    assert len((await service.confirm_review(session, wid, uid, data)).reviews) == 2
    # Reversal restores the exact stored before-image even if reviewed holding identity later drifts.
    asset = await session.get(Asset, c["tx"].asset_id)
    asset.external_metadata = {"evidence_asset_identity": {**asset.external_metadata["evidence_asset_identity"], "token_program": "program-drift"}}
    await session.commit()
    _, reverse = await confirm(session, wid, correction(c, package.reviews[-1].id, action="reverse", request_key="reverse"))
    await service.confirm_review(session, wid, uid, reverse)
    assert service._image(c["tx"]) == before
    assert len((await service.confirm_review(session, wid, uid, reverse)).reviews) == 3
    assert {row.id: row.payload for row in await session.scalars(select(InvestmentObservation))} == c["sources_before"]


@pytest.mark.parametrize("correction_case", [{"programs": ("program-a", "program-a", "program-a")}], indirect=True)
@pytest.mark.asyncio
async def test_current_holding_program_drift_revalidates_before_confirm(session, correction_case):
    c = correction_case
    wid, uid, gid = c["workspace"].id, c["user"].id, c["wallet"].id
    aid = await associate(session, c)
    request = correction(c, aid)
    _, data = await confirm(session, wid, request)
    asset = await session.get(Asset, c["tx"].asset_id)
    asset.external_metadata = {"evidence_asset_identity": {**asset.external_metadata["evidence_asset_identity"], "token_program": "program-b"}}
    await session.commit()
    before = await service._load(session, wid, gid)
    with pytest.raises(HTTPException) as stale:
        await service.confirm_review(session, wid, uid, data)
    assert stale.value.status_code == 409
    preview = await service.preview_review(session, wid, request)
    assert not preview.supported and "token_program_conflict" in preview.blockers
    with pytest.raises(HTTPException) as refused:
        await service.confirm_review(session, wid, uid, SourceReviewConfirm(request=request, expected_revision=preview.revision, preview_digest=preview.preview_digest))
    assert refused.value.status_code == 422
    assert (await service._load(session, wid, gid))["revision"] == before["revision"]
