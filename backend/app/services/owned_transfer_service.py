"""Workspace-scoped decisions over retained evidence and the existing asset ledger."""
import hashlib
import json
import uuid
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext

from fastapi import HTTPException
from sqlalchemy import select

from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.models.bank_connection import BankConnection
from app.models.investment_evidence import InvestmentHistoryCollection, InvestmentLeg, InvestmentObservation, InvestmentObservationLink
from app.models.owned_transfer import (
    InvestmentIncident, InvestmentMovementApplication, InvestmentOwnedTransfer, InvestmentOwnership,
)
from app.schemas.owned_transfer import (
    HoldingEffect, IncidentRead, LotRead, LotsRead, MovementApplicationRead, MovementPreview,
    MovementPreviewRequest, MovementRead, MovementSelection, OwnershipRead, TransferHolding,
    TransferIndex, TransferPreview, TransferPreviewRequest, TransferRead,
)
from app.services import movement_replay


def _error(status, code, message, **details):
    return HTTPException(status, {"code": code, "message": message, **details})


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def lock_workspace(session, workspace_id):
    """Shared financial lock order: every connection, then wallet, then holding.

    All ledger writers enter here before their own row locks. Existing sync may
    already own its connection, so transfer writers use NOWAIT rather than wait
    in an inverted order; the caller gets a retryable conflict, not a deadlock.
    """
    from sqlalchemy.exc import DBAPIError
    try:
        # ponytail: all workspace connections serialize investment writes; narrow to affected closures if contention matters.
        for model in (BankConnection, AssetGroup, Asset):
            await session.scalars(select(model).where(model.workspace_id == workspace_id)
                                  .order_by(model.id).with_for_update(nowait=True)
                                  .execution_options(populate_existing=True))
    except DBAPIError as exc:
        await session.rollback()
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise _error(409, "investment_busy", "Another operation is changing investment inventory; retry") from exc
        raise


async def _state(session, workspace_id):
    result = {}
    for key, model in (("assets", Asset), ("groups", AssetGroup), ("transactions", AssetTransaction),
                       ("observations", InvestmentObservation), ("legs", InvestmentLeg),
                       ("links", InvestmentObservationLink),
                       ("ownership", InvestmentOwnership), ("applications", InvestmentMovementApplication),
                       ("transfers", InvestmentOwnedTransfer), ("incidents", InvestmentIncident),
                       ("archives", InvestmentHistoryCollection)):
        rows = list((await session.scalars(select(model).where(model.workspace_id == workspace_id)
                                          .execution_options(populate_existing=True))).all())
        result[key] = {row.id: row for row in rows}
    result["workspace_id"] = workspace_id
    result["revision"] = _digest({key: [
        {column.name: getattr(row, column.name) for column in row.__table__.columns if not (key == "archives" and column.name == "payload")}
        for row in sorted(rows.values(), key=lambda r: str(r.id))
    ] for key, rows in result.items() if isinstance(rows, dict)})
    result["qualification"] = {}
    result["reason_cache"] = {}
    result["archive_qualification"] = {}
    result["canonical_legs"] = {}
    for leg in result["legs"].values():
        result["canonical_legs"].setdefault(_key(result, leg.id), []).append(leg)
    from app.services.investment_evidence_service import _canonical_application_legs, _input, _source_families
    families, result["conflicting_observations"] = _source_families(
        {str(row.id): _input(row) for row in result["observations"].values()},
        {str(row.id): row.identity_key for row in result["observations"].values()},
    )
    leg_families = {leg.id: (families[str(leg.observation_id)], leg.source_leg_key) for leg in result["legs"].values()}
    result["application_legs"] = {}
    for leg in result["legs"].values():
        family = leg_families[leg.id]
        related = [link for link in result["links"].values() if (families[str(link.observation_id)], link.source_leg_key) == family]
        result["application_legs"][leg.id] = _canonical_application_legs(family, leg_families, result["legs"].values(), related)
    return result


def _get(state, key, identifier):
    row = state[key].get(identifier)
    if row is None:
        raise _error(404, "evidence_not_found", "Referenced record is unavailable in this workspace")
    return row


def _facts(state, leg_id):
    leg = _get(state, "legs", leg_id)
    observation = _get(state, "observations", leg.observation_id)
    return leg, observation, leg.payload


def _asset_identity(item):
    return item.get("chain"), item.get("token_address"), item.get("token_program")


