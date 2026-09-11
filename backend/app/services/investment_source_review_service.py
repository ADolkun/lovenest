"""Document unequal source facts; explicitly correct only an independent acquisition."""
import json
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from fastapi import HTTPException
from sqlalchemy import select

from app.models.investment_evidence import InvestmentSourceReview
from app.models.asset_transaction import AssetTransaction
from app.models.owned_transfer import InvestmentMovementApplication
from app.models.recovery_evidence import InvestmentRecoveryEntry, InvestmentRecoveryReview
from app.models.workspace import Workspace
from app.schemas.investment_evidence import EvidenceLegInput, EvidenceTarget
from app.schemas.investment_source_review import (
    SourceReviewLeg, SourceReviewPackage, SourceReviewPreview, SourceReviewRead,
    SourceReviewRequest,
)
from app.services import asset_transaction_service as ledger
from app.services import investment_evidence_service as evidence


def _json(value):
    return json.loads(json.dumps(value, default=str))


async def review_rows(session, workspace_id):
    return list((await session.scalars(select(InvestmentSourceReview).where(
        InvestmentSourceReview.workspace_id == workspace_id,
    ).order_by(InvestmentSourceReview.created_at, InvestmentSourceReview.id)
      .execution_options(populate_existing=True))).all())


def correction_claims(rows):
    # A reversal restores values, not permission to apply the source again.
    return [row.payload for row in rows if row.payload["request"]["action"] == "correct"]


def active_corrections(rows):
    superseded = {row.supersedes_id for row in rows if row.supersedes_id}
    return [row for row in rows if row.id not in superseded and row.payload["request"]["action"] == "correct"]


async def guard_corrected_transactions(session, workspace_id, asset_ids, transaction_ids=(), *, deleting_scope=False):
    rows = await review_rows(session, workspace_id)
    ids = {str(value) for value in transaction_ids}
    assets = {str(value) for value in asset_ids}
    if any(row.payload["effects"]["before"]["id"] in ids or (
        deleting_scope and row.payload["effects"]["before"]["asset_id"] in assets
    ) for row in active_corrections(rows)):
        raise HTTPException(409, "Reverse the active source correction before editing, deleting or undoing this entry")


async def _load(session, workspace_id, group_id, *, lock=False):
    group, cid = await evidence._scope(session, workspace_id, group_id, lock=lock)
    observations, events, legs, links, assets, transactions = await evidence._state(session, workspace_id, group_id)
    rows = await review_rows(session, workspace_id)
    dependencies = {}
    for name, model in (("movements", InvestmentMovementApplication),
                        ("recovery_entries", InvestmentRecoveryEntry), ("recovery_reviews", InvestmentRecoveryReview)):
        dependencies[name] = list((await session.scalars(select(model).where(model.workspace_id == workspace_id)
                                      .execution_options(populate_existing=True))).all())
    collections = {"observations": observations, "events": events, "legs": legs, "links": links,
                   "assets": assets, "transactions": transactions, "reviews": rows, **dependencies}
    revision = evidence._digest({
        "workspace": str(workspace_id), "group": [str(group.id), str(cid), str(group.account_id)],
        **{key: [{column.name: getattr(row, column.name) for column in row.__table__.columns}
                 for row in sorted(values, key=lambda row: str(row.id))] for key, values in collections.items()},
    })
    workspace = await session.get(Workspace, workspace_id)
    target = EvidenceTarget(workspace_id=workspace_id, workspace_name=workspace.name,
                            group_id=group.id, group_name=group.name, account_id=group.account_id)
    return {**collections, "group": group, "connection_id": cid, "revision": revision, "target": target}


def _package(state):
    return SourceReviewPackage(
        revision=state["revision"], target=state["target"],
        legs=[SourceReviewLeg(leg_id=row.id, observation_ref=str(row.observation_id),
                              leg_key=row.source_leg_key, transaction_id=row.asset_transaction_id) for row in state["legs"]],
        reviews=[SourceReviewRead(id=row.id, request_key=row.request_key, supersedes_id=row.supersedes_id,
                                 created_by=row.created_by, created_at=row.created_at, payload=row.payload)
                 for row in state["reviews"] if row.group_id == state["group"].id],
    )


async def list_reviews(session, workspace_id, group_id):
    return _package(await _load(session, workspace_id, group_id))


