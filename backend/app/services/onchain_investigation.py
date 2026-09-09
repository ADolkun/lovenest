"""Read-only connected evidence and explicitly selected durable external research."""
import copy
import hashlib
import json
import re
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.models.workspace import Workspace
from app.models.investment_evidence import InvestmentHistoryCollection
from app.providers.onchain_transport import RequestBudget
from app.schemas.investment_timeline import TimelineAccount, TimelineSourceDetail
from app.schemas.onchain_investigation import InvestigationRead, InvestigationRequest
from app.schemas.onchain_history import HistoryRequest
from app.services import investment_timeline_service as timeline
from app.services import onchain_history as history

MAX_NODES = 24
RESEARCH_VERSION = "asset-leg-window-1"


def _digest(value):
    return hashlib.sha256(history._json_bytes(value)).hexdigest()


def _request(request):
    return InvestigationRequest.model_validate(request.model_dump(include=set(InvestigationRequest.model_fields)))


def _window(event, since, until, direction):
    instant = event.time.event_at
    if instant is not None:
        since = max(since, instant) if since and direction == "out" else instant if direction == "out" else since
        until = min(until, instant) if until and direction == "in" else instant if direction == "in" else until
    return since, until


def _qualified(leg):
    return (leg.is_current and leg.settlement_status == "settled" and leg.interpretation != "unresolved"
            and leg.execution_status in {"success", "completed", "complete", "filled", "settled", "confirmed"}
            and leg.classification != "fee" and (not leg.non_additive or "external_endpoints" in leg.reason_codes))


def _endpoint(leg, side):
    return getattr(leg, side + "_owner") or getattr(leg, side + "_address")


def _instruction_order(leg):
    match = re.fullmatch(r"instruction:(\d+)(?:/inner:(\d+))?", leg.derivation.get("path", ""))
    return (int(match[1]), int(match[2]) if match[2] is not None else -1) if match else None


def _bridge_targets(event, leg, events, direction):
    """Only retained producer evidence can corroborate a reviewed bridge pair."""
    targets = []
    for relation in event.mechanics:
        if relation.get("kind") != "bridge" or relation.get("state") != "confirmed":
            continue
        side, other = ("source", "destination") if direction == "out" else ("destination", "source")
        if relation.get(side + "_event_id") != event.event_id or relation.get(side + "_leg_id") != leg.leg_id:
            continue
        target = events.get(relation.get(other + "_event_id"))
        target_leg = next((item for item in target.legs if item.leg_id == relation.get(other + "_leg_id")), None) if target else None
        if target_leg and _qualified(target_leg):
            targets.append((target, target_leg))
    return targets if len(targets) == 1 else []


