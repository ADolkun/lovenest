"""Synthetic producer -> activity/API checks; no live financial or chain reads."""
import copy
import json
import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import event as sqlalchemy_event, select

from app.models.asset import Asset
from app.models.asset_transaction import AssetTransaction
from app.models.collection import Collection
from app.models.investment_evidence import InvestmentHistoryCollection, InvestmentLeg, InvestmentObservation
from app.schemas.investment_evidence import EvidenceAllocation, EvidenceDecision, EvidenceLegInput, EvidenceObservationInput
from app.services import investment_evidence_service as evidence
from app.services import investment_timeline_service as timeline
from tests import test_owned_transfers_integration as transfer_producer
from tests.test_investment_evidence import observation, save, wallet as wallet_fixture
from tests.test_onchain_history_api import A, connected_context, rpc_fixture
from tests.test_owned_transfer_regressions import retain_revision
from tests.test_owned_transfers_integration import (
    add_acquisition, apply_movement, checked, confirm_transfer, pair, retain_movement,
    select_lots, transfer_preview, transfers as transfers_fixture,
)
from tests.test_recovery_evidence_integration import package, receipt_journey, review
from tests.test_solana_history import RPC, TOKEN, TOKEN_2022, balance, instruction, signature, transaction

PREFIX = "/api/assets/timeline"
wallet = wallet_fixture
transfers = transfers_fixture


async def _financial_state(session):
    from app.models.account import Account
    from app.models.asset_value import AssetValue
    from app.models.owned_transfer import InvestmentMovementApplication, InvestmentOwnedTransfer
    result = {}
    for model in (Account, Asset, AssetTransaction, AssetValue, InvestmentMovementApplication, InvestmentOwnedTransfer):
        rows = (await session.scalars(select(model))).all()
        result[model.__tablename__] = [tuple(str(getattr(row, column.name)) for column in model.__table__.columns)
                                      for row in sorted(rows, key=lambda row: str(row.id))]
    return result


@pytest.mark.asyncio
async def test_candidates_remain_distinct_reviewed_sources_group_and_read_is_nonmutating(
    client, auth_headers, session, test_workspace, test_user, wallet,
):
    first = observation("source-A", source="coinbase_api")
    second = observation("source-B", source="csv")
    retained = await save(session, test_workspace, test_user, wallet, [first, second])
    before = await _financial_state(session)
    response = await client.get(PREFIX, headers=auth_headers)
    assert response.status_code == 200, response.text
    events = response.json()["events"]
    assert len(events) == 2
    assert all(event["linkage"] == "candidate" for event in events)
    assert all(event["basis"]["state"] == "unknown" for event in events)
    candidate = retained.evidence.records[0]
    await evidence.confirm_evidence(session, test_workspace.id, test_user.id, wallet.id, [EvidenceDecision(
        observation_ref=candidate.observation_ref, leg_key=candidate.leg_key, action="link",
        allocations=[EvidenceAllocation(leg_id=candidate.candidate_legs[0].leg_id, quantity="12")],
        reason="Synthetic documented source relationship",
    )], retained.evidence.revision)
    response = await client.get(PREFIX, headers=auth_headers)
    assert response.status_code == 200, response.text
    event = response.json()["events"][0]
    assert len(response.json()["events"]) == 1
    assert event["linkage"] == "confirmed"
    assert len(event["sources"]) == 2 and len(event["legs"]) == 1
    assert event["legs"][0]["quantity"] == "12"
    assert any(link["quantity"] == "12" for link in event["relationships"])
    detail = await client.get(f"{PREFIX}/{event['event_id']}", headers=auth_headers)
    assert detail.status_code == 200
    source = await client.get(event["sources"][0]["detail_url"], headers=auth_headers)
    assert source.status_code == 200 and source.json()["observation"]["legs"][0]["quantity"] == "12"
    assert response.headers["cache-control"] == source.headers["cache-control"] == "no-store"
    assert await _financial_state(session) == before


@pytest.mark.asyncio
async def test_collector_timeline_retains_archive_mechanics_fees_exact_payload_and_stable_identity(
    client, auth_headers, session, test_workspace, test_user, monkeypatch,
):
    connection, _, group = await connected_context(session, test_workspace.id, test_user.id)
    calls, _ = rpc_fixture(monkeypatch)
    response = await client.post("/api/onchain/history", headers=auth_headers, json={
        "connection_id": str(connection.id), "chain": "solana", "address": A, "ownership_confirmed": True,
    })
    assert response.status_code == 200, response.text
    saved = response.json()
    before, call_count = await _financial_state(session), len(calls)
    response = await client.get(PREFIX, headers=auth_headers, params={"group_id": str(group.id)})
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["events"]) == 2
    assert data["history_complete"] is False
    assert data["coverage"][0]["inventory"] == "unknown"
    assert sum(len(event["legs"]) for event in data["events"]) == 4
    event_ids = {event["event_id"] for event in data["events"]}
    fees = [leg for event in data["events"] for leg in event["legs"] if leg["classification"] == "fee"]
    assert len(fees) == 2 and sum(leg["non_additive"] for leg in fees) == 1
    source = next(source for event in data["events"] for source in event["sources"] if source["source_id"].startswith("archive:"))
    detail = await client.get(source["detail_url"], headers=auth_headers)
    assert detail.status_code == 200, detail.text
    assert json.loads(detail.json()["raw_payload"]["json"])["response"]["result"]["meta"]["preBalances"]
    assert len(calls) == call_count and await _financial_state(session) == before
    response = await client.post("/api/onchain/history", headers=auth_headers, json={
        **saved["request"], "collection_id": saved["collection_id"], "expected_revision": saved["revision"],
    })
    assert response.status_code == 200, response.text
    data = (await client.get(PREFIX, headers=auth_headers)).json()
    assert {event["event_id"] for event in data["events"]} == event_ids
    leg_ids = {leg["leg_id"] for event in data["events"] for leg in event["legs"]}
    archive = await session.get(InvestmentHistoryCollection, uuid.UUID(saved["collection_id"]))
    duplicate = InvestmentHistoryCollection(id=uuid.uuid4(), workspace_id=test_workspace.id, connection_id=connection.id,
        group_id=group.id, request=copy.deepcopy(archive.request), payload=copy.deepcopy(archive.payload),
        revision=archive.revision, size_bytes=archive.size_bytes)
    session.add(duplicate)
    await session.commit()
    overlapping = checked(await client.get(PREFIX, headers=auth_headers))
    assert len(overlapping["events"]) == 2
    assert {leg["leg_id"] for event in overlapping["events"] for leg in event["legs"]} == leg_ids
    await session.delete(archive)
    await session.commit()
    remaining = checked(await client.get(PREFIX, headers=auth_headers))
    assert {leg["leg_id"] for event in remaining["events"] for leg in event["legs"]} == leg_ids
    assert any(source["availability"] == "unavailable" for event in remaining["events"] for source in event["sources"])


