"""Actual trace frontier recovery and finality-aware retained synthetic reads."""

import asyncio
import json
from dataclasses import asdict
from unittest.mock import patch

import httpx
import pytest
from pydantic import TypeAdapter

from app.providers import onchain
from app.services import onchain_trace as trace
from tests.test_onchain_trace import history, moment
from tests.test_providers_onchain import (
    A, B, C, D, JAN23, _patched_client, _settings, _sig, _solana_handler, _tx,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("direction,moves", [
    ("out", [("direct", A, B, 20, 10), ("indirect", A, C, 5, 9),
             ("converge", C, B, 10, 8), ("previously-unread", B, D, 15, 7)]),
    ("in", [("direct", B, A, 10, 10), ("indirect", C, A, 25, 9),
            ("converge", B, C, 20, 8), ("previously-unread", D, B, 15, 7)]),
])
async def test_continue_reads_converging_window_without_dropping_edges_or_duplicate_payloads(direction, moves):
    serve = history(moves)
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((body["method"], body["params"][0]))
        return serve(request)

    state = trace.TraceState()
    with _settings(), _patched_client(handler):
        result = await trace.trace("solana", A, direction=direction, max_hops=4, state=state)
        initial_edges = asdict(result)["edges"]
        assert state.resumable and not result.complete
        # Exercise the same JSON serialization used by the Redis/API boundary.
        adapter = TypeAdapter(trace.TraceState)
        state = adapter.validate_json(adapter.dump_json(state))
        final = await trace.trace("solana", A, direction=direction, max_hops=4, state=state)
    assert all(edge in asdict(final)["edges"] for edge in initial_edges)
    assert {edge.reference for edge in final.edges} == {move[0] for move in moves}
    assert len(final.edges) == len({(edge.reference, edge.source, edge.target) for edge in final.edges})
    assert final.complete and not state.resumable
    node = next(node for node in final.nodes if node.address == B)
    assert len(node.window_coverages) == 2
    assert node.window_coverages[0] == node.coverage
    assert not node.unfinished_windows
    assert calls.count(("getTransaction", "direct")) == 1
    assert calls.count(("getTransaction", "converge")) == 1


async def test_return_to_root_continues_its_own_wider_window_and_stays_bounded():
    state = trace.TraceState()
    with _settings(), _patched_client(history([
        ("start", A, B, 10, 10), ("return", B, A, 20, 9), ("later", A, C, 30, 8),
    ])):
        await trace.trace("solana", A, since=moment(10), until=moment(10), max_hops=6, state=state)
        result = await trace.trace("solana", A, since=moment(10), until=moment(10), max_hops=6, state=state)
    assert {edge.reference for edge in result.edges} == {"start", "return", "later"}
    assert result.root_window == trace.TraceWindow(moment(10), moment(10))
    assert len(result.nodes) == 3 and len(state.work) <= 4
    assert result.complete


async def test_deadline_materializes_a_successful_sibling_and_leaves_no_task_alive():
    pending = 0
    calls = []
    serve = _solana_handler(
        signatures={A: [_sig("good", JAN23), _sig("blocked", JAN23)]},
        txs={"good": _tx(JAN23, {A: -10**9, B: 10**9})},
    )

    async def handler(request):
        nonlocal pending
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "getTransaction" and body["params"][0] == "blocked":
            pending += 1
            try:
                await asyncio.Event().wait()
            finally:
                pending -= 1
        return serve(request)

    state = trace.TraceState()
    with _settings(), _patched_client(handler), patch.object(trace, "TIME_BUDGET_SECONDS", 0.04):
        result = await asyncio.wait_for(trace.trace("solana", A, state=state), 0.5)
    assert [edge.reference for edge in result.edges] == ["good"]
    assert result.interruption is not None
    assert result.interruption.code == "deadline_exceeded"
    assert not result.complete and state.resumable
    assert pending == 0 and "getBalance" not in calls
    coverage = result.nodes[0].coverage
    assert coverage is not None
    assert coverage.pending_payloads + coverage.failed_payloads >= 1


async def test_root_throttle_after_signature_read_retains_checkpoint_even_without_edges():
    async def rpc(chain, method, params, **kwargs):
        if method == "getSignaturesForAddress":
            return [_sig("pending-only", JAN23)]
        raise onchain.OnchainRateLimited("synthetic throttle", 5)

    state = trace.TraceState()
    with _settings(), patch.object(onchain, "_json_rpc", rpc):
        result = await trace.trace("solana", A, state=state)
    assert not result.edges and not result.complete
    assert result.interruption is not None
    assert result.interruption.code == "upstream_rate_limited"
    coverage = result.nodes[0].coverage
    assert coverage is not None and coverage.pages_read == 1
    assert state.resumable


async def test_generic_unavailable_child_preserves_earlier_edges_and_remains_retryable():
    serve = _solana_handler(
        signatures={A: [_sig("good", JAN23)]},
        txs={"good": _tx(JAN23, {A: -10**9, B: 10**9})},
    )

    def handler(request):
        body = json.loads(request.content)
        if body["method"] == "getSignaturesForAddress" and body["params"][0] == B:
            return httpx.Response(503)
        return serve(request)

    state = trace.TraceState()
    with _settings(), _patched_client(handler):
        result = await trace.trace("solana", A, state=state)
    child = next(node for node in result.nodes if node.address == B)
    assert child.terminal_reason == "unavailable"
    assert result.edges and state.resumable and not result.complete