def walk_evidence(events, request, owned_endpoints):
    """Navigation is observed adjacency; it never allocates a depositor's funds."""
    root = events.get(request.event_id)
    selected = next((leg for leg in root.legs if leg.leg_id == request.leg_id), None) if root else None
    if selected is None:
        raise HTTPException(404, "Selected event leg is unavailable in this workspace")
    steps, frontier, boundaries, included, visited = [], {}, [], {}, set()
    queue = deque([(root, selected, 0, "selected", request.since, request.until)])
    while queue:
        event, leg, depth, via, since, until = queue.popleft()
        window = {"since": since.isoformat() if since else None, "until": until.isoformat() if until else None}
        identity = _digest([leg.chain, leg.canonical_asset_key, event.event_id, leg.leg_id, request.direction, window])
        if identity in visited:
            continue
        if len(visited) >= MAX_NODES:
            boundaries.append({"code": "node_limit", "event_id": event.event_id, "leg_id": leg.leg_id})
            break
        visited.add(identity)
        included[event.event_id] = event
        steps.append({"event_id": event.event_id, "leg_id": leg.leg_id, "asset_key": leg.canonical_asset_key,
                      "depth": depth, "via": via, "effective_window": window, "attributed_quantity": None})
        if not _qualified(leg):
            boundaries.append({"code": "unsettled_or_unresolved_leg", "event_id": event.event_id, "leg_id": leg.leg_id})
            continue
        if not timeline._within_window(event, since, until):
            boundaries.append({"code": "outside_requested_window", "event_id": event.event_id, "leg_id": leg.leg_id})
            continue
        if event.time.event_at is None:
            boundaries.append({"code": "event_time_order_unknown", "event_id": event.event_id, "leg_id": leg.leg_id})
        if depth >= request.max_hops:
            boundaries.append({"code": "hop_limit", "event_id": event.event_id, "leg_id": leg.leg_id})
            continue
        since, until = _window(event, since, until, request.direction)
        # Asset-changing relationships come only from the producer, never a symbol/time match.
        for relation in event.mechanics:
            if relation.get("kind") in {"swap", "wrap", "unwrap"} and leg.key in relation.get("legs", []):
                for sibling in event.legs:
                    if sibling.key in relation["legs"] and sibling.leg_id != leg.leg_id and _qualified(sibling):
                        if (request.direction == "out" and sibling.direction == "in") or (request.direction == "in" and sibling.direction == "out"):
                            queue.append((event, sibling, depth + 1, relation["kind"], since, until))
        if event.kind == "bridge" or any(item.get("kind") == "bridge" for item in event.mechanics):
            bridges = _bridge_targets(event, leg, events, request.direction)
            if not bridges:
                boundaries.append({"code": "bridge_destination_unresolved", "event_id": event.event_id, "leg_id": leg.leg_id})
            for target, target_leg in bridges:
                if not timeline._within_window(target, since, until):
                    continue
                queue.append((target, target_leg, depth + 1, "corroborated_bridge", since, until))
        # Reviewed producer relationships remain available even when their economics are unknown.
        for relation in event.relationships:
            target = events.get(relation.event_id)
            if relation.state == "confirmed" and target and timeline._within_window(target, since, until) and relation.kind in {"owned_transfer", "source_corroboration", "receipt", "disposition", "transfer"}:
                for target_leg in target.legs:
                    if target_leg.canonical_asset_key == leg.canonical_asset_key and _qualified(target_leg):
                        queue.append((target, target_leg, depth + 1, relation.kind, since, until))
        side, opposite = ("destination", "source") if request.direction == "out" else ("source", "destination")
        endpoint = _endpoint(leg, side)
        outpoint = (leg.derivation.get("outpoint") or leg.derivation.get("spent_outpoint")) if leg.chain == "bitcoin" else None
        if outpoint and not endpoint:
            endpoint = "outpoint:" + outpoint
        if not endpoint:
            # Exchange acquisitions and withdrawals can share an explicitly
            # mapped account even when that source has no on-chain endpoint.
            groups = {account.group_id for account in event.accounts if account.group_id}
            siblings = [(other, candidate) for other in events.values() if other.event_id != event.event_id
                        and groups.intersection(account.group_id for account in other.accounts)
                        and timeline._within_window(other, since, until)
                        for candidate in other.legs if candidate.canonical_asset_key == leg.canonical_asset_key
                        and _qualified(candidate) and candidate.direction == request.direction]
            siblings.sort(key=lambda pair: (pair[1].quantity is None, (pair[1].quantity or Decimal(0)).copy_negate(), pair[0].event_id))
            for other, candidate in siblings[:request.max_branches]:
                queue.append((other, candidate, depth + 1, "observed_account_activity", since, until))
        if not endpoint or not leg.chain:
            boundaries.append({"code": "endpoint_or_acquisition_unknown", "event_id": event.event_id, "leg_id": leg.leg_id})
            continue
        external = (leg.chain, endpoint) not in owned_endpoints
        candidates = []
        for other in events.values():
            if not timeline._within_window(other, since, until):
                continue
            for candidate in other.legs:
                if leg.chain == "bitcoin":
                    outgoing, incoming = ("outpoint", "spent_outpoint") if request.direction == "out" else ("spent_outpoint", "outpoint")
                    if other.event_id == event.event_id:
                        # Inspect the transaction's other side without allocating its outputs to this input.
                        adjacent = bool(leg.derivation.get(incoming) and candidate.derivation.get(outgoing))
                    else:
                        adjacent = bool(leg.derivation.get(outgoing) and leg.derivation[outgoing] == candidate.derivation.get(incoming))
                else:
                    adjacent = endpoint in {_endpoint(candidate, opposite), getattr(candidate, opposite + "_address")}
                if other.event_id == event.event_id and leg.chain != "bitcoin":
                    first, second = _instruction_order(leg), _instruction_order(candidate)
                    if (first is None or second is None or first == second or (second > first) != (request.direction == "out")
                        or getattr(leg, side + "_address") != getattr(candidate, opposite + "_address")):
                        continue
                if (candidate.canonical_asset_key == leg.canonical_asset_key and _qualified(candidate)
                    and adjacent
                    and (candidate.quantity is None or candidate.quantity >= request.minimums.get(candidate.canonical_asset_key, Decimal(0)))):
                    candidates.append((other, candidate))
        if not candidates and leg.chain != "bitcoin":
            groups = {account.group_id for account in event.accounts if account.group_id}
            candidates = [(other, candidate) for other in events.values() if other.event_id != event.event_id
                          and groups.intersection(account.group_id for account in other.accounts)
                          and timeline._within_window(other, since, until)
                          for candidate in other.legs if candidate.canonical_asset_key == leg.canonical_asset_key
                          and candidate.direction == request.direction and _qualified(candidate)
                          and _endpoint(candidate, opposite) is None]
        # Every comparison here is already restricted to one canonical asset and its units.
        candidates.sort(key=lambda pair: (pair[1].quantity is None, (pair[1].quantity or Decimal(0)).copy_negate(), pair[0].event_id, pair[1].leg_id))
        if len(candidates) > request.max_branches:
            boundaries.append({"code": "branch_limit", "event_id": event.event_id, "leg_id": leg.leg_id, "omitted": len(candidates) - request.max_branches})
        for other, candidate in candidates[:request.max_branches]:
            # External evidence is inspectable, with no claim that its full value belongs to this trail.
            same_transaction = leg.chain == "bitcoin" and other.event_id == event.event_id
            queue.append((other, candidate, depth if same_transaction else depth + 1,
                          "observed_transaction_activity" if same_transaction else "observed_external_activity" if external else "observed_owned_activity", since, until))
        if (external or outpoint) and not (leg.chain == "bitcoin" and request.direction == "out" and not leg.derivation.get("outpoint")):
            bounds = {"since": since.isoformat() if since else None, "until": until.isoformat() if until else None}
            item = {"event_id": event.event_id, "leg_id": leg.leg_id, "chain": leg.chain,
                    "address": getattr(leg, side + "_address") or endpoint, "asset_key": leg.canonical_asset_key,
                    "observed_owner": getattr(leg, side + "_owner"),
                    "direction": request.direction, **bounds, "depth": depth + 1, "attributed_quantity": None}
            item["key"] = _digest([leg.chain, item["address"], leg.canonical_asset_key, leg.leg_id, request.direction, bounds])
            frontier[item["key"]] = item
            boundaries.append({"code": "external_ownership_and_allocation_unknown" if external else "selected_outpoint_inspection", "event_id": event.event_id, "leg_id": leg.leg_id})
    return list(included.values()), steps, list(frontier.values()), boundaries


