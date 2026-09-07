"""Synthetic collector → durable evidence → crosswalk/API, without financial writes."""
import asyncio
import copy
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.models.bank_connection import BankConnection
from app.models.investment_evidence import InvestmentObservation
from app.models.user import User
from app.models.workspace import Workspace
from app.providers import solana_history as collector
from app.providers.onchain import ACCOUNT_EXTERNAL_ID
from app.schemas.onchain_history import HistoryRequest
from app.services import investment_evidence_service as crosswalk
from app.services import onchain_history as service
from app.services.connection_service import _wallet_external_id

A = "A" * 44
B = "B" * 44


async def connected_context(session, workspace_id, user_id):
    connection = BankConnection(
        id=uuid.uuid4(), workspace_id=workspace_id, user_id=user_id, provider="onchain",
        external_id="synthetic-connection", institution_name="Synthetic wallet", credentials={"addresses": [f"solana:{A}"]},
    )
    session.add(connection)
    await session.flush()
    account = Account(
        id=uuid.uuid4(), workspace_id=workspace_id, user_id=user_id, connection_id=connection.id,
        external_id=ACCOUNT_EXTERNAL_ID, name="Synthetic", type="investment", balance=Decimal("19.25"), currency="USD",
    )
    group = AssetGroup(
        id=uuid.uuid4(), workspace_id=workspace_id, user_id=user_id, connection_id=connection.id,
        external_id=_wallet_external_id(connection.external_id, account.external_id), source="onchain", name="Synthetic",
    )
    session.add_all([account, group])
    await session.commit()
    return connection, account, group


@pytest_asyncio.fixture
async def history_context(session, test_workspace, test_user):
    return await connected_context(session, test_workspace.id, test_user.id)


def native_transaction(*, incoming=False, slot=102):
    keys = [B, A] if incoming else [A, B]
    return {
        "slot": slot, "blockTime": 1738584000 + slot, "version": "legacy",
        "transaction": {"signatures": ["synthetic-receipt" if incoming else "synthetic-send"], "message": {
            "accountKeys": [{"pubkey": key} for key in keys],
            "instructions": [{"program": "system", "programId": collector.SYSTEM_PROGRAM, "parsed": {
                "type": "transfer", "info": {"source": keys[0], "destination": keys[1], "lamports": 4_000_000_000 if incoming else 3_000_000_000},
            }}],
        }},
        "meta": {
            "err": None, "fee": 10_000_000 if incoming else 20_000_000,
            "preBalances": [100_000_000_000, 11_000_000_000] if incoming else [15_000_000_000, 100_000_000_000],
            "postBalances": [95_990_000_000, 15_000_000_000] if incoming else [11_980_000_000, 103_000_000_000],
            "preTokenBalances": [], "postTokenBalances": [], "innerInstructions": [],
        },
    }


def rpc_fixture(monkeypatch):
    calls = []
    payloads = {"synthetic-receipt": native_transaction(incoming=True, slot=101), "synthetic-send": native_transaction()}

    async def rpc(*args, **kwargs):
        body = kwargs["json_body"]
        method, params = body["method"], body["params"]
        calls.append((method, params))
        if method == "getSlot":
            result = 200
        elif method == "getBlock":
            result = {"blockhash": "synthetic-anchor"}
        elif method == "getTokenAccountsByOwner":
            result = {"context": {"slot": 200}, "value": []}
        elif method == "getSignaturesForAddress":
            result = [] if params[1].get("before") else [
                {"signature": signature, "slot": payload["slot"], "blockTime": payload["blockTime"], "confirmationStatus": "finalized"}
                for signature, payload in reversed(list(payloads.items()))
            ]
        elif method == "getTransaction":
            result = payloads[params[0]]
        elif method == "getSignatureStatuses":
            result = {"value": [{"confirmationStatus": "finalized"}]}
        else:
            raise AssertionError(method)
        return {"jsonrpc": "2.0", "id": 1, "result": result}

    monkeypatch.setattr(collector, "request_json", rpc)
    return calls, payloads