def _movement_reasons(state, leg_id):
    if leg_id in state["reason_cache"]:
        return list(state["reason_cache"][leg_id])
    leg, observation, item = _facts(state, leg_id)
    source = observation.payload
    reasons = []
    if str(observation.id) in state["conflicting_observations"]:
        reasons.append("source_version_conflict")
    if item.get("classification") not in {"transfer", "fee"} or (item.get("classification") == "fee" and item.get("quantity_role") == "principal"):
        reasons.append("unsupported_movement_classification")
    owners = {part.id: part for part in state["application_legs"].get(leg_id, [])}
    owners.update({part.id: part for part in state["canonical_legs"].get(_key(state, leg_id), [])})
    for part in owners.values():
        if not part.applied_at and part.asset_transaction_id is None:
            continue
        owner = next((row for row in state["applications"].values() if row.leg_id == part.id), None)
        if owner is None or _key(state, owner.leg_id) != _key(state, leg_id):
            reasons.append("canonical_application_owned")
    if not observation.is_current:
        reasons.append("superseded_source")
    if source.get("source_kind") != "primary_activity":
        reasons.append("not_primary_movement")
    if source.get("settlement_status") != "settled":
        reasons.append("settlement_unresolved")
    if (source.get("network_status") or "").lower() not in {"finalized", "confirmed", "settled", "completed"}:
        reasons.append("network_settlement_unresolved")
    if item.get("chain") == "solana" and source.get("network_status") != "finalized":
        reasons.append("finality_unresolved")
    if (source.get("provider_status") or "").lower() in {"pending", "failed", "error", "canceled", "cancelled"} and item.get("quantity_role") not in {"network_fee", "withdrawal_fee", "intermediary_fee", "token_transfer_fee"}:
        reasons.append("principal_not_settled")
    if not source.get("provider_status"):
        reasons.append("provider_execution_unresolved")
    for field in ("chain", "token_address", "transaction_ref", "leg_ref", "source_address", "raw_units", "quantity_role"):
        if not item.get(field):
            reasons.append(f"missing_{field}")
    if item.get("quantity_role") == "principal" and not item.get("destination_address"):
        reasons.append("missing_destination_address")
    if item.get("chain") == "solana" and item.get("token_address") not in (None, "native") and not item.get("token_program"):
        reasons.append("missing_token_program")
    if item.get("quantity_role") not in {"principal", "network_fee", "withdrawal_fee", "intermediary_fee", "token_transfer_fee"}:
        reasons.append("quantity_semantics_unresolved")
    if item.get("quantity_role") in {"network_fee", "withdrawal_fee", "intermediary_fee", "token_transfer_fee"} and not item.get("fee_payer"):
        reasons.append("fee_payer_unknown")
    if item.get("quantity_role") in {"network_fee", "withdrawal_fee", "intermediary_fee", "token_transfer_fee"} and item.get("fee_payer") and item["fee_payer"] not in {item.get("source_address"), item.get("source_owner")}:
        reasons.append("fee_payer_conflict")
    if item.get("fee_semantics") not in {"separate", "none"}:
        reasons.append("fee_semantics_unresolved")
    quantity = item.get("quantity")
    if quantity is None or Decimal(quantity) <= 0:
        reasons.append("quantity_unavailable")
    elif item.get("raw_units") is not None and item.get("decimals") is not None:
        with localcontext(prec=512):
            if Decimal(item["raw_units"]).scaleb(-item["decimals"]) != Decimal(quantity):
                reasons.append("atomic_units_conflict")
            if Decimal(quantity).quantize(Decimal("1e-18")) != Decimal(quantity) or Decimal(quantity).adjusted() >= 20:
                reasons.append("ledger_precision_unsupported")
    else:
        reasons.append("atomic_units_unresolved")
    if not source.get("event_date"):
        reasons.append("movement_date_unavailable")
    for peer in state["canonical_legs"].get(_key(state, leg_id), []):
        if peer.id == leg_id:
            continue
        other = state["observations"].get(peer.observation_id)
        if other is None or not other.is_current:
            continue
        if any(peer.payload.get(field) not in (None, "unknown") and item.get(field) not in (None, "unknown")
               and peer.payload[field] != item[field] for field in ("quantity", "raw_units", "decimals", "fee_payer", "fee_semantics")):
            reasons.append("source_version_conflict")
        if other.payload.get("settlement_status") in {"failed", "pending"}:
            from app.services.investment_evidence_service import _input, _status_progression
            if other.identity_key != observation.identity_key or not _status_progression(_input(other), _input(observation)):
                reasons.append("source_settlement_conflict")
    # Current producer qualification includes conflicting peer collections.
    if source.get("source") == "onchain_history":
        from app.services.onchain_history import qualify_archive, project_observations
        scope = observation.connection_id, source.get("source_account_id")
        if scope not in state["archive_qualification"]:
            matching = [row for row in state["archives"].values() if row.connection_id == observation.connection_id
                        and f"solana:{row.request.get('address')}" == source.get("source_account_id")]
            current = []
            for row in matching:
                qualified = qualify_archive(row.payload, [(peer.id, peer.payload) for peer in matching if peer.id != row.id])
                current.extend(project_observations(qualified, row.id))
            state["archive_qualification"][scope] = current
        current = state["archive_qualification"][scope]
        if not any(o.source_local_id == source.get("source_local_id") and o.settlement_status == "settled"
                   and any(p.leg_ref == item.get("leg_ref") and p.quantity == (Decimal(quantity) if quantity is not None else None) for p in o.legs)
                   for o in current):
            reasons.append("archive_principal_unqualified")
    state["reason_cache"][leg_id] = sorted(set(reasons))
    return list(state["reason_cache"][leg_id])


def _ownership_reasons(state, ownership_id, asset_id, leg_id):
    assertion = _get(state, "ownership", ownership_id)
    asset = _get(state, "assets", asset_id) if asset_id is not None else None
    _, observation, item = _facts(state, leg_id)
    payload = assertion.payload
    reasons = []
    # The account retaining a fee observation is not necessarily its payer.
    if item.get("quantity_role") in {"network_fee", "withdrawal_fee", "intermediary_fee", "token_transfer_fee"} and item.get("direction") != "out":
        reasons.append("ownership_effect_unknown")
    if assertion.revoked_at or assertion.group_id != observation.group_id or (asset and assertion.group_id != asset.group_id):
        reasons.append("ownership_scope_conflict")
    if payload.get("chain") != item.get("chain"):
        reasons.append("ownership_chain_conflict")
    endpoint = "source" if item.get("direction") == "out" else "destination"
    if payload.get("address") and payload["address"] not in {item.get(f"{endpoint}_address"), item.get(f"{endpoint}_owner")}:
        reasons.append("ownership_endpoint_conflict")
    if payload.get("source_account_id") and payload["source_account_id"] != observation.payload.get("source_account_id"):
        reasons.append("ownership_account_conflict")
    event_date = observation.payload.get("event_date")
    if event_date and ((payload.get("valid_from") and event_date < payload["valid_from"])
                       or (payload.get("valid_until") and event_date > payload["valid_until"])):
        reasons.append("ownership_interval_conflict")
    group = state["groups"].get(assertion.group_id)
    if group is None or payload.get("connection_id") != str(group.connection_id) or payload.get("account_id") != str(group.account_id):
        reasons.append("account_mapping_changed")
    identity = ((asset.external_metadata or {}).get("evidence_asset_identity") or asset.external_metadata or {}) if asset else {}
    for field in ("chain", "token_address", "token_program"):
        if identity.get(field) and identity[field] != item.get(field):
            reasons.append("holding_asset_conflict")
    # Choosing a previously unbound manual holding records the canonical mapping.
    return reasons


def _key(state, leg_id):
    _, _, item = _facts(state, leg_id)
    return physical_movement_key(item) or _digest(["unresolved-leg", str(leg_id)])


def physical_movement_key(item):
    """Only fully identified physical legs share an application across sources."""
    if not all(item.get(field) for field in ("chain", "token_address", "transaction_ref", "leg_ref", "source_address", "direction", "quantity_role")):
        return None
    if item.get("quantity_role") == "principal" and not item.get("destination_address"):
        return None
    if item.get("chain") == "solana" and item.get("token_address") != "native" and not item.get("token_program"):
        return None
    if item.get("quantity_role") in {"network_fee", "withdrawal_fee", "intermediary_fee", "token_transfer_fee"}:
        return _digest(["fee", _asset_identity(item), item.get("transaction_ref"), item.get("leg_ref"),
                        item.get("quantity_role"), item.get("fee_payer") or item.get("source_address")])
    return _digest([_asset_identity(item), item.get("transaction_ref"), item.get("leg_ref"),
                    item.get("source_address"), item.get("destination_address"),
                    item.get("direction"), item.get("quantity_role")])


def _application(state, leg_id):
    key = _key(state, leg_id)
    return next((row for row in state["applications"].values()
                 if row.application_key == key or _key(state, row.leg_id) == key), None)