def research_events(row, state, *, events=None):
    """Reuse the timeline's source and leg adapter for the same durable payloads."""
    events = {} if events is None else events
    details = {}
    for research_key, retained in row.payload.get("investigations", {}).items():
        peers = [(peer.id, peer.payload) for peer in state.get("archives", {}).values()]
        archive = history.qualify_archive(retained["archive"], peers or [(row.id, row.payload)])
        for transaction_key, transaction in archive.get("transactions", {}).items():
            event_id = "research:" + _digest([str(row.workspace_id), archive.get("chain"), transaction_key])
            event = events.setdefault(event_id, timeline._new_event(event_id))
            event.reason_codes = sorted(set(event.reason_codes + ["external_ownership_and_allocation_unknown", *archive.get("gaps", [])]))
            legs = {leg.leg_id: leg for leg in event.legs}
            if transaction.get("versions") and transaction.get("canonical_version") is None:
                event.status, event.linkage = "conflicting", "conflicting"
                event.conflicting_fields.append("source_version")
            proxy = SimpleNamespace(id=row.id, group_id=row.group_id, connection_id=row.connection_id,
                                    request=retained["frontier"], updated_at=row.updated_at)
            coverage = timeline._coverage(proxy, archive)
            event.coverage.append(coverage)
            for version in transaction.get("versions", []) or [{}]:
                source, payload = timeline._archive_source(proxy, archive, transaction, version, state)
                source.source_id = "research:" + str(row.id) + ":" + _digest([research_key, transaction_key, version.get("version_id")])
                source.detail_url = "/api/assets/timeline/sources/" + source.source_id
                source.account = TimelineAccount()
                timeline._put_source(event, source)
                details[source.source_id] = TimelineSourceDetail(workspace_id=str(row.workspace_id), source=source,
                    raw_payload={"encoding": "json", "json": json.dumps(payload, separators=(",", ":"))} if payload else None,
                    transaction={"encoding": "json", "json": json.dumps(transaction, separators=(",", ":"))}, coverage=[coverage])
                if source.is_current:
                    for relation in version.get("relationships", []):
                        if relation not in event.mechanics:
                            event.mechanics.append(relation)
                for part in version.get("legs", []):
                    leg, asset = timeline._archive_leg(part, source, archive.get("owner"), state)
                    # Source owners remain observed metadata; none are asserted to belong to this workspace.
                    identity = timeline._archive_leg_identity(version, part, source)
                    leg.leg_id = "research-leg:" + _digest([event_id, identity])
                    leg.non_additive = bool(part.get("non_additive")) or (leg.execution_status != "success" and leg.classification != "fee")
                    if leg.leg_id in legs:
                        prior = legs[leg.leg_id]
                        prior.source_ids = sorted(set(prior.source_ids + leg.source_ids))
                    else:
                        legs[leg.leg_id] = leg
                        event.legs.append(leg)
                        event.assets.append(asset)
            timeline._finish(event)
            events[event_id] = event
    return events, details


