"""Project retained domain evidence. Reading activity never collects or applies money."""
import json
import uuid
from datetime import timezone
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.models.collection import Collection
from app.models.investment_evidence import InvestmentEvent
from app.models.recovery_evidence import InvestmentRecoveryEntry
from app.providers.onchain import CHAINS
from app.schemas.investment_evidence import EvidenceLegInput
from app.schemas.investment_timeline import (
    TimelineAccount, TimelineAsset, TimelineBasis, TimelineCoverage, TimelineEvent,
    TimelineLeg, TimelineRead, TimelineRelationship, TimelineSource, TimelineSourceDetail,
    TimelineTime,
)
from app.services import investment_evidence_service as evidence
from app.services import onchain_history as history
from app.services import owned_transfer_service as transfers
from app.services import recovery_evidence_service as recovery


def _time(data):
    precision = data.get("time_precision", "unknown")
    return TimelineTime(**{key: data.get(key) for key in (
        "event_at", "event_date", "event_time_raw", "timezone",
    )}, time_precision=precision,
        ordering="exact" if data.get("event_at") else "within_day_unknown" if data.get("event_date") else "unknown")


def _asset(item, source, holding=None):
    metadata = holding.external_metadata or {} if holding else {}
    explicit = metadata.get("evidence_asset_identity") or {}
    chain = item.get("chain") or explicit.get("chain") or metadata.get("chain")
    token = item.get("token_address") or explicit.get("token_address") or metadata.get("token_contract")
    if not token and chain and metadata.get("watch_only") and not metadata.get("token_contract"):
        token = "native"
    program = item.get("token_program") or explicit.get("token_program") or metadata.get("token_program")
    asset_id = str(holding.id) if holding else str(item["asset_id"]) if item.get("asset_id") else None
    known = {"chain": explicit.get("chain") or metadata.get("chain"),
             "token_address": explicit.get("token_address") or metadata.get("token_contract"),
             "token_program": explicit.get("token_program") or metadata.get("token_program"),
             "provider_asset_id": explicit.get("provider_asset_id"), "isin": explicit.get("isin")}
    if any(item.get(key) and value and item[key] != value for key, value in known.items()):
        asset_id = None
    if chain and token and (chain != "solana" or token == "native" or program):
        identity, status = ["chain", chain, token, program if token != "native" else None], "canonical"
    elif not chain and (item.get("isin") or explicit.get("isin")):
        identity, status = ["isin", item.get("isin") or explicit["isin"]], "canonical"
    elif not chain and (item.get("provider_asset_id") or explicit.get("provider_asset_id")):
        identity, status = ["provider", source.provider, item.get("provider_asset_id") or explicit["provider_asset_id"]], "canonical"
    elif asset_id:
        identity, status = ["holding", asset_id], "holding"
    else:
        identity, status = ["unresolved", source.source_id, item.get("key")], "unresolved"
    return TimelineAsset(
        canonical_asset_key=evidence._digest(identity), identity_status=status,
        asset_symbol=item.get("asset_symbol") or (holding.ticker if holding else None),
        chain=chain, token_address=token, token_program=program, asset_ids=[asset_id] if asset_id else [],
    )


def _account(state, group_id):
    group = state["groups"].get(group_id)
    account_id = state.get("account_ids", {}).get(group_id) or (group.account_id if group else None)
    return TimelineAccount(group_id=str(group_id) if group_id else None,
                           group_name=group.name if group else None,
                           account_id=str(account_id) if account_id else None,
                           connection_id=str(group.connection_id) if group and group.connection_id else None)


def _source(data, source_id, account, *, current=True):
    return TimelineSource(
        source_id=source_id, source=data["source"], provider=data["provider"],
        source_kind=data.get("source_kind", "primary_activity"),
        source_local_id=data.get("source_local_id"), source_locator=data.get("source_locator"),
        source_reference=data.get("source_reference") or data.get("reference"),
        observed_at=data.get("observed_at"), time=_time(data),
        original_type=data.get("source_fields", {}).get("classification"),
        provider_status=data.get("provider_status"), network_status=data.get("network_status"),
        settlement_status=data.get("settlement_status", "unknown"), is_current=current,
        detail_url=f"/api/assets/timeline/sources/{quote(source_id, safe='')}", account=account,
    )


def _history_id(workspace_id, connection_id, group_id, chain, owner, reference):
    return "history:" + evidence._digest([str(workspace_id), str(connection_id), str(group_id), chain, owner, reference])


def _event_id(state, leg):
    row = state["observations"][leg.observation_id]
    data = row.payload
    if data.get("source") == "onchain_history" and data.get("order_ref") and data.get("source_account_id"):
        chain, _, owner = data["source_account_id"].partition(":")
        return _history_id(state["workspace_id"], row.connection_id, row.group_id, chain, owner, data["order_ref"])
    return "event:" + str(leg.event_id)


def _new_event(identifier):
    return TimelineEvent(event_id=identifier, kind="unknown", status="unknown")


def _put_source(event, source):
    if source.source_id not in {item.source_id for item in event.sources}:
        event.sources.append(source)