def attach_timeline_reviews(state, events, details, displayed_leg_ids):
    """Expose the current financial entry alongside, never over, original facts."""
    rows = list(state.get("source_reviews", {}).values())
    for row in rows:
        for source in row.payload["sources"]:
            detail = details.get(source["observation_id"])
            if detail is not None:
                detail.setdefault("source_reviews", []).append({"id": str(row.id), "created_at": str(row.created_at), **row.payload})
    claims = correction_claims(rows)
    for claim in claims:
        tx = next((tx for tx in state["transactions"].values() if str(tx.id) == claim["effects"]["after"]["id"]), None)
        if tx is None:
            continue
        current = _image(tx)
        current["tax_basis_complete"] = False
        for source in claim["sources"]:
            detail = details.get(source["observation_id"])
            if detail is not None:
                detail["applied_entry"] = current
        owner_ids = {displayed_leg_ids.get(leg.id, str(leg.id)) for leg in state["legs"].values() if leg.asset_transaction_id == tx.id}
        for event in events.values():
            for leg in event.legs:
                if leg.leg_id in owner_ids:
                    leg.applied_entry = current


def _facts(state, identifier):
    leg = next((row for row in state["legs"] if row.id == identifier), None)
    if leg is None:
        raise HTTPException(404, "Source leg is unavailable in the selected wallet")
    row = next(row for row in state["observations"] if row.id == leg.observation_id)
    if row.group_id != state["group"].id or row.connection_id != state["connection_id"]:
        raise HTTPException(409, "Source wallet or connection mapping changed")
    return row, evidence._input(row), leg, EvidenceLegInput.model_validate(leg.payload)


def _review(state, identifier, action):
    row = next((row for row in state["reviews"] if row.id == identifier and row.group_id == state["group"].id), None)
    if row is None:
        raise HTTPException(404, "Source review is unavailable in the selected wallet")
    if row.payload["request"]["action"] != action:
        raise HTTPException(422, "This review does not support the requested operation")
    if any(other.supersedes_id == row.id for other in state["reviews"]):
        raise HTTPException(409, "This review has been superseded; refresh the review")
    return row


def _owners(state, leg):
    inputs = {str(row.id): evidence._input(row) for row in state["observations"]}
    families, conflicting = evidence._source_families(inputs, {str(row.id): row.identity_key for row in state["observations"]})
    if str(leg.observation_id) in conflicting:
        raise HTTPException(422, "Conflicting source versions require separate review")
    leg_families = {row.id: (families[str(row.observation_id)], row.source_leg_key) for row in state["legs"]}
    key = leg_families[leg.id]
    related = [link for link in state["links"] if (families[str(link.observation_id)], link.source_leg_key) == key]
    return [row for row in evidence._canonical_application_legs(key, leg_families, state["legs"], related) if row.applied_at]


def _image(tx):
    return _json({key: getattr(tx, key) for key in (
        "id", "asset_id", "kind", "quantity", "price", "fee", "date", "created_at", "source", "external_id", "import_id", "notes",
    )})


def _association(state, request):
    left = _facts(state, request.source_leg_id)
    right = _facts(state, request.target_leg_id)
    a, b = left[3], right[3]
    if left[0].id == right[0].id:
        raise HTTPException(422, "Choose two distinct source observations")
    if not evidence._same_asset(a, b) or a.direction != b.direction or (a.asset_id and b.asset_id and a.asset_id != b.asset_id):
        raise HTTPException(422, "Source asset or direction conflicts")
    if left[1].provider != right[1].provider or (left[1].source_account_id and right[1].source_account_id
                                               and left[1].source_account_id != right[1].source_account_id):
        raise HTTPException(422, "Source provider or account identity conflicts")
    sources = []
    for facts, semantics in ((left, request.source_semantics), (right, request.target_semantics)):
        row, observation, leg, item = facts
        sources.append({"observation_id": str(row.id), "identity_key": row.identity_key,
                        "fingerprint": row.fingerprint, "leg_id": str(leg.id), "leg_key": item.key,
                        "observation": observation.model_dump(mode="json"), "leg": item.model_dump(mode="json"),
                        "semantics": semantics.model_dump(mode="json")})
    return left, right, sources