async def _scope(session, workspace_id, request):
    state, events, _, _, _, _, aliases, _ = await timeline._project(session, workspace_id)
    request = _request(request)
    request.event_id = aliases.get(request.event_id, request.event_id)
    root = events.get(request.event_id)
    if root is None or not any(account.group_id for account in root.accounts):
        raise HTTPException(404, "Owned event unavailable in the selected workspace")
    selected = next((leg for leg in root.legs if leg.leg_id == request.leg_id), None)
    row = _event_collection(state, root, selected) if selected else None
    owned = {(archive.payload.get("chain"), archive.payload.get("owner")) for archive in state["archives"].values()
             if archive.payload.get("ownership_assertion")}
    attach_reviewed_bridges(state, events)
    if row is None and selected:
        _, steps, _, _ = walk_evidence(events, request, owned)
        for step in sorted(steps, key=lambda item: (item["depth"], item["event_id"], item["leg_id"])):
            reached = events[step["event_id"]]
            reached_leg = next(part for part in reached.legs if part.leg_id == step["leg_id"])
            row = _event_collection(state, reached, reached_leg)
            if row is not None:
                break
    # Read reachability is independent of the archive chosen to store new progress.
    for retained in state["archives"].values():
        research_events(retained, state, events=events)
    attach_reviewed_bridges(state, events)
    return state, events, owned, row, request


def _event_collection(state, event, leg):
    collections = {source.collection_id for source in event.sources if source.collection_id and source.is_current
                   and source.availability == "available" and source.source_id in leg.source_ids}
    rows = sorted((row for row in state["archives"].values() if str(row.id) in collections),
                  key=lambda row: (history._db_time(row.updated_at), str(row.id)), reverse=True)
    return rows[0] if rows else _root_collection(state, event, leg)


def _completed_frontier(state, row, request, frontier):
    """Reuse completed compatible reads without moving another root's writer binding."""
    from app.providers import bitcoin_history, evm_history, solana_history
    decoder = (bitcoin_history if frontier["chain"] == "bitcoin" else solana_history if frontier["chain"] == "solana" else evm_history).DECODER_VERSION
    if row.payload.get("source_identity") != history.history_source_identity(row.payload["chain"]):
        return False
    for peer in state["archives"].values():
        saved = peer.payload.get("investigations", {}).get(frontier["key"])
        if not saved or saved["archive"].get("resumable") is not False:
            continue
        binding = saved.get("binding", {})
        if (binding.get("version") != RESEARCH_VERSION
            or binding.get("root_anchor") != row.payload.get("anchor")
            or binding.get("root_decoder") != row.payload.get("decoder_version")
            or saved["archive"].get("decoder_version") != decoder
            or saved["archive"].get("source_identity") != history.history_source_identity(frontier["chain"])):
            continue
        if frontier["chain"] == "bitcoin":
            limits = saved["archive"].get("research", {}).get("limits", {})
            if (limits.get("hops", -1) < request.max_hops - frontier["depth"]
                or limits.get("branches", -1) < request.max_branches):
                continue
        return True
    return False


async def preview_investigation(session, workspace_id, request):
    state, events, owned, row, canonical = await _scope(session, workspace_id, request)
    included, steps, frontier, boundaries = walk_evidence(events, canonical, owned)
    if row is None:
        frontier = []
        boundaries.append({"code": "no_retained_collection"})
    else:
        for item in frontier:
            item["resumable"] = not _completed_frontier(state, row, canonical, item)
    return InvestigationRead(workspace_id=str(workspace_id), request=canonical, collection_id=row.id if row else None,
                             revision=row.revision if row else None, events=included, steps=steps, frontier=frontier, boundaries=boundaries)


