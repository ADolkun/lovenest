"""Review source evidence before crossing the existing buy/sell ledger boundary."""
import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from typing import cast

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.models.bank_connection import BankConnection
from app.models.import_log import ImportLog
from app.models.investment_evidence import (
    InvestmentEvent, InvestmentLeg, InvestmentObservation, InvestmentObservationLink,
)
from app.models.workspace import Workspace
from app.schemas.asset_import import AssetOrderImport
from app.schemas.investment_evidence import (
    EvidenceCandidate, EvidenceEffects, EvidenceLegInput, EvidenceLinkedLeg,
    EvidenceObservationInput, EvidencePreview,
    EvidenceReconciliation, EvidenceRecord, EvidenceResult, EvidenceSourceRef, EvidenceTarget,
)
from app.services import asset_import_service, asset_transaction_service


def _sum_exact(values):
    # The source schema bounds coefficients/exponents to 128 digits; 512
    # covers their products and aligned sums without rounding source facts.
    with localcontext(prec=512):
        return sum(values, Decimal("0"))


def _product_exact(left, right):
    with localcontext(prec=512):
        return left * right


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _identity(group_id, connection_id, observation: EvidenceObservationInput) -> str:
    return _digest([
        str(group_id), str(connection_id), observation.provider, observation.source,
        observation.source_account_id,
        observation.source_local_id or observation.source_reference or observation.reference,
    ])


def _fingerprint(observation: EvidenceObservationInput) -> str:
    # Retrieval time and display locator can change on an identical later
    # export. Neither changes the facts the source reported.
    return _digest(observation.model_dump(mode="json", exclude={
        "observed_at", "reference", "source_reference", "source_locator",
    }))


def _input(row: InvestmentObservation) -> EvidenceObservationInput:
    data = dict(row.payload)
    data["source_reference"] = data.get("source_reference") or data["reference"]
    data["reference"] = str(row.id)
    return EvidenceObservationInput.model_validate(data)


def _ref(observation: EvidenceObservationInput, key: str) -> EvidenceSourceRef:
    return EvidenceSourceRef(
        observation_ref=observation.reference, source=observation.source,
        source_local_id=observation.source_local_id,
        source_locator=observation.source_locator, leg_key=key,
    )


async def _scope(session, workspace_id, group_id, connection_id=None, *, lock=False):
    group = await session.scalar(select(AssetGroup).where(
        AssetGroup.id == group_id, AssetGroup.workspace_id == workspace_id,
    ))
    if group is None:
        raise HTTPException(404, "Group not found")
    # Match the sync lock order: connection, then wallet. A confirmation
    # cannot race the provider replacing its snapshot or writing its ledger.
    cid = connection_id or group.connection_id
    if connection_id and group.connection_id != connection_id:
        raise HTTPException(422, "Connection must own the selected wallet")
    if cid:
        query = select(BankConnection).where(
            BankConnection.id == cid, BankConnection.workspace_id == workspace_id,
        )
        connection = await session.scalar(query.with_for_update() if lock else query)
        if connection is None:
            raise HTTPException(404, "Connection not found")
    if lock:
        group = await session.scalar(select(AssetGroup).where(
            AssetGroup.id == group_id, AssetGroup.workspace_id == workspace_id,
        ).with_for_update().execution_options(populate_existing=True))
        if group is None or (cid and group.connection_id != cid):
            raise HTTPException(409, "Wallet mapping changed; preview again")
    if group.account_id:
        account = await session.scalar(select(Account).where(
            Account.id == group.account_id, Account.workspace_id == workspace_id,
        ))
        if account is None:
            raise HTTPException(404, "Account not found")
    return group, cid


async def _state(session, workspace_id, group_id):
    observations = list((await session.scalars(select(InvestmentObservation).where(
        InvestmentObservation.workspace_id == workspace_id,
        InvestmentObservation.group_id == group_id,
    ).execution_options(populate_existing=True))).all())
    events = list((await session.scalars(select(InvestmentEvent).where(
        InvestmentEvent.workspace_id == workspace_id, InvestmentEvent.group_id == group_id,
    ).execution_options(populate_existing=True))).all())
    event_ids = [event.id for event in events]
    legs = list((await session.scalars(select(InvestmentLeg).where(
        InvestmentLeg.workspace_id == workspace_id, InvestmentLeg.event_id.in_(event_ids),
    ).execution_options(populate_existing=True))).all()) if event_ids else []
    links = list((await session.scalars(select(InvestmentObservationLink).where(
        InvestmentObservationLink.workspace_id == workspace_id,
        InvestmentObservationLink.observation_id.in_([o.id for o in observations]),
    ).execution_options(populate_existing=True))).all()) if observations else []
    assets = list((await session.scalars(select(Asset).where(
        Asset.workspace_id == workspace_id, Asset.group_id == group_id,
    ).execution_options(populate_existing=True))).all())
    transactions = list((await session.scalars(select(AssetTransaction).where(
        AssetTransaction.workspace_id == workspace_id,
        AssetTransaction.asset_id.in_([asset.id for asset in assets]),
    ).execution_options(populate_existing=True))).all()) if assets else []
    return observations, events, legs, links, assets, transactions


def _legacy(tx, asset):
    return EvidenceObservationInput(
        reference=f"ledger:{tx.id}", source="ledger", provider=tx.source,
        source_local_id=tx.external_id, source_locator=f"asset_transactions/{tx.id}",
        event_date=tx.date, time_precision="date", settlement_status="settled",
        coverage=["original_source_precision_unavailable"],
        legs=[EvidenceLegInput(
            key="ledger", asset_symbol=asset.ticker, asset_id=asset.id,
            direction="in" if tx.kind == "buy" else "out", classification=tx.kind,
            quantity=tx.quantity, unit_price=tx.price, fee=tx.fee,
            valuation_currency=asset.currency, fee_currency=asset.currency,
            execution_currency=asset.currency, unit_price_origin="reported",
            execution_id=tx.external_id,
        )],
    )


def _same_asset(left: EvidenceLegInput, right: EvidenceLegInput) -> bool:
    for field in ("chain", "token_address", "isin", "provider_asset_id"):
        a, b = getattr(left, field), getattr(right, field)
        if a and b and a != b:
            return False
    if left.asset_id and right.asset_id and left.asset_id == right.asset_id:
        return True
    if any(getattr(left, field) and getattr(left, field) == getattr(right, field) for field in ("isin", "provider_asset_id")):
        return True
    if left.chain and left.chain == right.chain and left.token_address and left.token_address == right.token_address:
        return True
    return bool(left.asset_symbol and right.asset_symbol and left.asset_symbol.upper() == right.asset_symbol.upper())


