"""Follow native-coin movement across addresses, hop by hop.

The question this answers is "where did this money go" (or "where did it come
from"), which is not the same as "draw the transaction graph". A wallet's full
neighbourhood is unbounded and mostly noise — dust spam, airdrops, unrelated
history — so the walk is narrowed three ways: a time window inherited per hop,
the largest branches only, and a stop at pooled addresses. ADR 0010 argues
each one and why it is load-bearing.

The property that governs everything here: a trace may return less than the
whole trail, but it may never present less as the whole. Every address the
walk stops at carries a reason, and "nothing moved out of here" is only ever
said when the evidence was complete enough to say it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Optional

from app.providers.base import ProviderNotConfiguredError, ProviderRateLimited
from app.providers.onchain import (
    CHAINS,
    SATURATED_POOLED,
    SATURATED_UNPAGEABLE,
    Chain,
    OnchainDeadlineExceeded,
    OnchainRateLimited,
    Transfer,
    TransferCoverage,
    address_is_valid,
    native_balance,
    normalize_address,
    retained_transfers,
    session,
    transfers,
)

logger = logging.getLogger(__name__)

Direction = Literal["out", "in"]

MAX_HOPS_ALLOWED = 6
MAX_BRANCHES_ALLOWED = 5
# Every expanded address costs bounded history pages and payload requests,
# so the expansion cap bounds the request count. It does not bound
# the *time*, because a throttled node can spend thirty seconds on any one of
# them — TIME_BUDGET_SECONDS does that, and is what actually keeps a trace
# inside a request rather than holding a worker for minutes.
MAX_NODES = 24
TRANSFERS_PER_NODE = 25
TIME_BUDGET_SECONDS = 45.0

TERMINAL_MAX_HOPS = "max_hops"
TERMINAL_POOLED = SATURATED_POOLED
TERMINAL_UNPAGEABLE = SATURATED_UNPAGEABLE
TERMINAL_BUDGET = "budget"
TERMINAL_NO_MOVEMENT = "no_movement"
TERMINAL_NO_MATCH = "no_match"
TERMINAL_PARTIAL = "partial"
TERMINAL_UNAVAILABLE = "unavailable"
TERMINAL_RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class TraceWindow:
    since: Optional[datetime] = None
    until: Optional[datetime] = None


@dataclass(frozen=True)
class UnfinishedWindow:
    since: Optional[datetime]
    until: Optional[datetime]
    reason: str


@dataclass
class TraceNode:
    id: str
    chain: str
    address: str
    depth: int
    symbol: str
    balance: Optional[Decimal] = None
    balance_observed_at: Optional[datetime] = None
    terminal_reason: Optional[str] = None
    effective_window: TraceWindow = field(default_factory=TraceWindow)
    coverage: Optional[TransferCoverage] = None
    window_coverages: list[TransferCoverage] = field(default_factory=list)
    unfinished_windows: list[UnfinishedWindow] = field(default_factory=list)
    stop_reasons: list[str] = field(default_factory=list)
    branch_omitted_transfers: int = 0


@dataclass
class TraceEdge:
    source: str
    target: str
    chain: str
    symbol: str
    amount: Decimal
    reference: str
    occurred_at: datetime


@dataclass
class TraceInterruption:
    code: Literal["deadline_exceeded", "upstream_rate_limited"]
    phase: Literal["history", "balances"]
    retry_after_seconds: Optional[int] = None


@dataclass
class TraceResult:
    root: str
    direction: Direction
    nodes: list[TraceNode] = field(default_factory=list)
    edges: list[TraceEdge] = field(default_factory=list)
    truncated: bool = False
    interruption: Optional[TraceInterruption] = None
    scope: Literal["native_coin"] = "native_coin"
    root_window: TraceWindow = field(default_factory=TraceWindow)

    @property
    def complete(self) -> bool:
        return not self.truncated


@dataclass
class _Pending:
    address: str
    depth: int
    since: Optional[datetime]
    until: Optional[datetime]
    done: bool = False
    coverage: Optional[TransferCoverage] = None
    reasons: list[str] = field(default_factory=list)
    has_items: bool = False


@dataclass
class TraceState:
    """The actual bounded frontier and completed observations, never a replay recipe."""

    result: Optional[TraceResult] = None
    work: list[_Pending] = field(default_factory=list)
    reads: dict[str, Any] = field(default_factory=dict)
    expanded: list[str] = field(default_factory=list)
    selected: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    omitted: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)

    @property
    def resumable(self) -> bool:
        if self.reads.get("limited"):
            return False
        return any(not work.done for work in self.work) or (
            self.result is not None and self.result.interruption is not None
            and self.result.interruption.phase == "balances"
        )


def resolve_chain(chain_key: str) -> Chain:
    chain = CHAINS.get((chain_key or "").strip().lower())
    if chain is None:
        raise ValueError(f"Unknown chain {chain_key!r}. Supported: {', '.join(sorted(CHAINS))}.")
    return chain


def _as_utc(moment: Optional[datetime]) -> Optional[datetime]:
    """Read a naive timestamp as UTC rather than as the server's local time.

    The window is what makes a trace a claim about *this* money, and a server
    in a non-UTC zone would otherwise shift it by its own offset — quietly
    changing which transfers count.
    """
    if moment is None:
        return None
    try:
        return moment.replace(tzinfo=moment.tzinfo or timezone.utc).astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Date is outside the supported UTC range.") from exc


async def trace(
    chain_key: str,
    address: str,
    *,
    direction: Direction = "out",
    max_hops: int = 3,
    max_branches: int = 3,
    min_amount: Optional[Decimal] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    state: Optional[TraceState] = None,
) -> TraceResult:
    """Continue a bounded frontier, preserving selected edges and read observations.

    The caller validates checkpoint compatibility and workspace access. A new
    state starts a fresh investigation; a saved state never refetches completed
    work or silently refreshes the history and balance observations it retains.
    """
    chain = resolve_chain(chain_key)
    if direction not in ("out", "in"):
        raise ValueError("direction must be 'out' or 'in'")
    hops = max(1, min(int(max_hops), MAX_HOPS_ALLOWED))
    branches = max(1, min(int(max_branches), MAX_BRANCHES_ALLOWED))
    start = normalize_address(chain, address)
    if not address_is_valid(chain, start):
        raise ValueError(f"{address!r} is not a valid {chain.display_name} address.")
    since, until = _as_utc(since), _as_utc(until)
    if since is not None and until is not None and since > until:
        raise ValueError("The end date must be on or after the start date.")
    if min_amount is not None and (not min_amount.is_finite() or min_amount < 0):
        raise ValueError("Minimum amount must be finite and nonnegative.")

    state = state if state is not None else TraceState()
    if state.result is None:
        root_window = TraceWindow(since, until)
        root = TraceNode(
            id=_node_id(chain, start), chain=chain.key, address=start, depth=0,
            symbol=chain.symbol, effective_window=root_window,
        )
        state.result = TraceResult(
            root=root.id, direction=direction, nodes=[root], root_window=root_window,
        )
        state.work.append(_Pending(start, 0, since, until))
    result = state.result
    result.interruption = None
    # Work added for a converging, already-seen address waits for explicit
    # continuation. This preserves the original once-per-address request bound.
    queue = [work for work in state.work if not work.done]
    current: Optional[_Pending] = None
    consumed = False
    phase: Literal["history", "balances"] = "history"
    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    budget = asyncio.timeout(max(0, deadline - time.monotonic()))
    try:
        async with budget, session() as client:
            while queue:
                current = queue.pop(0)
                consumed = False
                if current.depth >= hops:
                    current.done, current.reasons = True, ["max_hops"]
                    continue
                if current.address not in state.expanded:
                    if len(state.expanded) >= MAX_NODES:
                        current.done, current.reasons = True, ["node_limit"]
                        continue
                    state.expanded.append(current.address)
                _check_deadline(deadline)
                try:
                    page = await transfers(
                        chain, current.address, limit=TRANSFERS_PER_NODE,
                        since=current.since, until=current.until,
                        client=client, deadline=deadline, read_state=state.reads,
                    )
                except (OnchainDeadlineExceeded, ProviderNotConfiguredError, ProviderRateLimited):
                    raise
                except Exception:
                    # Safe codes only. Provider failures must not expose URLs,
                    # API keys, raw responses or the private address in logs.
                    current.reasons = ["provider_unavailable"]
                    continue
                _consume_page(state, current, page, chain, branches, min_amount, queue)
                consumed = True
                _check_deadline(deadline)
                if page.interruption in ("deadline_exceeded", "upstream_rate_limited"):
                    if page.interruption == "upstream_rate_limited" and not _has_progress(state):
                        raise OnchainRateLimited("Trace provider throttled", page.retry_after_seconds)
                    result.interruption = TraceInterruption(
                        page.interruption, "history", page.retry_after_seconds,
                    )
                    break

            if result.interruption is None:
                phase = "balances"
                for node in result.nodes:
                    if node.balance_observed_at is not None:
                        continue
                    _check_deadline(deadline)
                    balance = await _balance_or_none(chain, node.address, client, deadline=deadline)
                    _check_deadline(deadline)
                    node.balance = balance
                    if balance is not None:
                        node.balance_observed_at = datetime.now(timezone.utc)
    except OnchainDeadlineExceeded:
        result.interruption = TraceInterruption("deadline_exceeded", phase)
    except TimeoutError:
        if not budget.expired():
            raise
        result.interruption = TraceInterruption("deadline_exceeded", phase)
    except ProviderRateLimited as exc:
        if phase == "history" and not _has_progress(state):
            raise
        result.interruption = TraceInterruption(
            "upstream_rate_limited", phase,
            exc.retry_after_seconds if isinstance(exc, OnchainRateLimited) else None,
        )

    if result.interruption and phase == "history":
        # The timeout has exited and the provider drained cancelled tasks. Read
        # only already-retained evidence here; never issue another RPC.
        if current is not None and not consumed:
            page = retained_transfers(
                chain, current.address, limit=TRANSFERS_PER_NODE,
                since=current.since, until=current.until, read_state=state.reads,
            )
            if page is not None:
                _consume_page(state, current, page, chain, branches, min_amount, queue)
        reason = ("time_budget" if result.interruption.code == "deadline_exceeded"
                  else "upstream_rate_limited")
        if current is not None:
            current.done = False
            if reason not in current.reasons:
                current.reasons.append(reason)
        for work in queue:
            if not work.done and work.depth < hops and reason not in work.reasons:
                work.reasons.append(reason)
    _summarize(state, chain, hops)
    return result


def _has_progress(state: TraceState) -> bool:
    return bool(state.result and state.result.edges) or any(
        work.coverage is not None and (
            work.coverage.pages_read or work.coverage.payloads_read
        ) for work in state.work
    )


def _consume_page(state, work, page, chain, branches, min_amount, queue) -> None:
    result = state.result
    assert result is not None
    nodes = {node.id: node for node in result.nodes}
    node = nodes[_node_id(chain, work.address)]
    work.coverage = page.coverage
    work.has_items = bool(page.items)
    work.done = not page.resumable
    work.reasons = list(page.coverage.stop_reasons) if page.coverage else ["coverage_unavailable"]
    if page.saturated:
        reason = "high_activity" if page.saturated == SATURATED_POOLED else "window_not_reached"
        if reason not in work.reasons:
            work.reasons.append(reason)
        return
    if page.trimmed and not {"payload_limit", "transfer_limit"}.intersection(work.reasons):
        work.reasons.append("transfer_limit")
    if not page.complete and not work.reasons:
        work.reasons.append("coverage_unavailable")

    candidates, _ = _pick(page.items, work.address, result.direction, len(page.items), min_amount)
    chosen = state.selected.setdefault(node.id, [])
    seen_edges = {(edge.reference, edge.source, edge.target) for edge in result.edges}
    omitted = state.omitted.setdefault(node.id, [])
    for transfer in candidates:
        key = (transfer.reference, transfer.sender, transfer.recipient)
        if key not in chosen:
            if len(chosen) >= branches:
                if key not in omitted:
                    omitted.append(key)
                continue
            chosen.append(key)
        edge_key = (transfer.reference, _node_id(chain, transfer.sender), _node_id(chain, transfer.recipient))
        if edge_key not in seen_edges:
            seen_edges.add(edge_key)
            result.edges.append(TraceEdge(
                source=edge_key[1], target=edge_key[2], chain=chain.key, symbol=chain.symbol,
                amount=transfer.amount, reference=transfer.reference, occurred_at=transfer.occurred_at,
            ))
        other = transfer.counterparty(work.address)
        other_id = _node_id(chain, other)
        window = TraceWindow(
            transfer.occurred_at if result.direction == "out" else None,
            None if result.direction == "out" else transfer.occurred_at,
        )
        depth = work.depth + 1
        existing = other_id in nodes
        if not existing:
            child = TraceNode(
                id=other_id, chain=chain.key, address=other, depth=depth,
                symbol=chain.symbol, effective_window=window,
            )
            nodes[other_id] = child
            result.nodes.append(child)
        else:
            nodes[other_id].depth = min(nodes[other_id].depth, depth)
        # Address-only dedup would lose a wider inherited horizon. A shallower
        # arrival also has more hop budget; it is separate bounded work.
        if any(
            prior.address == other and prior.depth <= depth
            and _contains(TraceWindow(prior.since, prior.until), window)
            for prior in state.work
        ):
            continue
        pending = _Pending(other, depth, window.since, window.until)
        if existing:
            pending.reasons = ["unexpanded_window"]
        state.work.append(pending)
        if not existing:
            queue.append(pending)
    # Each replayed observation has the same candidates. Do not count a
    # repeated omission again or free an already selected branch slot.
    node.branch_omitted_transfers = len(omitted)
    if omitted:
        work.reasons.append("branch_limit")


def _summarize(state: TraceState, chain: Chain, hops: int) -> None:
    result = state.result
    assert result is not None
    for node in result.nodes:
        node.stop_reasons = []
        node.unfinished_windows = []
        node.window_coverages = []
        work_items = [work for work in state.work if work.address == node.address]
        for work in work_items:
            if work.depth >= hops:
                work.done, work.reasons = True, ["max_hops"]
            if work.coverage is not None:
                node.window_coverages.append(work.coverage)
            for reason in work.reasons:
                _unfinished(node, TraceWindow(work.since, work.until), reason)
        node.coverage = node.window_coverages[0] if node.window_coverages else None
        if node.stop_reasons:
            reasons = node.stop_reasons
            node.terminal_reason = (
                TERMINAL_POOLED if "high_activity" in reasons else
                TERMINAL_RATE_LIMITED if "upstream_rate_limited" in reasons else
                TERMINAL_BUDGET if {"time_budget", "node_limit"}.intersection(reasons) else
                TERMINAL_MAX_HOPS if reasons == ["max_hops"] else
                TERMINAL_UNAVAILABLE if "provider_unavailable" in reasons else
                TERMINAL_PARTIAL
            )
            if state.selected.get(node.id) and not {
                "high_activity", "upstream_rate_limited", "time_budget", "node_limit", "max_hops",
            }.intersection(reasons):
                node.terminal_reason = None
        elif state.selected.get(node.id):
            node.terminal_reason = None
        else:
            # Zero selected branches is only a no-movement claim if the
            # provider examined a complete empty window.
            node.terminal_reason = (
                TERMINAL_NO_MATCH if any(work.has_items for work in work_items)
                else TERMINAL_NO_MOVEMENT
            )
    result.truncated = any(node.stop_reasons or node.unfinished_windows for node in result.nodes)


def _pick(
    items: list[Transfer],
    address: str,
    direction: Direction,
    branches: int,
    min_amount: Optional[Decimal],
) -> tuple[list[Transfer], int]:
    """The transfers worth following: right way, big enough, largest first."""
    candidates = [
        transfer
        for transfer in items
        if transfer.direction(address) == direction
        and transfer.counterparty(address) != address
        and (min_amount is None or transfer.amount >= min_amount)
    ]
    candidates.sort(
        key=lambda t: (t.amount, t.occurred_at, t.reference, t.sender, t.recipient), reverse=True
    )
    return candidates[:branches], max(0, len(candidates) - branches)


def _contains(outer: TraceWindow, inner: TraceWindow) -> bool:
    return (
        outer.since is None or (inner.since is not None and outer.since <= inner.since)
    ) and (
        outer.until is None or (inner.until is not None and outer.until >= inner.until)
    )


def _unfinished(node: TraceNode, window: TraceWindow, reason: str) -> None:
    # These are ranges not fully explored, not a claim about a precise missing slice.
    gap = UnfinishedWindow(window.since, window.until, reason)
    if gap not in node.unfinished_windows:
        node.unfinished_windows.append(gap)
    if reason not in node.stop_reasons:
        node.stop_reasons.append(reason)
    if node.terminal_reason in (TERMINAL_NO_MOVEMENT, TERMINAL_NO_MATCH):
        node.terminal_reason = TERMINAL_PARTIAL


def _mark(node: TraceNode, reason: str) -> None:
    if node.terminal_reason is None:
        node.terminal_reason = reason


def _node_id(chain: Chain, address: str) -> str:
    return f"{chain.key}:{address}"


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise OnchainDeadlineExceeded()


async def _balance_or_none(
    chain: Chain, address: str, client, *, deadline: Optional[float] = None
) -> Optional[Decimal]:
    """A node's balance is context, not the answer — never fail the trace for it."""
    try:
        return await native_balance(chain, address, client=client, deadline=deadline)
    except (OnchainDeadlineExceeded, ProviderRateLimited):
        raise
    except Exception:
        logger.debug("No balance for %s", address, exc_info=True)
        return None