async def continue_investigation(session, workspace_id, request):
    try:
        await session.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update(nowait=True))
        state, events, owned, row, canonical = await _scope(session, workspace_id, request)
        if row is None or row.id != request.collection_id:
            raise history._error(404, "investigation_unavailable", "Selected collection is not a source of this owned event")
        if row.revision != request.expected_revision:
            raise history._error(409, "history_revision_conflict", "Saved evidence changed; reopen before continuing")
        _, account, group, _ = await history._owned_context(session, workspace_id, HistoryRequest.model_validate(row.request))
        if row.group_id != (group.id if group else None) or row.payload.get("ownership_assertion", {}).get("account_id") != str(account.id):
            raise history._error(409, "history_restart_required", "Owned account mapping changed; restart explicitly")
        _, _, frontier, _ = walk_evidence(events, canonical, owned)
        chosen = next((item for item in frontier if item["key"] == request.frontier_key), None)
        if chosen is None:
            raise history._error(409, "investigation_frontier_changed", "Selected external leg or effective window changed")
        root_source = history.history_source_identity(row.payload["chain"])
        if row.payload["source_identity"] != root_source:
            raise history._error(409, "history_restart_required", "Saved collection source changed; restart explicitly")
        if _completed_frontier(state, row, canonical, chosen):
            raise history._error(409, "investigation_complete", "Selected evidence is already retained; reopen the preview")
        retained = copy.deepcopy(row.payload)
        entries = retained.setdefault("investigations", {})
        prior = entries.get(chosen["key"])
        binding = {"version": RESEARCH_VERSION, "request": canonical.model_dump(mode="json"), "frontier": chosen,
                   "root_anchor": row.payload.get("anchor"), "root_decoder": row.payload.get("decoder_version")}
        if prior and prior["binding"] != binding:
            raise history._error(409, "history_restart_required", "Research asset, leg, window, decoder or anchor changed")
        if not prior and len(entries) >= MAX_NODES:
            raise history._error(413, "investigation_node_limit", "Retained external endpoint limit reached")
        # Reserve room before upstream work, under the existing workspace quota lock.
        used = await session.scalar(select(func.coalesce(func.sum(InvestmentHistoryCollection.size_bytes), 0)).where(InvestmentHistoryCollection.workspace_id == workspace_id))
        prior_size = len(history._json_bytes(prior)) if prior else 0
        available = min(history.MAX_COLLECTION_BYTES - row.size_bytes, history.MAX_WORKSPACE_BYTES - used) + prior_size - 65536
        if available < 65536:
            raise history._error(413, "history_storage_limit", "Collection has insufficient evidence capacity")
        budget = RequestBudget(max_attempts=600)
        deadline = time.monotonic() + 45
        kwargs = {"source_identity": history.history_source_identity(chosen["chain"]),
                  "since": history._instant(chosen["since"]), "until": history._instant(chosen["until"]),
                  "state": copy.deepcopy(prior["archive"]) if prior else None, "budget": budget, "deadline": deadline,
                  "byte_limit": available}
        try:
            if chosen["chain"] == "bitcoin":
                from app.providers.bitcoin_history import continue_bitcoin_history
                # The selected producer leg supplies the outpoint; client text never does.
                event = events[chosen["event_id"]]
                leg = next(item for item in event.legs if item.leg_id == chosen["leg_id"])
                outpoint = leg.derivation.get("outpoint") or leg.derivation.get("spent_outpoint")
                if not outpoint:
                    raise history._error(422, "investigation_outpoint_unknown", "Selected Bitcoin leg has no supported outpoint")
                archive = await continue_bitcoin_history(row.payload["owner"], outpoint=outpoint, direction=chosen["direction"],
                    supplied_accounts=[{key: value for key, value in item.items() if value is not None} for item in row.request.get("supplied_accounts", [])],
                    max_hops=canonical.max_hops - chosen["depth"], max_branches=canonical.max_branches,
                    max_nodes=MAX_NODES - sum(len(entry["archive"].get("research", {}).get("nodes", [])) if isinstance(entry["archive"].get("research"), dict) else 1 for key, entry in entries.items() if key != chosen["key"]), **kwargs)
            else:
                if chosen["chain"] == "solana":
                    kwargs["research_address"] = chosen["address"]
                archive = await history.collect_archive(chosen["chain"], chosen.get("observed_owner") or chosen["address"], research=True, **kwargs)
        except ValueError as exc:
            raise history._error(409, "history_restart_required", "Research source, decoder or anchor changed; restart explicitly") from exc
        entry = {"binding": binding, "frontier": chosen, "archive": archive}
        entries[chosen["key"]] = entry
        encoded = history._json_bytes(retained)
        if len(encoded) > history.MAX_COLLECTION_BYTES or used - row.size_bytes + len(encoded) > history.MAX_WORKSPACE_BYTES:
            raise history._error(413, "history_storage_limit", "Producer exceeded its reserved evidence allowance")
        row.payload, row.size_bytes, row.revision = retained, len(encoded), hashlib.sha256(encoded).hexdigest()
        row.updated_at = datetime.now(timezone.utc)
        await session.commit()
        result = await preview_investigation(session, workspace_id, canonical)
        result.evidence = {"limits": {"seconds": 45, "attempts": 600, "attempts_used": budget.attempts, "concurrency": 1, "nodes": MAX_NODES},
                           "coverage": archive.get("coverage", {}), "gaps": archive.get("gaps", [])}
        return result
    except DBAPIError as exc:
        await session.rollback()
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise history._error(409, "history_busy", "Another operation is using this workspace; retry after it finishes") from exc
        raise
    except BaseException:
        await session.rollback()
        raise