def _status_progression(left, right):
    """A source filling previously unknown facts is not another execution."""
    if not left.source_local_id or left.source_local_id != right.source_local_id:
        return False
    if (left.source, left.provider, left.source_account_id) != (right.source, right.provider, right.source_account_id):
        return False
    if {left.settlement_status, right.settlement_status} - {"unknown", "pending", "settled"}:
        return False
    for field in ("source_kind", "event_time_raw", "event_date", "event_at", "order_ref"):
        a, b = getattr(left, field), getattr(right, field)
        if a is not None and b is not None and a != b:
            return False
    if {item.key for item in left.legs} != {item.key for item in right.legs}:
        return False
    for a in left.legs:
        b = next(part for part in right.legs if part.key == a.key)
        for field in type(a).model_fields:
            av, bv = getattr(a, field), getattr(b, field)
            if field == "unit_price_origin" and "unknown" in (av, bv):
                continue
            if av is not None and bv is not None and av != bv:
                return False
    return True


def _comparison_fields(left, item, right, other):
    fields = []
    if left.source_kind == "primary_activity" and right.source_kind == "primary_activity":
        if item.classification != other.classification:
            fields.append("classification")
        if left.settlement_status != "unknown" and right.settlement_status != "unknown" and left.settlement_status != right.settlement_status:
            fields.append("settlement_status")
        if item.execution_currency and other.execution_currency and item.execution_currency != other.execution_currency:
            fields.append("execution_currency")
        for field in ("unit_price", "subtotal", "total", "fee", "acquisition_basis"):
            a, b = getattr(item, field), getattr(other, field)
            if a is not None and b is not None and a != b and item.quantity == other.quantity:
                fields.append(field)
        if left.event_at and right.event_at and abs((left.event_at - right.event_at).total_seconds()) > 2:
            fields.append("event_at")
    if left.source_kind == "tax_workpaper" and item.classification not in {"lot", other.classification}:
        fields.append("classification")
    return fields


def _holding_conflicts(item, asset):
    metadata = asset.external_metadata or {}
    identity = metadata.get("evidence_asset_identity") or metadata
    fields = []
    if item.asset_symbol and asset.ticker and item.asset_symbol.upper() != asset.ticker.upper():
        fields.append("asset_symbol")
    for field in ("chain", "token_address", "provider_asset_id"):
        if getattr(item, field) and getattr(item, field) != identity.get(field):
            fields.append(field if identity.get(field) else "unverified_holding_identity")
    if item.isin and item.isin != asset.isin:
        fields.append("isin")
    return list(dict.fromkeys(fields))


def _conflicts(observation, leg):
    fields = []
    if leg.total is not None and leg.subtotal is not None and leg.fee is not None:
        expected = _sum_exact([leg.subtotal, leg.fee if leg.direction == "in" else leg.fee.copy_negate()])
        if expected != leg.total:
            fields.append("total")
    if leg.unit_price is not None and leg.subtotal is not None and leg.quantity is not None and leg.unit_price_origin not in {"derived_execution", "derived_spot"}:
        with localcontext() as ctx:
            ctx.prec = 512
            if leg.unit_price * leg.quantity != leg.subtotal:
                fields.append("subtotal")
    if leg.fee not in (None, Decimal("0")) and leg.fee_currency != leg.execution_currency:
        fields.append("fee_currency")
    if observation.settlement_status in {"failed", "pending"}:
        fields.append("settlement_status")
    if observation.reason_codes:
        fields.extend(observation.reason_codes)
    return list(dict.fromkeys(fields))


def _price(observation, leg, opening_boundary=None):
    secondary = observation.source_kind != "primary_activity"
    if secondary and not (opening_boundary and observation.source_kind in {"remaining_lots", "tax_workpaper"}):
        return None
    if leg.quantity is None or leg.quantity <= 0 or not observation.event_date:
        return None
    if not secondary and leg.classification not in {"buy", "sell", "income"}:
        return None
    if leg.execution_currency is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 512
        if leg.acquisition_basis is not None and (secondary or leg.classification != "sell"):
            return leg.acquisition_basis / leg.quantity
        if leg.unit_price is not None:
            return leg.unit_price
        if leg.subtotal is not None:
            return leg.subtotal / leg.quantity
    return None


def _candidate(observation, leg, other, other_leg, boundaries=None):
    if _status_progression(observation, other):
        return False
    boundaries = boundaries or {}
    for primary, secondary in ((observation, other), (other, observation)):
        boundary = boundaries.get(secondary.reference)
        if primary.source_kind == "primary_activity" and secondary.source_kind != "primary_activity" and boundary and primary.event_date and primary.event_date > boundary:
            return False
    if not _same_asset(leg, other_leg) or leg.direction != other_leg.direction:
        return False
    if observation.source_kind == "primary_activity" and other.source_kind == "balance_snapshot":
        return False
    same_source = (
        observation.source == other.source and observation.provider == other.provider
        and observation.source_account_id == other.source_account_id
    )
    if same_source and leg.execution_id and other_leg.execution_id and leg.execution_id != other_leg.execution_id:
        return False
    if observation.source_kind != "primary_activity" or other.source_kind != "primary_activity":
        return True  # lots and snapshots assert inventory, not another acquisition
    shared_ref = bool(
        (observation.order_ref and observation.order_ref == other.order_ref)
        or (leg.transaction_ref and leg.transaction_ref == other_leg.transaction_ref)
    )
    if shared_ref:
        return True
    if observation.event_date and other.event_date:
        return abs((observation.event_date - other.event_date).days) <= 1 and leg.quantity == other_leg.quantity
    return leg.quantity is not None and leg.quantity == other_leg.quantity