def _dependencies(state, asset, tx):
    # ponytail: reject any disposal/movement on this holding; lot-specific dependency proofs can narrow this later.
    reasons = []
    if any(row.asset_id == asset.id and row.kind != "buy" for row in state["transactions"]):
        reasons.append("dependent_disposal_or_movement")
    if any(row.asset_id == asset.id or str(tx.id) in row.payload.get("dependency_transactions", {}) for row in state["movements"]):
        reasons.append("retained_movement_dependency")
    asset_legs = {row.id for row in state["legs"] if row.asset_id == asset.id or row.asset_transaction_id == tx.id}
    if any(row.leg_id in asset_legs or row.payload.get("asset_id") == str(asset.id) for row in state["recovery_entries"]):
        reasons.append("retained_recovery_dependency")
    if any(str(tx.id) in json.dumps(row.payload, sort_keys=True) for row in state["recovery_reviews"]):
        reasons.append("retained_recovery_dependency")
    return reasons


def _replacement(observation, item, semantics, asset):
    reasons = []
    if observation.source_kind != "primary_activity" or item.classification != "buy" or item.direction != "in":
        reasons.append("only_primary_acquisition_supported")
    if observation.settlement_status != "settled" or observation.reason_codes:
        reasons.append("source_settlement_or_qualification_unresolved")
    if asset.type == "option" or ledger.is_option(asset.type):
        reasons.append("option_correction_unsupported")
    if item.execution_currency != asset.currency:
        reasons.append("execution_currency_conflict")
    if evidence._holding_conflicts(item, asset):
        reasons.append("holding_identity_unverified")
    if item.quantity is None or item.quantity <= 0:
        reasons.append("positive_source_quantity_required")
    field = semantics.amount_field
    meaning = {"unit_price": "execution_unit_price", "subtotal": "execution_subtotal", "total": "fee_inclusive_total"}.get(field)
    if meaning is None or semantics.amount_meaning != meaning:
        reasons.append("supported_execution_amount_required")
    if field == "unit_price" and item.unit_price_origin != "reported":
        reasons.append("reported_execution_price_required")
    amount = getattr(item, field)
    if amount is None:
        reasons.append("source_amount_unknown")
    inclusive = field == "total" and semantics.amount_meaning == "fee_inclusive_total"
    if not inclusive and item.fee is None:
        reasons.append("source_fee_unknown")
    if item.fee not in (None, Decimal(0)) and item.fee_currency != asset.currency:
        reasons.append("fee_currency_conflict")
    source_currency = observation.source_fields.get(f"{field}_currency")
    if source_currency and source_currency != asset.currency:
        reasons.append("selected_amount_currency_conflict")
    if amount is not None and semantics.decimal_places is not None:
        if amount != amount.quantize(Decimal(1).scaleb(-semantics.decimal_places)):
            reasons.append("declared_precision_conflicts_with_source")
    # The association records distinct meanings. Never apply _price's generic
    # derived-execution fallback or infer a residual fee from a rounded price.
    if reasons:
        return None, reasons
    price = amount if field == "unit_price" else amount / item.quantity
    fee = Decimal(0) if inclusive else item.fee
    if (evidence.asset_import_service._to_ledger_scale(item.quantity) != item.quantity
            or evidence.asset_import_service._to_ledger_scale(price) != price
            or fee != fee.quantize(Decimal("0.01")) or fee >= Decimal("1e13")
            or (field != "unit_price" and evidence._product_exact(price, item.quantity) != amount)):
        return None, ["ledger_scale_loss"]
    # Only compare amounts that are explicitly selected as execution amounts.
    # Other original values remain visible as unresolved, distinct assertions.
    if inclusive and item.subtotal is not None and item.fee is not None and item.subtotal + item.fee != amount:
        return None, ["fee_inclusive_total_conflicts_with_reported_subtotal_and_fee"]
    return {"quantity": str(item.quantity), "price": str(price), "fee": str(fee)}, []