def _movement_read(state, leg_id):
    leg, observation, item = _facts(state, leg_id)
    source = observation.payload
    application = _application(state, leg_id)
    fields = {key: item.get(key) for key in MovementRead.model_fields if key in item}
    fields.update({key: source.get(key) for key in ("source", "source_local_id", "source_locator", "event_date", "event_at", "time_precision", "provider_status", "network_status", "settlement_status")})
    return MovementRead(**fields, leg_id=leg.id, observation_id=observation.id, group_id=observation.group_id,
                        observation_ref=str(observation.id), leg_key=leg.source_leg_key,
                        application_id=application.id if application else None,
                        application_status="reversed" if application and application.reversed_at else "applied" if application else "unapplied",
                        reason_codes=_movement_reasons(state, leg_id))


def _tx_fingerprint(tx):
    return _digest([str(tx.id), tx.kind, str(tx.quantity), str(tx.price), str(tx.fee), str(tx.date), str(tx.created_at), tx.movement])


def _prefix(state, asset_id, leg_id, ordering_reviewed=False):
    _, observation, _ = _facts(state, leg_id)
    when = observation.payload.get("event_date")
    instant = observation.payload.get("event_at")
    rows = []
    ambiguous = False
    for tx in state["transactions"].values():
        if tx.asset_id != asset_id or not when or tx.date.isoformat() > when:
            continue
        if tx.date.isoformat() == when:
            if not instant or tx.source in {"manual", "import"} and not tx.movement:
                ambiguous = True
                if not ordering_reviewed:
                    continue
            elif _aware(tx.created_at) >= datetime.fromisoformat(instant):
                continue
        rows.append(tx)
    return rows, ambiguous


def _qualify(state):
    qualification = state["qualification"]
    active = [row for row in state["applications"].values() if not row.reversed_at]
    for row in active:
        reasons = _movement_reasons(state, row.leg_id) + _ownership_reasons(state, row.ownership_id, row.asset_id, row.leg_id)
        qualification[row.id] = {"settlement_complete": not reasons, "basis_complete": True, "missing_links": reasons,
                                 "invalid_root_transaction_ids": []}
        prefix, _ = _prefix(state, row.asset_id, row.leg_id, row.payload["request"].get("ordering_reviewed", False))
        existing_prefix = {str(tx.id) for tx in prefix if tx.id != row.transaction_id}
        if existing_prefix != set(row.payload.get("dependency_transactions", {})):
            qualification[row.id]["basis_complete"] = False
            qualification[row.id]["missing_links"].append("source_history_changed")
        for key, expected in row.payload.get("dependency_transactions", {}).items():
            tx = state["transactions"].get(uuid.UUID(key))
            if tx is None or _tx_fingerprint(tx) != expected:
                qualification[row.id]["basis_complete"] = False
                qualification[row.id]["missing_links"].append("acquisition_source_changed")
                qualification[row.id]["invalid_root_transaction_ids"].append(key)
        for key in row.payload.get("acquisition_observations", []):
            source = state["observations"].get(uuid.UUID(key))
            if source is None or not source.is_current or source.payload.get("settlement_status") != "settled":
                qualification[row.id]["basis_complete"] = False
                qualification[row.id]["missing_links"].append("acquisition_source_unqualified")
                qualification[row.id]["invalid_root_transaction_ids"].extend(
                    str(leg.asset_transaction_id) for leg in state["legs"].values() if str(leg.observation_id) == key and leg.asset_transaction_id
                )
    # Propagate principal and acquisition qualification through the recorded event DAG.
    for _ in range(len(active) + 1):
        changed = False
        for row in active:
            parents = list(row.payload.get("parent_applications", []))
            transfer = next((t for t in state["transfers"].values() if t.in_application_id == row.id and not t.reversed_at), None)
            if transfer:
                parents.append(str(transfer.out_application_id))
            for identifier in parents:
                parent = qualification.get(uuid.UUID(identifier))
                target = qualification[row.id]
                inherited = set(parent.get("invalid_root_transaction_ids", [])) if parent else set()
                if inherited - set(target["invalid_root_transaction_ids"]):
                    target["invalid_root_transaction_ids"] = sorted(set(target["invalid_root_transaction_ids"]) | inherited)
                    changed = True
                for field in ("settlement_complete", "basis_complete"):
                    if target[field] and (parent is None or not parent[field]):
                        target[field] = False
                        target["missing_links"].append("upstream_principal_unqualified" if field == "settlement_complete" else "upstream_acquisition_unqualified")
                        changed = True
        if not changed:
            break
    for row in active:
        tx = state["transactions"].get(row.transaction_id)
        if tx:
            tx._movement_read = {**(tx.movement or {}), **qualification[row.id],
                                 "missing_links": sorted(set((tx.movement or {}).get("missing_links", [])) | set(qualification[row.id]["missing_links"]))}


async def prepare_replay(session, workspace_id, *, if_movements=False):
    if if_movements and await session.scalar(select(InvestmentMovementApplication.id).where(
        InvestmentMovementApplication.workspace_id == workspace_id,
        InvestmentMovementApplication.reversed_at.is_(None),
    ).limit(1)) is None:
        return None
    state = await _state(session, workspace_id)
    _qualify(state)
    return state


def _lot_read(asset_id, lot):
    return LotRead(asset_id=asset_id, **{key: value for key, value in lot.items() if key in LotRead.model_fields},
                   basis_complete=lot.get("acquisition_cost") is not None)


def _effect(state, asset_id):
    txs = [tx for tx in state["transactions"].values() if tx.asset_id == asset_id]
    pos = movement_replay.replay(txs)
    return HoldingEffect(asset_id=asset_id, quantity=pos["units"],
                         known_basis_quantity=pos["known_basis_quantity"], unknown_basis_quantity=pos["unknown_basis_quantity"],
                         known_acquisition_cost=pos["known_acquisition_cost"], performance_basis=pos["cost_basis"],
                         basis_complete=pos["basis_complete"], settlement_complete=pos["settlement_complete"],
                         realized_gain=pos["realized_gain"], known_realized_gain=pos["known_realized_gain"],
                         unknown_disposition_quantity=pos["unknown_disposition_quantity"], missing_links=pos["missing_links"],
                         lots=[_lot_read(asset_id, lot) for lot in pos["lots"]])


