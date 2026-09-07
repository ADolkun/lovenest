"""Trace deadlines and cancellation, with synthetic transport/evidence only."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.providers import onchain
from app.services import onchain_trace
from tests.test_providers_onchain import (
    A, B, BTC_A, EVM, JAN23, _patched_client, _settings, _sig, _solana_handler, _tx,
)

pytestmark = pytest.mark.asyncio


async def test_transfer_evidence_is_read_before_optional_balances():
    calls = []
    serve = _solana_handler(
        signatures={A: [_sig("synthetic-move", JAN23)]},
        txs={"synthetic-move": _tx(JAN23, {A: -10**9, B: 10**9})},
    )

    def handler(request):
        calls.append(json.loads(request.content)["method"])
        return serve(request)

    with _settings(), _patched_client(handler):
        result = await onchain_trace.trace("solana", A, max_hops=1)
    assert len(result.edges) == 1
    assert calls[:2] == ["getSignaturesForAddress", "getTransaction"]
    assert calls[2:] == ["getBalance", "getBalance"]


async def test_a_payload_returning_after_the_deadline_cannot_finish_a_complete_trace():
    now = [time.monotonic()]
    calls = []
    serve = _solana_handler(
        signatures={A: [_sig("synthetic-late", JAN23)]},
        txs={"synthetic-late": _tx(JAN23, {A: -10**9, B: 10**9})},
    )

    def handler(request):
        method = json.loads(request.content)["method"]
        calls.append((method, now[0]))
        if method == "getTransaction":
            now[0] += 120
        return serve(request)

    with (
        _settings(), _patched_client(handler),
        patch.object(onchain_trace, "time", SimpleNamespace(monotonic=lambda: now[0])),
    ):
        result = await onchain_trace.trace("solana", A, max_hops=1)
    assert result.truncated
    assert not any(method == "getBalance" and at >= calls[0][1] + 45 for method, at in calls)


@pytest.mark.parametrize("stage", ["getSignaturesForAddress", "getTransaction", "getBalance"])
async def test_blocked_reads_stop_at_the_deadline_and_leave_no_payload_tasks(stage):
    pending = 0
    cancelled = 0
    calls = []
    serve = _solana_handler(
        signatures={A: [_sig("synthetic-blocked", JAN23)]},
        txs={"synthetic-blocked": _tx(JAN23, {A: -10**9, B: 10**9})},
    )

    async def handler(request):
        nonlocal pending, cancelled
        method = json.loads(request.content)["method"]
        calls.append(method)
        if method != stage:
            return serve(request)
        pending += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise
        finally:
            pending -= 1

    with (
        _settings(), _patched_client(handler),
        patch.object(onchain_trace, "TIME_BUDGET_SECONDS", 0.04),
    ):
        result = await asyncio.wait_for(onchain_trace.trace("solana", A, max_hops=2), 0.5)
    assert cancelled == 1
    assert pending == 0
    assert result.interruption is not None
    assert result.interruption.code == "deadline_exceeded"
    assert result.interruption.phase == ("balances" if stage == "getBalance" else "history")
    assert result.truncated is (stage != "getBalance")
    assert sum(method == "getBalance" for method in calls) <= 1
    if stage == "getBalance":
        assert len(result.edges) == 1
        assert all(node.balance is None for node in result.nodes)
    else:
        assert not result.edges


async def test_failed_optional_balances_stay_unknown_after_successful_history():
    serve = _solana_handler(
        signatures={A: [_sig("synthetic-good", JAN23)]},
        txs={"synthetic-good": _tx(JAN23, {A: -10**9, B: 10**9})},
    )

    def handler(request):
        if json.loads(request.content)["method"] == "getBalance":
            return httpx.Response(503)
        return serve(request)

    with _settings(), _patched_client(handler):
        # Read the child too so this isolates optional balance failure from a hop cap.
        result = await onchain_trace.trace("solana", A, max_hops=2)
    assert len(result.edges) == 1
    assert not result.truncated
    assert all(node.balance is None for node in result.nodes)
    ids = {node.id for node in result.nodes}
    assert all(edge.source in ids and edge.target in ids for edge in result.edges)


async def test_one_failed_payload_cancels_and_drains_its_siblings():
    sibling_started = asyncio.Event()
    release = asyncio.Event()
    pending = set()

    async def handler(request):
        body = json.loads(request.content)
        if body["method"] == "getSignaturesForAddress":
            return httpx.Response(200, json={"result": [
                _sig("synthetic-failure", JAN23), _sig("synthetic-sibling", JAN23),
            ]})
        if body["params"][0] == "synthetic-failure":
            await sibling_started.wait()
            return httpx.Response(400)
        task = asyncio.current_task()
        pending.add(task)
        sibling_started.set()
        try:
            await release.wait()
            return httpx.Response(200, json={"result": None})
        finally:
            pending.remove(task)

    with _settings(), _patched_client(handler):
        try:
            with pytest.raises(RuntimeError):
                await onchain.transfers(onchain.CHAINS["solana"], A, limit=25)
            unfinished = len(pending)
        finally:
            # Clean up even when exercising a regression that leaks siblings.
            release.set()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
    assert unfinished == 0


async def test_external_cancellation_propagates_and_cleans_up_all_payloads():
    started = asyncio.Event()
    pending = 0

    async def handler(request):
        nonlocal pending
        method = json.loads(request.content)["method"]
        if method == "getSignaturesForAddress":
            return httpx.Response(200, json={"result": [
                _sig("synthetic-one", JAN23), _sig("synthetic-two", JAN23),
            ]})
        if method == "getBalance":
            return httpx.Response(200, json={"result": {"value": 0}})
        pending += 1
        if pending == 2:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            pending -= 1

    with _settings(), _patched_client(handler):
        task = asyncio.create_task(onchain_trace.trace("solana", A))
        try:
            await asyncio.wait_for(started.wait(), 1)
        finally:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert pending == 0


async def test_retry_sleep_crossing_the_trace_deadline_is_cancelled():
    from app.providers import onchain_transport as rpc
    from tests.test_onchain_rpc import FakeClock, FakeCoordination

    cancelled = 0
    attempts = []

    async def blocked_sleep(seconds):
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise

    def handler(request):
        attempts.append(json.loads(request.content)["method"])
        return httpx.Response(429)

    coordinator = FakeCoordination(FakeClock())
    with (
        _settings(), _patched_client(handler),
        patch.object(rpc, "_redis_client", lambda: coordinator),
        patch.object(rpc, "asyncio", SimpleNamespace(timeout=asyncio.timeout, sleep=blocked_sleep)),
        patch.object(onchain, "RPC_RETRY_BACKOFF_SECONDS", 0.001),
        patch.object(onchain_trace, "TIME_BUDGET_SECONDS", 0.04),
    ):
        result = await asyncio.wait_for(onchain_trace.trace("solana", A), 0.5)
    assert attempts == ["getSignaturesForAddress"]
    assert cancelled == 1
    assert result.truncated
    assert result.interruption is not None
    assert result.interruption.code == "deadline_exceeded"


@pytest.mark.parametrize("chain,address,explorer_key", [
    ("base", EVM, ""), ("base", EVM, "synthetic-key"), ("bitcoin", BTC_A, ""),
])
async def test_non_rpc_history_sources_share_the_trace_deadline(chain, address, explorer_key):
    cancelled = 0

    async def handler(request):
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise

    with (
        _settings(etherscan_api_key=explorer_key), _patched_client(handler),
        patch.object(onchain_trace, "TIME_BUDGET_SECONDS", 0.04),
    ):
        result = await asyncio.wait_for(onchain_trace.trace(chain, address), 0.5)
    assert cancelled == 1
    assert result.truncated
    assert not result.edges
    assert result.interruption is not None
    assert result.interruption.phase == "history"