def _financial_effects(state, tx, replacement, asset, *, fee_treatment, source_fee):
    before = _image(tx)
    after = {**before, **{key: format(Decimal(value), ".2f" if key == "fee" else ".18f") for key, value in replacement.items()}}
    before_cost = Decimal(before["quantity"]) * Decimal(before["price"]) + Decimal(before["fee"])
    after_cost = Decimal(after["quantity"]) * Decimal(after["price"]) + Decimal(after["fee"])
    others = [row for row in state["transactions"] if row.asset_id == asset.id and row.id != tx.id]
    edited = AssetTransaction(**{**{key: getattr(tx, key) for key in ("kind", "date", "created_at")},
                                **{key: Decimal(after[key]) for key in ("quantity", "price", "fee")}})
    position = ledger._recompute([*others, edited], asset_type=asset.type)
    provider = asset.connection_id is not None
    comparable = not provider or (asset.units is not None and position["units"] == asset.units)
    cache = {
        "average_price": position["average_price"] if comparable else None,
        "purchase_price": position["cost_basis"].quantize(Decimal("0.01")) if comparable and position["units"] else None,
        "realized_gain": position["realized_gain"].quantize(Decimal("0.01")) if comparable else None,
    }
    return _json({"ledger_rows_added": 0, "before": before, "after": after,
                  "units_delta": Decimal(after["quantity"]) - Decimal(before["quantity"]),
                  "cost_before": before_cost, "cost_after": after_cost, "cost_delta": after_cost - before_cost,
                  "position_quantity_after": position["units"], "position_cost_after": position["cost_basis"],
                  "reported_quantity": asset.units if provider else None, "provider_snapshot_preserved": provider,
                  "provider_quantity_matches": comparable if provider else None, "cache_after": cache,
                  "fee_treatment": fee_treatment, "original_source_fee": source_fee,
                  "tax_basis_complete": False, "history_complete": False})


def _preview(state, request):
    blockers, sources = [], []
    effects = {"ledger_rows_added": 0, "units_delta": "0", "cost_delta": "0", "financial_ownership_changed": False}
    if request.action == "associate":
        _, _, sources = _association(state, request)
    elif request.action == "revoke":
        association = _review(state, request.review_id, "associate")
        sources = association.payload["sources"]
    else:
        if request.action == "correct":
            association = _review(state, request.review_id, "associate")
            declaration = SourceReviewRequest.model_validate(association.payload["request"])
            assert declaration.source_semantics is not None
            left, right, sources = _association(state, declaration)
            source_row, source, source_leg, item = left
            target_row, target, target_leg, _ = right
            if sources != association.payload["sources"]:
                blockers.append("association_source_or_mapping_changed")
            if not declaration.same_execution_reviewed:
                blockers.append("same_execution_source_review_required")
            if not source.source_account_id or source.source_account_id != target.source_account_id:
                blockers.append("source_account_identity_required")
            if not (item.asset_id or item.provider_asset_id or item.isin or (item.chain and item.token_address)):
                blockers.append("stable_asset_identity_required")
            owners = _owners(state, target_leg)
            if len(owners) != 1 or owners[0].asset_transaction_id is None:
                blockers.append("one_live_canonical_acquisition_required")
                tx = None
            else:
                tx = next((row for row in state["transactions"] if row.id == owners[0].asset_transaction_id), None)
            own = _owners(state, source_leg)
            if own and (tx is None or any(row.asset_transaction_id != tx.id for row in own)):
                blockers.append("independently_applied_source_requires_separate_correction")
            if any(claim["sources"][0]["identity_key"] == source_row.identity_key
                   and claim["sources"][0]["leg_key"] == item.key for claim in correction_claims(state["reviews"])):
                blockers.append("correction_source_already_claimed")
            if tx:
                asset = next(row for row in state["assets"] if row.id == tx.asset_id)
                if any(row.payload["effects"]["before"]["id"] == str(tx.id) for row in active_corrections(state["reviews"])):
                    blockers.append("reverse_active_correction_first")
                if target.source_kind != "primary_activity" or target.settlement_status != "settled" or target.reason_codes:
                    blockers.append("canonical_source_qualification_unresolved")
                if source.event_date != tx.date or target.event_date != tx.date or (
                    source.event_at and target.event_at and source.event_at.date() != target.event_at.date()
                ):
                    blockers.append("cross_date_correction_unsupported")
                replacement, reasons = _replacement(source, item, declaration.source_semantics, asset)
                blockers.extend(reasons)
                fee_treatment = "included_no_additional_fee" if declaration.source_semantics.amount_meaning == "fee_inclusive_total" else "separate_reported_fee"
                source_fee = str(item.fee) if item.fee is not None else None
        else:
            original = _review(state, request.review_id, "correct")
            sources = original.payload["sources"]
            tx = next((row for row in state["transactions"] if str(row.id) == original.payload["effects"]["after"]["id"]), None)
            if tx is None:
                blockers.append("canonical_transaction_unavailable")
            else:
                asset = next(row for row in state["assets"] if row.id == tx.asset_id)
                if _image(tx) != original.payload["effects"]["after"]:
                    blockers.append("corrected_transaction_changed")
                for source in sources:
                    retained = next((row for row in state["observations"] if str(row.id) == source["observation_id"]), None)
                    if retained is None or retained.connection_id != state["connection_id"]:
                        blockers.append("correction_source_mapping_changed")
                replacement = {key: original.payload["effects"]["before"][key] for key in ("quantity", "price", "fee")}
                fee_treatment, source_fee = "restore_previous_entry", original.payload["effects"]["original_source_fee"]
        if tx:
            if tx.kind != "buy" or tx.movement or tx.price is None or tx.fee is None:
                blockers.append("supported_canonical_buy_required")
            blockers.extend(_dependencies(state, asset, tx))
            if not blockers and replacement is not None:
                effects = _financial_effects(state, tx, replacement, asset, fee_treatment=fee_treatment, source_fee=source_fee)
                cache = effects["cache_after"]
                if (cache["purchase_price"] is not None and abs(Decimal(cache["purchase_price"])) >= Decimal("1e13")) or (
                    cache["average_price"] is not None and abs(Decimal(cache["average_price"])) >= Decimal("1e20")
                ) or abs(Decimal(effects["position_quantity_after"])) >= Decimal("1e20"):
                    blockers.append("holding_cache_range_exceeded")
                if effects["before"] == effects["after"]:
                    blockers.append("no_financial_change")
    digest = evidence._digest({"revision": state["revision"], "request": request.model_dump(mode="json"),
                               "sources": sources, "effects": effects, "blockers": blockers})
    return SourceReviewPreview(revision=state["revision"], preview_digest=digest, target=state["target"], request=request,
                               supported=not blockers, blockers=sorted(set(blockers)), sources=sources, effects=effects)