async def preview_evidence(
    session: AsyncSession, workspace_id, group_id, observations=(), *,
    connection_id=None, decisions=(), opening_boundary=None,
) -> EvidencePreview:
    group, cid = await _scope(session, workspace_id, group_id, connection_id)
    state = await _state(session, workspace_id, group_id)
    saved, events, legs, links, assets, txs = state
    by_id = {o.id: o for o in saved}
    event_by_id = {event.id: event for event in events}
    boundaries = {
        str(leg.observation_id): date.fromisoformat(event_by_id[leg.event_id].opening_boundary["as_of"])
        for leg in legs if event_by_id[leg.event_id].opening_boundary
    }
    known = {(o.identity_key, o.fingerprint): o for o in saved}
    inputs = {_input(o).reference: _input(o) for o in saved}
    identities = {o.reference: _identity(group.id, cid, o) for o in inputs.values()}
    for observation in observations:
        identity = _identity(group.id, cid, observation)
        old = known.get((identity, _fingerprint(observation)))
        item = _input(old) if old else observation
        inputs[item.reference] = item
        identities[item.reference] = identity
    for observation in inputs.values():
        for leg in observation.legs:
            if leg.asset_id and not any(a.id == leg.asset_id for a in assets):
                raise HTTPException(404, "Asset not found in selected wallet")
            if leg.asset_id:
                asset = next(a for a in assets if a.id == leg.asset_id)
                metadata = asset.external_metadata or {}
                identity = metadata.get("evidence_asset_identity") or metadata
                if any(getattr(leg, key) and identity.get(key) and getattr(leg, key) != identity[key] for key in ("chain", "token_address", "provider_asset_id")):
                    raise HTTPException(422, "Asset identity contradicts the selected holding")
    asset_by_id = {a.id: a for a in assets}
    represented = {(str(link.observation_id), link.source_leg_key) for link in links}
    pool = []
    for leg in legs:
        source = _input(by_id[leg.observation_id])
        if (source.reference, leg.source_leg_key) in represented:
            continue
        pool.append((leg.id, leg.event_id, source, EvidenceLegInput.model_validate(leg.payload), leg))
    wrapped = {leg.asset_transaction_id for leg in legs}
    for tx in txs:
        if tx.id not in wrapped:
            source = _legacy(tx, asset_by_id[tx.asset_id])
            pool.append((tx.id, tx.id, source, source.legs[0], None))
    records = []
    for observation in inputs.values():
        for item in observation.legs:
            own = next((leg for leg in legs if str(leg.observation_id) == observation.reference and leg.source_leg_key == item.key), None)
            related = [link for link in links if str(link.observation_id) == observation.reference and link.source_leg_key == item.key]
            active = [link for link in related if link.reversed_at is None]
            application = bool(own and own.applied_at) or any(
                leg.applied_at for leg in legs if leg.id in {link.leg_id for link in related}
            )
            application_reversed = bool(own and own.applied_at and own.asset_transaction_id is None)
            conflicts = _conflicts(observation, item)
            matches = [asset for asset in assets if asset.id == item.asset_id] if item.asset_id else [asset for asset in assets if asset.ticker and item.asset_symbol and asset.ticker.upper() == item.asset_symbol.upper()]
            if len(matches) == 1:
                conflicts.extend(_holding_conflicts(item, matches[0]))
            elif len(matches) > 1:
                conflicts.append("ambiguous_holding_identity")
            reasons = []
            candidates = []
            for lid, eid, other, other_leg, canonical in pool:
                if other.reference == observation.reference or lid in {link.leg_id for link in related}:
                    continue
                if _candidate(observation, item, other, other_leg, boundaries):
                    candidates.append(EvidenceCandidate(
                        leg_id=lid, event_id=eid, asset_symbol=other_leg.asset_symbol,
                        direction=other_leg.direction, classification=other_leg.classification,
                        quantity=other_leg.quantity, event_date=other.event_date,
                        source_refs=[_ref(other, other_leg.key)],
                    ))
                    conflicts.extend(_comparison_fields(observation, item, other, other_leg))
                    if item.classification != other_leg.classification and observation.source_kind == "tax_workpaper":
                        conflicts.append("classification")
            # Unsaved observations also participate: two unknown executions
            # in the same upload must not both be promised as safe to apply.
            ambiguous_draft = any(
                other.reference != observation.reference and any(
                    (other.reference, other_leg.key) not in represented
                    and (observation.reference, item.key) not in represented
                    and _candidate(observation, item, other, other_leg, boundaries) for other_leg in other.legs
                ) for other in inputs.values()
            )
            versions = {o.fingerprint for o in saved if o.identity_key == identities.get(observation.reference) and not _status_progression(observation, _input(o))}
            if versions and _fingerprint(observation) not in versions:
                conflicts.append("source_version")
            if len(versions) > 1:
                conflicts.append("source_version")
            price = _price(observation, item, opening_boundary)
            applicable = observation.source_kind == "primary_activity" and item.classification in {"buy", "sell", "income"}
            applicable |= bool(opening_boundary and observation.source_kind in {"remaining_lots", "tax_workpaper"})
            if observation.source_kind != "primary_activity":
                reasons.append("secondary_evidence")
            if conflicts:
                reasons.append("conflicting_source_fields")
            if candidates or ambiguous_draft:
                reasons.append("possible_overlap")
            # A two-sided conversion can be investigated without pretending
            # that its unlike assets are duplicate legs. Only a source order
            # reference groups them automatically in _retain.
            grouping = [other for other in inputs.values() if (
                other.reference != observation.reference
                and observation.source_kind == other.source_kind == "primary_activity"
                and observation.provider == other.provider
                and observation.order_ref is None and other.order_ref is None
                and observation.event_at is not None and other.event_at is not None
                and abs((observation.event_at - other.event_at).total_seconds()) <= 2
                and any(part.direction != item.direction and part.direction != "unknown" and not _same_asset(item, part) for part in other.legs)
            )]
            if grouping:
                reasons.append("possible_conversion_group")
            if application and candidates:
                reasons.append("already_applied_overlap")
            if applicable and price is None:
                reasons.append("missing_supported_acquisition_value")
            if applicable and price is not None and (
                asset_import_service._to_ledger_scale(price) != price
                or asset_import_service._to_ledger_scale(item.quantity) != item.quantity
                or (item.fee is not None and item.fee != item.fee.quantize(Decimal("0.01"), context=Context(prec=512)))
            ):
                conflicts.append("ledger_scale_loss")
            if observation.settlement_status == "unknown":
                reasons.append("settlement_review_required")
            if item.fee is None:
                reasons.append("unknown_fee")
            fee_supported = item.fee is not None or item.acquisition_basis is not None or item.unit_price_origin == "derived_execution"
            if applicable and not fee_supported:
                reasons.append("fee_assumption_required")
            match = "linked" if active else "conflicting" if conflicts else "candidate" if candidates or ambiguous_draft or grouping else "unmatched"
            status = "already_applied" if application else "not_applicable" if not applicable else "blocked" if conflicts or candidates or ambiguous_draft or price is None or not fee_supported else "eligible"
            if application_reversed:
                status = "blocked"
                reasons.append("application_reversed")
            if applicable and opening_boundary and not opening_boundary.overlap_reviewed:
                status = "blocked"
                reasons.append("opening_overlap_review_required")
            quantity = item.quantity or Decimal("0")
            effects = EvidenceEffects()
            if status == "eligible":
                effects = EvidenceEffects(
                    ledger_rows=1, units_delta=quantity if item.direction == "in" else quantity.copy_negate(),
                    basis_delta=_sum_exact([_product_exact(quantity, price), ((item.fee or Decimal("0")) if item.acquisition_basis is None and item.unit_price_origin != "derived_execution" else Decimal("0"))]) if item.direction == "in" else None,
                )
            refs = [_ref(observation, item.key)]
            refs.extend(_ref(other, part.key) for other in grouping for part in other.legs)
            linked_legs = []
            for link in active:
                target = next(part for part in legs if part.id == link.leg_id)
                target_observation = _input(by_id[target.observation_id])
                target_part = EvidenceLegInput.model_validate(target.payload)
                target_ref = _ref(target_observation, target_part.key)
                refs.append(target_ref)
                linked_legs.append(EvidenceLinkedLeg(
                    link_id=link.id, quantity=Decimal(link.quantity) if link.quantity else None,
                    leg=EvidenceCandidate(
                        leg_id=target.id, event_id=target.event_id,
                        asset_symbol=target_part.asset_symbol, direction=target_part.direction,
                        classification=target_part.classification, quantity=target_part.quantity,
                        event_date=target_observation.event_date, source_refs=[target_ref],
                    ),
                ))
            records.append(EvidenceRecord(
                observation_ref=observation.reference, leg_key=item.key,
                match_status=match, application_status=status, source_refs=refs, links=linked_legs,
                candidate_legs=candidates, link_ids=[link.id for link in active],
                reason_codes=list(dict.fromkeys(reasons)), conflicting_fields=list(dict.fromkeys(conflicts)), effects=effects,
            ))
    revision = _digest({
        "group": [str(group.id), str(group.connection_id), str(group.account_id)],
        "observations": sorted((identities[o.reference], _fingerprint(o)) for o in inputs.values()),
        "legs": sorted((str(leg.id), str(leg.asset_transaction_id), str(leg.applied_at)) for leg in legs),
        "links": sorted((str(link.id), str(link.reversed_at)) for link in links),
        "ledger": sorted((str(tx.id), str(tx.asset_id), tx.kind, str(tx.quantity), str(tx.price), str(tx.fee), str(tx.date)) for tx in txs),
        "assets": sorted((str(a.id), str(a.units), str(a.group_id), str(a.connection_id)) for a in assets),
        "opening_boundary": opening_boundary.model_dump(mode="json") if opening_boundary else None,
    })
    reconciliation = _reconciliation(inputs, records, legs, events, opening_boundary)
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(404, "Workspace not found")
    account_id = group.account_id
    if account_id is None and group.connection_id:
        from app.services.connection_service import _wallet_external_id
        connection = await session.get(BankConnection, group.connection_id)
        accounts = list((await session.scalars(select(Account).where(
            Account.workspace_id == workspace_id, Account.connection_id == group.connection_id,
        ))).all())
        mapped = [account.id for account in accounts if connection and (
            _wallet_external_id(connection.external_id, account.external_id) == group.external_id
        )]
        if len(mapped) == 1:
            account_id = mapped[0]
    return EvidencePreview(
        revision=revision, target=EvidenceTarget(
            workspace_id=workspace_id, workspace_name=workspace.name,
            group_id=group.id, group_name=group.name, account_id=account_id,
        ), observations=list(inputs.values()), records=records, reconciliation=reconciliation,
    )


