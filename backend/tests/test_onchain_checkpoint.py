"""Bounded private trace snapshots and explicit recovery; synthetic evidence only."""

import json
import os
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import redis.asyncio as redis
from redis.exceptions import RedisError

from app.services import onchain_checkpoint as checkpoints
from app.services import onchain_trace
from app.providers import onchain_reads
from tests.test_onchain_rpc import isolated_redis  # noqa: F401
from tests.test_providers_onchain import (
    A, B, C, EVM, JAN23, _patched_client, _settings, _sig, _solana_handler, _tx,
)

pytestmark = pytest.mark.asyncio


class MemoryRedis:
    def __init__(self):
        self.data = {}
        self.order = {}

    async def eval(self, script, numkeys, key, index, raw, ttl, now, count, index_ttl):
        self.data[key] = raw
        order = self.order.setdefault(index, [])
        order.append(key)
        while len(order) > int(count):
            self.data.pop(order.pop(0), None)
        return 1

    async def get(self, key):
        return self.data.get(key)


@pytest.fixture
def checkpoint_store(monkeypatch):
    store = MemoryRedis()
    monkeypatch.setattr(checkpoints.redis_store, "get_redis", AsyncMock(return_value=store))
    return store


def snapshot(workspace_id="synthetic-workspace"):
    started = datetime.now(timezone.utc)
    return {
        "version": checkpoints.VERSION, "workspace_id": workspace_id,
        "assumptions": checkpoints.assumptions("solana"),
        "request": {"chain": "solana"}, "state": {}, "result": {},
        "started_at": started.isoformat(),
        "expires_at": (started + timedelta(seconds=checkpoints.TTL_SECONDS)).isoformat(),
    }


async def test_checkpoint_checks_workspace_expiry_decoder_and_size(checkpoint_store):
    body = snapshot()
    token = checkpoints.new_token()
    await checkpoints.save(body["workspace_id"], token, body)
    assert await checkpoints.load(body["workspace_id"], token) == body
    for workspace, candidate in [("another-workspace", token), (body["workspace_id"], "bad-token")]:
        with pytest.raises(checkpoints.CheckpointError, match="missing_or_expired"):
            await checkpoints.load(workspace, candidate)
    body["version"] += 1
    await checkpoints.save(body["workspace_id"], token, body)
    with pytest.raises(checkpoints.CheckpointError, match="incompatible"):
        await checkpoints.load(body["workspace_id"], token)
    body["version"] = checkpoints.VERSION
    body["assumptions"] = "different-decoder-or-finality"
    await checkpoints.save(body["workspace_id"], token, body)
    with pytest.raises(checkpoints.CheckpointError, match="incompatible"):
        await checkpoints.load(body["workspace_id"], token)
    body["expires_at"] = "2000-01-01T00:00:00+00:00"
    checkpoint_store.data[checkpoints._key(body["workspace_id"], token)] = json.dumps(body)
    with pytest.raises(checkpoints.CheckpointError, match="missing_or_expired"):
        await checkpoints.load(body["workspace_id"], token)
    with pytest.raises(checkpoints.CheckpointError, match="missing_or_expired"):
        await checkpoints.save(body["workspace_id"], token, body)
    body = snapshot()
    body["state"] = {"too_large": "x" * checkpoints.MAX_BYTES}
    with pytest.raises(checkpoints.CheckpointError, match="storage_limit"):
        await checkpoints.save(body["workspace_id"], token, body)


@pytest.mark.usefixtures("isolated_redis")
async def test_real_redis_same_expiry_keeps_newest_and_bounds_retention(monkeypatch):
    client = redis.from_url(os.environ["REDIS_TEST_URL"], decode_responses=True)
    monkeypatch.setattr(checkpoints.redis_store, "get_redis", AsyncMock(return_value=client))
    workspace = str(uuid.uuid4())
    body = snapshot(workspace)
    # Reverse lexical order would evict the newest token with expiry scoring.
    tokens = [letter * 43 for letter in "zyxwvuts"]
    try:
        for token in tokens:
            await checkpoints.save(workspace, token, body)
            assert await checkpoints.load(workspace, token) == body
        index = checkpoints._key(workspace, "index")
        assert await client.zcard(index) == checkpoints.MAX_PER_WORKSPACE
        assert 0 < await client.ttl(index) <= checkpoints.TTL_SECONDS
        assert all([await client.exists(checkpoints._key(workspace, token)) for token in tokens[-5:]])
        with pytest.raises(checkpoints.CheckpointError, match="missing_or_expired"):
            await checkpoints.load(workspace, tokens[0])
    finally:
        await client.delete(checkpoints._key(workspace, "index"), *(checkpoints._key(workspace, token) for token in tokens))
        await client.aclose()