@pytest.mark.asyncio
async def test_collector_api_crosswalk_reconciliation_export_and_no_financial_writes(
    client, auth_headers, session, test_workspace, history_context, monkeypatch,
):
    connection, account, group = history_context
    calls, _ = rpc_fixture(monkeypatch)
    request = {"connection_id": str(connection.id), "chain": "solana", "address": A, "ownership_confirmed": True}
    response = await client.post("/api/onchain/history", headers=auth_headers, json=request)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["evidence"]["coverage"]["inventory"] == "unknown"
    equation = next(row for row in saved["evidence"]["reconciliation"] if row["account"] == A)
    assert Decimal(equation["opening"]) == Decimal("11")
    assert Decimal(equation["settled_change"]) == Decimal("0.98")
    assert Decimal(equation["closing"]) == Decimal("11.98")
    assert equation["status"] == "matched"
    assert equation["requested_interval_status"] == "unresolved"
    assert equation["scope"] == "observed_transaction_boundaries"
    assert len(saved["observations"]) == 2
    preview = await crosswalk.preview_evidence(session, test_workspace.id, group.id)
    assert len(preview.observations) == 2
    assert all(record.application_status == "not_applicable" for record in preview.records)
    prior_calls = len(calls)
    detail = await client.get(f"/api/onchain/history/{saved['collection_id']}", headers=auth_headers)
    exported = await client.get(f"/api/onchain/history/{saved['collection_id']}/export", headers=auth_headers)
    listing = await client.get("/api/onchain/history", headers=auth_headers)
    assert detail.status_code == exported.status_code == listing.status_code == 200
    assert len(calls) == prior_calls
    assert json.loads(exported.content)["evidence"] == saved["evidence"]
    assert exported.headers["cache-control"] == "no-store"
    assert "attachment" in exported.headers["content-disposition"]
    assert "evidence" not in listing.json()[0]
    assert listing.json()[0]["updated_at"].endswith("Z")
    repeated = await client.post("/api/onchain/history", headers=auth_headers, json={
        **saved["request"], "collection_id": saved["collection_id"], "expected_revision": saved["revision"],
    })
    assert repeated.status_code == 200, repeated.text
    assert await session.scalar(select(func.count()).select_from(InvestmentObservation)) == 2
    assert await session.scalar(select(func.count()).select_from(AssetTransaction)) == 0
    assert await session.scalar(select(func.count()).select_from(Asset)) == 0
    await session.refresh(account)
    assert account.balance == Decimal("19.25")


@pytest.mark.asyncio
async def test_history_authorization_and_explicit_ownership(
    client, auth_headers, viewer_auth_headers, session, test_user, test_workspace, history_context, monkeypatch,
):
    connection, _, _ = history_context
    rpc_fixture(monkeypatch)
    request = {"connection_id": str(connection.id), "chain": "solana", "address": A, "ownership_confirmed": True}
    assert (await client.post("/api/onchain/history", headers=viewer_auth_headers, json=request)).status_code == 403
    assert (await client.post("/api/onchain/history", headers=auth_headers, json={**request, "ownership_confirmed": False})).status_code == 422
    assert (await client.post("/api/onchain/history", headers=auth_headers, json={**request, "address": B})).status_code == 404
    assert (await client.post("/api/onchain/history", headers=auth_headers, json={**request, "chain": "base"})).status_code == 422
    assert (await client.post("/api/onchain/history", headers=auth_headers, json={**request, "since": "2025-01-01T00:00:00"})).status_code == 422
    saved = (await client.post("/api/onchain/history", headers=auth_headers, json=request)).json()
    other = Workspace(id=uuid.uuid4(), name="Other synthetic workspace", created_by_user_id=test_user.id)
    session.add(other)
    await session.commit()
    with pytest.raises(HTTPException) as wrong_workspace:
        await service.load_history(session, other.id, uuid.UUID(saved["collection_id"]))
    assert wrong_workspace.value.status_code == 404
    with pytest.raises(HTTPException) as wrong_connection:
        await service.collect_history(session, other.id, test_user.id, HistoryRequest.model_validate(request))
    assert wrong_connection.value.status_code == 404
    assert (await client.get(f"/api/onchain/history/{saved['collection_id']}/export", headers=viewer_auth_headers)).status_code == 200