def _reconciliation(inputs, records, legs, events, opening_boundary):
    identity_fields = ("asset_symbol", "chain", "token_address", "provider_asset_id", "isin")
    scopes = {}
    for observation in inputs.values():
        for item in observation.legs:
            key = tuple(getattr(item, field) for field in identity_fields)
            scopes.setdefault(key, []).append((observation, item))
    by_record = {(record.observation_ref, record.leg_key): record for record in records}
    result = []
    for identity, evidence in scopes.items():
        snapshots = [(observation, item) for observation, item in evidence if observation.source_kind == "balance_snapshot"]
        snapshot, snapshot_item = max(snapshots, key=lambda pair: str(pair[0].event_at or pair[0].event_date or "")) if snapshots else (None, None)
        boundary = opening_boundary
        relevant_events = [event for event in events if event.opening_boundary and any(
            leg.event_id == event.id and tuple(leg.payload.get(field) for field in identity_fields) == identity for leg in legs
        )]
        if boundary is None and relevant_events:
            from app.schemas.investment_evidence import EvidenceOpeningBoundary
            boundary = EvidenceOpeningBoundary.model_validate(relevant_events[0].opening_boundary)
        opening = None
        if boundary:
            lot_quantities = [item.quantity for observation, item in evidence if observation.source_kind in {"remaining_lots", "tax_workpaper"} and item.quantity is not None]
            if lot_quantities:
                opening = _sum_exact(lot_quantities)
        signed = Decimal("0")
        missing = {"lifetime_history_unverified"}
        if opening is None:
            missing.add("opening_balance_unknown")
        counted = set()
        for observation, item in evidence:
            missing.update(observation.coverage)
            record = by_record[(observation.reference, item.key)]
            if observation.source_kind != "primary_activity":
                continue
            own = next((leg for leg in legs if str(leg.observation_id) == observation.reference and leg.source_leg_key == item.key), None)
            # Corroboration does not add another movement. Only canonical
            # settled primary legs enter the quantity equation.
            if record.links or (record.application_status == "already_applied" and (own is None or not own.asset_transaction_id)):
                continue
            if record.match_status in {"candidate", "conflicting"} or observation.settlement_status != "settled":
                missing.add("unresolved_movements")
                continue
            if boundary and (observation.event_date is None or observation.event_date <= boundary.as_of):
                continue
            if snapshot and snapshot.event_date and (observation.event_date is None or observation.event_date > snapshot.event_date):
                continue
            if item.quantity is None or item.direction == "unknown":
                missing.add("unknown_movement_quantity")
                continue
            if item.classification not in {"buy", "sell", "income", "transfer", "fee"}:
                missing.add("unsupported_movement_classification")
                continue
            canonical_key = str(own.id) if own else (observation.reference, item.key)
            if canonical_key not in counted:
                signed = _sum_exact([signed, item.quantity if item.direction == "in" else item.quantity.copy_negate()])
                counted.add(canonical_key)
        closing = _sum_exact([opening, signed]) if opening is not None else None
        result.append(EvidenceReconciliation(
            **dict(zip(identity_fields, identity)), opening_quantity=opening,
            opening_assumption=boundary.assumption if boundary else "unknown",
            opening_as_of=boundary.as_of if boundary else None,
            snapshot_quantity=snapshot_item.quantity if snapshot_item else None,
            snapshot_as_of=(snapshot.event_time_raw or (snapshot.event_date.isoformat() if snapshot.event_date else None)) if snapshot else None,
            settled_movement_quantity=signed, expected_closing_quantity=closing,
            discrepancy=_sum_exact([snapshot_item.quantity, closing.copy_negate()]) if snapshot_item and snapshot_item.quantity is not None and closing is not None else None,
            missing_coverage=sorted(missing),
            unresolved_fee_semantics=any(item.fee is None for _, item in evidence),
            unresolved_funding_semantics=any(item.external_funding_amount is None for _, item in evidence),
        ))
    return result