@pytest.mark.parametrize(("settings", "canonical_settings"), [
    ({}, {}),
    ({"min_amount": None, "since": None, "until": None}, {}),
    (
        {"min_amount": "0.00", "since": "2025-01-24T00:00:00+02:00"},
        {"min_amount": "0.00", "since": "2025-01-23T22:00:00Z"},
    ),
    (
        {"min_amount": "0.12345678901234567890123456789", "until": "2025-01-24T01:00:00"},
        {"min_amount": "0.12345678901234567890123456789", "until": "2025-01-24T01:00:00Z"},
    ),
])
async def test_api_continue_reuses_reads_preserves_branches_and_observation_times(
    client, auth_headers, checkpoint_store, settings, canonical_settings,
):
    recovered = False
    calls = []
    good = _tx(JAN23, {A: -10**9, B: 10**9})
    larger = _tx(JAN23 + 1, {A: -9 * 10**9, C: 9 * 10**9})
    signatures = {A: [_sig("late-larger", JAN23 + 1), _sig("good", JAN23)], B: [_sig("good", JAN23)]}

    def handler(request):
        body = json.loads(request.content)
        calls.append((body["method"], body["params"][0]))
        return _solana_handler(balances={B: None}, signatures=signatures, txs={
            "good": good, **({"late-larger": larger} if recovered else {}),
        })(request)

    request = {"chain": " SOLANA ", "address": f" {A} ", "max_hops": 2, "max_branches": 1, **settings}
    canonical = {
        "chain": "solana", "address": A, "direction": "out", "max_hops": 2,
        "max_branches": 1, **canonical_settings,
    }
    with _settings(), _patched_client(handler):
        first = await client.post("/api/onchain/trace", headers=auth_headers, json=request)
        assert first.status_code == 200, first.text
        initial = first.json()
        assert initial["request"] == canonical
        assert initial["nodes"][0]["balance"] == "0E-9"
        assert initial["nodes"][1]["balance"] is None
        assert initial["nodes"][0]["coverage"]["next_cursor"] is None
        assert [edge["reference"] for edge in initial["edges"]] == ["good"]
        assert initial["continuation"]["status"] == "available"
        assert calls.count(("getTransaction", "good")) == 1
        token = initial["continuation"]["token"]
        key = checkpoints._key(initial["workspace_id"], token)
        saved = json.loads(checkpoint_store.data[key])
        assert saved["result"]["request"] == canonical
        # Even a legacy snapshot containing a token must not echo it in settings.
        saved["result"]["request"]["continuation_token"] = "synthetic-private-token"
        checkpoint_store.data[key] = json.dumps(saved)
        headers = {**auth_headers, "X-Workspace-Id": initial["workspace_id"], "X-Trace-Continuation": token}
        calls.clear()
        restored = await client.get("/api/onchain/trace/checkpoint", headers=headers)
        assert restored.json() == initial
        assert not calls
        recovered = True
        continued = await client.post("/api/onchain/trace", headers=headers, json={
            **request, "chain": "solana", "address": A, "continuation_token": token,
        })
        assert continued.status_code == 200, continued.text
        latest = continued.json()
        assert latest["request"] == canonical
        assert calls == [("getTransaction", "late-larger"), ("getBalance", B)]
        assert latest["nodes"][0]["balance"] == "0E-9"
        assert latest["nodes"][1]["balance"] is None
        assert latest["nodes"][0]["coverage"]["next_cursor"] is None
        assert latest["edges"] == initial["edges"]
        assert latest["nodes"][0]["branch_omitted_transfers"] == 1
        assert latest["continuation"]["status"] == "not_needed"
        assert not latest["complete"]  # The fixed branch bound is still a gap.
        assert latest["continuation"]["token"] != token
        assert latest["continuation"]["expires_at"] == initial["continuation"]["expires_at"]
        assert latest["started_at"] == initial["started_at"]
        assert [node["balance_observed_at"] for node in latest["nodes"]] == [node["balance_observed_at"] for node in initial["nodes"]]
        assert latest["nodes"][0]["coverage"]["fetched_at"] == initial["nodes"][0]["coverage"]["fetched_at"]
        assert (await client.get("/api/onchain/trace/checkpoint", headers=headers)).json() == initial