def _put_leg(event, item, identifier, source, state, *, current=True, non_additive=False):
    holding = state["assets"].get(uuid.UUID(str(item["asset_id"]))) if item.get("asset_id") else None
    asset = _asset(item, source, holding)
    event.assets.append(asset)
    event.legs.append(TimelineLeg(
        **item, leg_id=identifier, canonical_asset_key=asset.canonical_asset_key,
        group_id=source.account.group_id, source_ids=[source.source_id],
        settlement_status=source.settlement_status, execution_status=source.provider_status,
        is_current=current, non_additive=non_additive or (source.provider_status in {"failed", "error", "canceled", "cancelled"} and item.get("classification") != "fee"),
        reason_codes=["holding_identity_conflict"] if holding and str(holding.id) not in asset.asset_ids else [],
    ))
    _put_source(event, source)


def _review_url(group_id, activity="evidence"):
    if activity == "evidence":
        return "/import?" + urlencode({"tab": "investments", "mode": "evidence", **({"wallet": str(group_id)} if group_id else {})})
    if activity == "recovery":
        return "/assets?" + urlencode({"tab": "activity", "activity": "recovery", "recovery_wallet": str(group_id)})
    return "/assets?" + urlencode({"tab": "activity", "activity": activity, **({"wallet": str(group_id)} if group_id else {})})


def _merge_assets(items):
    result = {}
    for item in items:
        key = item.canonical_asset_key
        if key not in result:
            result[key] = item.model_copy(deep=True)
        else:
            result[key].asset_ids = sorted(set(result[key].asset_ids + item.asset_ids))
            result[key].asset_symbol = result[key].asset_symbol or item.asset_symbol
    return sorted(result.values(), key=lambda item: (item.asset_symbol or "", item.canonical_asset_key))


def _finish(event):
    event.assets = _merge_assets(event.assets)
    event.accounts = list({(s.account.group_id, s.account.account_id, s.account.connection_id): s.account for s in event.sources}.values())
    active = [s for s in event.sources if s.is_current and s.source_kind == "primary_activity"]
    times = [s.time for s in active] or [s.time for s in event.sources]
    dates = {str(t.event_date) for t in times if t.event_date}
    instants = {t.event_at for t in times if t.event_at}
    if len(instants) == 1 and all(t.event_at for t in times):
        event.time = times[0].model_copy()
    elif len(dates) == 1:
        event.time = TimelineTime(event_date=next(t.event_date for t in times if t.event_date), time_precision="date", ordering="within_day_unknown")
    elif times:
        event.time = TimelineTime(ordering="conflicting" if dates or instants else "unknown")
    states = {leg.settlement_status for leg in event.legs if leg.is_current and not leg.non_additive}
    if not states:
        states = {s.settlement_status for s in active}
    execution = {s.provider_status for s in active if s.provider_status}
    if event.status == "unknown":
        event.status = "failed" if execution and execution <= {"failed", "error", "canceled", "cancelled"} else next(iter(states)) if len(states) == 1 else "conflicting" if len(states) > 1 else "unknown"
    classifications = {leg.classification for leg in event.legs if leg.is_current and not leg.non_additive}
    kinds = {"buy": "acquisition", "sell": "disposal", "income": "income", "reward": "income", "fee": "fee", "transfer": "transfer", "move_in": "transfer", "move_out": "transfer", "swap": "swap", "bridge": "bridge", "wrap": "wrap", "unwrap": "unwrap"}
    principal = classifications - {"fee"}
    if event.kind == "unknown":
        event.kind = kinds.get(next(iter(principal)), "unknown") if len(principal) == 1 else "fee" if classifications == {"fee"} else "unknown"
    qualified_transfer = any(item["status"] == "confirmed" for item in event.transfers)
    if (event.conflicting_fields or event.status == "conflicting") and not qualified_transfer:
        event.linkage = "conflicting"
    elif any(r.state == "candidate" for r in event.relationships) and event.linkage != "confirmed":
        event.linkage = "candidate"
    if event.basis.state == "unknown" and event.linkage != "conflicting":
        # Only a source's explicit acquisition-basis field supports this fact.
        # Execution value, snapshots and tax-workpaper costs are different facts.
        costs = {leg.acquisition_basis for leg in event.legs if leg.is_current and not leg.non_additive and leg.acquisition_basis is not None}
        if len(costs) == 1 and len([leg for leg in event.legs if leg.is_current and not leg.non_additive and leg.classification != "fee"]) == 1:
            event.basis = TimelineBasis(state="known", acquisition_cost=next(iter(costs)), known_acquisition_cost=next(iter(costs)), reason_codes=["reported_acquisition_basis"])
    event.reason_codes = sorted(set(event.reason_codes))
    event.conflicting_fields = sorted(set(event.conflicting_fields))
    event.sources.sort(key=lambda item: item.source_id)
    event.legs.sort(key=lambda item: item.leg_id)


def _coverage(row, archive):
    axes = archive.get("coverage", {})
    times = [version.get("block_time") for tx in archive.get("transactions", {}).values() for version in tx.get("versions", []) if version.get("block_time") is not None]
    return TimelineCoverage(
        coverage_id="history:" + str(row.id), source="onchain_history", collection_id=str(row.id),
        group_id=str(row.group_id) if row.group_id else None, connection_id=str(row.connection_id) if row.connection_id else None,
        chain=archive.get("chain"), requested=archive.get("requested", {}),
        observed={"since": min(times) if times else None, "until": max(times) if times else None, "time_precision": "second" if times else "unknown", "anchor": archive.get("anchor")},
        last_successful_collection=history._db_time(row.updated_at) if axes.get("retrieval") == "complete" else None,
        **{key: axes.get(key, "unknown") for key in ("inventory", "retrieval", "interpretation", "settlement")},
        gaps=sorted(set(archive.get("gaps", []) + ["historical_inventory_unresolved", "opening_balance_unknown", "basis_unknown"])),
        streams=archive.get("streams", {}), source_url=f"/api/onchain/history/{row.id}",
    )