@pytest.mark.asyncio
async def test_dates_unknown_window_distinct_tokens_archived_asset_and_pagination_revision(
    client, auth_headers, session, test_workspace, test_user, wallet,
):
    items = []
    for index in range(61):
        item = observation(f"source-{index}", execution=f"execution-{index}")
        item.legs[0].chain = "solana"
        item.legs[0].token_address = "TOKEN-A" if index % 2 else "TOKEN-B"
        item.legs[0].token_program = "synthetic-program"
        item.event_at = None
        item.time_precision = "date"
        item.legs[0].classification = "transfer"
        item.legs[0].unit_price = item.legs[0].acquisition_basis = None
        if index == 60:
            item.event_date, item.time_precision = None, "unknown"
        items.append(item)
    await save(session, test_workspace, test_user, wallet, items)
    response = await client.get(PREFIX, headers=auth_headers, params={"limit": 20})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 61 and len(data["assets"]) == 2 and data["has_more"]
    first_ids = {event["event_id"] for event in data["events"]}
    second = await client.get(PREFIX, headers=auth_headers, params={"limit": 20, "offset": 20, "expected_revision": data["revision"]})
    assert second.status_code == 200, second.text
    assert not first_ids.intersection(event["event_id"] for event in second.json()["events"])
    filtered = await client.get(PREFIX, headers=auth_headers, params={"canonical_asset_key": data["assets"][0]["canonical_asset_key"]})
    assert all(len(event["assets"]) == 1 and event["assets"][0]["token_address"] == data["assets"][0]["token_address"] for event in filtered.json()["events"])
    empty = await client.get(PREFIX, headers=auth_headers, params={"kind": "swap"})
    assert empty.json()["events"] == [] and len(empty.json()["assets"]) == 2 and empty.json()["coverage"]
    window = await client.get(PREFIX, headers=auth_headers, params={"since": "2025-02-02T18:00:00-08:00", "until": "2025-02-02T19:00:00-08:00", "limit": 200})
    assert window.json()["total"] == 61
    assert all(event["time"]["event_at"] is None for event in window.json()["events"])
    assert any("event_window_unknown" in event["reason_codes"] for event in window.json()["events"])
    changed = observation("extra", execution="extra")
    await save(session, test_workspace, test_user, wallet, [changed])
    response = await client.get(PREFIX, headers=auth_headers, params={"offset": 20, "expected_revision": data["revision"]})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "timeline_scope_changed"


@pytest.mark.asyncio
async def test_archive_zero_legs_segmentation_and_missing_payload_preserve_event(
    client, auth_headers, session, test_workspace, test_user, monkeypatch,
):
    connection, _, group = await connected_context(session, test_workspace.id, test_user.id)
    rpc_fixture(monkeypatch)
    response = await client.post("/api/onchain/history", headers=auth_headers, json={
        "connection_id": str(connection.id), "chain": "solana", "address": A, "ownership_confirmed": True,
    })
    assert response.status_code == 200
    row = await session.get(InvestmentHistoryCollection, uuid.UUID(response.json()["collection_id"]))
    archive = copy.deepcopy(row.payload)
    txs = list(archive["transactions"].values())
    empty, many = txs[0]["versions"][0], txs[1]["versions"][0]
    empty["legs"], empty["observations"] = [], []
    empty["gaps"] = ["unsupported_transaction_version"]
    original = many["legs"][-1]
    many["legs"] = [{**copy.deepcopy(original), "key": f"synthetic-leg-{index}"} for index in range(101)]
    many["relationships"] = [{"kind": "swap", "legs": [leg["key"] for leg in many["legs"]]}]
    archive["payloads"].pop(empty["payload_digest"])
    row.payload = archive
    await session.commit()
    response = await client.get(PREFIX, headers=auth_headers)
    assert response.status_code == 200, response.text
    events = response.json()["events"]
    assert len(events) == 2
    event = next(event for event in events if event["kind"] == "swap")
    assert len(event["legs"]) == 101
    assert len({leg["leg_id"] for leg in event["legs"]}) == 101
    zero = next(event for event in events if not event["legs"])
    assert "no_decoded_legs" in zero["reason_codes"]
    missing = next(source for source in zero["sources"] if source["availability"] == "unavailable")
    source = await client.get(missing["detail_url"], headers=auth_headers)
    assert source.status_code == 200 and source.json()["raw_payload"] is None
    assert source.json()["source"]["unavailable_reason"] == "raw_payload_unavailable"