async def read_research_source(session, workspace_id, source_id, *, event_id=None, group_id=None, collection_id=None):
    try:
        identifier = uuid.UUID(source_id.split(":")[1])
    except (ValueError, IndexError):
        raise HTTPException(404, "Source unavailable in this workspace") from None
    row = await history.load_history(session, workspace_id, identifier)
    if group_id is not None and row.group_id != group_id:
        raise HTTPException(404, "Source unavailable in this wallet")
    # Reuse the producer's collection membership authorization.
    state, _, _, _, _, _, _, _ = await timeline._project(session, workspace_id)
    groups = await timeline._selected_groups(session, state, workspace_id, group_id, collection_id)
    if str(row.group_id) not in groups:
        raise HTTPException(404, "Source unavailable in this collection")
    events, details = research_events(row, state)
    source = details.get(source_id)
    if source is None or event_id and not any(event.event_id == event_id and any(s.source_id == source_id for s in event.sources) for event in events.values()):
        raise HTTPException(404, "Source unavailable in this event")
    return source


_BRIDGE_FIELDS = ("bridge_protocol", "bridge_message_id", "bridge_source_chain", "bridge_destination_chain",
                  "bridge_source_asset", "bridge_destination_asset")


def _root_collection(state, event, leg):
    groups = {account.group_id for account in event.accounts}
    return next((row for row in sorted(state["archives"].values(), key=lambda row: str(row.id))
                 if str(row.group_id) in groups and row.payload.get("chain") == leg.chain
                 and row.payload.get("ownership_assertion")
                 and any(transaction.get("signature") == leg.transaction_ref for transaction in row.payload.get("transactions", {}).values())), None)


def _bridge_fees(event, leg):
    if leg.fee is not None and leg.fee_currency:
        return [{"quantity": str(leg.fee), "asset": leg.fee_currency}]
    fees = [item for item in event.legs if item.classification == "fee" and item.transaction_ref == leg.transaction_ref and item.is_current]
    if not fees or any(item.quantity is None or item.settlement_status != "settled" or item.interpretation == "unresolved" for item in fees):
        return None
    return [{"quantity": str(item.quantity), "asset": item.canonical_asset_key, "payer": item.fee_payer} for item in fees]


def _bridge_source(event, leg):
    return next((source for source in event.sources if source.source_id in leg.source_ids and source.is_current
                 and source.availability == "available" and source.source_kind == "primary_activity"), None)