def _archive_source(row, archive, transaction, version, state):
    identifier = f"archive:{row.id}:{evidence._digest(transaction.get('signature'))}:{version.get('version_id', 'unavailable')}"
    digest = version.get("payload_digest")
    payload = archive.get("payloads", {}).get(digest)
    when = history._instant(version.get("block_time"))
    current = transaction.get("canonical_version") == version.get("version_id") and bool(version.get("version_id"))
    source = _source({
        "source": "onchain_history", "provider": "onchain", "source_local_id": transaction.get("signature"),
        "source_locator": f"onchain/history/{row.id}/payloads/{digest}" if digest else f"onchain/history/{row.id}",
        "event_at": when, "event_date": when.date() if when else None,
        "event_time_raw": str(version.get("original_timestamp")) if version.get("original_timestamp") is not None else None,
        "time_precision": version.get("time_precision", "unknown"), "timezone": "UTC" if when else None,
        "provider_status": version.get("execution"), "network_status": version.get("confirmation_status"),
        "settlement_status": version.get("settlement", "unknown") if current else "unknown",
        "observed_at": payload.get("retrieved_at") if payload else None,
    }, identifier, _account(state, row.group_id), current=current)
    source.collection_id, source.payload_digest = str(row.id), digest
    source.decoder_version = version.get("decoder_version") or archive.get("decoder_version")
    if payload is None:
        source.availability, source.unavailable_reason = "unavailable", transaction.get("retrieval_gap") or "raw_payload_unavailable"
    return source, payload


def _archive_leg(part, source, owner, state):
    identity = part.get("asset", {})
    source_owned = part.get("source_owner") == owner or part.get("source") == owner
    destination_owned = part.get("destination_owner") == owner or part.get("destination") == owner
    role = part.get("role", "unknown")
    fee = role in {"network_fee", "fee", "token_fee", "token_transfer_fee", "withheld_fee"}
    values: dict[str, Any] = {"key": part["key"], "chain": identity.get("chain"),
              "token_address": "native" if identity.get("native") else identity.get("mint") or identity.get("contract"),
              "token_program": identity.get("token_program"), "asset_symbol": identity.get("symbol"),
              "direction": "out" if source_owned and not destination_owned else "in" if destination_owned and not source_owned else "unknown",
              "classification": "fee" if fee else "unknown" if part.get("interpretation") == "unresolved" else "transfer",
              "quantity": None if part.get("interpretation") == "unresolved" else part.get("quantity"), "raw_units": part.get("raw_units"), "decimals": part.get("decimals"),
              "quantity_role": role if role in {"network_fee", "withdrawal_fee", "intermediary_fee", "token_transfer_fee", "principal", "balance_delta"} else "unknown",
              "source_address": part.get("source"), "destination_address": part.get("destination"),
              "source_owner": part.get("source_owner"), "destination_owner": part.get("destination_owner"),
              "fee_payer": part.get("source") if fee else None,
              "transaction_ref": source.source_local_id, "leg_ref": part["key"],
              "derivation": {key: str(value) for key, value in part.get("derivation", {}).items() if value is not None}}
    reasons = []
    if not source_owned and not destination_owned and role in {"principal", "utxo_input", "utxo_output", "issuance"}:
        reasons.append("external_endpoints")
    try:
        EvidenceLegInput.exact_decimal(values["quantity"])
    except ValueError:
        # Raw atomic units remain available when display precision exceeds the crosswalk contract.
        values["quantity"] = None
        reasons.append("display_quantity_out_of_range")
    asset = _asset(values, source)
    if identity.get("native") and identity.get("chain") in CHAINS:
        values["asset_symbol"] = asset.asset_symbol = CHAINS[identity["chain"]].symbol
    result = TimelineLeg(**values, leg_id=f"{source.source_id}:{part['key']}", canonical_asset_key=asset.canonical_asset_key,
                         group_id=source.account.group_id, source_ids=[source.source_id],
                         settlement_status=(part.get("settlement", "settled") if source.settlement_status == "settled" else source.settlement_status) if source.is_current else "unknown",
                         execution_status=source.provider_status, interpretation=part.get("interpretation"),
                         non_additive=bool(part.get("non_additive")) or source_owned == destination_owned or (source.provider_status in {"failed", "error", "unknown"} and not fee),
                         is_current=source.is_current, reason_codes=reasons,
                         **{key: part.get(key) for key in ("sender_debit_raw_units", "receiver_credit_raw_units", "withheld_fee_raw_units")})
    return result, asset


def _archive_leg_identity(version, part, source):
    # Retrieval anchors may differ while inclusion and exact decoded facts agree.
    # Raw-payload locators and observed maturity remain on each retained source.
    facts = {**part, "derivation": {key: value for key, value in part.get("derivation", {}).items() if key != "payload_digest"}}
    if part.get("asset", {}).get("chain") == "bitcoin":
        facts.pop("maturity_eligible", None)
        facts["derivation"].pop("prevout_source", None)
    return [version.get("evidence_fingerprint") or version.get("version_id"), source.decoder_version,
            version.get("block_hash"), version.get("block_height"), facts,
            source.is_current, source.settlement_status, source.provider_status]