@pytest.mark.asyncio
async def test_source_conflicts_and_failed_principal_keep_original_facts(
    client, auth_headers, session, test_workspace, test_user, wallet,
):
    a = observation("same-source")
    b = observation("same-source", quantity="13")
    await save(session, test_workspace, test_user, wallet, [a])
    await save(session, test_workspace, test_user, wallet, [b])
    failed = observation("failed", execution="failed")
    failed.provider_status = failed.settlement_status = "failed"
    failed.legs[0].classification = "transfer"
    failed.legs.append(failed.legs[0].model_copy(update={"key": "fee", "classification": "fee", "quantity": Decimal("0.03"), "quantity_role": "network_fee"}))
    await save(session, test_workspace, test_user, wallet, [failed])
    data = (await client.get(PREFIX, headers=auth_headers)).json()
    conflict = next(event for event in data["events"] if "source_version" in event["conflicting_fields"])
    assert {leg["quantity"] for leg in conflict["legs"]} == {"12", "13"}
    assert "source_version" in conflict["conflicting_fields"] and len(conflict["sources"]) == 2
    attempt = next(event for event in data["events"] if event["status"] == "failed")
    assert any(leg["quantity"] == "0.03" for leg in attempt["legs"])
    assert attempt["tax_treatment"] == "unresolved"


@pytest.mark.asyncio
async def test_workspace_collection_source_detail_and_validation_boundaries(
    client, auth_headers, session, test_workspace, test_user, wallet, monkeypatch,
):
    await save(session, test_workspace, test_user, wallet, [observation()])
    collection = Collection(workspace_id=test_workspace.id, user_id=test_user.id, name="Synthetic empty", accounts=[], asset_groups=[])
    session.add(collection)
    await session.commit()
    data = (await client.get(PREFIX, headers=auth_headers)).json()
    event = data["events"][0]
    assert (await client.get(PREFIX, headers=auth_headers, params={"collection_id": str(collection.id)})).json()["events"] == []
    assert (await client.get(f"{PREFIX}/{event['event_id']}", headers=auth_headers, params={"collection_id": str(collection.id)})).status_code == 404
    assert (await client.get(PREFIX, headers=auth_headers, params={"group_id": str(uuid.uuid4())})).status_code == 404
    assert (await client.get(PREFIX, headers=auth_headers, params={"asset_id": str(uuid.uuid4())})).status_code == 404
    assert (await client.get(f"{PREFIX}/sources/{uuid.uuid4()}", headers=auth_headers)).status_code == 404
    assert (await client.get(PREFIX, headers=auth_headers, params={"since": "2025-02-03T12:00:00"})).status_code == 422
    assert (await client.get(PREFIX, headers=auth_headers, params={"limit": 201})).status_code == 422
    from app.models.workspace import Workspace
    other = Workspace(name="Foreign synthetic workspace", created_by_user_id=test_user.id)
    session.add(other)
    await session.commit()
    assert (await timeline.list_timeline(session, other.id)).events == []
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as denied:
        await timeline.get_timeline_source(session, other.id, event["sources"][0]["source_id"])
    assert denied.value.status_code == 404
    async def unavailable(*args, **kwargs):
        raise HTTPException(404, "Source preview unavailable")
    monkeypatch.setattr(evidence, "preview_evidence", unavailable)
    data = (await client.get(PREFIX, headers=auth_headers)).json()
    assert len(data["events"]) == 1 and data["errors"][0]["code"] == "evidence_preview_unavailable"
    assert data["all_available_records_loaded"] is False


@pytest.mark.asyncio
async def test_confirmed_owned_transfer_groups_both_accounts_and_fees_then_unqualifies(transfers):
    v = transfers
    request = await select_lots(v, await pair(v, v.a, v.b))
    distinct = checked(await v.client.get(PREFIX, headers=v.headers))
    assert len([event for event in distinct["events"] if event["kind"] == "transfer"]) == 2
    fee = await retain_movement(v.session, v.a, direction="out", reference="synthetic-transfer",
        source=v.addresses[v.a.id], destination=None, quantity="0.02", leg_key="meta.fee",
        classification="fee", quantity_role="network_fee", fee_payer=v.addresses[v.a.id], fee_semantics="separate")
    lots = checked(await v.client.get("/api/assets/evidence/transfers/lots", headers=v.headers,
        params={"asset_id": str(v.a.id), "before_leg_id": str(fee.id)}))["lots"]
    request["fees"] = [{"leg_id": str(fee.id), "asset_id": str(v.a.id), "ownership_id": v.ownership[v.a.id],
        "allocations": [{"lot_id": lots[0]["lot_id"], "quantity": "0.02"}], "reason": "Synthetic independently supported fee"}]
    transfer = await confirm_transfer(v, request)
    before = await _financial_state(v.session)
    data = checked(await v.client.get(PREFIX, headers=v.headers))
    grouped = next(event for event in data["events"] if event["event_id"] == "owned-transfer:" + transfer["id"])
    assert grouped["linkage"] == "confirmed" and len(grouped["legs"]) == 3, grouped
    assert len(grouped["accounts"]) == 2 and grouped["basis"]["state"] == "known"
    assert sum(leg["classification"] == "fee" for leg in grouped["legs"]) == 1
    assert len(data["events"]) == 2  # Original acquisition + one transfer, no movement-ledger duplicates.
    other_source = next(source for source in grouped["sources"] if source["account"]["group_id"] == str(v.b.group_id))
    supported = await v.client.get(other_source["detail_url"], headers=v.headers, params={"group_id": str(v.a.group_id), "event_id": grouped["event_id"]})
    assert supported.status_code == 200
    denied = await v.client.get(other_source["detail_url"], headers=v.headers, params={"group_id": str(v.c.group_id)})
    assert denied.status_code == 404
    old_id = next(event["event_id"] for event in distinct["events"] if event["kind"] == "transfer")
    assert checked(await v.client.get(f"{PREFIX}/{old_id}", headers=v.headers))["event_id"] == grouped["event_id"]
    assert await _financial_state(v.session) == before
    leg = await v.session.get(InvestmentLeg, uuid.UUID(request["in_leg_id"]))
    source = await v.session.get(InvestmentObservation, leg.observation_id)
    source.is_current = False
    await v.session.commit()
    data = checked(await v.client.get(PREFIX, headers=v.headers))
    assert not any(event["event_id"].startswith("owned-transfer:") for event in data["events"])
    assert any(item["state"] == "unresolved" for event in data["events"] for item in event["relationships"] if item["kind"] == "owned_transfer")