def _selection(state, selection, ordering_reviewed=False):
    from app.services.option_contract import is_option
    rows, ambiguous = _prefix(state, selection.asset_id, selection.leg_id, ordering_reviewed)
    pos = movement_replay.replay(rows)
    _, _, item = _facts(state, selection.leg_id)
    quantity = Decimal(item["quantity"]) if item.get("quantity") is not None else None
    reasons = _movement_reasons(state, selection.leg_id) + _ownership_reasons(state, selection.ownership_id, selection.asset_id, selection.leg_id)
    if is_option(_get(state, "assets", selection.asset_id).type):
        reasons.append("unsupported_transfer_asset")
    if item.get("direction") not in {"in", "out"}:
        reasons.append("ownership_effect_unknown")
    if ambiguous and not ordering_reviewed:
        reasons.append("event_order_unresolved")
    lots = []
    if len({part.lot_id for part in selection.allocations}) != len(selection.allocations):
        reasons.append("duplicate_lot_selection")
    for part in selection.allocations:
        lot = next((lot for lot in pos["lots"] if lot["lot_id"] == part.lot_id), None)
        if lot is None or part.quantity > lot["quantity"]:
            reasons.append("recorded_lot_unavailable")
            continue
        selected = deepcopy(lot)
        with localcontext(prec=128):
            selected["acquisition_cost"] = lot["acquisition_cost"] * part.quantity / lot["quantity"] if lot["acquisition_cost"] is not None else None
        selected["quantity"] = part.quantity
        lots.append(selected)
    if item.get("direction") == "out":
        with localcontext(prec=128):
            if quantity is None or sum((part.quantity for part in selection.allocations), Decimal(0)) != quantity:
                reasons.append("lot_selection_required")
        if not pos["settlement_complete"]:
            reasons.append("source_inventory_unqualified")
    elif selection.allocations:
        reasons.append("incoming_selection_requires_transfer")
    with localcontext(prec=128):
        perf = pos["cost_basis"] * quantity / pos["units"] if quantity and pos["units"] and pos["cost_basis"] is not None else None
    return reasons, pos, lots, perf, rows


async def available_lots(session, workspace_id, asset_id, before_leg_id=None):
    state = await prepare_replay(session, workspace_id)
    _get(state, "assets", asset_id)
    rows = _prefix(state, asset_id, before_leg_id, True)[0] if before_leg_id else [tx for tx in state["transactions"].values() if tx.asset_id == asset_id]
    pos = movement_replay.replay(rows)
    return LotsRead(revision=state["revision"], asset_id=asset_id,
                    lots=[_lot_read(asset_id, lot) for lot in pos["lots"]], missing_links=pos["missing_links"])


def _preview_transfer(state, data):
    with localcontext(prec=128):
        return _preview_transfer_exact(state, data)


def _preview_transfer_exact(state, data):
    out = _movement_read(state, data.out_leg_id)
    incoming = _movement_read(state, data.in_leg_id)
    selection = MovementSelection(leg_id=data.out_leg_id, asset_id=data.source_asset_id,
                                  ownership_id=data.source_ownership_id, allocations=data.allocations, reason=data.reason)
    reasons, pos, lots, perf, _ = _selection(state, selection, data.ordering_reviewed)
    reasons += _movement_reasons(state, data.in_leg_id) + _ownership_reasons(state, data.destination_ownership_id, data.destination_asset_id, data.in_leg_id)
    left = _get(state, "ownership", data.source_ownership_id)
    right = _get(state, "ownership", data.destination_ownership_id)
    if left.payload["beneficial_owner"] != right.payload["beneficial_owner"]:
        reasons.append("beneficial_owner_conflict")
    if data.source_asset_id == data.destination_asset_id or out.direction != "out" or incoming.direction != "in":
        reasons.append("transfer_direction_conflict")
    for field in ("chain", "token_address", "token_program", "transaction_ref", "leg_ref", "source_address", "destination_address", "raw_units", "decimals", "quantity_role"):
        if getattr(out, field) != getattr(incoming, field):
            reasons.append(f"{field}_conflict")
    if out.quantity != incoming.quantity or out.quantity_role != "principal":
        reasons.append("principal_quantity_conflict")
    source_asset, destination_asset = _get(state, "assets", data.source_asset_id), _get(state, "assets", data.destination_asset_id)
    if source_asset.currency != destination_asset.currency:
        reasons.append("basis_currency_conflict")
    from app.services.option_contract import is_option
    if is_option(source_asset.type) or is_option(destination_asset.type):
        reasons.append("unsupported_transfer_asset")
    fee_reads = []
    for fee in data.fees:
        fee_read = _movement_read(state, fee.leg_id)
        fee_reads.append(fee_read)
        if fee_read.quantity_role == "principal" or fee_read.direction != "out":
            reasons.append("fee_role_conflict")
        fee_reasons, *_ = _selection(state, fee, data.ordering_reviewed)
        reasons.extend(fee_reasons)
    selected_fee_keys = {_key(state, fee.leg_id) for fee in data.fees}
    if len(selected_fee_keys) != len(data.fees):
        reasons.append("duplicate_fee_selection")
    for fee in fee_reads:
        if fee.quantity_role in {"network_fee", "token_transfer_fee"} and (fee.transaction_ref != out.transaction_ref or fee.chain != out.chain):
            reasons.append("fee_transaction_conflict")
    for leg in state["legs"].values():
        facts = leg.payload
        if facts.get("transaction_ref") != out.transaction_ref or facts.get("chain") != out.chain or facts.get("quantity_role") not in {"network_fee", "withdrawal_fee", "intermediary_fee", "token_transfer_fee"}:
            continue
        if facts.get("quantity") is not None and Decimal(facts["quantity"]) == 0:
            continue
        if all(_key(state, leg.id) != _key(state, fee.leg_id) for fee in fee_reads):
            fee_reads.append(_movement_read(state, leg.id))
        # A retained fee is required only against a supported owned payer.
        owned_payer = any(not _ownership_reasons(state, assertion.id, None, leg.id)
                          for assertion in state["ownership"].values())
        old_fee = _application(state, leg.id)
        if owned_payer and _key(state, leg.id) not in selected_fee_keys and not (old_fee and not old_fee.reversed_at):
            reasons.append("owned_fee_application_required")
    for leg_id in (data.out_leg_id, data.in_leg_id):
        old = _application(state, leg_id)
        if old and old.reversed_at:
            reasons.append("application_reversed")
        if old and any(t.out_application_id == old.id or t.in_application_id == old.id for t in state["transfers"].values()):
            reasons.append("movement_already_linked")
        if old and _dependencies(state, [old]):
            reasons.append("dependent_movement_conflict")
    unknown = sum((lot["quantity"] for lot in lots if lot["acquisition_cost"] is None), Decimal(0))
    basis = None if unknown or not lots else sum((lot["acquisition_cost"] for lot in lots), Decimal(0))
    effects = []
    if not reasons:
        prospective = {**state, "transactions": dict(state["transactions"])}
        _preview_row(prospective, selection, lots=lots, performance=perf, ordering_reviewed=data.ordering_reviewed)
        _preview_row(prospective, MovementSelection(leg_id=data.in_leg_id, asset_id=data.destination_asset_id,
                     ownership_id=data.destination_ownership_id, reason=data.reason), lots=lots, performance=perf,
                     ordering_reviewed=data.ordering_reviewed)
        for fee in data.fees:
            if not _application(state, fee.leg_id):
                _preview_row(prospective, fee, ordering_reviewed=data.ordering_reviewed)
        effects = [_effect(prospective, identifier) for identifier in sorted({data.source_asset_id, data.destination_asset_id, *(fee.asset_id for fee in data.fees)}, key=str)]
        if any(not effect.settlement_complete for effect in effects):
            reasons.append("source_inventory_conflict")
    conflicts = any("conflict" in reason or reason in {"recorded_lot_unavailable", "application_reversed", "movement_already_linked"} for reason in reasons)
    return TransferPreview(workspace_id=state["workspace_id"], revision=state["revision"],
                           status="conflicting" if conflicts else "candidate" if reasons else "exact", can_confirm=not reasons,
                           reason_codes=sorted(set(reasons)), out_movement=out, in_movement=incoming,
                           available_lots=[_lot_read(data.source_asset_id, lot) for lot in pos["lots"]],
                           principal_quantity=out.quantity, acquisition_cost=basis, performance_basis=perf,
                           known_acquisition_cost=sum((lot["acquisition_cost"] for lot in lots if lot["acquisition_cost"] is not None), Decimal(0)),
                           unknown_basis_quantity=unknown, fee_movements=fee_reads, effects=effects)