@pytest.mark.parametrize("bad", [
    pytest.param(None, id="null"),
    pytest.param({"status": "0", "message": "NOTOK", "result": "synthetic unsupported history"}, id="notok"),
])
@pytest.mark.parametrize("movement", [True, False])
async def test_api_failed_internal_stream_keeps_completed_history_and_retries_only_failure(
    client, auth_headers, checkpoint_store, bad, movement,
):
    calls = Counter()
    recovered = False
    recipient = "0x" + "cd" * 20
    row = {
        "hash": "synthetic-retained-evm", "timeStamp": str(JAN23), "value": str(10**18),
        "from": EVM, "to": recipient, "isError": "0",
    }

    def handler(request):
        action = request.url.params.get("action")
        calls[action] += 1
        if action == "txlist":
            return httpx.Response(200, json={"status": "1", "result": [row] if movement else []})
        if action == "txlistinternal":
            return httpx.Response(200, content=json.dumps(
                {"status": "1", "result": []} if recovered else bad,
            ), headers={"Content-Type": "application/json"})
        return httpx.Response(200, json={"result": "0x0"})

    request = {"chain": "base", "address": EVM, "max_hops": 1}
    with _settings(etherscan_api_key="synthetic-key"), _patched_client(handler):
        response = await client.post("/api/onchain/trace", headers=auth_headers, json=request)
        assert response.status_code == 200, response.text
        initial = response.json()
        assert [edge["reference"] for edge in initial["edges"]] == ([row["hash"]] if movement else [])
        root = initial["nodes"][0]
        assert root["coverage"]["pages_read"] == 1
        assert root["coverage"]["rows_read"] == int(movement)
        assert root["coverage"]["provider_exhausted"] is False
        assert "provider_unavailable" in root["stop_reasons"]
        assert not initial["complete"] and initial["continuation"]["status"] == "available"
        assert calls["txlist"] == calls["txlistinternal"] == 1
        token = initial["continuation"]["token"]
        headers = {**auth_headers, "X-Trace-Continuation": token}
        previous_calls = calls.copy()
        reopened = await client.get("/api/onchain/trace/checkpoint", headers=headers)
        assert reopened.status_code == 200 and reopened.json() == initial
        # This saved JSON is also the evidence serialized by Download.
        assert calls == previous_calls
        recovered = True
        continued = await client.post("/api/onchain/trace", headers=auth_headers, json={
            **request, "continuation_token": token,
        })
        assert continued.status_code == 200, continued.text
        latest = continued.json()
        assert latest["edges"] == initial["edges"]
        assert latest["nodes"][0]["coverage"]["provider_exhausted"] is True
        assert latest["nodes"][0]["coverage"]["pages_read"] == 2
        assert latest["nodes"][0]["coverage"]["fetched_at"] == root["coverage"]["fetched_at"]
        assert "provider_unavailable" not in latest["nodes"][0]["stop_reasons"]
        assert latest["continuation"]["status"] == "not_needed"
        assert calls["txlist"] == 1 and calls["txlistinternal"] == 2
        assert (await client.get("/api/onchain/trace/checkpoint", headers=headers)).json() == initial


async def test_api_refuses_foreign_changed_expired_and_unavailable_checkpoints_without_rpc(
    client, auth_headers, other_workspace_headers, checkpoint_store,
):
    with _settings(), _patched_client(_solana_handler()):
        started = await client.post("/api/onchain/trace", headers=auth_headers, json={"chain": "solana", "address": A})
        body = started.json()
        token = body["continuation"]["token"]
        request = {"chain": "solana", "address": A, "continuation_token": token}
        with patch.object(onchain_trace, "trace", AsyncMock(side_effect=AssertionError("no RPC"))) as trace:
            foreign = await client.get("/api/onchain/trace/checkpoint", headers={**other_workspace_headers, "X-Trace-Continuation": token})
            assert foreign.status_code == 409
            assert foreign.json()["detail"]["reason"] == "missing_or_expired"
            other_post = await client.post("/api/onchain/trace", headers=other_workspace_headers, json=request)
            assert other_post.status_code == 409
            denied = await client.get("/api/onchain/trace/checkpoint", headers={**other_workspace_headers, "X-Workspace-Id": body["workspace_id"], "X-Trace-Continuation": token})
            assert denied.status_code == 404
            changed = await client.post("/api/onchain/trace", headers=auth_headers, json={**request, "max_hops": 6})
            assert changed.status_code == 409
            assert changed.json()["detail"]["reason"] == "incompatible"
            invalid = await client.post("/api/onchain/trace", headers=auth_headers, json={**request, "continuation_token": "not-a-token"})
            assert invalid.status_code == 409
            with patch.object(onchain_reads, "READ_STATE_VERSION", "incompatible-decoder"):
                decoder_get = await client.get("/api/onchain/trace/checkpoint", headers={**auth_headers, "X-Trace-Continuation": token})
                decoder_post = await client.post("/api/onchain/trace", headers=auth_headers, json=request)
                assert decoder_get.status_code == decoder_post.status_code == 409
                assert decoder_get.json()["detail"]["reason"] == "incompatible"
            key = checkpoints._key(body["workspace_id"], token)
            saved = json.loads(checkpoint_store.data[key])
            saved["expires_at"] = "2000-01-01T00:00:00Z"
            checkpoint_store.data[key] = json.dumps(saved)
            expired = await client.post("/api/onchain/trace", headers=auth_headers, json=request)
            assert expired.status_code == 409
            checkpoint_store.get = AsyncMock(side_effect=RedisError("private-error-sentinel"))
            unavailable = await client.get("/api/onchain/trace/checkpoint", headers={**auth_headers, "X-Trace-Continuation": token})
            assert unavailable.status_code == 503
            assert unavailable.json()["detail"]["code"] == "trace_checkpoint_unavailable"
            assert "sentinel" not in unavailable.text
            trace.assert_not_called()


