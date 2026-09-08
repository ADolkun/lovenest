"""Synthetic connected research, durable source reads and evidence-only review."""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.investment_evidence import InvestmentHistoryCollection
from app.models.workspace import Workspace
from app.providers import solana_history as solana
from app.schemas.investment_timeline import TimelineAccount, TimelineAsset, TimelineEvent, TimelineLeg, TimelineSource, TimelineTime
from app.schemas.onchain_history import HistoryRequest
from app.schemas.onchain_investigation import InvestigationRequest
from app.services import onchain_history as history
from app.services import onchain_investigation as research
from app.services import investment_evidence_service as evidence
from tests.test_investment_timeline import _financial_state
from tests.test_onchain_history_api import A, B, connected_context, rpc_fixture

STAMP = datetime(2025, 2, 3, 12, tzinfo=timezone.utc)


def event(identifier, minute, legs, *, mechanics=None):
    when = TimelineTime(event_at=STAMP + timedelta(minutes=minute), time_precision="second")
    source = TimelineSource(source_id="source-" + identifier, source="synthetic", provider="synthetic",
        source_kind="primary_activity", detail_url="/synthetic", time=when, provider_status="success", settlement_status="settled")
    return TimelineEvent(event_id=identifier, kind="swap" if mechanics else "transfer", status="settled", time=when,
        accounts=[TimelineAccount(group_id="synthetic-wallet")], sources=[source], mechanics=mechanics or [],
        assets=[TimelineAsset(canonical_asset_key=part.canonical_asset_key, identity_status="canonical") for part in legs], legs=legs)


def leg(identifier, asset, quantity, source, destination, direction="out", **kwargs):
    return TimelineLeg(key=identifier, leg_id=identifier, canonical_asset_key=asset, chain="solana", token_address="native" if asset == "SOL" else "synthetic-USDC",
        quantity=Decimal(quantity), source_address=source, destination_address=destination,
        source_owner=source, destination_owner=destination, direction=direction, classification="transfer",
        execution_status="success", settlement_status="settled", transaction_ref=identifier, **kwargs)


def journey():
    entries = [event("receipt", 0, [leg("receive-6", "SOL", "6", "exchange", "A", "in")]),
        event("swap", 1, [leg("spend-4", "SOL", "4", "A", "pool"), leg("receive-400", "USDC", "400", "pool", "A", "in")],
              mechanics=[{"kind": "swap", "legs": ["spend-4", "receive-400"], "amount_source": "executed_inner_transfers"}]),
        event("move", 2, [leg("move-300", "USDC", "300", "A", "B")]),
        event("payment", 3, [leg("pay-250", "USDC", "250", "B", "C")]),
        event("pool", 4, [leg("pooled-999", "USDC", "999", "C", "D")]),
        event("unrelated", 4, [leg("unrelated-9", "USDC", "9", "X", "Y")])]
    return {entry.event_id: entry for entry in entries}


def test_bidirectional_cross_asset_frontiers_keep_sources_and_unknown_allocation():
    events = journey()
    owned = {("solana", owner) for owner in ("exchange", "A", "B")}
    request = InvestigationRequest(event_id="receipt", leg_id="receive-6", max_hops=6)
    result, steps, frontier, gaps = research.walk_evidence(events, request, owned)
    assert {row.event_id for row in result} == {"receipt", "swap", "move", "payment", "pool"}
    assert {step["asset_key"] for step in steps} == {"SOL", "USDC"}
    assert all(step["attributed_quantity"] is None for step in steps)
    assert next(row for row in result if row.event_id == "swap").legs[1].quantity == 400
    assert any(item["address"] == "C" for item in frontier)
    assert any(item["code"] == "external_ownership_and_allocation_unknown" for item in gaps)
    backward = research.walk_evidence(events, InvestigationRequest(event_id="payment", leg_id="pay-250", direction="in", max_hops=6), owned)
    assert {row.event_id for row in backward[0]} >= {"receipt", "swap", "move", "payment"}
    assert all(row.basis.state == "unknown" for row in result)


def test_minimums_compare_only_canonical_asset_units_and_keep_window_ties():
    events = journey()
    request = InvestigationRequest(event_id="receipt", leg_id="receive-6", max_hops=6, minimums={"SOL": "3", "USDC": "275"})
    result = research.walk_evidence(events, request, {("solana", "A"), ("solana", "B")})
    assert {row.event_id for row in result[0]} == {"receipt", "swap", "move"}
    events["move"].time = events["swap"].time.model_copy()
    events["move"].sources[0].time = events["swap"].time.model_copy()
    result = research.walk_evidence(events, InvestigationRequest(event_id="swap", leg_id="receive-400", since=STAMP + timedelta(minutes=1), until=STAMP + timedelta(minutes=1)), {("solana", "A")})
    assert "move" in {row.event_id for row in result[0]}
    with pytest.raises(ValidationError):
        InvestigationRequest(event_id="swap", leg_id="receive-400", minimums={"USDC": 0.1})
    with pytest.raises(ValidationError):
        InvestigationRequest(event_id="swap", leg_id="receive-400", since="2025-01-01")


@pytest.mark.parametrize("missing", [None, "unknown", "failed"])
def test_missing_execution_never_follows_principal(missing):
    events = journey()
    events["receipt"].legs[0].execution_status = missing
    result = research.walk_evidence(events, InvestigationRequest(event_id="receipt", leg_id="receive-6"), set())
    assert len(result[0]) == 1
    assert not result[2]
    assert result[3][0]["code"] == "unsettled_or_unresolved_leg"