def _preview_row(state, selection, *, lots=None, performance=None, ordering_reviewed=False):
    _, observation, item = _facts(state, selection.leg_id)
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, f"preview:{selection.leg_id}")
    when = observation.payload.get("event_at")
    kind = "move_in" if item["direction"] == "in" else "move_out" if item["quantity_role"] == "principal" else "fee"
    old = _application(state, selection.leg_id)
    if old and old.transaction_id:
        state["transactions"].pop(old.transaction_id, None)
    predecessors = [str(tx.id) for tx in _prefix(state, selection.asset_id, selection.leg_id, True)[0]] if ordering_reviewed else []
    tx = AssetTransaction(id=identifier, workspace_id=state["workspace_id"], asset_id=selection.asset_id,
                          kind=kind, quantity=Decimal(item["quantity"]), price=None, fee=Decimal(0),
                          date=date.fromisoformat(observation.payload["event_date"]),
                          created_at=datetime.fromisoformat(when) if when else _now(), source="evidence",
                          movement={"leg_id": str(selection.leg_id), "allocations": [part.model_dump(mode="json") for part in selection.allocations],
                                    "lots": _json(lots or []), "performance_basis": str(performance) if performance is not None else None,
                                    "predecessor_transaction_ids": predecessors,
                                    "ordering_reviewed": ordering_reviewed})
    state["transactions"][identifier] = tx


async def preview_transfer(session, workspace_id, data):
    return _preview_transfer(await prepare_replay(session, workspace_id), data)


async def preview_movement(session, workspace_id, data):
    state = await prepare_replay(session, workspace_id)
    reasons, pos, selected, *_ = _selection(state, data, data.ordering_reviewed)
    old = _application(state, data.leg_id)
    if old:
        reasons.append("application_reversed" if old.reversed_at else "already_applied")
    effects = []
    if not reasons:
        prospective = {**state, "transactions": dict(state["transactions"])}
        _preview_row(prospective, data, ordering_reviewed=data.ordering_reviewed)
        effects = [_effect(prospective, data.asset_id)]
        if not effects[0].settlement_complete:
            reasons.append("source_inventory_conflict")
    return MovementPreview(workspace_id=workspace_id, revision=state["revision"], can_confirm=not reasons,
                           reason_codes=sorted(set(reasons)), movement=_movement_read(state, data.leg_id),
                           selected_lots=[_lot_read(data.asset_id, lot) for lot in selected],
                           available_lots=[_lot_read(data.asset_id, lot) for lot in pos["lots"]], effects=effects)


def _ownership_read(row):
    payload = {key: value for key, value in row.payload.items() if key in OwnershipRead.model_fields}
    return OwnershipRead(**payload, id=row.id, workspace_id=row.workspace_id, asserted_by=row.asserted_by,
                         asserted_at=_aware(row.asserted_at), revoked_at=row.revoked_at)


async def create_ownership(session, workspace_id, user_id, data):
    await lock_workspace(session, workspace_id)
    state = await _state(session, workspace_id)
    group = _get(state, "groups", data.group_id)
    for identifier in data.evidence_observation_ids:
        observation = _get(state, "observations", identifier)
        if observation.group_id != group.id:
            raise _error(404, "evidence_not_found", "Ownership evidence is not in the selected account")
    payload = {**data.model_dump(mode="json"), "connection_id": str(group.connection_id), "account_id": str(group.account_id)}
    old = next((row for row in state["ownership"].values() if row.payload == payload and not row.revoked_at), None)
    if old:
        return _ownership_read(old)
    row = InvestmentOwnership(workspace_id=workspace_id, group_id=group.id, payload=payload, asserted_by=user_id)
    session.add(row)
    await session.commit()
    return _ownership_read(row)


async def revoke_ownership(session, workspace_id, identifier, expected_revision):
    await lock_workspace(session, workspace_id)
    state = await _state(session, workspace_id)
    row = _get(state, "ownership", identifier)
    if row.revoked_at:
        return _ownership_read(row)
    _revision(state, expected_revision)
    dependencies = [str(app.id) for app in state["applications"].values() if app.ownership_id == identifier and not app.reversed_at]
    if dependencies:
        raise _error(409, "dependent_movements", "Reverse dependent movements before revoking ownership", dependencies=dependencies)
    row.revoked_at = _now()
    await session.commit()
    return _ownership_read(row)


def _revision(state, expected):
    if state["revision"] != expected:
        raise _error(409, "revision_conflict", "Investment evidence or inventory changed; refresh the preview")


def _json(value):
    return json.loads(json.dumps(value, default=str))