async def _retain(session, workspace_id, group_id, connection_id, observations, log=None):
    rows = {}
    retained = 0
    for observation in observations:
        identity, fingerprint = _identity(group_id, connection_id, observation), _fingerprint(observation)
        row = await session.scalar(select(InvestmentObservation).where(
            InvestmentObservation.workspace_id == workspace_id,
            InvestmentObservation.identity_key == identity, InvestmentObservation.fingerprint == fingerprint,
        ))
        if row is None:
            payload = observation.model_dump(mode="json")
            payload["observed_at"] = payload["observed_at"] or datetime.now(timezone.utc).isoformat()
            row = InvestmentObservation(
                workspace_id=workspace_id, group_id=group_id, connection_id=connection_id,
                import_id=log.id if log else None, identity_key=identity, fingerprint=fingerprint, payload=payload,
            )
            session.add(row)
            await session.flush()
            retained += 1
        rows[observation.reference] = row
        rows[str(row.id)] = row
        event_key = _digest([
            str(group_id), str(connection_id), observation.provider, observation.source,
            observation.order_ref or row.identity_key,
        ])
        event = await session.scalar(select(InvestmentEvent).where(
            InvestmentEvent.workspace_id == workspace_id,
            InvestmentEvent.group_id == group_id, InvestmentEvent.event_key == event_key,
        ))
        if event is None:
            event = InvestmentEvent(workspace_id=workspace_id, group_id=group_id, event_key=event_key)
            session.add(event)
            await session.flush()
        for item in observation.legs:
            exists = await session.scalar(select(InvestmentLeg.id).where(
                InvestmentLeg.observation_id == row.id, InvestmentLeg.source_leg_key == item.key,
            ))
            if exists is None:
                session.add(InvestmentLeg(
                    workspace_id=workspace_id, event_id=event.id, observation_id=row.id,
                    source_leg_key=item.key, payload=item.model_dump(mode="json"), asset_id=item.asset_id,
                ))
    await session.flush()
    return rows, retained


async def _already_done(session, workspace_id, group_id, observations, decisions):
    saved, _, legs, links, _, _ = await _state(session, workspace_id, group_id)
    group, cid = await _scope(session, workspace_id, group_id)
    aliases = {str(o.id): o.id for o in saved}
    for o in observations:
        old = next((r for r in saved if r.identity_key == _identity(group.id, cid, o) and r.fingerprint == _fingerprint(o)), None)
        if old is None:
            return False
        aliases[o.reference] = old.id
    for decision in decisions:
        oid = aliases.get(decision.observation_ref)
        if oid is None:
            return False
        if decision.action == "apply":
            if not any(part.observation_id == oid and part.source_leg_key == decision.leg_key and part.applied_at for part in legs):
                return False
        if decision.action == "link":
            existing = {link.leg_id: link for link in links if link.observation_id == oid and link.source_leg_key == decision.leg_key and link.reversed_at is None}
            if set(existing) != {a.leg_id for a in decision.allocations}:
                return False
            if any(a.quantity is not None and str(a.quantity) != existing[a.leg_id].quantity for a in decision.allocations):
                return False
    return True


async def _link(session, workspace_id, user_id, row, item, decision, group_id, log=None):
    if not decision.allocations or not decision.reason:
        raise HTTPException(422, "Reviewed links require allocations and a reason")
    if len({a.leg_id for a in decision.allocations}) != len(decision.allocations):
        raise HTTPException(422, "A target leg may appear only once")
    observation = _input(row)
    targets = []
    for allocation in decision.allocations:
        target = await session.scalar(select(InvestmentLeg).join(InvestmentEvent).where(
            InvestmentLeg.id == allocation.leg_id, InvestmentLeg.workspace_id == workspace_id,
            InvestmentEvent.group_id == group_id,
        ))
        if target is None:
            tx = await session.scalar(select(AssetTransaction).join(Asset).where(
                AssetTransaction.id == allocation.leg_id, AssetTransaction.workspace_id == workspace_id,
                Asset.group_id == group_id, Asset.workspace_id == workspace_id,
            ))
            if tx is None:
                raise HTTPException(404, "Target leg not found")
            asset = await session.get(Asset, tx.asset_id)
            legacy = _legacy(tx, asset)
            retained, _ = await _retain(session, workspace_id, group_id, row.connection_id, [legacy])
            target = await session.scalar(select(InvestmentLeg).where(
                InvestmentLeg.observation_id == retained[legacy.reference].id,
                InvestmentLeg.source_leg_key == "ledger",
            ))
            target.asset_transaction_id, target.applied_at = tx.id, tx.created_at or datetime.now(timezone.utc)
        target_item = EvidenceLegInput.model_validate(target.payload)
        source = _input(await session.get(InvestmentObservation, target.observation_id))
        if target.observation_id == row.id:
            raise HTTPException(422, "An observation cannot corroborate itself")
        if not _same_asset(item, target_item) or item.direction != target_item.direction:
            raise HTTPException(422, "Target asset or direction conflicts")
        if observation.source_kind == "primary_activity" and item.classification != target_item.classification:
            raise HTTPException(422, "Primary classifications conflict")
        if observation.source_kind == "tax_workpaper" and item.classification not in {"lot", target_item.classification}:
            raise HTTPException(422, "Tax workpaper classification conflicts with primary evidence")
        if _conflicts(observation, item) or _conflicts(source, target_item) or _comparison_fields(observation, item, source, target_item):
            raise HTTPException(422, "Resolve conflicting source fields before confirming a link")
        if observation.provider != source.provider and observation.provider != "csv" and source.source != "ledger":
            raise HTTPException(422, "Provider identity conflicts")
        if source.source == observation.source and source.source_account_id != observation.source_account_id:
            raise HTTPException(422, "Source account identity conflicts")
        quantity = allocation.quantity if allocation.quantity is not None else target_item.quantity
        if quantity is None or quantity <= 0 or target_item.quantity is None or quantity > target_item.quantity:
            raise HTTPException(422, "Allocation must be positive and within the target leg quantity")
        targets.append((target, quantity))
    if item.quantity is None or _sum_exact(q for _, q in targets) != item.quantity:
        raise HTTPException(422, "Allocations must account for the source quantity exactly")
    if observation.source_kind == "primary_activity":
        with localcontext() as ctx:
            ctx.prec = 512
            for field in ("unit_price", "subtotal", "total", "fee", "acquisition_basis"):
                reported = getattr(item, field)
                parts = [(EvidenceLegInput.model_validate(target.payload), quantity) for target, quantity in targets]
                if reported is None or any(getattr(part, field) is None for part, _ in parts):
                    continue
                if field == "unit_price":
                    expected = sum((Fraction(cast(Decimal, part.unit_price)) * Fraction(quantity) for part, quantity in parts), Fraction()) / Fraction(item.quantity)
                else:
                    expected = sum((Fraction(getattr(part, field)) * Fraction(quantity) / Fraction(cast(Decimal, part.quantity)) for part, quantity in parts), Fraction())
                if expected != Fraction(reported):
                    raise HTTPException(422, f"Allocated {field} conflicts with the source amount")
    own = await session.scalar(select(InvestmentLeg).where(
        InvestmentLeg.observation_id == row.id, InvestmentLeg.source_leg_key == item.key,
    ))
    if own.applied_at and any(t.applied_at and t.asset_transaction_id != own.asset_transaction_id for t, _ in targets):
        raise HTTPException(409, "already_applied_overlap: existing ledger rows require separate correction")
    prior = list((await session.scalars(select(InvestmentObservationLink).where(
        InvestmentObservationLink.observation_id == row.id,
        InvestmentObservationLink.source_leg_key == item.key,
    ))).all())
    if any(link.reversed_at is None and link.leg_id not in {t.id for t, _ in targets} for link in prior):
        raise HTTPException(409, "Observation already has a different confirmed allocation")
    written = 0
    for target, quantity in targets:
        existing = next((link for link in prior if link.leg_id == target.id), None)
        if existing:
            if existing.reversed_at:
                raise HTTPException(409, "A reversed link remains retained; a new review is required")
            continue
        session.add(InvestmentObservationLink(
            workspace_id=workspace_id, observation_id=row.id, source_leg_key=item.key,
            leg_id=target.id, import_id=log.id if log else None, role="corroborating",
            quantity=str(quantity), reason=decision.reason, reason_codes=["reviewed_source_link"],
            conflicting_fields=[], reviewed_by=user_id,
        ))
        written += 1
    return written