async def _project(session, workspace_id):
    # ponytail: project the existing local evidence scope before paging; move projection to SQL if large histories make reads slow.
    state = await transfers.prepare_replay(session, workspace_id)
    state["workspace_id"] = workspace_id
    state["events"] = {row.id: row for row in (await session.scalars(select(InvestmentEvent).where(InvestmentEvent.workspace_id == workspace_id))).all()}
    result, sources, details, records, errors, coverage, aliases = {}, {}, {}, {}, [], [], {}
    for leg in state["legs"].values():
        original = "event:" + str(leg.event_id)
        canonical_id = _event_id(state, leg)
        if original != canonical_id:
            aliases[original] = canonical_id
    for group in state["groups"].values():
        try:
            preview = await evidence.preview_evidence(session, workspace_id, group.id)
            records.update({(record.observation_ref, record.leg_key): record for record in preview.records})
            if preview.target.account_id:
                state.setdefault("account_ids", {})[group.id] = preview.target.account_id
        except (HTTPException, ValidationError) as exc:
            errors.append({"group_id": str(group.id), "code": "evidence_preview_unavailable", "status": getattr(exc, "status_code", 422)})
    for row in state["observations"].values():
        sid = str(row.id)
        account = _account(state, row.group_id)
        if state.get("account_ids", {}).get(row.group_id):
            account.account_id = str(state["account_ids"][row.group_id])
        source = _source(row.payload, sid, account, current=row.is_current)
        if source.source == "onchain_history" and source.source_locator:
            parts = source.source_locator.split("/")
            if len(parts) == 5 and parts[:2] == ["onchain", "history"] and parts[3] == "payloads":
                source.collection_id, source.payload_digest = parts[2], parts[4]
                archive_row = next((archive for archive in state["archives"].values() if str(archive.id) == parts[2]), None)
                if archive_row is None or parts[4] not in archive_row.payload.get("payloads", {}):
                    source.availability, source.unavailable_reason = "unavailable", "raw_payload_unavailable"
        sources[sid] = source
        details[sid] = {"observation": row.payload}
    families, _ = evidence._source_families(
        {str(row.id): evidence._input(row) for row in state["observations"].values()},
        {str(row.id): row.identity_key for row in state["observations"].values()},
    )
    # Complete confirmed grouping before attaching noncurrent/unqualified reviews.
    transfer_reads = sorted((transfers._transfer_read(state, row) for row in state["transfers"].values()),
                            key=lambda read: (read.status != "confirmed", str(read.id)))
    selected_leg_ids = {identifier for read in transfer_reads if read.status == "confirmed"
                        for identifier in (read.request.out_leg_id, read.request.in_leg_id, *(fee.leg_id for fee in read.request.fees))}
    canonical, displayed_leg_ids = {}, {}
    for leg in state["legs"].values():
        sid = str(leg.observation_id)
        family = (families[sid], leg.source_leg_key)
        canonical.setdefault(family, []).append(leg)
    for members in canonical.values():
        # A compatible source family has one leg identity; retain all source versions.
        members.sort(key=lambda leg: (not state["observations"][leg.observation_id].is_current,
                                     -{"settled": 2, "pending": 1}.get(state["observations"][leg.observation_id].payload.get("settlement_status"), 0),
                                     leg.id not in selected_leg_ids, str(leg.id)))
        representative = members[0]
        targets = []
        for member in members:
            record = records.get((str(member.observation_id), member.source_leg_key))
            if record and not record.conflicting_fields and state["observations"][member.observation_id].is_current:
                targets.extend(link for link in record.links if link.leg.leg_id in state["legs"])
        own_id = _event_id(state, representative)
        destinations = sorted({_event_id(state, state["legs"][link.leg.leg_id]) for link in targets}) or [own_id]
        for identifier in destinations:
            event = result.setdefault(identifier, _new_event(identifier))
            if targets:
                event.linkage = "confirmed"
            elif state["observations"][representative.observation_id].payload.get("source_kind", "primary_activity") == "primary_activity":
                item = dict(representative.payload)
                current_members = [member for member in members if state["observations"][member.observation_id].is_current]
                # The producer checks every family member for direct compatibility.
                # Union complementary known facts; storage IDs only stabilize display identity.
                for member in current_members:
                    for field, value in member.payload.items():
                        if value is not None and (item.get(field) is None or field == "unit_price_origin" and item.get(field) == "unknown"):
                            item[field] = value
                _put_leg(event, item, str(representative.id), sources[str(representative.observation_id)], state,
                         current=state["observations"][representative.observation_id].is_current)
                event.legs[-1].source_ids = sorted({str(member.observation_id) for member in current_members or [representative]})
                displayed_leg_ids.update({member.id: str(representative.id) for member in members})
            else:
                # Secondary records are available as source context, never timeline movements.
                if not event.legs and not event.sources:
                    result.pop(identifier, None)
                continue
            for member in members:
                sid = str(member.observation_id)
                _put_source(event, sources[sid])
                event.reason_codes.extend(state["observations"][member.observation_id].payload.get("reason_codes", []))
                record = records.get((sid, member.source_leg_key))
                if record:
                    event.reason_codes.extend(record.reason_codes)
                    event.conflicting_fields.extend(record.conflicting_fields)
                    for candidate in record.candidate_legs:
                        target = state["legs"].get(candidate.leg_id)
                        target_id = _event_id(state, target) if target else "ledger:" + str(candidate.leg_id)
                        if target_id != identifier:
                            event.relationships.append(TimelineRelationship(kind="source_overlap", state="candidate", event_id=target_id,
                                source_id=sid, leg_id=str(candidate.leg_id), reason_codes=["producer_candidate"], review_url=_review_url(state["observations"][member.observation_id].group_id)))
                    for link in record.links:
                        event.relationships.append(TimelineRelationship(kind="source_corroboration", state="confirmed" if not record.conflicting_fields else "conflicting",
                            source_id=sid, event_id="event:" + str(link.leg.event_id), review_id=str(link.link_id),
                            leg_id=str(link.leg.leg_id), quantity=link.quantity, conflicting_fields=record.conflicting_fields,
                            review_url=_review_url(state["observations"][member.observation_id].group_id)))
            if len(destinations) == 1 and targets:
                aliases[own_id] = destinations[0]
    # Legacy applications appear once, including secondary-source opening-lot applications.
    wrapped = {leg.asset_transaction_id for leg in state["legs"].values() if leg.asset_transaction_id}
    wrapped.update(app.transaction_id for app in state["applications"].values() if app.transaction_id)
    for tx in state["transactions"].values():
        if tx.id in wrapped:
            continue
        holding = state["assets"].get(tx.asset_id)
        if holding is None:
            continue
        data = evidence._legacy(tx, holding).model_dump(mode="json")
        identifier = "ledger:" + str(tx.id)
        source = _source(data, identifier, _account(state, holding.group_id))
        sources[identifier], details[identifier] = source, {"observation": data}
        event = result.setdefault(identifier, _new_event(identifier))
        _put_leg(event, data["legs"][0], identifier, source, state)
        event.reason_codes.append("original_source_precision_unavailable")
    # Archive versions are the producer's supplied mechanics, not a second decode.
    archives = list(state["archives"].values())
    archive_events, archive_leg_ids = set(), {}
    archive: dict[str, Any]
    for row in archives:
        try:
            peers = [(peer.id, peer.payload) for peer in archives if peer.id != row.id]
            archive = history.qualify_archive(row.payload, peers)
        except (KeyError, TypeError, ValueError):
            archive = {**row.payload, "coverage": {"interpretation": "unavailable", "settlement": "unknown"}, "gaps": [*row.payload.get("gaps", []), "archive_qualification_unavailable"]}
            errors.append({"collection_id": str(row.id), "group_id": str(row.group_id) if row.group_id else None, "code": "archive_qualification_unavailable"})
        cov = _coverage(row, archive)
        coverage.append(cov)
        for key, transaction in archive.get("transactions", {}).items():
            identifier = _history_id(workspace_id, row.connection_id, row.group_id, archive.get("chain"), archive.get("owner"), key)
            event = result.setdefault(identifier, _new_event(identifier))
            if identifier not in archive_events:
                event.legs = []  # Archive legs replace their 100-leg crosswalk projections.
                event.assets = []
                archive_events.add(identifier)
            event.coverage.append(cov)
            event.native_trace_url = "/assets?" + urlencode({"tab": "activity", "activity": "wallets",
                "chain": row.request.get("chain", archive.get("chain")), "address": row.request.get("address", archive.get("owner")),
                **({"wallet": str(row.group_id)} if row.group_id else {}),
                **{key: row.request[key] for key in ("since", "until") if row.request.get(key)}})
            event.reason_codes.extend(transaction.get("retrieval_gap") and [transaction["retrieval_gap"]] or [])
            if transaction.get("revision_status") in {"conflicting_or_reorganized", "cross_collection_conflict"} or (transaction.get("versions") and not transaction.get("canonical_version")):
                event.status, event.linkage = "conflicting", "conflicting"
                event.conflicting_fields.append("source_version")
            for version in transaction.get("versions", []) or [{}]:
                source, payload = _archive_source(row, archive, transaction, version, state)
                if "archive_qualification_unavailable" in archive.get("gaps", []):
                    source.is_current, source.settlement_status = False, "unknown"
                sources[source.source_id] = source
                details[source.source_id] = {"raw_payload": {"encoding": "json", "json": json.dumps(payload, separators=(",", ":"), ensure_ascii=False)} if payload else None,
                                             "transaction": {"encoding": "json", "json": json.dumps({k: v for k, v in transaction.items() if k != "versions"} | {"versions": [version]}, separators=(",", ":"))}, "coverage": [cov]}
                _put_source(event, source)
                event.reason_codes.extend(version.get("gaps", []))
                if not version.get("legs"):
                    event.reason_codes.append("no_decoded_legs")
                if source.is_current:
                    event.mechanics.extend(version.get("relationships", []))
                    mechanics = {relation.get("kind") for relation in version.get("relationships", [])}
                    if len(mechanics) == 1:
                        event.kind = next(iter(mechanics))
                for part in version.get("legs", []):
                    leg, asset = _archive_leg(part, source, archive.get("owner"), state)
                    leg.leg_id = "archive-leg:" + evidence._digest([identifier, _archive_leg_identity(version, part, source)])
                    cache = state.setdefault("archive_leg_ids", {})
                    prior = cache.get(leg.leg_id)
                    if prior:
                        prior.source_ids = sorted(set(prior.source_ids + leg.source_ids))
                        leg = prior
                    else:
                        cache[leg.leg_id] = leg
                        event.legs.append(leg)
                        event.assets.append(asset)
                    if source.is_current:
                        archive_leg_ids[(identifier, source.payload_digest, part["key"])] = leg.leg_id
                # Source locators are retained even when a raw payload has disappeared.
                for existing in event.sources:
                    if existing.source_id != source.source_id and existing.source_locator == source.source_locator:
                        existing.collection_id, existing.payload_digest = source.collection_id, source.payload_digest
                        existing.decoder_version = source.decoder_version
                        existing.availability, existing.unavailable_reason = source.availability, source.unavailable_reason
                        details[existing.source_id].update(details[source.source_id])
    leg_events = {}
    for leg in state["legs"].values():
        identifier, seen = _event_id(state, leg), set()
        if identifier in archive_events:
            # Archive display IDs stay stable; qualify the original selected
            # observation against its exact retained payload and instruction.
            source = sources[str(leg.observation_id)]
            displayed_leg_ids[leg.id] = archive_leg_ids.get((identifier, source.payload_digest, leg.source_leg_key))
        while identifier in aliases and identifier not in seen:
            seen.add(identifier)
            identifier = aliases[identifier]
        leg_events[leg.id] = identifier
    for read in transfer_reads:
        request = read.request
        principal_ids = {leg_events.get(request.out_leg_id), leg_events.get(request.in_leg_id)} - {None}
        ids = principal_ids | {leg_events.get(fee.leg_id) for fee in request.fees} - {None}
        available = [result[identifier] for identifier in sorted(ids, key=str) if identifier in result]
        if read.status == "confirmed" and all(leg_events.get(leg_id) in result for leg_id in (request.out_leg_id, request.in_leg_id, *(fee.leg_id for fee in request.fees))):
            identifier = "owned-transfer:" + str(read.id)
            event = result.setdefault(identifier, _new_event(identifier))
            event.linkage = "confirmed"
            for part in available:
                event.legs.extend(part.legs)
                event.assets.extend(part.assets)
                for source in part.sources:
                    _put_source(event, source)
                event.relationships.extend(part.relationships)
                event.transfers.extend(part.transfers)
                event.coverage.extend(part.coverage)
                event.reason_codes.extend(part.reason_codes)
                event.conflicting_fields.extend(part.conflicting_fields)
                if part.status == "conflicting":
                    event.status = "conflicting"
                aliases[part.event_id] = identifier
                result.pop(part.event_id, None)
            # A later pair can reuse any principal or fee source event in this group.
            leg_events = {leg_id: identifier if event_id in ids else event_id for leg_id, event_id in leg_events.items()}
            research_urls = {part.native_trace_url for part in available if part.native_trace_url}
            event.native_trace_url = next(iter(research_urls)) if len(research_urls) == 1 else None
            available = [event]
        for event in available:
            event.transfers.append(read.model_dump(mode="json"))
            event.relationships.append(TimelineRelationship(kind="owned_transfer", state=read.status, review_id=str(read.id),
                reason_codes=read.reason_codes, review_url=_review_url(None, "transfers") + "&transfer=" + str(read.id)))
            if read.status != "confirmed":
                event.reason_codes.extend(read.reason_codes)
    recovery_groups = set((await session.scalars(select(InvestmentRecoveryEntry.group_id).where(InvestmentRecoveryEntry.workspace_id == workspace_id))).all())
    for group_id in recovery_groups:
        try:
            package = await recovery.list_recovery(session, workspace_id, group_id)
        except (HTTPException, ValidationError):
            errors.append({"group_id": str(group_id), "code": "recovery_detail_unavailable"})
            continue
        for event in result.values():
            sids = {source.source_id for source in event.sources}
            entries = [entry for entry in package.entries if str(entry.observation_id) in sids]
            if not entries:
                continue
            entry_ids = {entry.id for entry in entries}
            reviews = [review for review in package.reviews if review.entry_id in entry_ids or review.target_entry_id in entry_ids]
            event.recovery.append({"group_id": str(group_id), "entries": [entry.model_dump(mode="json") for entry in entries], "reviews": [review.model_dump(mode="json") for review in reviews]})
            for review in reviews:
                if not review.is_current:
                    continue
                event.relationships.append(TimelineRelationship(kind=review.relation_kind or review.kind,
                    state="unresolved" if review.blockers else review.relation_state or review.assertion_status or "unresolved",
                    review_id=str(review.id), reason_codes=review.blockers, conflicting_fields=review.conflicting_fields,
                    review_url=_review_url(group_id, "recovery")))
    for identifier in aliases:
        seen = {identifier}
        target = aliases[identifier]
        while target in aliases and target not in seen:
            seen.add(target)
            target = aliases[target]
        aliases[identifier] = target
    for event in result.values():
        raw_leg_ids = {str(identifier) for identifier, eid in leg_events.items() if aliases.get(eid, eid) == event.event_id}
        event.incidents = [transfers._incident_read(row).model_dump(mode="json") for row in state["incidents"].values() if str(row.leg_id) in raw_leg_ids]
        for relation in event.relationships:
            if relation.event_id in aliases:
                relation.event_id = aliases[relation.event_id]
        if not event.coverage:
            for source in event.sources:
                source_gaps = details.get(source.source_id, {}).get("observation", {}).get("coverage", [])
                cov = TimelineCoverage(coverage_id=f"source:{source.source}:{source.account.group_id}", source=source.source,
                    group_id=source.account.group_id, connection_id=source.account.connection_id,
                    observed={"time_precision": source.time.time_precision}, gaps=sorted(set(source_gaps + ["collection_interval_unknown", "opening_balance_unknown"])))
                event.coverage.append(cov)
                coverage.append(cov)
        _finish(event)
        if any(part["status"] == "confirmed" for part in event.transfers):
            # TransferRead owns pair-specific basis. Never apply one pair's cost to
            # another movement or sum transfers that may carry the same lot twice.
            qualified = [read for read in transfer_reads if str(read.id) in {part["id"] for part in event.transfers} and read.status == "confirmed"]
            event.basis = TimelineBasis(reason_codes=["transfer_basis_scoped_to_selected_legs"])
            if qualified:
                current_principal = [leg for leg in event.legs if leg.is_current and leg.classification != "fee" and leg.quantity_role != "balance_delta"]
                if {leg.classification for leg in current_principal} - {"transfer", "move_in", "move_out"}:
                    event.kind = "unknown"
                if len({leg.settlement_status for leg in current_principal}) > 1 or any(
                    leg.execution_status in {"failed", "error", "canceled", "cancelled"} for leg in current_principal
                ):
                    event.status = "conflicting"
            if len(qualified) == 1:
                read = qualified[0]
                selected = {displayed_leg_ids.get(leg_id) for leg_id in (read.request.out_leg_id, read.request.in_leg_id, *(fee.leg_id for fee in read.request.fees))}
                current_ids = {leg.leg_id for leg in event.legs if leg.is_current and (not leg.non_additive or leg in current_principal)}
                if current_ids == selected:
                    event.basis = TimelineBasis(state="known" if read.unknown_basis_quantity == 0 else "partial" if read.principal_quantity > read.unknown_basis_quantity else "unknown",
                        acquisition_cost=read.acquisition_cost, known_acquisition_cost=read.known_acquisition_cost,
                        unknown_basis_quantity=read.unknown_basis_quantity, reason_codes=read.reason_codes)
    revision = evidence._digest({"state": state["revision"], "events": [(str(row.id), row.event_key) for row in state["events"].values()],
                                 "coverage": [item.model_dump(mode="json") for item in coverage], "errors": errors,
                                 "recovery": [event.recovery for event in result.values()]})
    return state, result, sources, details, coverage, errors, aliases, revision