async def _write_application(session, state, user_id, selection, *, ordering_reviewed=False, incoming_lots=None, incoming_perf=None):
    old = _application(state, selection.leg_id)
    if old:
        if old.reversed_at:
            raise _error(409, "application_reversed", "This movement was reversed; retain it for a new explicit review")
        if old.asset_id != selection.asset_id:
            raise _error(409, "application_conflict", "This movement already belongs to another holding")
        if selection.allocations and old.payload["request"]["allocations"] != [part.model_dump(mode="json") for part in selection.allocations]:
            raise _error(409, "allocation_conflict", "This movement already has a different recorded lot selection")
        return old, False
    reasons, _, lots, perf, prefix = _selection(state, selection, ordering_reviewed)
    if reasons:
        raise _error(422, "movement_unqualified", "Resolve movement evidence and lot selections first", reason_codes=sorted(set(reasons)))
    leg, observation, item = _facts(state, selection.leg_id)
    identifier = uuid.uuid4()
    kind = "move_in" if item["direction"] == "in" else "move_out" if item["quantity_role"] == "principal" else "fee"
    parents = {str(tx.movement["application_id"]) for tx in prefix if tx.movement and tx.movement.get("application_id")}
    acquisition_observations = [str(part.observation_id) for part in state["legs"].values() if part.asset_transaction_id in {tx.id for tx in prefix}]
    payload = {"request": MovementPreviewRequest(**selection.model_dump(), ordering_reviewed=ordering_reviewed).model_dump(mode="json"),
               "dependency_transactions": {str(tx.id): _tx_fingerprint(tx) for tx in prefix},
               "selected_lots": _json(lots),
               "acquisition_observations": acquisition_observations, "parent_applications": sorted(parents)}
    movement = {"application_id": str(identifier), "leg_id": str(leg.id), "allocations": [part.model_dump(mode="json") for part in selection.allocations],
                "lots": _json(incoming_lots if incoming_lots is not None else lots if kind != "move_in" else []),
                "performance_basis": str(incoming_perf) if incoming_perf is not None else str(perf) if perf is not None and kind != "move_in" else None,
                "missing_links": ["acquisition_missing"] if kind == "move_in" and incoming_lots is None else [],
                "predecessor_transaction_ids": [str(tx.id) for tx in prefix] if ordering_reviewed else [],
                "ordering_reviewed": ordering_reviewed}
    if kind == "move_in" and incoming_lots is not None and incoming_perf is None:
        movement["missing_links"] = ["performance_basis_unknown"]
    when = observation.payload.get("event_at")
    tx = AssetTransaction(id=uuid.uuid4(), workspace_id=state["workspace_id"], asset_id=selection.asset_id,
                          kind=kind, quantity=Decimal(item["quantity"]), price=None, fee=Decimal(0),
                          date=date.fromisoformat(observation.payload["event_date"]),
                          created_at=datetime.fromisoformat(when) if when else _now(), source="evidence", movement=movement,
                          notes="Reviewed quantity movement; tax treatment unresolved")
    row = InvestmentMovementApplication(id=identifier, workspace_id=state["workspace_id"], application_key=_key(state, leg.id),
                                        leg_id=leg.id, asset_id=selection.asset_id, ownership_id=selection.ownership_id,
                                        transaction_id=tx.id, payload=payload, created_by=user_id)
    session.add(tx)
    await session.flush()
    session.add(row)
    leg.asset_id, leg.asset_transaction_id, leg.applied_at = selection.asset_id, tx.id, _now()
    asset = _get(state, "assets", selection.asset_id)
    metadata = dict(asset.external_metadata or {})
    metadata["evidence_asset_identity"] = {key: item.get(key) for key in ("chain", "token_address", "token_program")}
    asset.external_metadata = metadata
    state["applications"][row.id] = row
    state["transactions"][tx.id] = tx
    await session.flush()
    return row, True


async def _cache(session, state, asset_ids):
    from app.services.asset_transaction_service import recompute_and_cache
    for asset_id in sorted(asset_ids, key=str):
        await recompute_and_cache(session, _get(state, "assets", asset_id))


async def qualify_asset_reads(session, workspace_id, reads):
    if not reads:
        return reads
    applications = await session.scalar(select(InvestmentMovementApplication.id).where(
        InvestmentMovementApplication.workspace_id == workspace_id,
    ).limit(1))
    if applications is None:
        return reads
    state = await prepare_replay(session, workspace_id)
    affected = {app.asset_id for app in state["applications"].values() if not app.reversed_at}
    result = []
    for read in reads:
        if read.id in affected:
            effect = _effect(state, read.id)
            if not effect.settlement_complete or effect.performance_basis is None:
                read = read.model_copy(update={"purchase_price": None, "average_price": None, "total_invested": None,
                                               "gain_loss": None, "gain_loss_primary": None, "realized_gain": None})
            elif effect.realized_gain is None:
                read = read.model_copy(update={"realized_gain": None})
        result.append(read)
    return result


async def _validate_written(session, workspace_id, asset_ids):
    state = await prepare_replay(session, workspace_id)
    failures = [str(asset_id) for asset_id in asset_ids if not _effect(state, asset_id).settlement_complete]
    if failures:
        raise _error(409, "source_inventory_conflict", "Principal and fee selections overconsume or invalidate source inventory", dependencies=failures)


