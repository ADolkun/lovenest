"""Coverage and convergence through the real tracer with synthetic RPC evidence."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.providers.base import ProviderRateLimited
from app.providers.onchain import OnchainDeadlineExceeded, Transfers
from app.services import onchain_trace as trace
from tests.test_providers_onchain import (
    A, B, C, D, JAN23, _patched_client, _settings, _sig, _solana_handler, _tx,
)

pytestmark = pytest.mark.asyncio


def moment(offset=0):
    return datetime.fromtimestamp(JAN23 + offset, timezone.utc)


def history(moves):
    signatures, txs = {}, {}
    for reference, sender, recipient, offset, amount in moves:
        for address in (sender, recipient):
            signatures.setdefault(address, []).append(_sig(reference, JAN23 + offset))
        txs[reference] = _tx(JAN23 + offset, {sender: -amount * 10**9, recipient: amount * 10**9})
    for rows in signatures.values():
        rows.sort(key=lambda row: row["blockTime"], reverse=True)
    return _solana_handler(signatures=signatures, txs=txs)


@pytest.mark.parametrize("direction,moves,first,later", [
    ("out", [("direct", A, B, 20, 10), ("indirect", A, C, 5, 9),
             ("converge", C, B, 10, 8), ("unread", B, D, 15, 7)], 20, 10),
    ("in", [("direct", B, A, 10, 10), ("indirect", C, A, 25, 9),
            ("converge", B, C, 20, 8), ("unread", D, B, 15, 7)], 10, 20),
])
async def test_convergence_keeps_the_first_window_and_names_the_unexpanded_range(
    direction, moves, first, later,
):
    with _settings(), _patched_client(history(moves)):
        result = await trace.trace("solana", A, direction=direction, max_hops=4)
    node = next(node for node in result.nodes if node.address == B)
    expected_first = trace.TraceWindow(moment(first), None) if direction == "out" else trace.TraceWindow(None, moment(first))
    expected_later = trace.UnfinishedWindow(moment(later), None, "unexpanded_window") if direction == "out" else trace.UnfinishedWindow(None, moment(later), "unexpanded_window")
    assert node.effective_window == expected_first
    assert node.unfinished_windows == [expected_later]
    assert node.stop_reasons == ["unexpanded_window"]
    assert node.terminal_reason == "partial"
    assert node.coverage is not None
    assert node.coverage.pages_read == 1
    assert {edge.reference for edge in result.edges} == {"direct", "indirect", "converge"}
    assert result.truncated and not result.complete


@pytest.mark.parametrize("direction", ["out", "in"])
async def test_child_windows_are_inclusive_and_do_not_inherit_the_other_root_bound(direction):
    moves = [("root", A, B, 10, 10), ("child", B, C, 20, 9), ("same-time", B, D, 10, 8)]
    if direction == "in":
        moves = [(ref, target, source, 30 - offset, amount) for ref, source, target, offset, amount in moves]
    at = moment(10 if direction == "out" else 20)
    with _settings(), _patched_client(history(moves)):
        result = await trace.trace("solana", A, direction=direction, since=at, until=at, max_hops=3)
    assert result.root_window == trace.TraceWindow(at, at)
    child = next(node for node in result.nodes if node.address == B)
    assert child.effective_window == (trace.TraceWindow(at, None) if direction == "out" else trace.TraceWindow(None, at))
    assert {edge.reference for edge in result.edges} == {"root", "child", "same-time"}
    assert result.complete and not result.truncated


@pytest.mark.parametrize("direction", ["out", "in"])
async def test_contained_revisits_and_duplicate_edges_do_not_create_false_gaps(direction):
    moves = [("direct", A, B, 10, 10), ("indirect", A, C, 5, 9), ("converge", C, B, 20, 8)]
    if direction == "in":
        moves = [(ref, target, source, 30 - offset, amount) for ref, source, target, offset, amount in moves]
    with _settings(), _patched_client(history(moves)):
        result = await trace.trace("solana", A, direction=direction, max_hops=4)
    assert result.complete
    assert all(not node.unfinished_windows for node in result.nodes)
    assert len(result.edges) == len({(edge.reference, edge.source, edge.target) for edge in result.edges}) == 3


async def test_return_to_root_reports_a_window_beyond_its_original_ceiling_and_stays_bounded():
    with _settings(), _patched_client(history([
        ("start", A, B, 10, 10), ("return", B, A, 20, 9), ("later", A, C, 30, 8),
    ])):
        result = await trace.trace("solana", A, since=moment(10), until=moment(10), max_hops=6)
    root = result.nodes[0]
    assert root.effective_window == result.root_window == trace.TraceWindow(moment(10), moment(10))
    assert root.unfinished_windows == [trace.UnfinishedWindow(moment(20), None, "unexpanded_window")]
    assert {edge.reference for edge in result.edges} == {"start", "return"}
    assert len(result.nodes) == 2 and not result.complete


async def test_equal_candidate_order_has_the_same_branch_and_completeness():
    moves = [("alpha", A, B, 0, 1), ("zeta", A, C, 0, 1)]
    results = []
    for ordered in (moves, list(reversed(moves))):
        with _settings(), _patched_client(history(ordered)):
            results.append(await trace.trace("solana", A, max_branches=1))
    assert results[0].edges == results[1].edges
    assert results[0].truncated == results[1].truncated
    assert [node.unfinished_windows for node in results[0].nodes] == [node.unfinished_windows for node in results[1].nodes]
    assert [edge.reference for edge in results[0].edges] == ["zeta"]
    assert results[0].nodes[0].branch_omitted_transfers == 1
    assert results[0].nodes[0].stop_reasons == ["branch_limit"]
    assert not results[0].complete


@pytest.mark.parametrize("cap,reason", [("hops", "max_hops"), ("nodes", "node_limit")])
async def test_unread_children_keep_their_effective_windows_at_each_traversal_cap(cap, reason):
    with (
        _settings(), _patched_client(history([("start", A, B, 10, 10)])),
        patch.object(trace, "MAX_NODES", 1 if cap == "nodes" else 24),
    ):
        result = await trace.trace("solana", A, max_hops=1 if cap == "hops" else 3)
    child = result.nodes[1]
    assert child.coverage is None
    assert child.effective_window == trace.TraceWindow(moment(10), None)
    assert child.unfinished_windows == [trace.UnfinishedWindow(moment(10), None, reason)]
    assert child.stop_reasons == [reason]
    assert result.truncated and not result.complete


async def test_missing_payload_with_a_successful_edge_keeps_coverage_on_the_nonterminal():
    handler = _solana_handler(
        signatures={A: [_sig("good", JAN23), _sig("missing", JAN23)]},
        txs={"good": _tx(JAN23, {A: -10**9, B: 10**9})},
    )
    with _settings(), _patched_client(handler):
        result = await trace.trace("solana", A)
    root = result.nodes[0]
    assert root.terminal_reason is None
    assert root.coverage is not None
    assert root.coverage.missing_payloads == 1
    assert root.coverage.payloads_requested == 2 and root.coverage.payloads_read == 1
    assert "missing_payload" in root.stop_reasons
    assert result.truncated and len(result.edges) == 1


async def test_legacy_metadata_absence_never_becomes_confirmed_empty_history():
    with _settings(), _patched_client(_solana_handler()), patch.object(trace, "transfers", AsyncMock(return_value=Transfers([]))):
        result = await trace.trace("solana", A)
    assert result.nodes[0].coverage is None
    assert result.nodes[0].stop_reasons == ["coverage_unavailable"]
    assert result.nodes[0].terminal_reason == "partial"
    assert not result.complete


@pytest.mark.parametrize("error,reason", [
    (OnchainDeadlineExceeded(), "time_budget"),
    (ProviderRateLimited("synthetic throttle"), "upstream_rate_limited"),
    (RuntimeError("synthetic unreadable node"), "provider_unavailable"),
])
async def test_interrupted_or_unavailable_reads_keep_completed_and_unfinished_windows(error, reason):
    original = trace.transfers

    async def read(chain, address, **kwargs):
        if address == B:
            raise error
        return await original(chain, address, **kwargs)

    with (
        _settings(), _patched_client(history([("first", A, B, 20, 10), ("queued", A, C, 10, 9)])),
        patch.object(trace, "transfers", read),
    ):
        result = await trace.trace("solana", A)
    assert result.nodes[0].coverage is not None
    assert result.nodes[0].coverage.payloads_read == 2
    assert len(result.edges) == 2 and not result.complete
    failed = next(node for node in result.nodes if node.address == B)
    queued = next(node for node in result.nodes if node.address == C)
    assert failed.coverage is None
    assert failed.unfinished_windows == [trace.UnfinishedWindow(moment(20), None, reason)]
    if reason == "provider_unavailable":
        assert result.interruption is None
        assert queued.coverage is not None and not queued.stop_reasons
    else:
        assert result.interruption is not None
        assert result.interruption.phase == "history"
        assert queued.coverage is None
        assert queued.unfinished_windows == [trace.UnfinishedWindow(moment(10), None, reason)]


async def test_payload_cap_is_not_counted_as_branch_omission():
    with (
        _settings(), _patched_client(history([("first", A, B, 10, 10), ("second", A, C, 20, 9)])),
        patch.object(trace, "TRANSFERS_PER_NODE", 1),
    ):
        result = await trace.trace("solana", A)
    root = result.nodes[0]
    assert root.coverage is not None
    assert root.coverage.omitted_signatures == 1
    assert root.branch_omitted_transfers == 0
    assert "payload_limit" in root.stop_reasons
    assert len(result.edges) == 1 and not result.complete


@pytest.mark.parametrize("signatures,terminal,complete", [
    ([], "no_movement", True),
    ([_sig("missing", JAN23)], "partial", False),
])
async def test_complete_empty_history_is_distinct_from_unreadable_history(signatures, terminal, complete):
    with _settings(), _patched_client(_solana_handler(signatures={A: signatures})):
        result = await trace.trace("solana", A)
    assert not result.edges
    assert result.nodes[0].terminal_reason == terminal
    assert result.complete is complete and result.truncated is not complete
