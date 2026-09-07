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
from typing import Literal, Optional

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
    terminal_reason: Optional[str] = None
    effective_window: TraceWindow = field(default_factory=TraceWindow)
    coverage: Optional[TransferCoverage] = None
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


@dataclass(frozen=True)
class _Pending:
    address: str
    depth: int
    since: Optional[datetime]
    until: Optional[datetime]


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
) -> TraceResult:
    """Walk the transfer graph outward from ``address``.

    ``since``/``until`` bound the root's own window — the transfers that
    started the trail. Deeper hops derive their window from the transfer that
    reached them, so a caller only has to say when the money left, not when
    each subsequent hop happened.

    A misconfigured deployment raises rather than being recorded as one
    address that could not be read: it is true of every address the walk would
    visit, so reporting it per node would dress a total failure up as a trail
    that happens to end early. A throttle mid-walk is the same fact but arrives
    with findings already in hand, so it ends the walk and marks every address
    left unreached — discarding a trail that is real as far as it goes would
    answer a smaller question than the one the caller asked.
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

    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    root_window = TraceWindow(since, until)
    root = TraceNode(
        id=_node_id(chain, start), chain=chain.key, address=start, depth=0, symbol=chain.symbol,
        effective_window=root_window,
    )
    result = TraceResult(root=root.id, direction=direction, nodes=[root], root_window=root_window)
    nodes: dict[str, TraceNode] = {root.id: root}
    queue: list[_Pending] = [_Pending(start, 0, since, until)]
    expanded = 0
    seen_edges: set[tuple[str, str, str]] = set()
    current_node = root
    phase: Literal["history", "balances"] = "history"
    budget = asyncio.timeout(max(0, deadline - time.monotonic()))
    try:
        async with budget, session() as client:
            while queue:
                pending = queue.pop(0)
                node = nodes[_node_id(chain, pending.address)]
                current_node = node
                if pending.depth >= hops:
                    _mark(node, TERMINAL_MAX_HOPS)
                    _unfinished(node, node.effective_window, "max_hops")
                    continue
                if expanded >= MAX_NODES:
                    _mark(node, TERMINAL_BUDGET)
                    _unfinished(node, node.effective_window, "node_limit")
                    continue
                _check_deadline(deadline)
                expanded += 1

                try:
                    page = await transfers(
                        chain,
                        pending.address,
                        limit=TRANSFERS_PER_NODE,
                        since=pending.since,
                        until=pending.until,
                        client=client,
                        deadline=deadline,
                    )
                    _check_deadline(deadline)
                except (OnchainDeadlineExceeded, ProviderNotConfiguredError, ProviderRateLimited):
                    raise
                except Exception:
                    logger.warning(
                        "Trace could not read %s on %s", pending.address, chain.key, exc_info=True
                    )
                    # The reason stays a code, never the exception's text: an
                    # upstream URL carries an API key and this field is rendered
                    # to the user.
                    _mark(node, TERMINAL_UNAVAILABLE)
                    _unfinished(node, node.effective_window, "provider_unavailable")
                    continue

                node.coverage = page.coverage
                reasons = list(page.coverage.stop_reasons) if page.coverage else ["coverage_unavailable"]
                if page.saturated:
                    reasons.append("high_activity" if page.saturated == SATURATED_POOLED else "window_not_reached")
                if page.trimmed and not {"payload_limit", "transfer_limit"}.intersection(reasons):
                    reasons.append("transfer_limit")
                if not page.complete and not reasons:
                    reasons.append("coverage_unavailable")
                for reason in reasons:
                    _unfinished(node, node.effective_window, reason)

                if page.saturated:
                    _mark(node, page.saturated)
                    continue

                followed, node.branch_omitted_transfers = _pick(
                    page.items, pending.address, direction, branches, min_amount
                )
                if node.branch_omitted_transfers:
                    _unfinished(node, node.effective_window, "branch_limit")
                if not followed:
                    # Three different silences, and only one of them means the
                    # money stopped here. Claiming that for the other two would
                    # exonerate an address the walk simply could not see past.
                    if node.stop_reasons:
                        _mark(node, TERMINAL_PARTIAL)
                    elif page.items:
                        _mark(node, TERMINAL_NO_MATCH)
                    else:
                        _mark(node, TERMINAL_NO_MOVEMENT)
                    continue
                for transfer in followed:
                    other = transfer.counterparty(pending.address)
                    other_id = _node_id(chain, other)
                    key = (transfer.reference, transfer.sender, transfer.recipient)
                    if key not in seen_edges:
                        seen_edges.add(key)
                        result.edges.append(
                            TraceEdge(
                                source=_node_id(chain, transfer.sender),
                                target=_node_id(chain, transfer.recipient),
                                chain=chain.key,
                                symbol=chain.symbol,
                                amount=transfer.amount,
                                reference=transfer.reference,
                                occurred_at=transfer.occurred_at,
                            )
                        )
                    child_window = TraceWindow(
                        since=transfer.occurred_at if direction == "out" else None,
                        until=None if direction == "out" else transfer.occurred_at,
                    )
                    if other_id in nodes:
                        # ponytail: expand each address once; retain skipped ranges
                        # until a bounded resume workflow can revisit them.
                        if not _contains(nodes[other_id].effective_window, child_window):
                            _unfinished(nodes[other_id], child_window, "unexpanded_window")
                        continue
                    child = TraceNode(
                        id=other_id,
                        chain=chain.key,
                        address=other,
                        depth=pending.depth + 1,
                        symbol=chain.symbol,
                        effective_window=child_window,
                    )
                    nodes[other_id] = child
                    result.nodes.append(child)
                    queue.append(
                        _Pending(
                            address=other,
                            depth=pending.depth + 1,
                            # The hop inherits the transfer's instant as its own
                            # horizon — see ADR 0010.
                            since=child_window.since,
                            until=child_window.until,
                        )
                    )

            # Balances are optional current context. Transfer evidence gets the
            # budget first, and no balance is accepted after the deadline.
            phase = "balances"
            for node in result.nodes:
                _check_deadline(deadline)
                balance = await _balance_or_none(chain, node.address, client, deadline=deadline)
                _check_deadline(deadline)
                node.balance = balance
    except OnchainDeadlineExceeded:
        result.interruption = TraceInterruption("deadline_exceeded", phase)
    except TimeoutError:
        if not budget.expired():
            raise
        result.interruption = TraceInterruption("deadline_exceeded", phase)
    except ProviderRateLimited as exc:
        if phase == "history" and not result.edges:
            raise
        result.interruption = TraceInterruption(
            "upstream_rate_limited", phase,
            exc.retry_after_seconds if isinstance(exc, OnchainRateLimited) else None,
        )

    if result.interruption and phase == "history":
        reason = (
            TERMINAL_BUDGET if result.interruption.code == "deadline_exceeded"
            else TERMINAL_RATE_LIMITED
        )
        _mark(current_node, reason)
        gap = "time_budget" if result.interruption.code == "deadline_exceeded" else "upstream_rate_limited"
        _unfinished(current_node, current_node.effective_window, gap)
        for waiting in queue:
            if waiting.depth < hops:
                waiting_node = nodes[_node_id(chain, waiting.address)]
                _mark(waiting_node, reason)
                _unfinished(waiting_node, waiting_node.effective_window, gap)

    for node in result.nodes:
        if node.terminal_reason is None and node.depth >= hops:
            _mark(node, TERMINAL_MAX_HOPS)
            _unfinished(node, node.effective_window, "max_hops")
    result.truncated = any(node.stop_reasons or node.unfinished_windows for node in result.nodes)
    return result


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