async def confirm_transfer(session, workspace_id, user_id, data):
    try:
        await lock_workspace(session, workspace_id)
        state = await prepare_replay(session, workspace_id)
        request = TransferPreviewRequest.model_validate(data.model_dump(exclude={"expected_revision"}))
        identity = _digest([_key(state, data.out_leg_id), _key(state, data.in_leg_id)])
        out_existing, in_existing = _application(state, data.out_leg_id), _application(state, data.in_leg_id)
        old = next((t for t in state["transfers"].values() if t.identity_key == identity
                    or (out_existing and in_existing and t.out_application_id == out_existing.id and t.in_application_id == in_existing.id)), None)
        if old:
            if old.reversed_at or old.payload["request"] != request.model_dump(mode="json"):
                raise _error(409, "transfer_conflict", "This movement pair has a different or reversed recorded decision")
            return _transfer_read(state, old)
        _revision(state, data.expected_revision)
        preview = _preview_transfer(state, request)
        if not preview.can_confirm:
            raise _error(422, "transfer_unqualified", "Resolve transfer evidence and recorded lot selections", reason_codes=preview.reason_codes)
        outgoing = MovementSelection(leg_id=data.out_leg_id, asset_id=data.source_asset_id,
                                     ownership_id=data.source_ownership_id, allocations=data.allocations, reason=data.reason)
        _, _, selected, perf, _ = _selection(state, outgoing, data.ordering_reviewed)
        out_app, out_created = await _write_application(session, state, user_id, outgoing, ordering_reviewed=data.ordering_reviewed)
        transfer_id = uuid.uuid4()
        for lot in selected:
            lot["lineage"] = [*lot.get("lineage", []), str(transfer_id)]
        incoming = MovementSelection(leg_id=data.in_leg_id, asset_id=data.destination_asset_id,
                                     ownership_id=data.destination_ownership_id, reason=data.reason)
        in_app, in_created = await _write_application(session, state, user_id, incoming, ordering_reviewed=data.ordering_reviewed,
                                                      incoming_lots=selected, incoming_perf=perf)
        if not in_created:
            # Preserve ownership of the pre-existing application for atomic unlink restoration.
            in_tx = _get(state, "transactions", in_app.transaction_id)
            previous_in = deepcopy(in_tx.movement)
            in_tx.movement = {**in_tx.movement, "lots": _json(selected), "performance_basis": str(perf) if perf is not None else None, "missing_links": []}
        else:
            previous_in = None
        fee_ids = []
        for fee in data.fees:
            fee_app, _ = await _write_application(session, state, user_id, fee, ordering_reviewed=data.ordering_reviewed)
            fee_ids.append(str(fee_app.id))
        row = InvestmentOwnedTransfer(id=transfer_id, workspace_id=workspace_id, identity_key=identity,
                                      out_application_id=out_app.id, in_application_id=in_app.id, created_by=user_id,
                                      payload={"request": request.model_dump(mode="json"), "principal_quantity": str(preview.principal_quantity),
                                               "acquisition_cost": str(preview.acquisition_cost) if preview.acquisition_cost is not None else None,
                                               "known_acquisition_cost": str(preview.known_acquisition_cost),
                                               "performance_basis": str(perf) if perf is not None else None,
                                               "unknown_basis_quantity": str(preview.unknown_basis_quantity), "out_created": out_created,
                                               "in_created": in_created, "previous_in": previous_in, "fee_application_ids": fee_ids})
        session.add(row)
        for app in (out_app, in_app):
            tx = _get(state, "transactions", app.transaction_id)
            tx.movement = {**tx.movement, "transfer_id": str(transfer_id)}
        await session.flush()
        affected = {data.source_asset_id, data.destination_asset_id, *(fee.asset_id for fee in data.fees)}
        await _validate_written(session, workspace_id, affected)
        await _cache(session, state, affected)
        await session.commit()
        return await get_transfer(session, workspace_id, row.id)
    except Exception:
        await session.rollback()
        raise


async def apply_movement(session, workspace_id, user_id, data):
    try:
        await lock_workspace(session, workspace_id)
        state = await prepare_replay(session, workspace_id)
        old = _application(state, data.leg_id)
        request = MovementPreviewRequest.model_validate(data.model_dump(exclude={"expected_revision"}))
        if old:
            if old.reversed_at or old.payload["request"] != request.model_dump(mode="json"):
                raise _error(409, "application_conflict", "Movement already has a different or reversed application")
            return _application_read(state, old)
        _revision(state, data.expected_revision)
        row, _ = await _write_application(session, state, user_id, MovementSelection.model_validate(request.model_dump(exclude={"ordering_reviewed"})), ordering_reviewed=data.ordering_reviewed)
        await _validate_written(session, workspace_id, {row.asset_id})
        await _cache(session, state, {row.asset_id})
        await session.commit()
        return _application_read(await prepare_replay(session, workspace_id), row)
    except Exception:
        await session.rollback()
        raise


def _transfer_read(state, row):
    payload = row.payload
    apps = [state["applications"][row.out_application_id], state["applications"][row.in_application_id]]
    reasons = sorted({reason for app in apps for reason in state["qualification"].get(app.id, {}).get("missing_links", [])})
    received = state["transactions"].get(apps[1].transaction_id)
    received_lots = (getattr(received, "_movement_read", None) or received.movement or {}).get("lots", []) if received else []
    invalid_roots = state["qualification"].get(apps[1].id, {}).get("invalid_root_transaction_ids", [])
    with localcontext(prec=128):
        known_cost = sum((Decimal(lot["acquisition_cost"]) for lot in received_lots
                         if lot.get("acquisition_cost") is not None and lot.get("root_transaction_id") not in invalid_roots), Decimal(0))
        unknown_quantity = sum((Decimal(lot["quantity"]) for lot in received_lots
                                if lot.get("acquisition_cost") is None or lot.get("root_transaction_id") in invalid_roots), Decimal(0))
    principal_valid = all(state["qualification"].get(app.id, {}).get("settlement_complete", True) for app in apps)
    return TransferRead(id=row.id, workspace_id=row.workspace_id, revision=state["revision"],
                        status="reversed" if row.reversed_at else "unresolved" if reasons else "confirmed",
                        request=payload["request"], principal_quantity=payload["principal_quantity"],
                        acquisition_cost=None if reasons else payload["acquisition_cost"],
                        known_acquisition_cost=known_cost if invalid_roots and principal_valid else Decimal(0) if reasons else payload.get("known_acquisition_cost", payload["acquisition_cost"] or "0"),
                        performance_basis=None if reasons else payload["performance_basis"],
                        unknown_basis_quantity=unknown_quantity if invalid_roots and principal_valid else payload["principal_quantity"] if reasons else payload["unknown_basis_quantity"],
                        created_at=_aware(row.created_at), reversed_at=row.reversed_at, reason_codes=reasons,
                        effects=[_effect(state, asset_id) for asset_id in sorted({app.asset_id for app in apps}, key=str)])


def _application_read(state, row):
    reasons = state["qualification"].get(row.id, {}).get("missing_links", [])
    qualification = state["qualification"].get(row.id, {})
    tx = state["transactions"].get(row.transaction_id)
    selected = deepcopy(row.payload.get("selected_lots", (tx.movement or {}).get("lots", []) if tx and row.payload["request"].get("allocations") else []))
    for lot in selected:
        invalid_roots = qualification.get("invalid_root_transaction_ids", [])
        if qualification.get("settlement_complete") is False or (qualification.get("basis_complete") is False
                and (not invalid_roots or lot.get("root_transaction_id") in invalid_roots)):
            lot["acquisition_cost"] = lot["acquired"] = None
            lot["missing_links"] = sorted(set(lot.get("missing_links", [])) | set(reasons))
    return MovementApplicationRead(id=row.id, workspace_id=row.workspace_id, revision=state["revision"],
                                   status="reversed" if row.reversed_at else "unresolved" if reasons else "applied",
                                   request=row.payload["request"], created_at=_aware(row.created_at), reversed_at=row.reversed_at,
                                   selected_lots=[_lot_read(row.asset_id, lot) for lot in selected],
                                   reason_codes=reasons, effects=[_effect(state, row.asset_id)])


async def get_transfer(session, workspace_id, identifier):
    state = await prepare_replay(session, workspace_id)
    return _transfer_read(state, _get(state, "transfers", identifier))