async def test_cache_save_failure_keeps_downloadable_root_evidence(client, auth_headers, checkpoint_store):
    checkpoint_store.eval = AsyncMock(side_effect=RedisError("private-error-sentinel"))
    handler = _solana_handler(
        signatures={A: [_sig("retained", JAN23), _sig("missing", JAN23)]},
        txs={"retained": _tx(JAN23, {A: -10**9, B: 10**9})},
    )
    with _settings(), _patched_client(handler):
        response = await client.post("/api/onchain/trace", headers=auth_headers, json={"chain": "solana", "address": A})
    assert response.status_code == 200
    body = response.json()
    assert len(body["edges"]) == 1 and not body["complete"]
    assert body["continuation"] == {"status": "unavailable", "token": None, "expires_at": None, "reason": "storage_unavailable"}
    assert "sentinel" not in response.text


async def test_reopen_rechecks_membership_and_never_trusts_saved_access(
    client, auth_headers, session, test_workspace, test_user, checkpoint_store,
):
    from sqlalchemy import delete
    from app.models.workspace import WorkspaceMember

    headers = {**auth_headers, "X-Workspace-Id": str(test_workspace.id)}
    with _settings(), _patched_client(_solana_handler()):
        started = await client.post("/api/onchain/trace", headers=headers, json={"chain": "solana", "address": A})
        assert started.headers["cache-control"] == "no-store"
        token = started.json()["continuation"]["token"]
        await session.execute(delete(WorkspaceMember).where(
            WorkspaceMember.workspace_id == test_workspace.id,
            WorkspaceMember.user_id == test_user.id,
        ))
        await session.commit()
        with patch.object(onchain_trace, "trace", AsyncMock(side_effect=AssertionError("no RPC"))) as trace:
            response = await client.get("/api/onchain/trace/checkpoint", headers={**headers, "X-Trace-Continuation": token})
            assert response.status_code == 404
            response = await client.post("/api/onchain/trace", headers=headers, json={
                "chain": "solana", "address": A, "continuation_token": token,
            })
            assert response.status_code == 404
            trace.assert_not_called()


async def test_retention_cap_offers_restart_and_never_continues_the_limited_state(
    client, auth_headers, checkpoint_store,
):
    from app.providers import onchain_reads

    handler = _solana_handler(
        signatures={A: [_sig("retained", JAN23)]},
        txs={"retained": _tx(JAN23, {A: -10**9, B: 10**9})},
    )
    with _settings(), _patched_client(handler), patch.object(onchain_reads, "MAX_READ_BYTES", 1):
        response = await client.post("/api/onchain/trace", headers=auth_headers, json={"chain": "solana", "address": A})
        assert response.status_code == 200
        result = response.json()
        assert result["continuation"]["status"] == "unavailable"
        assert result["continuation"]["reason"] == "retention_limit"
        assert not result["complete"]
        token = result["continuation"]["token"]
        with patch.object(onchain_trace, "trace", AsyncMock(side_effect=AssertionError("no RPC"))) as trace:
            continued = await client.post("/api/onchain/trace", headers=auth_headers, json={
                "chain": "solana", "address": A, "continuation_token": token,
            })
            assert continued.status_code == 409
            reopened = await client.get("/api/onchain/trace/checkpoint", headers={**auth_headers, "X-Trace-Continuation": token})
            assert reopened.status_code == 200 and reopened.json() == result
            trace.assert_not_called()