@pytest.mark.asyncio
async def test_collector_preview_continue_source_export_and_permissions(client, auth_headers, viewer_auth_headers, session, test_workspace, test_user, monkeypatch):
    connection, _, group = await connected_context(session, test_workspace.id, test_user.id)
    calls, _ = rpc_fixture(monkeypatch)
    saved = await history.collect_history(session, test_workspace.id, test_user.id, HistoryRequest(connection_id=connection.id, address=A, ownership_confirmed=True))
    before = await _financial_state(session)
    events = (await client.get("/api/assets/timeline", headers=auth_headers)).json()["events"]
    send = next(item for item in events if any(part["source_address"] == A and part["classification"] != "fee" for part in item["legs"]))
    selected = next(part for part in send["legs"] if part["classification"] != "fee")
    request = {"event_id": send["event_id"], "leg_id": selected["leg_id"], "direction": "out"}
    count = len(calls)
    preview = await client.post("/api/onchain/investigation/preview", headers=viewer_auth_headers, json=request)
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert len(calls) == count
    assert data["collection_id"] == str(saved.collection_id)
    chosen = next(item for item in data["frontier"] if item["address"] == B)
    continuation = {**data["request"], "collection_id": data["collection_id"], "expected_revision": data["revision"], "frontier_key": chosen["key"]}
    assert (await client.post("/api/onchain/investigation/continue", headers=viewer_auth_headers, json=continuation)).status_code == 403
    assert (await client.post("/api/onchain/investigation/continue", headers=auth_headers, json={**continuation, "frontier_key": "forged"})).status_code == 409
    assert len(calls) == count
    response = await client.post("/api/onchain/investigation/continue", headers=auth_headers, json=continuation)
    assert response.status_code == 200, response.text
    continued = response.json()
    assert continued["revision"] != data["revision"]
    research_calls = calls[count:]
    assert not any(method == "getTokenAccountsByOwner" for method, _ in research_calls)
    assert all(params[0] == B for method, params in research_calls if method == "getSignaturesForAddress")
    assert (await client.post("/api/onchain/investigation/continue", headers=auth_headers, json=continuation)).status_code == 409
    row = await session.get(InvestmentHistoryCollection, saved.collection_id)
    assert row.payload["inventory"].keys() == {A}
    assert len(row.payload["investigations"]) == 1
    for stored in row.payload["investigations"].values():
        assert stored["archive"]["inventory"][B]["kind"] == "research_endpoint"
    all_events, _ = research.research_events(row, (await research.timeline._project(session, test_workspace.id))[0])
    external = next(iter(all_events.values()))
    source = external.sources[0]
    source_read = await client.get(source.detail_url, headers=auth_headers, params={"event_id": external.event_id, "group_id": str(group.id)})
    assert source_read.status_code == 200, source_read.text
    assert json.loads(source_read.json()["raw_payload"]["json"])["response"]["result"]
    reads = len(calls)
    exported = await client.get(f"/api/onchain/history/{saved.collection_id}/export", headers=auth_headers)
    assert exported.status_code == 200 and len(calls) == reads
    assert json.loads(exported.content)["evidence"]["investigations"]
    assert await _financial_state(session) == before
    other = Workspace(id=uuid.uuid4(), name="Other synthetic", created_by_user_id=test_user.id)
    session.add(other)
    await session.commit()
    with pytest.raises(HTTPException) as error:
        await research.read_research_source(session, other.id, source.source_id)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_external_cancel_drains_and_preserves_successful_payloads(monkeypatch):
    calls = []
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def rpc(*args, **kwargs):
        method = kwargs["json_body"]["method"]
        calls.append(method)
        kwargs["budget"].consume()
        if method == "getSlot":
            return {"result": 5}
        if method == "getBlock":
            return {"result": {"blockhash": "synthetic-anchor"}}
        entered.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()
    monkeypatch.setattr(solana, "request_json", rpc)
    baseline = asyncio.all_tasks()
    task = asyncio.create_task(solana.collect_solana_history("observed-owner", source_identity="synthetic-source", research=True, research_address="selected-token-account"))
    await entered.wait()
    task.cancel()
    archive = await task
    assert stopped.is_set() and len(archive["payloads"]) == 2
    assert archive["gaps"] == ["cancelled"]
    assert archive["inventory"].keys() == {"selected-token-account"}
    assert archive["limits"]["attempts_used"] == 3
    assert not asyncio.all_tasks() - baseline


def bridge_observation(reference, role, group_chain, transaction_ref, source, destination, asset_key, other_asset, *, quantity="3", fee="0"):
    from app.schemas.investment_evidence import EvidenceLegInput, EvidenceObservationInput
    metadata = {"bridge_protocol": "SyntheticBridge", "bridge_message_id": "synthetic-message-A", "bridge_role": role,
                "bridge_source_chain": "solana", "bridge_destination_chain": "ethereum",
                "bridge_source_asset": asset_key if role == "send" else other_asset,
                "bridge_destination_asset": other_asset if role == "send" else asset_key}
    return EvidenceObservationInput(reference=reference, source="csv", provider="onchain", source_account_id=group_chain + ":synthetic-owner",
        source_local_id=reference, source_locator="synthetic/independent-execution/" + reference,
        event_at=STAMP, event_date=STAMP.date(), time_precision="second", provider_status="success", network_status="finalized", settlement_status="settled",
        legs=[EvidenceLegInput(key="bridge-" + role, chain=group_chain, token_address="native", transaction_ref=transaction_ref,
            leg_ref="bridge-" + role, direction="out" if role == "send" else "in", classification="bridge", quantity=quantity,
            source_address=source, destination_address=destination, source_owner=source, destination_owner=destination,
            fee=fee, fee_currency=asset_key, derivation=metadata)])