def _dependencies(state, applications):
    ids = {str(row.id) for row in applications}
    transaction_ids = {str(row.transaction_id) for row in applications if row.transaction_id}
    dependents = [str(row.id) for row in state["applications"].values() if not row.reversed_at and str(row.id) not in ids
                  and (set(row.payload.get("parent_applications", [])) & ids or set(row.payload.get("dependency_transactions", {})) & transaction_ids)]
    for row in applications:
        tx = state["transactions"].get(row.transaction_id)
        if tx:
            dependents.extend(str(other.id) for other in state["transactions"].values()
                              if other.asset_id == tx.asset_id and str(other.id) not in transaction_ids
                              and other.kind == "sell" and other.date >= tx.date)
    return sorted(set(dependents))


async def _remove_application(session, state, app):
    tx = state["transactions"].get(app.transaction_id)
    if tx:
        for leg in state["legs"].values():
            if leg.asset_transaction_id == tx.id:
                leg.asset_transaction_id = None  # applied_at intentionally remains a tombstone.
        app.transaction_id = None
        await session.flush()
        await session.delete(tx)
    app.reversed_at = _now()


async def reverse_transfer(session, workspace_id, identifier, expected_revision):
    try:
        await lock_workspace(session, workspace_id)
        state = await prepare_replay(session, workspace_id)
        row = _get(state, "transfers", identifier)
        if row.reversed_at:
            return _transfer_read(state, row)
        _revision(state, expected_revision)
        apps = [state["applications"][row.out_application_id], state["applications"][row.in_application_id]]
        dependencies = _dependencies(state, apps)
        if dependencies:
            raise _error(409, "dependent_movements", "Reverse or review dependent activity first", dependencies=dependencies)
        for index, app in enumerate(apps):
            if row.payload["out_created" if index == 0 else "in_created"]:
                await _remove_application(session, state, app)
            elif index == 1:
                _get(state, "transactions", app.transaction_id).movement = row.payload["previous_in"]
        row.reversed_at = _now()
        await session.flush()
        await _cache(session, state, {app.asset_id for app in apps})
        await session.commit()
        return await get_transfer(session, workspace_id, identifier)
    except Exception:
        await session.rollback()
        raise


async def reverse_movement(session, workspace_id, identifier, expected_revision):
    try:
        await lock_workspace(session, workspace_id)
        state = await prepare_replay(session, workspace_id)
        row = _get(state, "applications", identifier)
        if row.reversed_at:
            return _application_read(state, row)
        _revision(state, expected_revision)
        dependencies = _dependencies(state, [row]) + [str(t.id) for t in state["transfers"].values() if not t.reversed_at and (identifier in {t.out_application_id, t.in_application_id} or str(identifier) in t.payload.get("fee_application_ids", []))]
        if dependencies:
            raise _error(409, "dependent_movements", "Reverse dependent links before this movement", dependencies=dependencies)
        await _remove_application(session, state, row)
        await session.flush()
        await _cache(session, state, {row.asset_id})
        await session.commit()
        return _application_read(await prepare_replay(session, workspace_id), row)
    except Exception:
        await session.rollback()
        raise


async def guard_asset_mutation(session, workspace_id, asset_ids, *, transaction_ids=(), before_date=None):
    """Shared pre-write guard for manual changes, imports, undo and deletion."""
    await lock_workspace(session, workspace_id)
    state = await _state(session, workspace_id)
    tx_ids = {str(identifier) for identifier in transaction_ids}
    dependencies = []
    for row in state["applications"].values():
        if row.reversed_at and not (before_date is None and row.asset_id in asset_ids):
            continue
        if row.asset_id in asset_ids:
            _, observation, _ = _facts(state, row.leg_id)
            if before_date is None or not observation.payload.get("event_date") or before_date.isoformat() <= observation.payload["event_date"]:
                dependencies.append(str(row.id))
        if set(row.payload.get("dependency_transactions", {})) & tx_ids:
            dependencies.append(str(row.id))
    if dependencies:
        raise _error(409, "dependent_movements", "This change would alter recorded movement or lot decisions; reverse dependents first", dependencies=sorted(set(dependencies)))


async def guard_scope_mutation(session, workspace_id, *, group_ids=(), account_ids=(), connection_ids=()):
    state = await _state(session, workspace_id)
    groups = set(group_ids) | {group.id for group in state["groups"].values()
                               if group.account_id in account_ids or group.connection_id in connection_ids}
    await guard_asset_mutation(session, workspace_id, {asset.id for asset in state["assets"].values() if asset.group_id in groups})
    retained = [str(row.id) for row in state["ownership"].values() if row.group_id in groups]
    if retained:
        raise _error(409, "retained_ownership", "This mapping retains ownership evidence; keep the account or wallet for its history", dependencies=retained)


def _incident_read(row):
    return IncidentRead(**row.payload, id=row.id, workspace_id=row.workspace_id, created_by=row.created_by,
                        created_at=_aware(row.created_at), updated_at=_aware(row.updated_at))


async def annotate_incident(session, workspace_id, user_id, data, identifier=None):
    state = await _state(session, workspace_id)
    _, _, item = _facts(state, data.leg_id)
    if item.get("direction") != "out" or item.get("classification") not in {"transfer", "unknown"}:
        raise _error(422, "incident_requires_outbound", "Annotate an outbound external principal movement")
    app = _application(state, data.leg_id)
    if app and any(t.out_application_id == app.id and not t.reversed_at for t in state["transfers"].values()):
        raise _error(409, "owned_transfer_incident", "An internal transfer is not an external incident payment")
    for ref in data.evidence_observation_ids:
        _get(state, "observations", ref)
    for ref in data.related_fee_leg_ids:
        _, _, fee = _facts(state, ref)
        if fee.get("quantity_role") not in {"network_fee", "withdrawal_fee", "intermediary_fee", "token_transfer_fee"}:
            raise _error(422, "fee_role_conflict", "Incident fees must reference separate supported fee legs")
    row = _get(state, "incidents", identifier) if identifier else InvestmentIncident(workspace_id=workspace_id, leg_id=data.leg_id, created_by=user_id)
    row.payload, row.updated_at = data.model_dump(mode="json"), _now()
    session.add(row)
    await session.commit()
    return _incident_read(row)


async def list_transfers(session, workspace_id):
    state = await prepare_replay(session, workspace_id)
    return TransferIndex(workspace_id=workspace_id, revision=state["revision"],
                         transfers=[_transfer_read(state, row) for row in state["transfers"].values()],
                         movements=[_movement_read(state, row.id) for row in state["legs"].values() if row.payload.get("classification") in {"transfer", "fee", "unknown"}],
                         applications=[_application_read(state, row) for row in state["applications"].values()],
                         ownership=[_ownership_read(row) for row in state["ownership"].values()],
                         holdings=[TransferHolding.model_validate(row) for row in state["assets"].values()],
                         incidents=[_incident_read(row) for row in state["incidents"].values()])