async def _selected_groups(session, state, workspace_id, group_id, collection_id):
    selected = set(state["groups"]) | {None}
    if group_id is not None:
        if group_id not in state["groups"]:
            raise HTTPException(404, "Wallet not found")
        selected = {group_id}
    if collection_id is not None:
        collection = await session.scalar(select(Collection).where(Collection.id == collection_id, Collection.workspace_id == workspace_id))
        if collection is None:
            raise HTTPException(404, "Collection not found")
        # Assets' collection scope is the collection's explicitly selected wallets.
        selected &= {group.id for group in collection.asset_groups}
    return {str(identifier) if identifier else None for identifier in selected}


def _within_window(event, since, until):
    if not since and not until:
        return True
    for source in event.sources:
        time = source.time
        if time.event_at:
            if (since is None or time.event_at >= since) and (until is None or time.event_at <= until):
                return True
        elif time.event_date:
            if (since is None or time.event_date >= since.date()) and (until is None or time.event_date <= until.date()):
                event.reason_codes.append("intra_day_window_unknown")
                return True
        else:
            event.reason_codes.append("event_window_unknown")
            return True
    return False


def _sort_key(event):
    value = event.time
    day = value.event_at.astimezone(timezone.utc).date().isoformat() if value.event_at else value.event_date.isoformat() if value.event_date else ""
    return day, value.event_at.astimezone(timezone.utc).isoformat() if value.event_at else "", event.event_id