@pytest.mark.asyncio
async def test_reobserved_versions_remain_retained_but_superseded_interpretations_cannot_settle(
    session, test_workspace, test_user, history_context, monkeypatch,
):
    connection, _, group = history_context
    _, payloads = rpc_fixture(monkeypatch)
    request = HistoryRequest(connection_id=connection.id, address=A, ownership_confirmed=True)
    saved = await service.collect_history(session, test_workspace.id, test_user.id, request)
    payloads["synthetic-send"]["meta"]["postBalances"][0] -= 1
    refreshed = await service.collect_history(session, test_workspace.id, test_user.id, HistoryRequest(
        **saved.request, collection_id=saved.collection_id, expected_revision=saved.revision, reobserve=True,
    ))
    assert refreshed.evidence["transactions"]["solana:synthetic-send"]["canonical_version"] is None
    rows = list(await session.scalars(select(InvestmentObservation)))
    old_send = next(row for row in rows if row.payload["order_ref"] == "solana:synthetic-send")
    assert old_send.is_current is False
    assert old_send.payload["settlement_status"] == "settled"  # Source fact remains immutable.
    qualified = crosswalk._input(old_send)
    assert qualified.settlement_status == "unknown"
    assert "superseded_history_observation" in crosswalk._conflicts(qualified, qualified.legs[0])
    preview = await crosswalk.preview_evidence(session, test_workspace.id, group.id)
    record = next(item for item in preview.records if item.observation_ref == str(old_send.id))
    assert record.match_status == "conflicting"
    assert "superseded_history_observation" in record.conflicting_fields


