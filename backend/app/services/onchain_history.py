"""Workspace-authorized, durable history evidence; never applies financial rows."""
import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.models.account import Account
from app.models.asset_group import AssetGroup
from app.models.bank_connection import BankConnection
from app.models.investment_evidence import InvestmentHistoryCollection, InvestmentObservation
from app.models.workspace import Workspace
from app.providers.onchain import (
    ACCOUNT_EXTERNAL_ID, address_is_valid, normalize_address, parse_addresses, rpc_url,
)
from app.schemas.investment_evidence import EvidenceLegInput, EvidenceObservationInput
from app.schemas.onchain_history import HistoryRead, HistoryRequest, HistorySummary
from app.services import investment_evidence_service as crosswalk
from app.services.onchain_trace import resolve_chain

MAX_COLLECTION_BYTES = 32 * 1024 * 1024
MAX_WORKSPACE_BYTES = 128 * 1024 * 1024
MAX_COLLECTIONS = 100


def _error(status, code, message, reason=None):
    return HTTPException(status, {"code": code, "message": message, "reason": reason})


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _asset_key(asset):
    return (asset.get("chain"), bool(asset.get("native")), asset.get("mint"), asset.get("token_program"))


def _instant(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _db_time(value):
    return value.replace(tzinfo=timezone.utc) if value.utcoffset() is None else value.astimezone(timezone.utc)


def current_versions(archive):
    for transaction in archive.get("transactions", {}).values():
        current = next((v for v in transaction.get("versions", []) if v["version_id"] == transaction.get("canonical_version")), None)
        if current is not None:
            yield transaction, current


def project_observations(archive, collection_id):
    """Lossless segments keep the existing 100-leg import boundary intact."""
    owner = archive["owner"]
    result = []
    for transaction, version in current_versions(archive):
        legs = []
        bounded_quantities = set()
        for leg in version.get("legs", []):
            if leg.get("non_additive"):
                continue
            source_owned = leg.get("source_owner") == owner or leg.get("source") == owner
            destination_owned = leg.get("destination_owner") == owner or leg.get("destination") == owner
            if source_owned == destination_owned:
                continue  # Internal account rearrangement is retained in the archive, not an owned quantity change.
            asset = leg["asset"]
            native = bool(asset.get("native"))
            roles = {"network_fee", "fee", "token_fee", "token_transfer_fee", "withheld_fee"}
            quantity = None if leg.get("interpretation") == "unresolved" else leg.get("quantity")
            try:
                EvidenceLegInput.exact_decimal(quantity)
            except ValueError:
                # The producer archive supports all u8 token decimals; the
                # financial crosswalk deliberately has a narrower range.
                quantity = None
                bounded_quantities.add(leg["key"])
            legs.append(EvidenceLegInput(
                key=leg["key"], chain=asset["chain"],
                token_address="native" if native else asset.get("mint"),
                provider_asset_id=":".join(str(part or "") for part in _asset_key(asset)),
                asset_symbol="SOL" if native else None,
                direction="out" if source_owned else "in",
                quantity=quantity,
                classification="unknown" if leg.get("interpretation") == "unresolved" else "fee" if leg.get("role") in roles else "transfer",
                transaction_ref=transaction["signature"], leg_ref=leg["key"],
            ))
        for offset in range(0, len(legs), 100):
            segment = offset // 100
            source_id = f"{transaction['signature']}:{segment}"
            when = _instant(version.get("block_time"))
            settled = version.get("settlement") == "settled" and version.get("in_requested_window") is True
            result.append(EvidenceObservationInput(
                reference=f"{collection_id}:{source_id}:{version['version_id']}",
                source="onchain_history", provider="onchain", source_account_id=f"solana:{owner}",
                account_external_id=ACCOUNT_EXTERNAL_ID, source_local_id=source_id,
                source_locator=f"onchain/history/{collection_id}/payloads/{version['payload_digest']}",
                observed_at=_instant(archive.get("payloads", {}).get(version["payload_digest"], {}).get("retrieved_at")),
                event_time_raw=str(version.get("block_time")) if when else None,
                event_at=when, event_date=when.date() if when else None,
                timezone="UTC" if when else None, time_precision="second" if when else "unknown",
                provider_status=version.get("execution"), network_status=version.get("confirmation_status"),
                settlement_status="settled" if settled else "pending" if version.get("settlement") == "provisional" else "unknown",
                order_ref=f"solana:{transaction['signature']}",
                coverage=["historical_inventory_unresolved", "basis_unknown", "funding_unknown"],
                reason_codes=(
                    (["requested_window_unresolved"] if version.get("in_requested_window") is not True else [])
                    + (["crosswalk_quantity_out_of_range"] if any(leg.key in bounded_quantities for leg in legs[offset:offset + 100]) else [])
                ),
                legs=legs[offset:offset + 100],
            ))
    return result


def reconcile_history(archive):
    """Exact quantities at observed transaction boundaries, separately qualified UTC scope."""
    rows = {}
    versions = list(current_versions(archive))
    for transaction, version in versions:
        if version.get("in_requested_window") is False:
            continue
        for observation in version.get("observations", []):
            asset = observation.get("asset")
            account = observation.get("account")
            if not asset or not account or observation.get("owner") != archive["owner"]:
                continue
            key = (account, _asset_key(asset))
            row = rows.setdefault(key, {"account": account, "asset": asset, "samples": [], "changes": []})
            row["samples"].append((version, observation, transaction["signature"]))
    for row in rows.values():
        account, identity = row["account"], _asset_key(row["asset"])
        for _, version in versions:
            if version.get("settlement") != "settled" or version.get("in_requested_window") is not True:
                continue
            for leg in version.get("legs", []):
                if leg.get("non_additive") or leg.get("interpretation") == "unresolved":
                    continue
                if _asset_key(leg["asset"]) != identity or leg.get("settlement", "settled") != "settled":
                    continue
                quantity = leg.get("quantity")
                if quantity is None:
                    continue
                signed = int(leg.get("destination") == account) - int(leg.get("source") == account)
                if signed:
                    row["changes"].append(Decimal(quantity) if signed > 0 else Decimal(quantity).copy_negate())
        samples = sorted(row.pop("samples"), key=lambda sample: (sample[0].get("slot") or -1, sample[2]))
        first, last = samples[0], samples[-1]
        reasons = ["requested_boundary_snapshots_unavailable", "historical_inventory_unresolved", "basis_unknown"]
        if archive.get("coverage", {}).get("retrieval") != "complete":
            reasons.append("retrieval_incomplete")
        interpretation_gaps = {gap for sample in samples for gap in sample[0].get("gaps", [])}
        reasons.extend(sorted(interpretation_gaps))
        opening = first[1].get("pre_quantity")
        closing = last[1].get("post_quantity")
        if any(sample[0].get("settlement") != "settled" for sample in samples):
            reasons.append("provisional_observations")
            opening = closing = None
        if any(sample[0].get("in_requested_window") is not True for sample in samples):
            reasons.append("requested_window_unresolved")
            opening = closing = None
        if len({sample[0].get("slot") for sample in samples}) != len(samples):
            reasons.append("same_slot_transaction_order_unknown")
            opening = closing = None
        if opening is None:
            reasons.append("opening_quantity_unknown")
        if closing is None:
            reasons.append("closing_quantity_unknown")
        if any(sample[0].get("block_time") is None for sample in samples):
            reasons.append("missing_timestamp")
        with localcontext(prec=512):
            change = sum(row.pop("changes"), Decimal("0"))
            expected = Decimal(opening) + change if opening is not None else None
            discrepancy = Decimal(closing) - expected if closing is not None and expected is not None else None
        row.update(
            opening=opening, closing=closing, settled_change=str(change),
            expected_closing=str(expected) if expected is not None else None,
            discrepancy=str(discrepancy) if discrepancy is not None else None,
            status="unknown" if discrepancy is None or interpretation_gaps else "matched" if discrepancy == 0 else "discrepancy",
            scope="observed_transaction_boundaries", requested_interval_status="unresolved",
            opening_snapshot={"slot": first[0].get("slot"), "transaction": first[2], "position": "before", "time": first[0].get("block_time")},
            closing_snapshot={"slot": last[0].get("slot"), "transaction": last[2], "position": "after", "time": last[0].get("block_time")},
            reasons=reasons, missing_coverage=reasons,
        )
    return list(rows.values())


def _transaction_conflicts(archives):
    """Hash each content-addressed payload once for the whole authorized scope."""
    facts = {}
    payload_hashes = {}
    for collection_id, source in archives:
        for key, transaction in source.get("transactions", {}).items():
            identity = (source.get("chain"), source.get("owner"), key)
            fact = facts.setdefault(identity, {"variants": set(), "collections": set(), "conflicting": False})
            fact["collections"].add(str(collection_id) if collection_id else "this_collection")
            fact["conflicting"] |= transaction.get("revision_status") in {"conflicting_or_reorganized", "cross_collection_conflict"}
            for version in transaction.get("versions", []):
                digest = version["payload_digest"]
                payload = source.get("payloads", {}).get(digest)
                if payload is None:
                    continue
                if digest not in payload_hashes:
                    payload_hashes[digest] = hashlib.sha256(_json_bytes(payload["response"].get("result"))).hexdigest()
                fact["variants"].add(payload_hashes[digest])
    return {identity: sorted(fact["collections"]) for identity, fact in facts.items() if len(fact["variants"]) > 1 or fact["conflicting"]}


def qualify_archive(archive, peers):
    """An overlapping older collection cannot resurrect contradicted chain facts."""
    conflicts = _transaction_conflicts([(None, archive), *peers])
    qualified = copy.deepcopy(archive)
    for key, transaction in qualified.get("transactions", {}).items():
        identity = (archive.get("chain"), archive.get("owner"), key)
        if identity in conflicts:
            transaction["canonical_version"] = None
            transaction["revision_status"] = "cross_collection_conflict"
            transaction["conflicting_collections"] = conflicts[identity]
            qualified.setdefault("gaps", []).append("cross_collection_conflict")
    qualified["gaps"] = sorted(set(qualified.get("gaps", [])))
    if "cross_collection_conflict" in qualified["gaps"]:
        qualified.setdefault("coverage", {}).update(interpretation="partial", settlement="partial")
    qualified["reconciliation"] = reconcile_history(qualified)
    return qualified


async def _peer_archives(session, workspace_id, connection_id, owner, collection_id=None):
    query = select(InvestmentHistoryCollection).where(
        InvestmentHistoryCollection.workspace_id == workspace_id,
        InvestmentHistoryCollection.connection_id == connection_id,
        InvestmentHistoryCollection.request["address"].as_string() == owner,
    )
    if collection_id:
        query = query.where(InvestmentHistoryCollection.id != collection_id)
    return [(row.id, row.payload) for row in (await session.scalars(query)).all()]


async def _owned_context(session, workspace_id, request):
    try:
        chain = resolve_chain(request.chain)
    except ValueError as exc:
        raise _error(422, "history_unsupported", "Unknown history chain") from exc
    if chain.key != "solana":
        raise _error(422, "history_unsupported", "Owned historical evidence is currently supported only for Solana")
    address = normalize_address(chain, request.address)
    if not address_is_valid(chain, address):
        raise _error(422, "history_invalid_address", "Invalid address for the selected chain")
    connection = await session.scalar(select(BankConnection).where(
        BankConnection.id == request.connection_id, BankConnection.workspace_id == workspace_id,
        BankConnection.provider == "onchain",
    ).with_for_update(nowait=True).execution_options(populate_existing=True))
    if connection is None:
        raise _error(404, "history_context_unavailable", "Connected wallet not found")
    watched = []
    for entry in (connection.credentials or {}).get("addresses", []):
        try:
            watched.extend(parse_addresses(str(entry)))
        except ValueError:
            continue
    if not any(item.chain.key == chain.key and item.address == address for item in watched):
        raise _error(404, "history_context_unavailable", "Connected wallet not found")
    account = await session.scalar(select(Account).where(
        Account.workspace_id == workspace_id, Account.connection_id == connection.id,
        Account.external_id == ACCOUNT_EXTERNAL_ID,
    ))
    if account is None:
        raise _error(409, "history_account_unavailable", "Sync the connection to establish its account mapping first")
    from app.services.connection_service import _wallet_external_id
    group = await session.scalar(select(AssetGroup).where(
        AssetGroup.workspace_id == workspace_id, AssetGroup.connection_id == connection.id,
        AssetGroup.external_id == _wallet_external_id(connection.external_id, account.external_id),
    ).with_for_update(nowait=True))
    for supplied in request.supplied_accounts:
        if supplied.owner != address or not address_is_valid(chain, supplied.address) or supplied.address == address:
            raise _error(422, "history_invalid_account", "Historical token accounts require valid distinct addresses and the selected owner")
    return connection, account, group, address


async def load_history(session, workspace_id, collection_id):
    row = await session.scalar(select(InvestmentHistoryCollection).where(
        InvestmentHistoryCollection.id == collection_id, InvestmentHistoryCollection.workspace_id == workspace_id,
    ).execution_options(populate_existing=True))
    if row is None:
        raise _error(404, "history_unavailable", "Saved history is unavailable")
    return row


def history_read(row, evidence=None):
    evidence = evidence if evidence is not None else row.payload
    observations = project_observations(evidence, row.id)
    projection_gaps = {reason for item in observations for reason in item.reason_codes if reason.startswith("crosswalk_")}
    if row.group_id is None:
        projection_gaps.add("crosswalk_account_mapping_unavailable")
    evidence = {**evidence, "crosswalk_gaps": sorted(projection_gaps),
                "gaps": sorted(set(evidence.get("gaps", [])) | projection_gaps)}
    return HistoryRead(
        collection_id=row.id, revision=row.revision, request=row.request, evidence=evidence,
        observations=observations, group_id=row.group_id, updated_at=_db_time(row.updated_at),
    )


async def read_history(session, workspace_id, collection_id):
    row = await load_history(session, workspace_id, collection_id)
    peers = await _peer_archives(session, workspace_id, row.connection_id, row.request["address"], row.id)
    # revision binds the retained continuation state; current qualification also
    # considers later source conflicts, without mutating a read/export request.
    return history_read(row, qualify_archive(row.payload, peers))


async def list_history(session, workspace_id, connection_id=None):
    query = select(InvestmentHistoryCollection).where(InvestmentHistoryCollection.workspace_id == workspace_id)
    if connection_id:
        if await session.scalar(select(BankConnection.id).where(
            BankConnection.id == connection_id, BankConnection.workspace_id == workspace_id,
        )) is None:
            raise _error(404, "history_context_unavailable", "Connected wallet not found")
        query = query.where(InvestmentHistoryCollection.connection_id == connection_id)
    rows = (await session.scalars(query.order_by(InvestmentHistoryCollection.updated_at.desc()).limit(100))).all()
    connections = {}
    for row in rows:
        connections.setdefault(row.connection_id, []).append((row.id, row.payload))
    conflicts = {connection: _transaction_conflicts(archives) for connection, archives in connections.items()}
    return [HistorySummary(
        collection_id=row.id, revision=row.revision, request=row.request, updated_at=_db_time(row.updated_at),
        coverage={**row.payload.get("coverage", {}), **({"interpretation": "partial", "settlement": "partial"} if any(
            (row.payload.get("chain"), row.payload.get("owner"), key) in conflicts[row.connection_id]
            for key in row.payload.get("transactions", {})
        ) else {})},
        transaction_count=len(row.payload.get("transactions", {})),
    ) for row in rows]


async def collect_history(session, workspace_id, user_id, request: HistoryRequest):
    from app.providers.solana_history import collect_solana_history
    try:
        # Serialize quota reservation and current-version projection per workspace.
        # ponytail: workspace lock spans bounded RPC collection; move to reservations if contention matters.
        await session.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update(nowait=True))
        connection, account, group, address = await _owned_context(session, workspace_id, request)
        canonical = request.model_dump(mode="json", exclude={"collection_id", "expected_revision", "reobserve"})
        canonical.update(chain="solana", address=address)
        canonical["supplied_accounts"] = sorted(canonical["supplied_accounts"], key=lambda item: item["address"])
        row = await load_history(session, workspace_id, request.collection_id) if request.collection_id else None
        source_identity = "solana-rpc:" + hashlib.sha256(rpc_url(resolve_chain("solana")).encode()).hexdigest()
        if row:
            if row.request != canonical or row.connection_id != connection.id or row.group_id != (group.id if group else None) or row.payload["source_identity"] != source_identity or row.payload.get("ownership_assertion", {}).get("account_id") != str(account.id):
                raise _error(409, "history_restart_required", "Saved collection settings changed; start a new collection explicitly")
            if row.revision != request.expected_revision:
                raise _error(409, "history_revision_conflict", "Saved history changed; reopen before continuing")
        used = await session.scalar(select(func.coalesce(func.sum(InvestmentHistoryCollection.size_bytes), 0)).where(
            InvestmentHistoryCollection.workspace_id == workspace_id,
        ))
        count = await session.scalar(select(func.count()).select_from(InvestmentHistoryCollection).where(
            InvestmentHistoryCollection.workspace_id == workspace_id,
        ))
        if not row and count >= MAX_COLLECTIONS:
            raise _error(413, "history_storage_limit", "Workspace has reached its durable collection count limit")
        if used - (row.size_bytes if row else 0) + MAX_COLLECTION_BYTES > MAX_WORKSPACE_BYTES:
            raise _error(413, "history_storage_limit", "Workspace evidence storage cannot reserve another collection; export existing evidence")
        try:
            evidence = await collect_solana_history(
                address, source_identity=source_identity, since=request.since, until=request.until,
                supplied_accounts=canonical["supplied_accounts"], state=copy.deepcopy(row.payload) if row else None,
                reobserve=request.reobserve,
            )
        except ValueError as exc:
            raise _error(409, "history_restart_required", "Saved source, decoder, anchor or inventory changed; restart explicitly") from exc
        now = datetime.now(timezone.utc)
        evidence.setdefault("ownership_assertion", {
            "user_id": str(user_id), "asserted_at": now.isoformat(), "connection_id": str(connection.id),
            "account_id": str(account.id), "address": address, "assertion": "user_confirmed",
            "scope": "declared address only; historical token ownership requires source evidence",
        })
        peers = await _peer_archives(session, workspace_id, connection.id, address, row.id if row else None)
        evidence = qualify_archive(evidence, peers)
        evidence["storage"] = {
            "collection_limit_bytes": MAX_COLLECTION_BYTES, "workspace_limit_bytes": MAX_WORKSPACE_BYTES,
            "collection_count_limit": MAX_COLLECTIONS,
            "retention": "durable_until_workspace_deletion", "checkpoint_expiry_independent": True,
        }
        encoded = _json_bytes(evidence)
        if len(encoded) > MAX_COLLECTION_BYTES:
            raise _error(413, "history_storage_limit", "Collector exceeded its durable evidence byte limit")
        revision = hashlib.sha256(encoded).hexdigest()
        if row is None:
            row = InvestmentHistoryCollection(
                id=uuid.uuid4(), workspace_id=workspace_id, connection_id=connection.id,
                group_id=group.id if group else None, request=canonical,
            )
            session.add(row)
        row.payload, row.revision, row.size_bytes, row.updated_at = evidence, revision, len(encoded), now
        group_id = group.id if group else None
        projected = project_observations(evidence, row.id)
        current_ids = {crosswalk._identity(group_id, connection.id, item) for item in projected}
        saved = (await session.scalars(select(InvestmentObservation).where(
            InvestmentObservation.workspace_id == workspace_id, InvestmentObservation.connection_id == connection.id,
            InvestmentObservation.group_id == group_id,
        ))).all()
        transaction_ids = {transaction["signature"] for transaction in evidence.get("transactions", {}).values()}
        for old in saved:
            if old.payload.get("source") == "onchain_history" and old.payload.get("source_account_id") == f"solana:{address}" and old.payload.get("order_ref") in {f"solana:{sig}" for sig in transaction_ids}:
                old.is_current = old.identity_key in current_ids and any(
                    old.identity_key == crosswalk._identity(group_id, connection.id, item) and old.fingerprint == crosswalk._fingerprint(item)
                    for item in projected
                )
        retained, _ = await crosswalk._retain(session, workspace_id, group_id, connection.id, projected)
        for retained_row in retained.values():
            retained_row.is_current = True
        await session.commit()
        return history_read(row)
    except DBAPIError as exc:
        await session.rollback()
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise _error(409, "history_busy", "Another operation is using this workspace; retry after it finishes") from exc
        raise
    except Exception:
        await session.rollback()
        raise