def _bridge_candidates(state, events):
    endpoints = [(event, leg) for event in events.values() for leg in event.legs
                 if leg.is_current and leg.derivation.get("bridge_role") in {"send", "receive"}]
    result = []
    qualified_archives = {}
    for event, leg in endpoints:
        metadata = leg.derivation
        if metadata["bridge_role"] != "send":
            continue
        receivers = [(other, part) for other, part in endpoints if part.derivation["bridge_role"] == "receive"
                     and part.derivation.get("bridge_protocol") == metadata.get("bridge_protocol")
                     and part.derivation.get("bridge_message_id") == metadata.get("bridge_message_id")]
        identities = {(part.chain, part.transaction_ref, part.leg_ref or part.key) for _, part in receivers}
        target, receiving = receivers[0] if receivers else (None, None)
        source, destination = _bridge_source(event, leg), _bridge_source(target, receiving) if target else None
        row = _root_collection(state, event, leg)
        reasons = []
        if len({_bridge_endpoint_facts(other, part) for other, part in receivers}) > 1:
            reasons.append("bridge_destination_fact_conflict")
        senders = [(other, part) for other, part in endpoints if part.derivation["bridge_role"] == "send"
                   and part.derivation.get("bridge_protocol") == metadata.get("bridge_protocol")
                   and part.derivation.get("bridge_message_id") == metadata.get("bridge_message_id")]
        if len({_bridge_endpoint_facts(other, part) for other, part in senders}) > 1:
            reasons.append("bridge_source_fact_conflict")
        if len(identities) != 1:
            reasons.append("bridge_destination_conflict" if identities else "bridge_destination_missing")
        if not all(isinstance(metadata.get(key), str) and metadata[key].strip() for key in _BRIDGE_FIELDS):
            reasons.append("bridge_mapping_unknown")
        if receiving and any(metadata.get(key) != receiving.derivation.get(key) for key in _BRIDGE_FIELDS):
            reasons.append("bridge_mapping_conflict")
        if not _qualified(leg) or leg.direction != "out" or receiving and (not _qualified(receiving) or receiving.direction != "in"):
            reasons.append("bridge_execution_or_settlement_unknown")
        if receiving and (metadata.get("bridge_source_chain") != leg.chain or metadata.get("bridge_destination_chain") != receiving.chain
                          or metadata.get("bridge_source_asset") != leg.canonical_asset_key or metadata.get("bridge_destination_asset") != receiving.canonical_asset_key
                          or leg.chain == receiving.chain):
            reasons.append("bridge_endpoint_mapping_conflict")
        if not source or not destination or receiving is None or source.source_id == destination.source_id or not leg.transaction_ref or not receiving.transaction_ref:
            reasons.append("bridge_independent_execution_evidence_unknown")
        elif source.source_locator and source.source_locator == destination.source_locator:
            reasons.append("bridge_duplicated_attestation")
        if leg.quantity is None or receiving and receiving.quantity is None:
            reasons.append("bridge_quantity_unknown")
        fees = _bridge_fees(event, leg)
        destination_fees = _bridge_fees(target, receiving) if target else None
        if fees is None or destination_fees is None:
            reasons.append("bridge_fee_unknown")
        if row is None:
            reasons.append("no_retained_collection")
        else:
            if row.id not in qualified_archives:
                qualified_archives[row.id] = history.qualify_archive(row.payload, [(peer.id, peer.payload) for peer in state["archives"].values() if peer.id != row.id])
            reasons.extend(_root_bridge_reasons(qualified_archives[row.id], leg))
        candidate: dict = {"source_event_id": event.event_id, "source_leg_id": leg.leg_id,
                     "destination_event_id": target.event_id if target else None, "destination_leg_id": receiving.leg_id if receiving else None,
                     "source_id": source.source_id if source else None, "destination_source_id": destination.source_id if destination else None,
                     "protocol": metadata.get("bridge_protocol"), "message_id": metadata.get("bridge_message_id"),
                     "source_label": leg.chain, "destination_label": receiving.chain if receiving else metadata.get("bridge_destination_chain"),
                     "collection_id": str(row.id) if row else None, "revision": row.revision if row else None,
                     "status": "unresolved" if reasons else "eligible", "reason_codes": sorted(set(reasons)),
                     "source_quantity": str(leg.quantity) if leg.quantity is not None else None,
                     "destination_quantity": str(receiving.quantity) if receiving and receiving.quantity is not None else None,
                     "source_fees": fees, "destination_fees": destination_fees,
                     "source_asset": leg.canonical_asset_key, "destination_asset": receiving.canonical_asset_key if receiving else None}
        for side, endpoint_leg in (("source", leg), ("destination", receiving)):
            candidate[side + "_summary"] = ({key: getattr(endpoint_leg, key) for key in (
                "chain", "canonical_asset_key", "transaction_ref", "source_address", "destination_address", "source_owner", "destination_owner")}
                | {"fee_asset": endpoint_leg.fee_currency, "quantity": str(endpoint_leg.quantity) if endpoint_leg.quantity is not None else None,
                   "fee": str(endpoint_leg.fee) if endpoint_leg.fee is not None else None}) if endpoint_leg else None
        for retained in state["archives"].values():
            for review in retained.payload.get("bridges", []):
                same_pair = all(candidate.get(key) == review.get(key) for key in ("source_event_id", "source_leg_id", "destination_event_id", "destination_leg_id", "source_id", "destination_source_id"))
                if same_pair and not reasons:
                    candidate.update(status="confirmed", review_id=review["review_id"])
                elif receiving and review.get("destination_leg_id") == receiving.leg_id and review.get("source_leg_id") != leg.leg_id:
                    candidate["status"] = "unresolved"
                    candidate["reason_codes"].append("bridge_destination_already_reviewed")
        result.append(candidate)
    return result


def attach_reviewed_bridges(state, events):
    # Raw decoder relationships are evidence, not a reviewed cross-chain link.
    for event in events.values():
        event.mechanics = [relation for relation in event.mechanics if not (relation.get("kind") == "bridge" and relation.get("review_id"))]
        for relation in event.mechanics:
            if relation.get("kind") == "bridge":
                relation["state"] = "unresolved"
    for candidate in _bridge_candidates(state, events):
        if candidate["status"] == "confirmed":
            relation = {**candidate, "kind": "bridge", "state": "confirmed", "tax_treatment": "unknown", "basis": "unknown"}
            for side in ("source", "destination"):
                event = events[candidate[side + "_event_id"]]
                event.mechanics.append(relation)


async def bridge_candidates(session, workspace_id, event_id):
    state, events, _, _, _, _, aliases, _ = await timeline._project(session, workspace_id)
    identifier = aliases.get(event_id, event_id)
    if identifier not in events:
        raise HTTPException(404, "Event unavailable in the selected workspace")
    candidates = [candidate for candidate in _bridge_candidates(state, events)
                  if identifier in (candidate["source_event_id"], candidate["destination_event_id"])]
    shown = candidates[:100]
    if len(candidates) > len(shown):
        shown[-1]["omitted_candidates"] = len(candidates) - len(shown)
        shown[-1]["reason_codes"].append("bridge_candidate_limit")
    return shown


