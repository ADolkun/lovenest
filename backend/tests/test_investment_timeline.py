"""Synthetic producer -> activity/API checks; no live financial or chain reads."""
import copy
import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.asset import Asset
from app.models.asset_transaction import AssetTransaction
from app.models.collection import Collection
from app.models.investment_evidence import InvestmentHistoryCollection, InvestmentLeg, InvestmentObservation
from app.schemas.investment_evidence import EvidenceAllocation, EvidenceDecision
from app.services import investment_evidence_service as evidence
from app.services import investment_timeline_service as timeline
from tests.test_investment_evidence import observation, save, wallet as wallet_fixture
from tests.test_onchain_history_api import A, connected_context, rpc_fixture
from tests.test_owned_transfers_integration import (
    add_acquisition, apply_movement, checked, confirm_transfer, pair, retain_movement,
    select_lots, transfer_preview, transfers as transfers_fixture,
)
from tests.test_recovery_evidence_integration import receipt_journey
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