async def _apply(session, workspace_id, user_id, group, row, item, decision, *, log=None, opening_boundary=None, allow_unpriced=False):
    own = await session.scalar(select(InvestmentLeg).where(
        InvestmentLeg.observation_id == row.id, InvestmentLeg.source_leg_key == item.key,
    ))
    if own.applied_at:
        return 0
    observation = _input(row)
    preview = await preview_evidence(session, workspace_id, group.id, opening_boundary=opening_boundary)
    record = next(r for r in preview.records if r.observation_ref == str(row.id) and r.leg_key == item.key)
    if record.application_status == "already_applied":
        return 0
    if record.application_status != "eligible":
        raise HTTPException(422, {"reason": "application_blocked", "record": record.model_dump(mode="json")})
    if observation.settlement_status != "settled" and not decision.settlement_confirmed:
        raise HTTPException(422, "Settlement must be explicitly reviewed before applying this activity")
    if opening_boundary and (not opening_boundary.overlap_reviewed or observation.event_date > opening_boundary.as_of):
        raise HTTPException(422, "Opening lots require a reviewed as-of and overlap boundary")
    price = _price(observation, item, opening_boundary)
    if price is None or observation.event_date is None:
        raise HTTPException(422, "Supported value and acquisition date are required")
    price, quantity = asset_import_service._to_ledger_scale(price), asset_import_service._to_ledger_scale(item.quantity)
    if price is None or quantity is None or quantity <= 0:
        raise HTTPException(422, "Amount cannot be represented by the ledger")
    kind = "sell" if item.classification == "sell" else "buy"
    if (kind == "buy" and item.direction != "in") or (kind == "sell" and item.direction != "out"):
        raise HTTPException(422, "Classification and direction conflict")
    assets = list((await session.scalars(select(Asset).where(
        Asset.workspace_id == workspace_id, Asset.group_id == group.id,
        Asset.id == item.asset_id if item.asset_id else Asset.ticker == item.asset_symbol,
    ))).all())
    if len(assets) > 1:
        raise HTTPException(422, "Asset identity is ambiguous in this wallet")
    asset = assets[0] if assets else None
    if asset and _holding_conflicts(item, asset):
        raise HTTPException(422, "Source asset identity does not establish the selected holding")
    if asset and asset.currency != item.execution_currency:
        raise HTTPException(422, "Valuation currency differs from the holding currency")
    if asset is None:
        if not item.asset_symbol or kind != "buy":
            raise HTTPException(422, "An identified holding is required")
        order = AssetOrderImport(
            row=1, ticker=item.asset_symbol, date=observation.event_date,
            kind=kind, quantity=quantity, price=price, currency=item.execution_currency,
        )
        quote = None
        if not allow_unpriced:
            quote = await asset_import_service.get_market_price_provider().get_quote(item.asset_symbol)
            if quote is None:
                raise HTTPException(422, "Unknown ticker; explicitly allow an unpriced holding")
        if quote and quote.currency != item.execution_currency:
            raise HTTPException(422, "Quote currency conflicts with reported acquisition value")
        asset = asset_import_service._new_holding(user_id, workspace_id, group.id, order, quote)
        asset.external_metadata = {"investment_evidence_created": True, "evidence_asset_identity": {
            k: getattr(item, k) for k in ("chain", "token_address", "isin", "provider_asset_id")
        }}
        session.add(asset)
        await session.flush()
    fee = Decimal("0") if item.acquisition_basis is not None or item.unit_price_origin == "derived_execution" else item.fee
    tx = AssetTransaction(
        asset_id=asset.id, workspace_id=workspace_id, kind=kind,
        quantity=quantity, price=price, fee=fee, date=observation.event_date,
        source="import", external_id=item.execution_id, import_id=log.id if log else None,
        created_at=observation.event_at or datetime.now(timezone.utc),
        notes=f"{observation.provider} {item.classification}; reviewed source evidence"[:500],
    )
    session.add(tx)
    await session.flush()
    # Reload the prospective ledger before validating: SQLite returns naive
    # timestamps while Postgres returns aware instants. Source time is kept
    # separately; all rows in a replay must use one database representation.
    existing = list((await session.scalars(select(AssetTransaction).where(
        AssetTransaction.asset_id == asset.id,
    ).execution_options(populate_existing=True))).all())
    asset_transaction_service._raise_if_oversell(existing, asset_type=asset.type)
    own.asset_id, own.asset_transaction_id, own.applied_at = asset.id, tx.id, datetime.now(timezone.utc)
    if opening_boundary:
        event = await session.get(InvestmentEvent, own.event_id)
        event.opening_boundary = opening_boundary.model_dump(mode="json")
    await session.flush()
    if asset.connection_id:
        from app.services.connection_service import _ledger_reconciles
        if await _ledger_reconciles(session, asset):
            await asset_transaction_service.recompute_and_cache(session, asset)
    else:
        await asset_transaction_service.recompute_and_cache(session, asset)
    return 1