def test_projection_zero_101_legs_and_exact_unknown_quantities():
    payload = native_transaction()
    digest = "synthetic-payload"
    version = collector.decode_solana_transaction("synthetic-send", payload, payload_digest=digest, owner=A, confirmation_status="finalized", anchor_slot=200)
    base = version["legs"][-1]
    version["legs"] = [{**base, "key": f"instruction:{index}", "quantity": "0.12345678901234567890123456789012345678"} for index in range(101)]
    archive = {"owner": A, "transactions": {"solana:synthetic-send": {"signature": "synthetic-send", "versions": [version], "canonical_version": version["version_id"]}}, "payloads": {}}
    segments = service.project_observations(archive, uuid.uuid4())
    assert [len(item.legs) for item in segments] == [100, 1]
    assert sum(len(item.legs) for item in segments) == 101
    assert str(segments[0].legs[0].quantity) == "0.12345678901234567890123456789012345678"
    version["legs"] = [{**base, "key": "token-fee", "role": "token_transfer_fee"}]
    assert service.project_observations(archive, uuid.uuid4())[0].legs[0].classification == "fee"
    version["legs"] = []
    assert service.project_observations(archive, uuid.uuid4()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("opposite_order", [False, True])
async def test_overlapping_collections_cannot_resurrect_contradicted_source(
    session, test_workspace, test_user, history_context, monkeypatch, opposite_order,
):
    connection, _, group = history_context
    _, payloads = rpc_fixture(monkeypatch)
    if opposite_order:
        payloads["synthetic-send"]["meta"]["postBalances"][0] -= 1
    request = HistoryRequest(connection_id=connection.id, address=A, ownership_confirmed=True)
    first = await service.collect_history(session, test_workspace.id, test_user.id, request)
    payloads["synthetic-send"]["meta"]["postBalances"][0] += 1 if opposite_order else -1
    second = await service.collect_history(session, test_workspace.id, test_user.id, request)
    assert second.evidence["transactions"]["solana:synthetic-send"]["canonical_version"] is None
    reopened = await service.read_history(session, test_workspace.id, first.collection_id)
    assert reopened.evidence["transactions"]["solana:synthetic-send"]["canonical_version"] is None
    assert "cross_collection_conflict" in reopened.evidence["gaps"]
    repeated = await service.collect_history(session, test_workspace.id, test_user.id, HistoryRequest(
        **first.request, collection_id=first.collection_id, expected_revision=first.revision,
    ))
    assert repeated.evidence["transactions"]["solana:synthetic-send"]["canonical_version"] is None
    rows = list(await session.scalars(select(InvestmentObservation).where(InvestmentObservation.group_id == group.id)))
    assert all(not row.is_current for row in rows if row.payload["order_ref"] == "solana:synthetic-send")


@pytest_asyncio.fixture
async def history_pg_context(postgres_sessions):
    async with postgres_sessions() as session:
        user = User(email="synthetic-history@example.invalid", hashed_password="unused-synthetic")
        session.add(user)
        await session.flush()
        workspace = Workspace(name="Synthetic history", created_by_user_id=user.id)
        session.add(workspace)
        await session.flush()
        connection, _, _ = await connected_context(session, workspace.id, user.id)
        return postgres_sessions, workspace.id, user.id, connection.id


@pytest.mark.asyncio
async def test_postgres_concurrent_resume_and_quota_reservation(history_pg_context, monkeypatch):
    sessions, workspace_id, user_id, connection_id = history_pg_context
    rpc_fixture(monkeypatch)
    async with sessions() as session:
        saved = await service.collect_history(session, workspace_id, user_id, HistoryRequest(connection_id=connection_id, address=A, ownership_confirmed=True))
    original = collector.collect_solana_history

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        result["synthetic_resume_marker"] = "changed"
        return result

    monkeypatch.setattr(collector, "collect_solana_history", changed)
    async def resume():
        async with sessions() as session:
            try:
                return await service.collect_history(session, workspace_id, user_id, HistoryRequest(
                    **saved.request, collection_id=saved.collection_id, expected_revision=saved.revision,
                ))
            except HTTPException as exc:
                return exc.status_code

    results = await asyncio.gather(resume(), resume())
    assert sum(result == 409 for result in results) == 1
    async with sessions() as session:
        row = await service.load_history(session, workspace_id, saved.collection_id)
        initial_size = row.size_bytes
    monkeypatch.setattr(service, "MAX_WORKSPACE_BYTES", service.MAX_COLLECTION_BYTES + initial_size + 1)

    async def new_collection():
        async with sessions() as session:
            try:
                return await service.collect_history(session, workspace_id, user_id, HistoryRequest(connection_id=connection_id, address=A, ownership_confirmed=True))
            except HTTPException as exc:
                return exc.status_code

    quota_results = await asyncio.gather(new_collection(), new_collection())
    assert sum(result in (409, 413) for result in quota_results) == 1
    if 409 in quota_results:
        assert await new_collection() == 413
    async with sessions() as session:
        row = await service.load_history(session, workspace_id, saved.collection_id)
        row.size_bytes = service.MAX_WORKSPACE_BYTES - service.MAX_COLLECTION_BYTES + 1
        await session.commit()
    async with sessions() as session:
        with pytest.raises(HTTPException) as full:
            await service.collect_history(session, workspace_id, user_id, HistoryRequest(connection_id=connection_id, address=A, ownership_confirmed=True))
        assert full.value.status_code == 413


@pytest.mark.asyncio
async def test_export_keeps_rpc_u64_and_changed_source_requires_restart(
    client, auth_headers, session, test_workspace, test_user, history_context, monkeypatch,
):
    connection, _, _ = history_context
    rpc_fixture(monkeypatch)
    saved = await service.collect_history(session, test_workspace.id, test_user.id, HistoryRequest(connection_id=connection.id, address=A, ownership_confirmed=True))
    row = await service.load_history(session, test_workspace.id, saved.collection_id)
    archive = copy.deepcopy(row.payload)
    archive["synthetic_integer"] = 18446744073709551615
    row.payload = archive
    await session.commit()
    exported = await client.get(f"/api/onchain/history/{saved.collection_id}/export", headers=auth_headers)
    assert b"18446744073709551615" in exported.content
    assert json.loads(exported.content)["evidence"]["synthetic_integer"] == 18446744073709551615
    monkeypatch.setattr(service, "rpc_url", lambda _: "https://synthetic.invalid/private-secret")
    with pytest.raises(HTTPException) as incompatible:
        await service.collect_history(session, test_workspace.id, test_user.id, HistoryRequest(
            **saved.request, collection_id=saved.collection_id, expected_revision=saved.revision,
        ))
    assert incompatible.value.status_code == 409
    assert "private-secret" not in str(incompatible.value.detail)


@pytest.mark.asyncio
async def test_high_decimal_token_keeps_raw_archive_and_qualifies_crosswalk_projection(
    client, auth_headers, session, history_context, monkeypatch,
):
    connection, _, _ = history_context
    _, payloads = rpc_fixture(monkeypatch)
    tiny = payloads["synthetic-send"]
    tiny["transaction"]["message"]["accountKeys"].extend([{"pubkey": "token-A"}, {"pubkey": "token-B"}])
    tiny["transaction"]["message"]["instructions"].append({"programId": collector.onchain.SOLANA_TOKEN_PROGRAMS[0], "parsed": {
        "type": "transferChecked", "info": {"source": "token-A", "destination": "token-B", "mint": "tiny-mint",
                                              "tokenAmount": {"amount": "1", "decimals": 200}},
    }})
    tiny["meta"]["preBalances"].extend([0, 0])
    tiny["meta"]["postBalances"].extend([0, 0])
    for field, amounts in (("preTokenBalances", ("1", "0")), ("postTokenBalances", ("0", "1"))):
        tiny["meta"][field] = [{"accountIndex": index, "mint": "tiny-mint", "owner": owner,
                                "programId": collector.onchain.SOLANA_TOKEN_PROGRAMS[0],
                                "uiTokenAmount": {"amount": amount, "decimals": 200}}
                               for index, owner, amount in zip((2, 3), (A, "external"), amounts)]
    response = await client.post("/api/onchain/history", headers=auth_headers, json={
        "connection_id": str(connection.id), "chain": "solana", "address": A, "ownership_confirmed": True,
    })
    assert response.status_code == 200, response.text
    saved = response.json()
    assert "crosswalk_quantity_out_of_range" in saved["evidence"]["crosswalk_gaps"]
    assert "crosswalk_quantity_out_of_range" in saved["evidence"]["gaps"]
    source = next(item for item in saved["observations"] if item["order_ref"] == "solana:synthetic-send")
    assert next(leg for leg in source["legs"] if leg["token_address"] == "tiny-mint")["quantity"] is None
    archived = saved["evidence"]["transactions"]["solana:synthetic-send"]["versions"][0]
    assert Decimal(next(leg["quantity"] for leg in archived["legs"] if leg["asset"]["mint"] == "tiny-mint")) == Decimal("1e-200")
    assert await session.scalar(select(func.count()).select_from(InvestmentObservation)) == 2
    assert await session.scalar(select(func.count()).select_from(AssetTransaction)) == 0
    detail = await client.get(f"/api/onchain/history/{saved['collection_id']}", headers=auth_headers)
    exported = await client.get(f"/api/onchain/history/{saved['collection_id']}/export", headers=auth_headers)
    assert detail.status_code == exported.status_code == 200
    assert json.loads(exported.content)["evidence"]["payloads"] == saved["evidence"]["payloads"]


@pytest.mark.asyncio
async def test_missing_group_retains_unmapped_crosswalk_without_financial_mapping(
    session, test_workspace, test_user, history_context, monkeypatch,
):
    connection, account, group = history_context
    await session.delete(group)
    await session.commit()
    rpc_fixture(monkeypatch)
    request = HistoryRequest(connection_id=connection.id, address=A, ownership_confirmed=True)
    saved = await service.collect_history(session, test_workspace.id, test_user.id, request)
    assert saved.group_id is None
    assert "crosswalk_account_mapping_unavailable" in saved.evidence["crosswalk_gaps"]
    assert "crosswalk_account_mapping_unavailable" in saved.evidence["gaps"]
    rows = list(await session.scalars(select(InvestmentObservation)))
    assert len(rows) == 2
    assert all(row.group_id is None and row.connection_id == connection.id for row in rows)
    await service.collect_history(session, test_workspace.id, test_user.id, HistoryRequest(
        **saved.request, collection_id=saved.collection_id, expected_revision=saved.revision,
    ))
    assert await session.scalar(select(func.count()).select_from(InvestmentObservation)) == 2
    assert await session.scalar(select(func.count()).select_from(AssetGroup)) == 0
    assert await session.scalar(select(func.count()).select_from(AssetTransaction)) == 0
    await session.refresh(account)
    assert account.balance == Decimal("19.25")


@pytest.mark.asyncio
async def test_summary_listing_hashes_payloads_once_without_reconciling_raw_archives(monkeypatch):
    rows = []
    for collection in range(100):
        archive = {"chain": "solana", "owner": A, "coverage": {"interpretation": "complete", "settlement": "complete"},
                   "transactions": {}, "payloads": {}}
        for transaction in range(100):
            digest = f"payload-{collection}-{transaction}"
            archive["transactions"][f"solana:{transaction}"] = {"versions": [{"payload_digest": digest}]}
            archive["payloads"][digest] = {"response": {"result": {"transaction": transaction, "log": "x" * 9000}}}
        if collection == 99:
            archive["transactions"]["solana:0"]["revision_status"] = "conflicting_or_reorganized"
        rows.append(SimpleNamespace(id=uuid.uuid4(), connection_id="synthetic-connection", revision="revision", request={},
                                    payload=archive, updated_at=datetime.now(timezone.utc)))
    session = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))
    serialized = 0
    original = service._json_bytes

    def count(value):
        nonlocal serialized
        serialized += 1
        return original(value)

    def no_reconciliation(*args):
        raise AssertionError("summary listing must not reconstruct transaction reconciliation")

    monkeypatch.setattr(service, "_json_bytes", count)
    monkeypatch.setattr(service, "reconcile_history", no_reconciliation)
    result = await service.list_history(session, uuid.uuid4())
    assert len(result) == 100
    assert serialized == 100 * 100  # Every payload once, not once for every summary.
    assert all(row.coverage["settlement"] == "partial" for row in result)


@pytest.mark.asyncio
@pytest.mark.parametrize("locked_model", [Workspace, BankConnection, AssetGroup])
async def test_postgres_busy_mapping_locks_do_not_wait_or_start_rpc(history_pg_context, monkeypatch, locked_model):
    sessions, workspace_id, user_id, connection_id = history_pg_context
    calls, _ = rpc_fixture(monkeypatch)
    async with sessions() as holder:
        predicate = locked_model.id == workspace_id if locked_model is Workspace else (
            locked_model.id == connection_id if locked_model is BankConnection else locked_model.connection_id == connection_id
        )
        await holder.scalar(select(locked_model).where(predicate).with_for_update())
        async with sessions() as contender:
            with pytest.raises(HTTPException) as busy:
                await asyncio.wait_for(service.collect_history(contender, workspace_id, user_id, HistoryRequest(
                    connection_id=connection_id, address=A, ownership_confirmed=True,
                )), timeout=2)
            assert busy.value.status_code == 409
            assert isinstance(busy.value.detail, dict)
            assert busy.value.detail["code"] == "history_busy"
        assert calls == []