@pytest.mark.asyncio
async def test_zero_cost_known_lots_plus_unknown_quantity_stay_partial(transfers):
    v = transfers
    await add_acquisition(v.session, v.b, quantity="2", price="0")
    unknown = await retain_movement(v.session, v.b, direction="in", reference="synthetic-unknown", quantity="1",
        source="D" * 44, destination=v.addresses[v.b.id], when="2025-02-04T12:00:00+00:00")
    await apply_movement(v, v.b, unknown)
    request = await pair(v, v.b, v.c, quantity="3", reference="synthetic-mixed", when="2025-02-05T12:00:00+00:00")
    preview = await transfer_preview(v, request)
    request["allocations"] = [{"lot_id": lot["lot_id"], "quantity": lot["quantity"]} for lot in preview["available_lots"]]
    transfer = await confirm_transfer(v, request)
    data = checked(await v.client.get(PREFIX, headers=v.headers))
    event = next(event for event in data["events"] if event["event_id"] == "owned-transfer:" + transfer["id"])
    assert event["basis"]["state"] == "partial"
    assert Decimal(event["basis"]["known_acquisition_cost"]) == 0
    assert Decimal(event["basis"]["unknown_basis_quantity"]) == 1
    assert event["basis"]["acquisition_cost"] is None


@pytest.mark.asyncio
async def test_recovery_receipt_transfer_sale_remain_nonadditive_and_read_only(transfers):
    v = transfers
    await receipt_journey(v)
    before = await _financial_state(v.session)
    data = checked(await v.client.get(PREFIX, headers=v.headers))
    assert len(data["events"]) == 4  # Acquisition, recovery receipt, owned transfer and disposal.
    assert any(event["kind"] == "disposal" for event in data["events"])
    receipt = next(event for event in data["events"] if event["recovery"])
    assert len(receipt["legs"]) == 1 and Decimal(receipt["legs"][0]["quantity"]) == 4
    assert receipt["basis"]["state"] == "unknown" and receipt["tax_treatment"] == "unresolved"
    assert any(link["kind"] == "notice_receipt" for link in receipt["relationships"])
    assert await _financial_state(v.session) == before