async def list_timeline(session, workspace_id, *, group_id=None, collection_id=None, asset_id=None,
                        canonical_asset_key=None, source=None, status=None, kind=None, direction=None,
                        since=None, until=None, limit=50, offset=0, expected_revision=None):
    for value in (since, until):
        if value and value.utcoffset() is None:
            raise HTTPException(422, "Timeline bounds require an explicit timezone")
    if since and until and since > until:
        raise HTTPException(422, "Timeline end must be on or after start")
    since = since.astimezone(timezone.utc) if since else None
    until = until.astimezone(timezone.utc) if until else None
    state, events, _, _, coverage, errors, _, revision = await _project(session, workspace_id)
    groups = await _selected_groups(session, state, workspace_id, group_id, collection_id)
    scoped = [event for event in events.values() if any(account.group_id in groups for account in event.accounts)]
    options = _merge_assets([asset for event in scoped for asset in event.assets])
    asset_keys = None
    if asset_id:
        holding = state["assets"].get(asset_id)
        if holding is None:
            raise HTTPException(404, "Asset not found")
        asset_keys = {asset.canonical_asset_key for asset in options if str(asset_id) in asset.asset_ids}
        hint = _source({"source": holding.source, "provider": holding.source}, "holding:" + str(asset_id), _account(state, holding.group_id))
        holding_identity = _asset({}, hint, holding)
        asset_keys.add(holding_identity.canonical_asset_key)
        if holding_identity.identity_status == "canonical":
            for asset in options:
                if asset.canonical_asset_key == holding_identity.canonical_asset_key and str(asset_id) not in asset.asset_ids:
                    asset.asset_ids.append(str(asset_id))
        metadata = holding.external_metadata or {}
        if holding_identity.chain == "solana" and metadata.get("token_contract") and not holding_identity.token_program:
            supported, unresolved = set(), False
            for row in state["archives"].values():
                if row.group_id != holding.group_id or row.connection_id != holding.connection_id or row.payload.get("owner") != metadata.get("address") or row.payload.get("chain") != holding_identity.chain:
                    continue
                if row.payload.get("ownership_assertion", {}).get("assertion") != "user_confirmed":
                    unresolved = True
                    continue
                for tx in row.payload.get("transactions", {}).values():
                    for version in tx.get("versions", []):
                        if tx.get("canonical_version") != version.get("version_id"):
                            continue
                        matching = [item for item in version.get("ownership", []) if item.get("mint") == metadata["token_contract"]
                                    and item.get("owner") == metadata["address"]]
                        if not matching:
                            continue
                        if any("owner" in gap or "conflict" in gap for gap in version.get("gaps", [])):
                            unresolved = True
                            continue
                        programs = {item.get("token_program") for item in matching}
                        if None in programs or len(programs) != 1:
                            unresolved = True
                            continue
                        candidates = [asset for event in scoped if any(s.collection_id == str(row.id) and s.source_id.startswith("archive:") and s.is_current and s.availability == "available" for s in event.sources)
                                      for asset in event.assets if asset.chain == holding_identity.chain
                                      and asset.token_address == metadata["token_contract"] and asset.token_program in programs]
                        supported.update(asset.canonical_asset_key for asset in candidates)
            if len(supported) == 1 and not unresolved:
                asset_keys.update(supported)
                for asset in options:
                    if asset.canonical_asset_key in supported and str(asset_id) not in asset.asset_ids:
                        asset.asset_ids.append(str(asset_id))
                for event in scoped:
                    if any(asset.canonical_asset_key in supported for asset in event.assets):
                        event.reason_codes.append("resolved_from_retained_owned_identity")
            else:
                errors.append({"group_id": str(holding.group_id), "asset_id": str(asset_id), "code": "holding_identity_unresolved"})
    scope_revision = evidence._digest([revision, sorted(str(group) for group in groups), str(asset_id), canonical_asset_key, source, status, kind, direction, str(since), str(until)])
    if (expected_revision and expected_revision != scope_revision) or (offset and not expected_revision):
        raise HTTPException(409, {"code": "timeline_scope_changed", "message": "Activity or filters changed; reload the first page"})
    filtered = [event for event in scoped
        if (asset_keys is None or any(asset.canonical_asset_key in asset_keys for asset in event.assets))
        and (canonical_asset_key is None or any(asset.canonical_asset_key == canonical_asset_key for asset in event.assets))
        and (source is None or any(item.source == source or item.provider == source for item in event.sources))
        and (status is None or event.status == status or event.linkage == status)
        and (kind is None or event.kind == kind)
        and (direction is None or any(leg.direction == direction for leg in event.legs))
        and _within_window(event, since, until)]
    filtered.sort(key=_sort_key, reverse=True)
    selected_coverage: dict[str, TimelineCoverage] = {}
    for item in coverage:
        if item.group_id in groups and (source is None or item.source == source):
            if item.coverage_id in selected_coverage:
                selected_coverage[item.coverage_id].gaps = sorted(set(selected_coverage[item.coverage_id].gaps + item.gaps))
            else:
                selected_coverage[item.coverage_id] = item.model_copy(deep=True)
    has_more = offset + limit < len(filtered)
    scoped_errors = [error for error in errors if error.get("group_id") in groups or "group_id" not in error]
    return TimelineRead(workspace_id=str(workspace_id), revision=scope_revision, events=filtered[offset:offset + limit], assets=options,
        coverage=list(selected_coverage.values()), total=len(filtered), limit=limit, offset=offset, has_more=has_more,
        all_available_records_loaded=not has_more and not scoped_errors, errors=scoped_errors)