async def review_bridge(session, workspace_id, user_id, request):
    try:
        await session.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update(nowait=True))
        row = await history.load_history(session, workspace_id, request.collection_id)
        if row.revision != request.expected_revision:
            raise history._error(409, "history_revision_conflict", "Evidence changed; reopen the bridge candidate")
        state, events, _, _, _, _, _, _ = await timeline._project(session, workspace_id)
        fields = ("source_event_id", "source_leg_id", "destination_event_id", "destination_leg_id", "source_id", "destination_source_id")
        candidate = next((item for item in _bridge_candidates(state, events)
                          if item["collection_id"] == str(row.id) and all(item.get(key) == getattr(request, key) for key in fields)), None)
        if candidate is None or candidate["status"] == "unresolved":
            raise history._error(409, "bridge_evidence_unresolved", "Both independently supported executed endpoints, mappings and fees are required")
        if candidate["status"] == "confirmed":
            return candidate
        payload = copy.deepcopy(row.payload)
        reviews = payload.setdefault("bridges", [])
        if len(reviews) >= 100:
            raise history._error(413, "bridge_review_limit", "Collection bridge review limit reached")
        review = {key: getattr(request, key) for key in fields}
        review.update(review_id=_digest(review), reviewed_by=str(user_id), reviewed_at=datetime.now(timezone.utc).isoformat(),
                      decision="reviewed_relationship", current_validation="required_on_read")
        reviews.append(review)
        encoded = history._json_bytes(payload)
        used = await session.scalar(select(func.coalesce(func.sum(InvestmentHistoryCollection.size_bytes), 0)).where(InvestmentHistoryCollection.workspace_id == workspace_id))
        if len(encoded) > history.MAX_COLLECTION_BYTES or used - row.size_bytes + len(encoded) > history.MAX_WORKSPACE_BYTES:
            raise history._error(413, "history_storage_limit", "Bridge evidence storage limit reached")
        row.payload, row.size_bytes, row.revision, row.updated_at = payload, len(encoded), hashlib.sha256(encoded).hexdigest(), datetime.now(timezone.utc)
        await session.commit()
        return {**candidate, "status": "confirmed", "review_id": review["review_id"], "revision": row.revision}
    except DBAPIError as exc:
        await session.rollback()
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise history._error(409, "history_busy", "Another operation is using this workspace") from exc
        raise
    except BaseException:
        await session.rollback()
        raise


async def bridge_review_statuses(session, workspace_id, row):
    state, events, _, _, _, _, _, _ = await timeline._project(session, workspace_id)
    candidates = _bridge_candidates(state, events)
    result = []
    for review in row.payload.get("bridges", []):
        current = next((candidate for candidate in candidates if candidate.get("review_id") == review["review_id"]), None)
        result.append({"review_id": review["review_id"], "status": "confirmed" if current else "unresolved",
                       "reason_codes": [] if current else ["bridge_sources_changed_or_unavailable"]})
    return result


def _root_bridge_reasons(archive, leg):
    """An imported label cannot contradict its retained source-chain execution."""
    reasons = []
    facts = [(version, part) for transaction, version in history.current_versions(archive)
             if transaction.get("signature") == leg.transaction_ref
             for part in version.get("legs", []) if part.get("key") == (leg.leg_ref or leg.key)]
    if len(facts) != 1:
        return ["bridge_source_leg_uncorroborated"]
    version, part = facts[0]
    identity = part.get("asset", {})
    asset = _digest(["chain", identity.get("chain"), "native" if identity.get("native") else identity.get("mint") or identity.get("contract"), identity.get("token_program") if not identity.get("native") else None])
    if (version.get("execution") != "success" or version.get("settlement") != "settled"
        or part.get("settlement", "settled") != "settled" or part.get("interpretation") == "unresolved" or part.get("non_additive")
        or asset != leg.canonical_asset_key or part.get("quantity") is None or Decimal(part["quantity"]) != leg.quantity
        or part.get("source") != leg.source_address or part.get("destination") != leg.destination_address):
        reasons.append("bridge_source_execution_conflict")
    if leg.fee is not None:
        fees = [item for item in version.get("legs", []) if item.get("role") == "network_fee"]
        known = [item for item in fees if item.get("quantity") is not None and item.get("settlement", "settled") == "settled"]
        if len(known) != len(fees) or not fees:
            reasons.append("bridge_source_fee_uncorroborated")
        elif len(fees) != 1 or Decimal(fees[0]["quantity"]) != leg.fee or not fees[0]["asset"].get("native") or leg.fee_currency != _digest(["chain", leg.chain, "native", None]):
            reasons.append("bridge_source_fee_conflict")
    return reasons


def _bridge_endpoint_facts(event, leg):
    fees = _bridge_fees(event, leg)
    return (leg.canonical_asset_key, leg.quantity, leg.source_address, leg.destination_address,
            leg.source_owner, leg.destination_owner, leg.execution_status, leg.settlement_status, leg.interpretation,
            tuple((key, leg.derivation.get(key)) for key in _BRIDGE_FIELDS),
            frozenset((item["asset"], Decimal(item["quantity"]), item.get("payer")) for item in fees) if fees is not None else None)