async def _import_evidence(
    session, workspace_id, user_id, group_id, observations, *, filename="evidence.csv",
    connection_id=None, decisions=(), opening_boundary=None, expected_revision,
    allow_unpriced=False,
) -> EvidenceResult:
    group, cid = await _scope(session, workspace_id, group_id, connection_id, lock=True)
    if len({o.reference for o in observations}) != len(observations):
        raise HTTPException(422, "Observation references must be unique within an upload")
    preview = await preview_evidence(session, workspace_id, group_id, observations, connection_id=cid, opening_boundary=opening_boundary)
    if preview.revision != expected_revision:
        if await _already_done(session, workspace_id, group_id, observations, decisions):
            return EvidenceResult(evidence=await preview_evidence(session, workspace_id, group_id, opening_boundary=opening_boundary))
        raise HTTPException(409, "Evidence changed; refresh the preview before confirming")
    log = None
    if observations or any(decision.action != "retain" for decision in decisions):
        log = ImportLog(
            user_id=user_id, workspace_id=workspace_id, entity="asset_evidence",
            filename=filename or "evidence.csv", format="csv", transaction_count=0,
        )
        session.add(log)
        await session.flush()
    rows, retained = await _retain(session, workspace_id, group_id, cid, observations, log)
    for row in (await session.scalars(select(InvestmentObservation).where(
        InvestmentObservation.workspace_id == workspace_id, InvestmentObservation.group_id == group_id,
    ))).all():
        rows[str(row.id)] = row
    imported = linked = 0
    for decision in decisions:
        row = rows.get(decision.observation_ref)
        if row is None:
            raise HTTPException(404, "Observation not found")
        item = next((part for part in _input(row).legs if part.key == decision.leg_key), None)
        if item is None:
            raise HTTPException(404, "Source leg not found")
        if decision.action == "link":
            linked += await _link(session, workspace_id, user_id, row, item, decision, group_id, log)
        elif decision.action == "apply":
            imported += await _apply(
                session, workspace_id, user_id, group, row, item, decision,
                log=log, opening_boundary=opening_boundary, allow_unpriced=allow_unpriced,
            )
    await session.flush()
    log_id = None
    if log:
        if retained or imported or linked:
            log.transaction_count = retained
            log_id = log.id
        else:
            await session.delete(log)
    await session.commit()
    return EvidenceResult(
        import_log_id=log_id, imported=imported, retained=retained, linked=linked,
        evidence=await preview_evidence(session, workspace_id, group_id, opening_boundary=opening_boundary),
    )


async def import_evidence(session, workspace_id, user_id, group_id, observations, **kwargs):
    try:
        return await _import_evidence(session, workspace_id, user_id, group_id, observations, **kwargs)
    except Exception:
        await session.rollback()
        raise


async def confirm_evidence(session, workspace_id, user_id, group_id, decisions, expected_revision, *, opening_boundary=None, allow_unpriced=False):
    return await import_evidence(
        session, workspace_id, user_id, group_id, [], decisions=decisions,
        expected_revision=expected_revision, opening_boundary=opening_boundary, allow_unpriced=allow_unpriced,
    )


async def reverse_link(session, workspace_id, link_id, expected_revision, *, opening_boundary=None):
    link = await session.scalar(select(InvestmentObservationLink).where(
        InvestmentObservationLink.id == link_id, InvestmentObservationLink.workspace_id == workspace_id,
    ))
    if link is None:
        raise HTTPException(404, "Link not found")
    observation = await session.get(InvestmentObservation, link.observation_id)
    await _scope(session, workspace_id, observation.group_id, lock=True)
    preview = await preview_evidence(session, workspace_id, observation.group_id, opening_boundary=opening_boundary)
    if link.reversed_at is not None:
        return preview
    if preview.revision != expected_revision:
        raise HTTPException(409, "Evidence changed; refresh the preview before unlinking")
    link.reversed_at = datetime.now(timezone.utc)
    await session.commit()
    return await preview_evidence(session, workspace_id, observation.group_id, opening_boundary=opening_boundary)


async def undo_evidence_import(session, workspace_id, log):
    """Reverse this application's owned rows, retaining every source fact."""
    links = list((await session.scalars(select(InvestmentObservationLink).where(
        InvestmentObservationLink.workspace_id == workspace_id,
        InvestmentObservationLink.import_id == log.id,
    ))).all())
    transactions = list((await session.scalars(select(AssetTransaction).where(
        AssetTransaction.workspace_id == workspace_id, AssetTransaction.import_id == log.id,
    ))).all())
    assets = {tx.asset_id: await session.get(Asset, tx.asset_id) for tx in transactions}
    for group_id in sorted({asset.group_id for asset in assets.values() if asset and asset.group_id}, key=str):
        await _scope(session, workspace_id, group_id, lock=True)
    removable = []
    for tx in transactions:
        leg = await session.scalar(select(InvestmentLeg).where(InvestmentLeg.asset_transaction_id == tx.id))
        supported = await session.scalar(select(InvestmentObservationLink.id).where(
            InvestmentObservationLink.leg_id == leg.id,
            InvestmentObservationLink.reversed_at.is_(None),
            InvestmentObservationLink.import_id != log.id,
        ).limit(1)) if leg else None
        if supported:
            tx.import_id = None
        else:
            removable.append((tx, leg))
    remove_ids = {tx.id for tx, _ in removable}
    for asset in assets.values():
        remaining = list((await session.scalars(select(AssetTransaction).where(
            AssetTransaction.asset_id == asset.id, AssetTransaction.id.not_in(remove_ids),
        ))).all())
        if asset.connection_id:
            # The provider's balance is independent evidence. An undo that
            # would disagree with it needs an explicit later correction.
            if asset_transaction_service._recompute(remaining, asset_type=asset.type)["units"] != asset.units:
                raise HTTPException(409, "Undo would invalidate the provider balance; review dependent activity first")
        asset_transaction_service._raise_if_oversell(remaining, asset_type=asset.type)
    for tx, leg in removable:
        if leg:
            leg.asset_transaction_id = None
        await session.delete(tx)
    for link in links:
        link.reversed_at = link.reversed_at or datetime.now(timezone.utc)
    await session.flush()
    for asset in assets.values():
        remaining = await session.scalar(select(AssetTransaction.id).where(AssetTransaction.asset_id == asset.id).limit(1))
        if remaining is None and (asset.external_metadata or {}).get("investment_evidence_created"):
            for leg in (await session.scalars(select(InvestmentLeg).where(InvestmentLeg.asset_id == asset.id))).all():
                leg.asset_id = None
            await session.delete(asset)
        elif remaining is not None:
            await asset_transaction_service.recompute_and_cache(session, asset)
    await session.commit()