async def preview_review(session, workspace_id, request):
    state = await _load(session, workspace_id, request.group_id)
    with localcontext(prec=512):
        return _preview(state, request)


async def confirm_review(session, workspace_id, user_id, data):
    try:
        request = data.request
        state = await _load(session, workspace_id, request.group_id, lock=True)
        fingerprint = evidence._digest(request.model_dump(mode="json"))
        prior = next((row for row in state["reviews"] if row.request_key == request.request_key), None)
        if prior:
            if prior.fingerprint != fingerprint or prior.group_id != request.group_id:
                raise HTTPException(409, "Request key already identifies a different review")
            return _package(state)
        with localcontext(prec=512):
            preview = _preview(state, request)
            if preview.revision != data.expected_revision or preview.preview_digest != data.preview_digest:
                raise HTTPException(409, "Source review changed; preview again before applying")
            if not preview.supported:
                raise HTTPException(422, {"message": "Source correction is unsupported", "blockers": preview.blockers})
            if request.action in {"correct", "reverse"}:
                after = preview.effects["after"]
                tx = next(row for row in state["transactions"] if str(row.id) == after["id"])
                asset = next(row for row in state["assets"] if row.id == tx.asset_id)
                for key in ("quantity", "price", "fee"):
                    setattr(tx, key, Decimal(after[key]))
                await session.flush()
                if asset.connection_id:
                    # Buy-only recompute_and_cache overwrites provider snapshots.
                    # Set only derived financial caches from the pure preview.
                    for key, value in preview.effects["cache_after"].items():
                        setattr(asset, key, Decimal(value) if value is not None else None)
                else:
                    await ledger.recompute_and_cache(session, asset)
            session.add(InvestmentSourceReview(
                workspace_id=workspace_id, group_id=request.group_id,
                request_key=request.request_key, fingerprint=fingerprint,
                supersedes_id=request.review_id if request.action in {"revoke", "reverse"} else None,
                payload={"request": request.model_dump(mode="json"), "sources": preview.sources,
                         "effects": preview.effects, "preview_revision": preview.revision,
                         "preview_digest": preview.preview_digest}, created_by=user_id, created_at=datetime.now(timezone.utc),
            ))
            await session.commit()
        return await list_reviews(session, workspace_id, request.group_id)
    except Exception:
        await session.rollback()
        raise
