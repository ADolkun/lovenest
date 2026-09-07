"""Actual trace frontier recovery and finality-aware retained synthetic reads."""

import asyncio
import json
from dataclasses import asdict
from unittest.mock import patch

import httpx
import pytest
from pydantic import TypeAdapter

from app.api import onchain as api
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


@pytest.mark.parametrize("direction", ["out", "in"])
@pytest.mark.parametrize("recovery", ["complete", "partial", "noncontaining", "branch_limit"])
async def test_shallower_recovery_retires_only_a_completed_containing_hop_gap(direction, recovery):
    moves = [
        ("long-start", A, B, 10, 10), ("long-end", B, C, 20, 9),
        ("short", A, C, 25 if recovery == "noncontaining" else 5, 8),
    ]
    if recovery == "partial":
        moves.append(("unread", C, D, 30, 7))
    elif recovery == "branch_limit":
        moves.extend([
            ("selected", C, D, 30, 7), ("cycle", C, B, 32, 6), ("omitted", C, A, 35, 5),
        ])
    if direction == "in":
        moves = [(ref, target, source, 40 - offset, amount)
                 for ref, source, target, offset, amount in moves]
    serve = history(moves)
    recovered = False
    calls = []

    def handler(request):
        body = json.loads(request.content)
        key = (body["method"], body["params"][0])
        calls.append(key)
        if key == ("getTransaction", "unread") or (
            key == ("getTransaction", "short") and not recovered
        ):
            return httpx.Response(200, json={"result": None})
        return serve(request)

    state = trace.TraceState()
    with _settings(), _patched_client(handler):
        first = await trace.trace("solana", A, direction=direction, max_hops=2, max_branches=2,
                                  since=moment(0), until=moment(40), state=state)
        original = asdict(first)
        child = next(node for node in first.nodes if node.address == C)
        assert child.depth == 2 and child.stop_reasons == ["max_hops"]
        assert not first.complete and state.resumable
        recovered = True
        for _ in range(2):
            # The API stores this JSON-compatible adapter output in its checkpoint.
            state = api._state_adapter.validate_python(json.loads(json.dumps(
                api._state_adapter.dump_python(state, mode="json"),
            )))
            final = await trace.trace("solana", A, direction=direction, max_hops=2, max_branches=2,
                                      since=moment(0), until=moment(40), state=state)
        response = api.TraceRead(**asdict(final), complete=final.complete)
        response = api.TraceRead.model_validate_json(response.model_dump_json())
    child = next(node for node in final.nodes if node.address == C)
    assert child.depth == 1
    assert asdict(child.effective_window) == original["nodes"][2]["effective_window"]
    assert asdict(final.root_window) == original["root_window"]
    assert all(edge in asdict(final)["edges"] for edge in original["edges"])
    for before, after in zip(original["nodes"], final.nodes):
        assert before["balance"] == after.balance
        assert before["balance_observed_at"] == after.balance_observed_at
    assert calls.count(("getTransaction", "long-start")) == 1
    assert calls.count(("getTransaction", "long-end")) == 1
    assert len(final.edges) == len({(edge.reference, edge.source, edge.target) for edge in final.edges})
    assert response.complete == final.complete
    assert response.nodes[2].stop_reasons == child.stop_reasons
    if recovery in {"complete", "branch_limit"}:
        assert final.complete == (recovery == "complete")
        assert not state.resumable
        assert child.stop_reasons == (["branch_limit"] if recovery == "branch_limit" else [])
        assert all(gap.reason != "max_hops" for gap in child.unfinished_windows)
        assert child.window_coverages[0].complete
        with _settings(), _patched_client(history(moves)):
            fresh = await trace.trace("solana", A, direction=direction, max_hops=2, max_branches=2,
                                      since=moment(0), until=moment(40))
        assert fresh.complete == final.complete
        fresh_child = next(node for node in fresh.nodes if node.address == C)
        assert child.stop_reasons == fresh_child.stop_reasons
        assert {tuple(asdict(edge).items()) for edge in final.edges} == {
            tuple(asdict(edge).items()) for edge in fresh.edges
        }
    else:
        assert not final.complete
        assert "max_hops" in child.stop_reasons
        assert state.resumable == (recovery == "partial")
        if recovery == "partial":
            assert "missing_payload" in child.stop_reasons
        else:
            assert child.window_coverages[0].complete


@pytest.mark.parametrize("other_reason", [
    "branch_limit", "missing_payload", "unsupported_payload", "time_budget", "node_limit",
])
async def test_hop_supersession_preserves_other_reasons_and_complete_observations(other_reason):
    window = trace.TraceWindow(moment(10), None)
    coverage = onchain.TransferCoverage(moment(5), provider_exhausted=True)
    node = trace.TraceNode("solana:" + C, "solana", C, 1, "SOL", effective_window=window)
    capped = trace._Pending(C, 2, window.since, window.until, done=True,
                            reasons=["max_hops", other_reason])
    completed = trace._Pending(C, 1, moment(5), None, done=True, coverage=coverage)
    state = trace.TraceState(result=trace.TraceResult(node.id, "out", nodes=[node]),
                             work=[capped, completed])
    trace._summarize(state, onchain.CHAINS["solana"], 2)
    assert node.stop_reasons == [other_reason]
    assert node.unfinished_windows == [trace.UnfinishedWindow(window.since, None, other_reason)]
    assert node.window_coverages == [coverage] and node.coverage is coverage
    assert state.work == [capped, completed]
    assert state.result is not None and not state.result.complete


@pytest.mark.parametrize("obstruction", [
    "pending", "coverage_unavailable", "incomplete", "wrong_depth", "wrong_address",
    "unsupported_payload", "time_budget", "node_limit",
])
async def test_unsuccessful_obligation_cannot_supersede_a_hop_gap(obstruction):
    coverage = onchain.TransferCoverage(moment(5), provider_exhausted=True)
    node = trace.TraceNode("solana:" + C, "solana", C, 1, "SOL")
    capped = trace._Pending(C, 2, moment(10), None, done=True, reasons=["max_hops"])
    candidate = trace._Pending(C, 1, moment(5), None, done=True, coverage=coverage)
    if obstruction == "pending":
        candidate.done = False
    elif obstruction == "coverage_unavailable":
        candidate.coverage = None
    elif obstruction == "incomplete":
        coverage.provider_exhausted = False
    elif obstruction == "wrong_depth":
        candidate.depth = 3
    elif obstruction == "wrong_address":
        candidate.address = B
    else:
        candidate.reasons = [obstruction]
        coverage.gap(obstruction)
    state = trace.TraceState(result=trace.TraceResult(node.id, "out", nodes=[node]),
                             work=[capped, candidate])
    trace._summarize(state, onchain.CHAINS["solana"], 2)
    assert "max_hops" in node.stop_reasons
    assert trace.UnfinishedWindow(moment(10), None, "max_hops") in node.unfinished_windows
    assert state.result is not None and not state.result.complete