@pytest.mark.asyncio
async def test_imported_bridge_review_is_reachable_bidirectional_version_qualified_and_nonmutating(
    client, auth_headers, viewer_auth_headers, session, test_workspace, test_user, monkeypatch,
):
    connection, _, group = await connected_context(session, test_workspace.id, test_user.id)
    rpc_fixture(monkeypatch)
    saved = await history.collect_history(session, test_workspace.id, test_user.id, HistoryRequest(connection_id=connection.id, address=A, ownership_confirmed=True))
    sol = evidence._digest(["chain", "solana", "native", None])
    eth = evidence._digest(["chain", "ethereum", "native", None])
    send = bridge_observation("source-executed-record", "send", "solana", "synthetic-send", A, B, sol, eth)
    source_version = next(version for transaction, version in history.current_versions(saved.evidence) if transaction["signature"] == "synthetic-send")
    send.legs[0].leg_ref = next(part["key"] for part in source_version["legs"] if part["role"] == "principal")
    send.legs[0].fee = Decimal("0.02")
    receive = bridge_observation("destination-executed-record", "receive", "ethereum", "synthetic-receive-eth", "bridge-pool", "receiver", eth, sol, quantity="2.9")
    import_preview = await evidence.preview_evidence(session, test_workspace.id, group.id, [send, receive])
    imported = await client.post("/api/assets/import", headers=auth_headers, json={
        "mode": "evidence", "group_id": str(group.id), "expected_revision": import_preview.revision,
        "observations": [send.model_dump(mode="json"), receive.model_dump(mode="json")],
    })
    assert imported.status_code == 200, imported.text
    before = await _financial_state(session)
    events = (await client.get("/api/assets/timeline", headers=auth_headers)).json()["events"]
    source_event = next(event for event in events if any(leg["derivation"].get("bridge_role") == "send" for leg in event["legs"]))
    response = await client.get("/api/onchain/investigation/bridge-candidates", headers=auth_headers, params={"event_id": source_event["event_id"]})
    assert response.status_code == 200, response.text
    candidate = response.json()[0]
    assert candidate["status"] == "eligible", candidate
    assert candidate["source_summary"]["quantity"] == "3"
    assert candidate["destination_summary"]["quantity"] == "2.9"
    request = {key: candidate[key] for key in ("collection_id", "source_event_id", "source_leg_id", "destination_event_id", "destination_leg_id", "source_id", "destination_source_id")}
    request.update(expected_revision=candidate["revision"], reviewed=True)
    assert (await client.post("/api/onchain/investigation/bridge", headers=viewer_auth_headers, json=request)).status_code == 403
    approved = await client.post("/api/onchain/investigation/bridge", headers=auth_headers, json=request)
    assert approved.status_code == 200, approved.text
    for side, direction in (("source", "out"), ("destination", "in")):
        preview = await client.post("/api/onchain/investigation/preview", headers=auth_headers, json={
            "event_id": candidate[side + "_event_id"], "leg_id": candidate[side + "_leg_id"], "direction": direction})
        assert preview.status_code == 200, preview.text
        assert {candidate["source_event_id"], candidate["destination_event_id"]} <= {event["event_id"] for event in preview.json()["events"]}
    exported = await client.get(f"/api/onchain/history/{saved.collection_id}/export", headers=auth_headers)
    assert exported.status_code == 200
    assert exported.json()["evidence"]["bridge_reviews"][0]["status"] == "confirmed"
    from app.models.investment_evidence import InvestmentObservation
    destination = await session.get(InvestmentObservation, uuid.UUID(candidate["destination_source_id"]))
    destination.is_current = False
    await session.commit()
    stale = await client.get(f"/api/onchain/history/{saved.collection_id}/export", headers=auth_headers)
    assert stale.json()["evidence"]["bridge_reviews"][0]["status"] == "unresolved"
    assert await _financial_state(session) == before


@pytest.mark.parametrize("change,reason", [
    ("pending", "bridge_execution_or_settlement_unknown"), ("fee", "bridge_fee_unknown"),
    ("mapping", "bridge_mapping_conflict"), ("empty", "bridge_mapping_unknown"),
    ("duplicated", "bridge_duplicated_attestation"), ("conflict", "bridge_destination_conflict"),
])
def test_bridge_review_cannot_supply_missing_execution_or_mapping(change, reason):
    sol, eth = "SOL", "ETH"
    source = event("bridge-source", 0, [leg("send", sol, "3", "A", "pool", fee=Decimal(0), fee_currency=sol)])
    destination = event("bridge-destination", 1, [leg("receive", eth, "2.9", "otherpool", "B", "in", fee=Decimal(0), fee_currency=eth)])
    destination.legs[0].chain = "ethereum"
    for current, role in ((source, "send"), (destination, "receive")):
        current.legs[0].source_ids = [current.sources[0].source_id]
        current.sources[0].source_locator = "synthetic/" + role
        current.legs[0].derivation = {"bridge_protocol": "Synthetic", "bridge_message_id": "message-A", "bridge_role": role,
            "bridge_source_chain": "solana", "bridge_destination_chain": "ethereum", "bridge_source_asset": sol, "bridge_destination_asset": eth}
    row = SimpleNamespace(id=uuid.uuid4(), group_id="synthetic-wallet", revision="revision", payload={"chain": "solana", "owner": "A", "ownership_assertion": {"address": "A"}, "transactions": {"solana:send": {"signature": "send"}}})
    state = {"archives": {row.id: row}}
    events = {source.event_id: source, destination.event_id: destination}
    if change == "pending":
        destination.legs[0].settlement_status = "provisional"
    elif change == "fee":
        destination.legs[0].fee = None
    elif change == "mapping":
        destination.legs[0].derivation["bridge_destination_asset"] = "wrong-contract"
    elif change == "empty":
        source.legs[0].derivation["bridge_message_id"] = ""
    elif change == "duplicated":
        destination.sources[0].source_locator = source.sources[0].source_locator
    else:
        extra = destination.model_copy(deep=True)
        extra.event_id = "conflicting-destination"
        extra.legs[0].transaction_ref = "other-execution"
        events[extra.event_id] = extra
    candidate = research._bridge_candidates(state, events)[0]
    assert candidate["status"] == "unresolved"
    assert reason in candidate["reason_codes"]