async def get_timeline_event(session, workspace_id, event_id, *, group_id=None, collection_id=None):
    state, events, _, _, _, _, aliases, _ = await _project(session, workspace_id)
    groups = await _selected_groups(session, state, workspace_id, group_id, collection_id)
    event = events.get(aliases.get(event_id, event_id))
    if event is None or not any(account.group_id in groups for account in event.accounts):
        raise HTTPException(404, "Event unavailable in the selected scope")
    return event


async def get_timeline_source(session, workspace_id, source_id, *, group_id=None, collection_id=None, event_id=None):
    if source_id.startswith("research:"):
        from app.services.onchain_investigation import read_research_source
        return await read_research_source(session, workspace_id, source_id, group_id=group_id, collection_id=collection_id, event_id=event_id)
    state, events, sources, details, _, _, aliases, _ = await _project(session, workspace_id)
    groups = await _selected_groups(session, state, workspace_id, group_id, collection_id)
    source = sources.get(source_id)
    scoped_events = [event for event in events.values() if any(account.group_id in groups for account in event.accounts)
                     and (event_id is None or event.event_id == aliases.get(event_id, event_id))]
    supporting = any(source_id in {part.source_id for part in event.sources} for event in scoped_events)
    if source is None or not supporting and (event_id is not None or source.account.group_id not in groups):
        raise HTTPException(404, "Source unavailable in this workspace")
    return TimelineSourceDetail(workspace_id=str(workspace_id), source=source, **details[source_id])