async def sync_evidence(session, connection, observations, trades, holdings, synced_account_ids=None):
    """Retain one provider read and append only unambiguous supported trades.

    The caller owns the connection lock and transaction, and retains the
    existing snapshot reconciliation after this function returns.
    """
    from app.services.connection_service import _wallet_external_id

    touched = {}
    grouped = {}
    for observation in observations:
        if synced_account_ids is not None and observation.account_external_id not in synced_account_ids:
            continue
        asset = holdings.get(observation.holding_external_id)
        group_id = asset.group_id if asset else None
        if group_id is None:
            group_id = await session.scalar(select(AssetGroup.id).where(
                AssetGroup.workspace_id == connection.workspace_id,
                AssetGroup.connection_id == connection.id,
                AssetGroup.external_id == _wallet_external_id(connection.external_id, observation.account_external_id),
            ))
        if group_id is None and observation.account_external_id:
            # A zero-balance portfolio can have a current account and history
            # without any current holding. The account, never a historical
            # label in an uploaded row, authorizes its review destination.
            account = await session.scalar(select(Account).where(
                Account.workspace_id == connection.workspace_id,
                Account.connection_id == connection.id,
                Account.external_id == observation.account_external_id,
            ))
            if account is not None:
                from app.services.asset_group_service import ensure_group_for_connection
                group = await ensure_group_for_connection(
                    session, user_id=connection.user_id, connection_id=connection.id,
                    workspace_id=connection.workspace_id, source=connection.provider,
                    external_id=_wallet_external_id(connection.external_id, account.external_id),
                    default_name=account.name, institution_id=account.institution_id,
                )
                linked_group = await session.scalar(select(AssetGroup.id).where(AssetGroup.account_id == account.id))
                if linked_group is None or linked_group == group.id:
                    group.account_id = account.id
                group_id = group.id
        if group_id is not None:
            grouped.setdefault(group_id, []).append(observation)
    trade_by_key = {(trade.holding_external_id, trade.external_id): trade for trade in trades}
    for group_id in sorted(grouped, key=str):
        await _scope(session, connection.workspace_id, group_id, connection.id, lock=True)
        rows, _ = await _retain(session, connection.workspace_id, group_id, connection.id, grouped[group_id])
        pending = {}
        for observation in sorted(grouped[group_id], key=lambda o: str(o.event_at or o.event_date or "")):
            row = rows[observation.reference]
            asset = holdings.get(observation.holding_external_id)
            if asset is None:
                continue
            for item in observation.legs:
                trade = trade_by_key.get((observation.holding_external_id, item.execution_id))
                if trade is None:
                    continue
                own = await session.scalar(select(InvestmentLeg).where(
                    InvestmentLeg.observation_id == row.id, InvestmentLeg.source_leg_key == item.key,
                ))
                # Only an actual provider-owned historical row establishes
                # this identity. A CSV external ID lives in another namespace.
                existing = await session.scalar(select(AssetTransaction).where(
                    AssetTransaction.asset_id == asset.id,
                    AssetTransaction.workspace_id == connection.workspace_id,
                    AssetTransaction.source == connection.provider,
                    AssetTransaction.external_id == trade.external_id,
                ).limit(1))
                if existing is not None:
                    already_bound = await session.scalar(select(InvestmentLeg).where(
                        InvestmentLeg.asset_transaction_id == existing.id,
                    ))
                    if already_bound is None:
                        own.asset_id = asset.id
                        own.asset_transaction_id = existing.id
                        own.applied_at = existing.created_at or datetime.now(timezone.utc)
                    elif already_bound.id != own.id:
                        prior = _input(await session.get(InvestmentObservation, already_bound.observation_id))
                        compatible = _status_progression(prior, observation) or (
                            prior.source == "ledger" and existing.kind == trade.kind
                            and existing.quantity == trade.quantity and existing.price == trade.price
                        )
                        linked = await session.scalar(select(InvestmentObservationLink.id).where(
                            InvestmentObservationLink.observation_id == row.id,
                            InvestmentObservationLink.source_leg_key == item.key,
                            InvestmentObservationLink.leg_id == already_bound.id,
                        ))
                        if compatible and linked is None:
                            session.add(InvestmentObservationLink(
                                workspace_id=connection.workspace_id, observation_id=row.id,
                                source_leg_key=item.key, leg_id=already_bound.id,
                                role="corroborating", quantity=str(item.quantity) if item.quantity is not None else None,
                                reason="Same verified provider execution after a source update",
                                reason_codes=["same_scoped_execution"], conflicting_fields=[], reviewed_by=None,
                            ))
                    continue
                if own.applied_at:
                    continue
                pending[own.id] = (row, own, observation, item, trade, asset)
        await session.flush()
        if not pending:
            continue
        # ponytail: one pairwise match scan per wallet; indexed source/time
        # candidate retrieval is the upgrade if individual histories outgrow it.
        preview = await preview_evidence(session, connection.workspace_id, group_id)
        records = {(record.observation_ref, record.leg_key): record for record in preview.records}
        for row, own, observation, item, trade, asset in pending.values():
            record = records[(str(row.id), item.key)]
            if record.application_status == "already_applied":
                continue
            if record.match_status in {"candidate", "conflicting"} or _conflicts(observation, item):
                continue
            if not item.execution_id or observation.settlement_status != "settled" or observation.event_at is None:
                continue
            # TradeData already carries the provider's established pricing
            # policy (including explicit reward valuation). Preserve it;
            # never substitute an observation's displayed market value.
            tx = AssetTransaction(
                asset_id=asset.id, workspace_id=asset.workspace_id,
                kind=trade.kind, quantity=trade.quantity, price=trade.price,
                date=trade.occurred_at.date(), created_at=trade.occurred_at,
                source=connection.provider, external_id=trade.external_id, notes=trade.notes,
            )
            session.add(tx)
            await session.flush()
            own.asset_id, own.asset_transaction_id, own.applied_at = asset.id, tx.id, datetime.now(timezone.utc)
            touched[asset.id] = asset
        await session.flush()
    return touched


async def evidence_holding_for_sync(session, connection, holding, group_id):
    """Reuse only a uniquely mapped holding this evidence flow created."""
    candidates = list((await session.scalars(select(Asset).where(
        Asset.workspace_id == connection.workspace_id, Asset.group_id == group_id,
        Asset.ticker == holding.ticker, Asset.connection_id.is_(None),
    ))).all())
    candidates = [a for a in candidates if (a.external_metadata or {}).get("investment_evidence_created")]
    if not candidates:
        return None, False
    if len(candidates) != 1:
        return None, True
    asset = candidates[0]
    identity = (asset.external_metadata or {}).get("evidence_asset_identity") or {}
    metadata = holding.metadata or {}
    verified = False
    for field in ("chain", "token_address", "provider_asset_id"):
        if identity.get(field) and identity[field] != metadata.get(field):
            return None, True
        if field in {"token_address", "provider_asset_id"} and identity.get(field):
            verified = True
    if identity.get("isin") and identity["isin"] != holding.isin:
        return None, True
    verified |= bool(identity.get("isin") and identity["isin"] == holding.isin)
    if not verified:
        linked_sources = (await session.scalars(select(InvestmentObservation)
            .join(InvestmentObservationLink, InvestmentObservationLink.observation_id == InvestmentObservation.id)
            .join(InvestmentLeg, InvestmentLeg.id == InvestmentObservationLink.leg_id)
            .where(
                InvestmentObservation.workspace_id == connection.workspace_id,
                InvestmentObservation.connection_id == connection.id,
                InvestmentObservationLink.reversed_at.is_(None), InvestmentLeg.asset_id == asset.id,
            ))).all()
        verified = any(
            source.payload.get("source") == f"{connection.provider}_api"
            and source.payload.get("holding_external_id") == holding.external_id
            for source in linked_sources
        )
    if not verified:
        return None, True
    asset.source, asset.external_id = connection.provider, holding.external_id
    return asset, False