@pytest.mark.asyncio
async def test_one_observation_allocation_does_not_collapse_two_events(session, test_workspace, test_user, wallet):
    a = observation("part-A", quantity="4", execution="A")
    b = observation("part-B", quantity="6", execution="B")
    aggregate = observation("aggregate", source="coinbase_api", quantity="10", execution="aggregate")
    retained = await save(session, test_workspace, test_user, wallet, [a, b, aggregate])
    rows = (await session.scalars(select(InvestmentLeg))).all()
    source_rows = {str(row.id): row for row in (await session.scalars(select(InvestmentObservation))).all()}
    total = next(row for row in rows if source_rows[str(row.observation_id)].payload["source_local_id"] == "aggregate")
    targets = [row for row in rows if row.id != total.id]
    await evidence.confirm_evidence(session, test_workspace.id, test_user.id, wallet.id, [EvidenceDecision(
        observation_ref=str(total.observation_id), leg_key=total.source_leg_key, action="link",
        allocations=[EvidenceAllocation(leg_id=row.id, quantity=row.payload["quantity"]) for row in targets],
        reason="Synthetic source records exactly identify both executions",
    )], retained.evidence.revision)
    result = await timeline.list_timeline(session, test_workspace.id)
    assert len(result.events) == 2 and {event.legs[0].quantity for event in result.events} == {Decimal("4"), Decimal("6")}
    assert all(len(event.sources) == 2 for event in result.events)
    assert all({link.quantity for link in event.relationships if link.kind == "source_corroboration"} == {Decimal("4"), Decimal("6")} for event in result.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguity", [None, "program", "owner", "wallet"])
async def test_legacy_onchain_holding_resolves_only_qualified_owned_identity(
    client, auth_headers, session, test_workspace, test_user, monkeypatch, ambiguity,
):
    from app.providers import solana_history
    from app.models.asset_group import AssetGroup
    connection, _, group = await connected_context(session, test_workspace.id, test_user.id)
    payload = transaction([instruction(TOKEN, "transferChecked", source="token-A", destination="token-B", mint="mint-A",
        tokenAmount={"amount": "1000000", "decimals": 6})], keys=(A, "token-A", "token-B"),
        pre=(11000000000, 2000000, 2000000), post=(10970000000, 2000000, 2000000),
        tokens_pre=[balance(1, 2000000, owner=A), balance(2, 0, owner="other")],
        tokens_post=[balance(1, 1000000, owner=A), balance(2, 1000000, owner="other")])
    rpc = RPC(pages={(A, None): [signature("synthetic-token-transfer")]}, payloads={"synthetic-token-transfer": payload})
    monkeypatch.setattr(solana_history, "request_json", rpc)
    saved = checked(await client.post("/api/onchain/history", headers=auth_headers, json={
        "connection_id": str(connection.id), "address": A, "ownership_confirmed": True,
    }))
    token = Asset(id=uuid.uuid4(), workspace_id=test_workspace.id, user_id=test_user.id, group_id=group.id,
        connection_id=connection.id, name="Synthetic archived token", type="crypto", currency="USD", source="onchain",
        ticker="SYN", valuation_method="market_price", units=Decimal("0"), is_archived=True,
        external_metadata={"chain": "solana", "address": A, "watch_only": True, "token_contract": "mint-A"})
    session.add(token)
    row = await session.get(InvestmentHistoryCollection, uuid.UUID(saved["collection_id"]))
    archive = copy.deepcopy(row.payload)
    if ambiguity == "program":
        duplicate = copy.deepcopy(next(iter(archive["transactions"].values())))
        duplicate["signature"] = "synthetic-other-program"
        version = duplicate["versions"][0]
        for leg in version["legs"]:
            if leg["asset"].get("mint") == "mint-A":
                leg["asset"]["token_program"] = TOKEN_2022
        for owner in version["ownership"]:
            owner["token_program"] = TOKEN_2022
        archive["transactions"]["solana:synthetic-other-program"] = duplicate
        row.payload = archive
    elif ambiguity == "owner":
        for tx in archive["transactions"].values():
            for version in tx["versions"]:
                version["gaps"].append("changed_token_owner")
        row.payload = archive
    elif ambiguity == "wallet":
        other = AssetGroup(workspace_id=test_workspace.id, user_id=test_user.id, name="Synthetic lookalike")
        session.add(other)
        await session.flush()
        token.group_id = other.id
    await session.commit()
    result = checked(await client.get(PREFIX, headers=auth_headers, params={"asset_id": str(token.id)}))
    if ambiguity:
        assert result["events"] == []
        assert any(error["code"] == "holding_identity_unresolved" for error in result["errors"])
    else:
        assert len(result["events"]) == 1 and len(result["events"][0]["legs"]) == 2
        assert "resolved_from_retained_owned_identity" in result["events"][0]["reason_codes"]
        selected = [asset for asset in result["assets"] if str(token.id) in asset["asset_ids"]]
        assert len(selected) == 1 and selected[0]["token_program"] == TOKEN
    native = Asset(id=uuid.uuid4(), workspace_id=test_workspace.id, user_id=test_user.id, group_id=group.id,
        connection_id=connection.id, name="Synthetic native", type="crypto", currency="USD", source="onchain",
        ticker="SOL", valuation_method="market_price", units=Decimal("0"), is_archived=True,
        external_metadata={"chain": "solana", "address": A, "watch_only": True})
    session.add(native)
    await session.commit()
    native_read = checked(await client.get(PREFIX, headers=auth_headers, params={"asset_id": str(native.id)}))
    assert native_read["events"] and any(leg["asset_symbol"] == "SOL" for event in native_read["events"] for leg in event["legs"])


@pytest.mark.asyncio
async def test_unavailable_sibling_preview_does_not_change_selected_wallet_completion(
    client, auth_headers, session, test_workspace, test_user, wallet, monkeypatch,
):
    from app.models.asset_group import AssetGroup
    from fastapi import HTTPException
    await save(session, test_workspace, test_user, wallet, [observation()])
    sibling = AssetGroup(workspace_id=test_workspace.id, user_id=test_user.id, name="Synthetic unavailable sibling")
    session.add(sibling)
    await session.commit()
    original = evidence.preview_evidence
    async def preview(*args, **kwargs):
        if args[2] == sibling.id:
            raise HTTPException(404, "Unavailable source")
        return await original(*args, **kwargs)
    monkeypatch.setattr(evidence, "preview_evidence", preview)
    read = checked(await client.get(PREFIX, headers=auth_headers, params={"group_id": str(wallet.id)}))
    assert read["errors"] == [] and read["all_available_records_loaded"] is True
    assert read["history_complete"] is False


def test_exact_sort_day_uses_utc_without_changing_original_timestamp():
    from app.schemas.investment_timeline import TimelineEvent, TimelineTime
    event = TimelineEvent(event_id="event-A", kind="transfer", status="settled",
        time=TimelineTime(event_at="2025-02-02T18:00:00-08:00", time_precision="second", ordering="exact"))
    assert timeline._sort_key(event)[:2] == ("2025-02-03", "2025-02-03T02:00:00+00:00")
    assert event.time.event_at is not None
    assert event.time.event_at.isoformat() == "2025-02-02T18:00:00-08:00"


@pytest.mark.parametrize("reverse_order", [False, True])
async def test_shared_transaction_retains_each_selected_transfer_fee_and_unrelated_leg(transfers, reverse_order):
    v = transfers
    ref, at = "synthetic-batch-transfer", datetime.fromisoformat("2025-02-03T12:00:00+00:00")
    principals = [EvidenceLegInput(
        key=f"ix:{index}", asset_id=v.a.id, asset_symbol="SYN", chain="solana", token_address="native",
        direction="out", classification="transfer", quantity=str(quantity), raw_units=str(quantity * 10**9), decimals=9,
        transaction_ref=ref, leg_ref=f"ix:{index}", source_address=v.addresses[v.a.id],
        destination_address=v.addresses[destination.id], source_owner=v.addresses[v.a.id],
        destination_owner=v.addresses[destination.id], quantity_role="principal", fee_semantics="none",
    ) for index, (destination, quantity) in enumerate([(v.b, 3), (v.c, 2)])]
    fees = [leg.model_copy(update={"key": f"fee:{index}", "leg_ref": f"fee:{index}", "classification": "fee",
        "quantity": Decimal("0.02"), "raw_units": "20000000", "destination_address": None, "destination_owner": None,
        "quantity_role": "network_fee", "fee_payer": v.addresses[v.a.id], "fee_semantics": "separate"})
        for index, leg in enumerate(principals)]
    unrelated = principals[0].model_copy(update={"key": "unrelated", "leg_ref": "unrelated", "classification": "swap",
        "quantity": Decimal("1"), "raw_units": "1000000000"})
    source = EvidenceObservationInput(reference=ref, source="csv", provider="onchain", source_local_id=ref,
        source_account_id=v.addresses[v.a.id], source_locator="synthetic/batch.csv", event_at=at, event_date=at.date(),
        event_time_raw=at.isoformat(), time_precision="second", timezone="UTC", observed_at=at,
        provider_status="completed", network_status="finalized", settlement_status="settled", legs=[*principals, *fees, unrelated])
    preview = await evidence.preview_evidence(v.session, v.workspace.id, v.a.group_id, [source])
    await evidence.import_evidence(v.session, v.workspace.id, v.user.id, v.a.group_id, [source], expected_revision=preview.revision)
    outgoing = {leg.source_leg_key: leg for leg in await v.session.scalars(select(InvestmentLeg).where(InvestmentLeg.asset_id == v.a.id))}
    requests = []
    for index, (destination, quantity) in enumerate([(v.b, 3), (v.c, 2)]):
        incoming = await retain_movement(v.session, destination, direction="in", reference=ref,
            source=v.addresses[v.a.id], destination=v.addresses[destination.id], quantity=str(quantity), leg_key=f"ix:{index}")
        requests.append({"out_leg_id": str(outgoing[f"ix:{index}"].id), "in_leg_id": str(incoming.id),
            "source_asset_id": str(v.a.id), "destination_asset_id": str(destination.id),
            "source_ownership_id": v.ownership[v.a.id], "destination_ownership_id": v.ownership[destination.id],
            "reason": "Synthetic selected instruction and receipt"})
    original = checked(await v.client.get(PREFIX, headers=v.headers))
    original_ids = {event["event_id"] for event in original["events"] if not event["event_id"].startswith("ledger:")}
    fee_lots = (await select_lots(v, requests[0]))["allocations"]
    for index in (0, 1):
        await apply_movement(v, v.a, outgoing[f"fee:{index}"],
            allocations=[{"lot_id": fee_lots[0]["lot_id"], "quantity": "0.02"}])
    confirmed = []
    for index in ([1, 0] if reverse_order else [0, 1]):
        request = await select_lots(v, requests[index])
        request["fees"] = [{"leg_id": str(outgoing[f"fee:{index}"].id), "asset_id": str(v.a.id),
            "ownership_id": v.ownership[v.a.id], "reason": "Synthetic independently selected fee",
            "allocations": [{"lot_id": request["allocations"][0]["lot_id"], "quantity": "0.02"}]}]
        confirmed.append(await confirm_transfer(v, request))
        data = checked(await v.client.get(PREFIX, headers=v.headers))
        grouped = next(event for event in data["events"] if any(part["id"] == confirmed[-1]["id"] for part in event["transfers"]))
        assert grouped["kind"] == "unknown"  # The unrelated swap is not a transfer.
        assert grouped["basis"]["state"] == "unknown" and grouped["basis"]["acquisition_cost"] is None
        assert grouped["basis"]["reason_codes"] == ["transfer_basis_scoped_to_selected_legs"]
    before = await _financial_state(v.session)
    data = checked(await v.client.get(PREFIX, headers=v.headers))
    grouped = next(event for event in data["events"] if event["transfers"])
    assert len(grouped["legs"]) == 7 and len(grouped["sources"]) == 3
    assert {leg["leg_id"] for leg in grouped["legs"]} == {
        leg["leg_id"] for event in original["events"] if event["event_id"] in original_ids for leg in event["legs"]}
    for transfer in confirmed:
        retained = next(part for part in grouped["transfers"] if part["id"] == transfer["id"])
        request = retained["request"]
        assert {request["out_leg_id"], request["in_leg_id"], request["fees"][0]["leg_id"]} <= {leg["leg_id"] for leg in grouped["legs"]}
        assert retained["acquisition_cost"] == transfer["acquisition_cost"]
        assert any(link["review_id"] == transfer["id"] and link["state"] == "confirmed" for link in grouped["relationships"])
    assert {Decimal(part["acquisition_cost"]) for part in grouped["transfers"]} == {Decimal("60"), Decimal("40")}
    for identifier in original_ids | {"owned-transfer:" + part["id"] for part in confirmed}:
        assert checked(await v.client.get(f"{PREFIX}/{identifier}", headers=v.headers))["event_id"] == grouped["event_id"]
    for source in grouped["sources"]:
        assert (await v.client.get(source["detail_url"], headers=v.headers, params={"event_id": grouped["event_id"]})).status_code == 200
    assert await _financial_state(v.session) == before


@pytest.mark.parametrize("reverse_order", [False, True])
async def test_compatible_complementary_enrichment_preserves_exact_known_facts(session, test_workspace, test_user, wallet, reverse_order):
    sparse = observation("synthetic-enrichment", quantity="3")
    leg = sparse.legs[0]
    leg.quantity = leg.unit_price = leg.subtotal = leg.total = leg.fee = None
    leg.unit_price_origin, leg.classification = "unknown", "transfer"
    quantity = sparse.model_copy(deep=True)
    quantity.legs[0].quantity = Decimal("3.1234567890123456789012345678")
    costs = sparse.model_copy(deep=True)
    costs.legs[0].fee = costs.legs[0].acquisition_basis = Decimal("0")
    costs.legs[0].unit_price_origin = "reported"
    def fixed_ids(mapper, connection, row):
        row.id = uuid.UUID(int=101 if row.payload["fee"] is None and row.payload["quantity"] is None else 102 if row.payload["quantity"] else 103)
    sqlalchemy_event.listen(InvestmentLeg, "before_insert", fixed_ids)
    try:
        for item in ([costs, quantity, sparse] if reverse_order else [sparse, quantity, costs]):
            await save(session, test_workspace, test_user, wallet, [item])
    finally:
        sqlalchemy_event.remove(InvestmentLeg, "before_insert", fixed_ids)
    before = await _financial_state(session)
    result = await timeline.list_timeline(session, test_workspace.id)
    assert len(result.events) == 1
    event = result.events[0]
    assert len(event.sources) == 3 and len(event.legs) == 1
    projected = event.legs[0]
    assert projected.quantity == quantity.legs[0].quantity
    assert projected.fee == 0 and projected.unit_price is None and projected.unit_price_origin == "reported"
    assert event.basis.state == "known" and event.basis.acquisition_cost == 0
    assert set(projected.source_ids) == {source.source_id for source in event.sources}
    assert await _financial_state(session) == before


async def test_superseded_recovery_review_is_historical_only(transfers):
    v = transfers
    await receipt_journey(v)
    saved = await package(v)
    old = next(item for item in saved["reviews"] if item["key"] == "notice-receipt")
    replacement = {key: value for key, value in old.items() if key not in {
        "id", "created_at", "created_by", "is_current", "blockers", "ready_for_review"}}
    replacement.update(key="reconsidered-notice-receipt", supersedes_id=old["id"], relation_state="candidate",
        reason="Synthetic documented reconsideration")
    revised = await review(v, [replacement])
    prior = next(item for item in revised["reviews"] if item["id"] == old["id"])
    assert prior["is_current"] is False and prior["blockers"] == []
    before = await _financial_state(v.session)
    data = checked(await v.client.get(PREFIX, headers=v.headers))
    assert not any(link["review_id"] == old["id"] for event in data["events"] for link in event["relationships"])
    history = [item for event in data["events"] for detail in event["recovery"] for item in detail["reviews"]]
    assert any(item["id"] == old["id"] and item["is_current"] is False for item in history)
    current = next(item for item in revised["reviews"] if item["key"] == replacement["key"])
    assert any(link["review_id"] == current["id"] and link["state"] == ("unresolved" if current["blockers"] else "candidate")
        for event in data["events"] for link in event["relationships"])
    assert await _financial_state(v.session) == before


@pytest.mark.parametrize("invalidation", ["reverse", "conflict"])
async def test_reversed_transfer_keeps_independent_reported_zero_basis(transfers, invalidation):
    v = transfers
    request = await pair(v, v.a, v.b)
    incoming = await v.session.get(InvestmentLeg, uuid.UUID(request["in_leg_id"]))
    await retain_revision(v.session, v, incoming, acquisition_basis="0")
    confirmed = await confirm_transfer(v, await select_lots(v, request))
    url = "/api/assets/evidence/transfers/" + confirmed["id"]
    detail = checked(await v.client.get(url, headers=v.headers))
    if invalidation == "reverse":
        checked(await v.client.delete(url, headers=v.headers, params={"expected_revision": detail["revision"]}))
    else:
        outgoing = await v.session.get(InvestmentLeg, uuid.UUID(request["out_leg_id"]))
        await retain_revision(v.session, v, outgoing, quantity="4", raw_units="4000000000")
    before = await _financial_state(v.session)
    events = checked(await v.client.get(PREFIX, headers=v.headers))["events"]
    event = next(event for event in events if any(leg["acquisition_basis"] == "0" for leg in event["legs"]))
    assert all(part["status"] == ("reversed" if invalidation == "reverse" else "unresolved") for part in event["transfers"])
    assert event["basis"]["state"] == "known" and Decimal(event["basis"]["acquisition_cost"]) == 0
    assert event["basis"]["reason_codes"] == ["reported_acquisition_basis"]
    assert await _financial_state(v.session) == before


@pytest.mark.parametrize("overlapping", [False, True])
async def test_collector_transfer_preserves_archive_ids_and_exact_pair_basis(
    session, test_workspace, test_user, client, auth_headers, monkeypatch, overlapping,
):
    original_confirm, observed = transfer_producer.confirm_transfer, []
    async def confirm_and_inspect(v, request):
        transfer = await original_confirm(v, request)
        if overlapping:
            archives = list(await session.scalars(select(InvestmentHistoryCollection).where(
                InvestmentHistoryCollection.workspace_id == test_workspace.id)))
            for archive in archives:
                checked(await client.post("/api/onchain/history", headers=v.headers, json=archive.request))
        before = await _financial_state(session)
        events = checked(await client.get(PREFIX, headers=v.headers))["events"]
        event = next(event for event in events if any(part["id"] == transfer["id"] for part in event["transfers"]))
        assert all(leg["leg_id"].startswith("archive-leg:") for leg in event["legs"])
        assert len({leg["leg_id"] for leg in event["legs"]}) == len(event["legs"])
        if overlapping:
            assert all(len(leg["source_ids"]) == 2 for leg in event["legs"])
        assert event["basis"]["state"] == "known" and Decimal(event["basis"]["acquisition_cost"]) == 60
        for leg_id in (request["out_leg_id"], request["in_leg_id"], *(fee["leg_id"] for fee in request["fees"])):
            raw = await session.get(InvestmentLeg, uuid.UUID(leg_id))
            assert any(leg["key"] == raw.source_leg_key for leg in event["legs"])
            assert checked(await client.get(f"{PREFIX}/event:{raw.event_id}", headers=v.headers))["event_id"] == event["event_id"]
        assert await _financial_state(session) == before
        observed.append(event["event_id"])
        return transfer
    monkeypatch.setattr(transfer_producer, "confirm_transfer", confirm_and_inspect)
    await transfer_producer.test_actual_collector_outputs_confirm_with_fee_and_reobserve_to_unresolved(
        session, test_workspace, test_user, client, auth_headers, monkeypatch)
    assert len(observed) == 1
    events = checked(await client.get(PREFIX, headers={**auth_headers, "X-Workspace-Id": str(test_workspace.id)}))["events"]
    assert not any(part["status"] == "confirmed" for event in events for part in event["transfers"])


@pytest.mark.parametrize("repeat", ["same_anchor", "later_anchor", "conflicting_facts", "coinbase_maturity"])
async def test_bitcoin_collector_timeline_deduplicates_only_compatible_anchor_facts(
    client, auth_headers, session, test_workspace, test_user, monkeypatch, repeat,
):
    from tests.test_bitcoin_history import Esplora, install, output, spend, transaction

    # Base58Check of a synthetic fixture hash; every Esplora request is mocked.
    address = "16g4GWdshjQrAYmfmvWxub92twwr3JFWgx"
    connection, _, group = await connected_context(session, test_workspace.id, test_user.id)
    connection.credentials = {"addresses": ["bitcoin:" + address]}
    await session.commit()
    coinbase = repeat == "coinbase_maturity"
    inputs = [{"is_coinbase": True, "txid": "0" * 64, "vout": 4294967295, "scriptsig": "aa"}] if coinbase else [spend(address=address)]
    payload = transaction(inputs=inputs, outputs=[output(990, address if coinbase else "external-A", "52")])
    rpc = Esplora({f"/address/{address}/txs/chain": [payload]})
    install(monkeypatch, rpc)
    request = {"connection_id": str(connection.id), "chain": "bitcoin", "address": address, "ownership_confirmed": True}
    first = checked(await client.post("/api/onchain/history", headers=auth_headers, json=request))
    initial = checked(await client.get(PREFIX, headers=auth_headers))["events"][0]
    if repeat != "same_anchor":
        rpc.tip = 194 if coinbase else 101
    if repeat == "conflicting_facts":
        payload["vout"][0]["value"], payload["fee"] = 980, 20
    second = checked(await client.post("/api/onchain/history", headers=auth_headers, json=request))
    before, call_count = await _financial_state(session), len(rpc.calls)
    retained = {row.id: copy.deepcopy(row.payload) for row in await session.scalars(
        select(InvestmentHistoryCollection).where(InvestmentHistoryCollection.workspace_id == test_workspace.id))}
    events = checked(await client.get(PREFIX, headers=auth_headers, params={"group_id": str(group.id)}))["events"]
    assert len(events) == 1
    event = events[0]
    assert event["event_id"] == initial["event_id"]
    sources = [source for source in event["sources"] if source["source_id"].startswith("archive:")]
    assert len(sources) == 2
    assert {source["collection_id"] for source in sources} == {first["collection_id"], second["collection_id"]}
    versions = []
    for source in sources:
        detail = checked(await client.get(source["detail_url"], headers=auth_headers))
        assert detail["raw_payload"] and detail["transaction"]
        version = json.loads(detail["transaction"]["json"])["versions"][0]
        assert version["payload_digest"] == source["payload_digest"]
        versions.append(version)
    if coinbase:
        assert {(version["confirmations"], version["legs"][0]["maturity_eligible"]) for version in versions} == {(6, False), (100, True)}
    assert len({leg["leg_id"] for leg in event["legs"]}) == len(event["legs"])
    if repeat == "conflicting_facts":
        assert event["status"] == event["linkage"] == "conflicting"
        assert len(event["legs"]) == 6
        assert all(not leg["is_current"] and leg["settlement_status"] == "unknown" for leg in event["legs"])
        assert all(len(leg["source_ids"]) == 1 for leg in event["legs"])
    else:
        assert event["status"] == "settled"
        assert len(event["legs"]) == (1 if coinbase else 3)
        assert {leg["leg_id"] for leg in event["legs"]} == {leg["leg_id"] for leg in initial["legs"]}
        assert all(leg["is_current"] and leg["settlement_status"] == "settled" for leg in event["legs"])
        assert all(set(leg["source_ids"]) == {source["source_id"] for source in sources} for leg in event["legs"])
        if coinbase:
            assert event["legs"][0]["raw_units"] == "990" and not event["legs"][0]["non_additive"]
        else:
            fee = next(leg for leg in event["legs"] if leg["classification"] == "fee")
            assert fee["raw_units"] == "10" and fee["non_additive"]
            assert Decimal(fee["quantity"]) == Decimal("0.0000001")
    assert len(rpc.calls) == call_count and await _financial_state(session) == before
    assert {row.id: row.payload for row in await session.scalars(select(InvestmentHistoryCollection).where(
        InvestmentHistoryCollection.workspace_id == test_workspace.id))} == retained