@pytest.mark.asyncio
async def test_changed_solana_anchor_invalidates_interpretation_without_background_collection(monkeypatch):
    from tests.test_solana_history import RPC, collect, install, signature, transaction
    rpc = RPC(pages={("owner-A", None): [signature("synthetic-send")]}, payloads={"synthetic-send": transaction()})
    install(monkeypatch, rpc)
    archive = await collect()
    calls = []
    async def changed(*args, **kwargs):
        calls.append(kwargs["json_body"]["method"])
        return {"result": {"blockhash": "reorganized-anchor"}}
    monkeypatch.setattr(solana, "request_json", changed)
    refreshed = await collect(state=archive)
    assert calls == ["getBlock"]
    assert "anchor_changed_restart_required" in refreshed["gaps"]
    assert all(tx["canonical_version"] is None for tx in refreshed["transactions"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("sponsored", [True, False])
async def test_actual_solana_wrap_swap_outputs_cross_assets_inside_one_transaction(client, auth_headers, session, test_workspace, test_user, monkeypatch, sponsored):
    from tests.test_solana_history import RPC, TOKEN, balance, instruction, signature, transaction
    connection, account, _ = await connected_context(session, test_workspace.id, test_user.id)
    mint = solana.WSOL_MINT
    raw = solana._ROUTE_DISCRIMINATOR + b"synthetic-quoted-999999"
    value, encoded = int.from_bytes(raw, "big"), ""
    while value:
        value, remainder = divmod(value, 58)
        encoded = solana._B58[remainder] + encoded
    shift = int(sponsored)
    keys = (["sponsor"] if sponsored else []) + [A, "owned-wsol", "pool-wsol", "pool-usdc", "owned-usdc"]
    pre = ([1_000_000_000] if sponsored else []) + [6_000_000_000, 0, 2_000_000, 2_000_000, 2_000_000]
    post = ([970_000_000] if sponsored else []) + [1_998_000_000 if sponsored else 1_968_000_000, 2_000_000, 4_002_000_000, 2_000_000, 2_000_000]
    payload = transaction([
        instruction(solana.SYSTEM_PROGRAM, "createAccount", source=A, newAccount="owned-wsol", lamports=4_002_000_000),
        instruction(TOKEN, "initializeAccount3", account="owned-wsol", owner=A, mint=mint),
        instruction(TOKEN, "syncNative", account="owned-wsol"), {"programId": solana.JUPITER_PROGRAM, "data": encoded},
    ], keys=keys, pre=pre, post=post,
        tokens_pre=[balance(2 + shift, 0, owner="pool", mint=mint, decimals=9), balance(3 + shift, 500_000_000, owner="pool", mint="synthetic-usdc"), balance(4 + shift, 0, owner=A, mint="synthetic-usdc")],
        tokens_post=[balance(1 + shift, 0, owner=A, mint=mint, decimals=9), balance(2 + shift, 4_000_000_000, owner="pool", mint=mint, decimals=9),
                     balance(3 + shift, 100_000_000, owner="pool", mint="synthetic-usdc"), balance(4 + shift, 400_000_000, owner=A, mint="synthetic-usdc")])
    payload["meta"]["innerInstructions"] = [{"index": 3, "instructions": [
        instruction(TOKEN, "transferChecked", source="owned-wsol", destination="pool-wsol", mint=mint, tokenAmount={"amount": "4000000000", "decimals": 9}),
        instruction(TOKEN, "transferChecked", source="pool-usdc", destination="owned-usdc", mint="synthetic-usdc", tokenAmount={"amount": "400000000", "decimals": 6}),
    ]}]
    rpc = RPC(pages={(A, None): [signature("synthetic-atomic-swap")]}, payloads={"synthetic-atomic-swap": payload})
    monkeypatch.setattr(solana, "request_json", rpc)
    saved = await history.collect_history(session, test_workspace.id, test_user.id, HistoryRequest(connection_id=connection.id, address=A, ownership_confirmed=True))
    version = next(history.current_versions(saved.evidence))[1]
    assert version["gaps"] == []
    assert {relationship["kind"] for relationship in version["relationships"]} == {"wrap", "swap"}
    timeline_events = (await client.get("/api/assets/timeline", headers=auth_headers)).json()["events"]
    current = timeline_events[0]
    native = next(item for item in current["legs"] if item["key"].endswith("wrap_native"))
    preview = await client.post("/api/onchain/investigation/preview", headers=auth_headers, json={"event_id": current["event_id"], "leg_id": native["leg_id"], "max_hops": 6})
    assert preview.status_code == 200, preview.text
    steps = preview.json()["steps"]
    assert len({step["asset_key"] for step in steps}) == 3
    actual_output = next(item for item in current["legs"] if item["destination_address"] == "owned-usdc")
    assert Decimal(actual_output["quantity"]) == 400
    reverse = await client.post("/api/onchain/investigation/preview", headers=auth_headers, json={"event_id": current["event_id"], "leg_id": actual_output["leg_id"], "direction": "in", "max_hops": 6})
    assert len({step["asset_key"] for step in reverse.json()["steps"]}) == 3
    equation = next(row for row in saved.evidence["reconciliation"] if row["account"] == A)
    assert Decimal(equation["settled_change"]) == Decimal("-4.002" if sponsored else "-4.032")
    assert Decimal(equation["discrepancy"]) == 0
    await session.refresh(account)
    assert account.balance == Decimal("19.25")


@pytest.mark.asyncio
async def test_research_workspace_quota_and_changed_binding_fail_before_rpc(client, auth_headers, session, test_workspace, test_user, monkeypatch):
    connection, _, _ = await connected_context(session, test_workspace.id, test_user.id)
    calls, _ = rpc_fixture(monkeypatch)
    saved = await history.collect_history(session, test_workspace.id, test_user.id, HistoryRequest(connection_id=connection.id, address=A, ownership_confirmed=True))
    events = (await research.timeline._project(session, test_workspace.id))[1]
    current = next(item for item in events.values() if any(part.source_address == A and part.classification != "fee" for part in item.legs))
    selected = next(part for part in current.legs if part.classification != "fee")
    preview = await research.preview_investigation(session, test_workspace.id, InvestigationRequest(event_id=current.event_id, leg_id=selected.leg_id))
    data = {**preview.request.model_dump(mode="json"), "collection_id": str(saved.collection_id), "expected_revision": preview.revision, "frontier_key": preview.frontier[0]["key"]}
    row = await history.load_history(session, test_workspace.id, saved.collection_id)
    count = len(calls)
    monkeypatch.setattr(history, "MAX_WORKSPACE_BYTES", row.size_bytes + 1000)
    response = await client.post("/api/onchain/investigation/continue", headers=auth_headers, json=data)
    assert response.status_code == 413 and len(calls) == count
    monkeypatch.setattr(history, "MAX_WORKSPACE_BYTES", 128 * 1024 * 1024)
    monkeypatch.setattr(history, "rpc_url", lambda _: "https://synthetic.invalid/changed-secret")
    response = await client.post("/api/onchain/investigation/continue", headers=auth_headers, json=data)
    assert response.status_code == 409 and len(calls) == count
    assert "changed-secret" not in response.text
    assert (await client.post("/api/onchain/investigation/continue", headers=auth_headers, json={**data, "collection_id": str(uuid.uuid4())})).status_code == 404


@pytest.mark.asyncio
async def test_shared_attempt_and_byte_limits_keep_completed_siblings(monkeypatch):
    from app.providers.onchain_transport import RequestBudget
    from tests.test_solana_history import RPC, install, signature, transaction
    rpc = RPC(pages={("selected-external", None): [signature("oversized-payload")]}, payloads={"oversized-payload": {**transaction(), "large": "x" * 90000}})
    install(monkeypatch, rpc)
    budget = RequestBudget(max_attempts=2)
    partial = await solana.collect_solana_history("selected-external", source_identity="synthetic", research=True, budget=budget)
    assert budget.attempts == 2 and len(partial["payloads"]) == 2
    assert "request_limit" in partial["gaps"]
    limited = await solana.collect_solana_history("selected-external", source_identity="synthetic", research=True, byte_limit=80000)
    assert len(limited["payloads"]) == 3
    assert limited["unavailable_payloads"][0]["reason"] == "payload_byte_limit"
    assert limited["streams"]["selected-external"]["pending"]
    assert limited["transactions"]["solana:oversized-payload"]["versions"] == []
    rpc.payloads["oversized-payload"].pop("large")
    resumed = await solana.collect_solana_history("selected-external", source_identity="synthetic", research=True, byte_limit=80000, state=limited)
    assert len(resumed["transactions"]["solana:oversized-payload"]["versions"]) == 1


def test_chain_and_internal_execution_fingerprints_are_independently_qualified():
    def archive(chain, trace=None):
        version = {"version_id": "version", "payload_digest": "payload", "evidence_fingerprint": "same-body"}
        if trace:
            version["internal_evidence_fingerprint"] = trace
        return {"chain": chain, "owner": "same-address", "transactions": {chain + ":same-reference": {"signature": "same-reference", "versions": [version]}}, "payloads": {"payload": {"response": {"result": {"tx": "same-reference"}}}}}
    assert not history._transaction_conflicts([(1, archive("ethereum")), (2, archive("base", "trace-A"))])
    assert not history._transaction_conflicts([(1, archive("ethereum")), (2, archive("ethereum", "trace-A"))])
    assert history._transaction_conflicts([(1, archive("ethereum", "trace-B")), (2, archive("ethereum", "trace-A"))])


@pytest.mark.asyncio
async def test_postgres_two_external_resumes_converge_once(postgres_sessions, monkeypatch):
    from fastapi import HTTPException
    from app.models.user import User
    from app.schemas.onchain_investigation import InvestigationContinue
    async with postgres_sessions() as session:
        user = User(email="synthetic-investigation@example.invalid", hashed_password="synthetic-unused")
        session.add(user)
        await session.flush()
        workspace = Workspace(name="Synthetic research", created_by_user_id=user.id)
        session.add(workspace)
        await session.flush()
        connection, _, _ = await connected_context(session, workspace.id, user.id)
        workspace_id, user_id = workspace.id, user.id
        rpc_fixture(monkeypatch)
        saved = await history.collect_history(session, workspace_id, user_id, HistoryRequest(connection_id=connection.id, address=A, ownership_confirmed=True))
        events = (await research.timeline._project(session, workspace_id))[1]
        current = next(item for item in events.values() if any(part.source_address == A and part.classification != "fee" for part in item.legs))
        selected = next(part for part in current.legs if part.classification != "fee")
        preview = await research.preview_investigation(session, workspace_id, InvestigationRequest(event_id=current.event_id, leg_id=selected.leg_id))
        request = InvestigationContinue(**preview.request.model_dump(), collection_id=saved.collection_id, expected_revision=saved.revision, frontier_key=preview.frontier[0]["key"])
    async def resume():
        async with postgres_sessions() as session:
            try:
                return await research.continue_investigation(session, workspace_id, request)
            except HTTPException as exc:
                return exc.status_code
    results = await asyncio.gather(resume(), resume())
    assert sum(item == 409 for item in results) == 1
    async with postgres_sessions() as session:
        row = await session.get(InvestmentHistoryCollection, saved.collection_id)
        assert len(row.payload["investigations"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("acquisition_known", [(True, True), (False, True), (True, False), (False, False)], ids=["known-sponsored", "missing-sponsored", "known-owned", "missing-owned"])
async def test_exchange_acquisition_conversion_owned_move_and_external_payment_api(client, auth_headers, session, test_workspace, test_user, monkeypatch, acquisition_known):
    acquisition_known, sponsored = acquisition_known if isinstance(acquisition_known, tuple) else (acquisition_known, True)
    from app.models.asset_group import AssetGroup
    from app.schemas.investment_evidence import EvidenceLegInput, EvidenceObservationInput
    from tests.test_solana_history import RPC, TOKEN, balance, instruction, signature, transaction
    connection, _, group = await connected_context(session, test_workspace.id, test_user.id)
    connection.credentials = {"addresses": ["solana:" + A, "solana:" + B]}
    exchange = AssetGroup(workspace_id=test_workspace.id, user_id=test_user.id, name="Synthetic Exchange")
    session.add(exchange)
    await session.commit()
    stamp = 1800000000
    raw, encoded = int.from_bytes(solana._ROUTE_DISCRIMINATOR + b"quote-999999", "big"), ""
    while raw:
        raw, digit = divmod(raw, 58)
        encoded = solana._B58[digit] + encoded
    receipt = transaction([instruction(solana.SYSTEM_PROGRAM, "transfer", source="exchange", destination=A, lamports=6_000_000_000)],
        keys=("sponsor", "exchange", A), pre=(1_000_000_000, 10_000_000_000, 0), post=(990_000_000, 4_000_000_000, 6_000_000_000), fee=10_000_000)
    swap = transaction([{"programId": solana.JUPITER_PROGRAM, "data": encoded}], keys=("sponsor", A, "pool", "pool-usdc", "token-A"),
        pre=(990_000_000, 6_000_000_000, 0, 0, 0), post=(980_000_000, 2_000_000_000, 4_000_000_000, 0, 0), fee=10_000_000,
        tokens_pre=[balance(3, 500_000_000, owner="pool", mint="synthetic-usdc"), balance(4, 0, owner=A, mint="synthetic-usdc")],
        tokens_post=[balance(3, 100_000_000, owner="pool", mint="synthetic-usdc"), balance(4, 400_000_000, owner=A, mint="synthetic-usdc")])
    swap["meta"]["innerInstructions"] = [{"index": 0, "instructions": [
        instruction(solana.SYSTEM_PROGRAM, "transfer", source=A, destination="pool", lamports=4_000_000_000),
        instruction(TOKEN, "transferChecked", source="pool-usdc", destination="token-A", mint="synthetic-usdc", tokenAmount={"amount": "400000000", "decimals": 6})]}]
    moved = transaction([instruction(TOKEN, "transferChecked", source="token-A", destination="token-B", mint="synthetic-usdc", tokenAmount={"amount": "300000000", "decimals": 6})],
        keys=("sponsor", "token-A", "token-B"), pre=(980_000_000, 0, 0), post=(970_000_000, 0, 0), fee=10_000_000,
        tokens_pre=[balance(1, 400_000_000, owner=A, mint="synthetic-usdc"), balance(2, 0, owner=B, mint="synthetic-usdc")],
        tokens_post=[balance(1, 100_000_000, owner=A, mint="synthetic-usdc"), balance(2, 300_000_000, owner=B, mint="synthetic-usdc")])
    paid = transaction([instruction(TOKEN, "transferChecked", source="token-B", destination="token-C", mint="synthetic-usdc", tokenAmount={"amount": "250000000", "decimals": 6})],
        keys=("sponsor", "token-B", "token-C"), pre=(970_000_000, 0, 0), post=(960_000_000, 0, 0), fee=10_000_000,
        tokens_pre=[balance(1, 300_000_000, owner=B, mint="synthetic-usdc"), balance(2, 0, owner="external-C", mint="synthetic-usdc")],
        tokens_post=[balance(1, 50_000_000, owner=B, mint="synthetic-usdc"), balance(2, 250_000_000, owner="external-C", mint="synthetic-usdc")])
    payloads = {"receipt-6": receipt, "swap-4-400": swap, "move-300": moved, "pay-250": paid}
    if not sponsored:
        for payload in (receipt, swap):
            keys = payload["transaction"]["message"]["accountKeys"]
            keys[0], keys[1] = keys[1], keys[0]
            for field in ("preBalances", "postBalances"):
                values = payload["meta"][field]
                values[0], values[1] = values[1], values[0]
            payload["meta"]["postBalances"][0] -= 10_000_000
            payload["meta"]["postBalances"][1] += 10_000_000
        for payload, owner, opening in ((moved, A, 1_990_000_000), (paid, B, 100_000_000)):
            payload["transaction"]["message"]["accountKeys"][0] = owner
            payload["meta"]["preBalances"][0], payload["meta"]["postBalances"][0] = opening, opening - 10_000_000
    for index, payload in enumerate(payloads.values()):
        payload.update(slot=10 + index, blockTime=stamp + index * 60)
    pages = {(A, None): [signature("swap-4-400", stamp=stamp + 60), signature("receipt-6", stamp=stamp)],
             ("token-A", None): [signature("move-300", stamp=stamp + 120)],
             (B, None): [signature("pay-250", stamp=stamp + 180), signature("move-300", stamp=stamp + 120)]}
    rpc = RPC(pages=pages, payloads=payloads)
    monkeypatch.setattr(solana, "request_json", rpc)
    archives = {}
    for owner in (A, B):
        result = await client.post("/api/onchain/history", headers=auth_headers, json={"connection_id": str(connection.id), "address": owner, "chain": "solana", "ownership_confirmed": True})
        assert result.status_code == 200, result.text
        archives[owner] = result.json()["evidence"]
    native = next(row for row in archives[A]["reconciliation"] if row["account"] == A and row["asset"]["native"])
    assert Decimal(native["closing"]) == Decimal("2" if sponsored else "1.98")
    assert Decimal(native["discrepancy"]) == 0
    for owner, token, closing in ((A, "token-A", "100"), (B, "token-B", "50")):
        equation = next(row for row in archives[owner]["reconciliation"] if row["account"] == token and not row["asset"]["native"])
        assert Decimal(equation["closing"]) == Decimal(closing) and Decimal(equation["discrepancy"]) == 0
    if not sponsored:
        native_b = next(row for row in archives[B]["reconciliation"] if row["account"] == B and row["asset"]["native"])
        assert Decimal(native_b["closing"]) == Decimal("0.09") and Decimal(native_b["discrepancy"]) == 0
    acquisition = EvidenceObservationInput(reference="exchange-buy-10", source="csv", provider="coinbase", source_account_id="exchange-account", source_local_id="buy-10", source_locator="synthetic/exchange/buy-10",
        event_at=datetime.fromtimestamp(stamp - 60, timezone.utc), time_precision="second", provider_status="completed", settlement_status="settled",
        legs=[EvidenceLegInput(key="buy-10", chain="solana", token_address="native", classification="buy", direction="in", quantity="10", acquisition_basis="200")])
    withdrawal = acquisition.model_copy(deep=True)
    withdrawal.reference, withdrawal.source_local_id, withdrawal.source_locator = "exchange-withdraw-6", "withdraw-6", "synthetic/exchange/withdraw-6"
    withdrawal.event_at = datetime.fromtimestamp(stamp, timezone.utc)
    withdrawal.legs = [EvidenceLegInput(key="withdraw-6", chain="solana", token_address="native", classification="transfer", direction="out", quantity="6", transaction_ref="receipt-6", source_address="exchange", destination_address=A)]
    if not acquisition_known:
        withdrawal.legs[0].transaction_ref = None
    observations = [acquisition, withdrawal] if acquisition_known else [withdrawal]
    preview = await evidence.preview_evidence(session, test_workspace.id, exchange.id, observations)
    imported = await client.post("/api/assets/import", headers=auth_headers, json={"mode": "evidence", "group_id": str(exchange.id), "expected_revision": preview.revision, "observations": [item.model_dump(mode="json") for item in observations]})
    assert imported.status_code == 200, imported.text
    before = await _financial_state(session)
    events = (await client.get("/api/assets/timeline", headers=auth_headers)).json()["events"]
    bought = next(item for item in events if any(part["key"] == ("buy-10" if acquisition_known else "withdraw-6") for part in item["legs"]))
    payment = next(item for item in events if any(part["transaction_ref"] == "pay-250" and part["classification"] != "fee" for part in item["legs"]))
    for root, selected, direction in ((bought, bought["legs"][0], "out"), (payment, next(part for part in payment["legs"] if part["classification"] != "fee"), "in")):
        response = await client.post("/api/onchain/investigation/preview", headers=auth_headers, json={"event_id": root["event_id"], "leg_id": selected["leg_id"], "direction": direction, "max_hops": 6, "max_branches": 5})
        assert response.status_code == 200, response.text
        rows = response.json()["events"]
        assert response.json()["collection_id"] is not None
        quantities = {Decimal(part["quantity"]) for row in rows for part in row["legs"] if part["quantity"] and part["classification"] != "fee"}
        expected = {Decimal(6), Decimal(4), Decimal(400), Decimal(300), Decimal(250)}
        if acquisition_known:
            expected.add(Decimal(10))
        assert expected <= quantities
        assert all(row["sources"] for row in rows)
        assert all(row["tax_treatment"] == "unresolved" for row in rows)
        assert all(row["basis"]["state"] == "unknown" for row in rows if not acquisition_known or row["event_id"] != bought["event_id"])
        if direction == "out":
            data = response.json()
            frontier = next(item for item in data["frontier"] if item["address"] == "token-C")
            continued = await client.post("/api/onchain/investigation/continue", headers=auth_headers, json={
                **data["request"], "collection_id": data["collection_id"], "expected_revision": data["revision"], "frontier_key": frontier["key"]})
            assert continued.status_code == 200, continued.text
            assert continued.json()["revision"] != data["revision"]
    assert await _financial_state(session) == before


@pytest.mark.asyncio
async def test_bridge_candidate_limit_applies_after_selected_event_filter(monkeypatch):
    from unittest.mock import AsyncMock
    events = {}
    for index in range(101):
        item = event(f"bridge-{index}", index, [leg(f"leg-{index}", "SOL", "1", "A", "pool", derivation={"bridge_role": "send"})])
        events[item.event_id] = item
    monkeypatch.setattr(research.timeline, "_project", AsyncMock(return_value=({"archives": {}}, events, {}, {}, [], [], {}, "revision")))
    candidates = await research.bridge_candidates(None, uuid.uuid4(), "bridge-100")
    assert len(candidates) == 1 and candidates[0]["source_event_id"] == "bridge-100"


@pytest.mark.parametrize("field,value", [("quantity", Decimal("900")), ("settlement_status", "provisional"),
    ("destination_address", "different-recipient"), ("fee", Decimal("7"))])
def test_same_bridge_destination_identity_conflicting_facts_never_choose_first(field, value):
    source = event("bridge-source", 0, [leg("send", "SOL", "3", "A", "pool", fee=Decimal(0), fee_currency="SOL")])
    destination = event("bridge-destination", 1, [leg("receive", "ETH", "2.9", "otherpool", "B", "in", fee=Decimal(0), fee_currency="ETH")])
    destination.legs[0].chain = "ethereum"
    for current, role in ((source, "send"), (destination, "receive")):
        current.legs[0].source_ids = [current.sources[0].source_id]
        current.legs[0].derivation = {"bridge_protocol": "Synthetic", "bridge_message_id": "message-A", "bridge_role": role,
            "bridge_source_chain": "solana", "bridge_destination_chain": "ethereum", "bridge_source_asset": "SOL", "bridge_destination_asset": "ETH"}
    conflict = destination.model_copy(deep=True)
    conflict.event_id = "conflicting-destination-observation"
    conflict.legs[0].leg_id = "conflicting-leg-storage-id"
    setattr(conflict.legs[0], field, value)
    for receivers in ((destination, conflict), (conflict, destination)):
        events = {item.event_id: item for item in (source, *receivers)}
        result = research._bridge_candidates({"archives": {}}, events)[0]
        assert result["status"] == "unresolved"
        assert "bridge_destination_fact_conflict" in result["reason_codes"]


def test_converging_paths_keep_overlapping_effective_window_provenance():
    rows = [event("root", 0, [leg("root-receipt", "SOL", "10", "origin", "A", "in")]),
            event("branch-B", 1, [leg("to-B", "SOL", "4", "A", "B")]),
            event("branch-C", 2, [leg("to-C", "SOL", "6", "A", "C")]),
            event("merge-B", 3, [leg("B-to-D", "SOL", "4", "B", "D")]),
            event("merge-C", 4, [leg("C-to-D", "SOL", "6", "C", "D")]),
            event("common", 5, [leg("common-payment", "SOL", "9", "D", "external")])]
    result = research.walk_evidence({row.event_id: row for row in rows}, InvestigationRequest(event_id="root", leg_id="root-receipt", max_hops=6, until=STAMP + timedelta(minutes=6)), {("solana", owner) for owner in ("A", "B", "C", "D")})
    common = [step for step in result[1] if step["leg_id"] == "common-payment"]
    assert {step["effective_window"]["since"] for step in common} == {(STAMP + timedelta(minutes=minute)).isoformat() for minute in (3, 4)}
    assert all(step["effective_window"]["until"] == (STAMP + timedelta(minutes=6)).isoformat() for step in common)
    assert all(step["attributed_quantity"] is None for step in common)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["plain", "fee", "hook", "confidential"])
async def test_token_2022_archive_timeline_and_continuation_boundary(client, auth_headers, session, test_workspace, test_user, monkeypatch, kind):
    from tests.test_solana_history import RPC, TOKEN_2022, balance, instruction, signature, transaction
    connection, _, _ = await connected_context(session, test_workspace.id, test_user.id)
    credited = 9_900_000 if kind == "fee" else 10_000_000
    transfer = instruction(TOKEN_2022, "transferCheckedWithFee" if kind == "fee" else "transferChecked", source="token-A", destination="token-B", mint="synthetic-t2022",
                           tokenAmount={"amount": "10000000", "decimals": 6})
    if kind == "fee":
        transfer["parsed"]["info"]["feeAmount"] = {"amount": "100000", "decimals": 6}
    instructions = [transfer]
    if kind == "hook":
        instructions.append(instruction(TOKEN_2022, "transferHook", source="token-A", destination="token-B"))
    if kind == "confidential":
        instructions = [instruction(TOKEN_2022, "confidentialTransfer", source="token-A", destination="token-B")]
    payload = transaction(instructions, keys=(A, "token-A", "token-B"), pre=(1, 0, 0), post=(1, 0, 0), fee=0,
        tokens_pre=[balance(1, 10_000_000, owner=A, mint="synthetic-t2022", program=TOKEN_2022), balance(2, 0, owner="external", mint="synthetic-t2022", program=TOKEN_2022)],
        tokens_post=[balance(1, 0, owner=A, mint="synthetic-t2022", program=TOKEN_2022), balance(2, credited, owner="external", mint="synthetic-t2022", program=TOKEN_2022)])
    monkeypatch.setattr(solana, "request_json", RPC(pages={(A, None): [signature("t2022")]}, payloads={"t2022": payload}))
    saved = await client.post("/api/onchain/history", headers=auth_headers, json={"connection_id": str(connection.id), "address": A, "ownership_confirmed": True})
    assert saved.status_code == 200, saved.text
    current = (await client.get("/api/assets/timeline", headers=auth_headers)).json()["events"][0]
    principal = next((part for part in current["legs"] if part["quantity_role"] == "principal"), None)
    selected = principal or current["legs"][0]
    preview = await client.post("/api/onchain/investigation/preview", headers=auth_headers, json={"event_id": current["event_id"], "leg_id": selected["leg_id"]})
    assert preview.status_code == 200, preview.text
    if kind in {"plain", "fee"}:
        assert principal is not None
        assert Decimal(principal["quantity"]) == Decimal("9.9" if kind == "fee" else "10")
        assert preview.json()["frontier"]
        if kind == "fee":
            assert (principal["sender_debit_raw_units"], principal["receiver_credit_raw_units"], principal["withheld_fee_raw_units"]) == ("10000000", "9900000", "100000")
            assert any(part["quantity_role"] == "token_transfer_fee" and Decimal(part["quantity"]) == Decimal("0.1") for part in current["legs"])
    else:
        assert not preview.json()["frontier"]
        assert principal is None or principal["quantity"] is None and principal["interpretation"] == "unresolved"
        assert "unsupported_token_extension_or_instruction" in current["reason_codes"]


def test_indexed_known_path_fields_conflict_only_on_overlapping_facts():
    def archive(owner, fields):
        return {"chain": "ethereum", "owner": owner, "transactions": {"ethereum:tx": {"signature": "tx", "versions": [{"version_id": "v", "payload_digest": "p", "evidence_fingerprint": "body", "indexed_internal_fingerprints": fields}]}}, "payloads": {"p": {"response": {"result": "same"}}}}
    assert not history._transaction_conflicts([(1, archive("A", {"trace:0/value": "five"})), (2, archive("B", {"trace:0/value": "five", "trace:0/outcome": "success", "trace:1/value": "seven"}))])
    conflicts = history._transaction_conflicts([(1, archive("A", {"trace:0/value": "five"})), (2, archive("B", {"trace:0/value": "999"}))])
    assert ("ethereum", "A", "ethereum:tx") in conflicts and ("ethereum", "B", "ethereum:tx") in conflicts


@pytest.mark.asyncio
async def test_nested_external_conflict_qualifies_event_source_and_export(client, auth_headers, session, test_workspace, test_user, monkeypatch):
    import copy
    from tests import test_evm_history as evm
    connection, _, group = await connected_context(session, test_workspace.id, test_user.id)
    connection.credentials = {"addresses": ["ethereum:" + evm.OWNER]}
    await session.commit()
    rpc = evm.RPC({evm.TX: evm.bundle(logs=[])})
    evm.install(monkeypatch, rpc)
    response = await client.post("/api/onchain/history", headers=auth_headers, json={"connection_id": str(connection.id), "chain": "ethereum", "address": evm.OWNER, "ownership_confirmed": True})
    assert response.status_code == 200, response.text
    row = await history.load_history(session, test_workspace.id, uuid.UUID(response.json()["collection_id"]))
    rpc.payloads[evm.TX] = evm.bundle(logs=[], amount=2)
    conflicting = await evm.history.collect_evm_history(evm.OTHER, chain="ethereum", source_identity=evm.history.evm_source_identity("ethereum"), research=True)
    assert evm.current(conflicting)["settlement"] == "settled"
    original = copy.deepcopy(row.payload)
    original["investigations"] = {"selected-external": {"archive": conflicting, "frontier": {"chain": "ethereum"}}}
    row.payload, row.size_bytes = original, len(history._json_bytes(original))
    await session.commit()
    before = await _financial_state(session)
    state = (await research.timeline._project(session, test_workspace.id))[0]
    external, _ = research.research_events(row, state)
    selected = next(iter(external.values()))
    assert selected.status == "conflicting"
    assert all(part.settlement_status != "settled" for part in selected.legs)
    source = selected.sources[0]
    opened = await client.get(source.detail_url, headers=auth_headers, params={"event_id": selected.event_id, "group_id": str(group.id)})
    assert opened.status_code == 200, opened.text
    assert opened.json()["source"]["is_current"] is False
    exported = await client.get(f"/api/onchain/history/{row.id}/export", headers=auth_headers)
    assert exported.status_code == 200, exported.text
    qualified = exported.json()["evidence"]
    assert qualified["transactions"]["ethereum:" + evm.TX]["canonical_version"] is None
    assert qualified["investigations"]["selected-external"]["archive"]["transactions"]["ethereum:" + evm.TX]["canonical_version"] is None
    assert qualified["investigations"]["selected-external"]["archive"]["payloads"]
    assert await _financial_state(session) == before
