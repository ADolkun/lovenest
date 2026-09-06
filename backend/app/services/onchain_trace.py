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
    Transfer,
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
# Every expanded address costs a signature page plus a request per transaction
# it contributes, so the node cap bounds the request count. It does not bound
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


@dataclass
class TraceNode:
    id: str
    chain: str
    address: str
    depth: int
    symbol: str
    balance: Optional[Decimal] = None
    terminal_reason: Optional[str] = None


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
class TraceResult:
    root: str
    direction: Direction
    nodes: list[TraceNode] = field(default_factory=list)
    edges: list[TraceEdge] = field(default_factory=list)
    truncated: bool = False


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
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=timezone.utc)


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

    A misconfigured deployment or a throttled node raises rather than being
    recorded as one address that could not be read: both are true of every
    address the walk would visit, so reporting them per node would dress a
    total failure up as a trail that happens to end early.
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

    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    async with session() as client:
        root = TraceNode(
            id=_node_id(chain, start),
            chain=chain.key,
            address=start,
            depth=0,
            symbol=chain.symbol,
            balance=await _balance_or_none(chain, start, client),
        )
        result = TraceResult(root=root.id, direction=direction, nodes=[root])
        nodes: dict[str, TraceNode] = {root.id: root}
        queue: list[_Pending] = [_Pending(start, 0, since, until)]
        expanded = 0
        seen_edges: set[tuple[str, str, str]] = set()

        while queue:
            pending = queue.pop(0)
            node = nodes[_node_id(chain, pending.address)]
            if pending.depth >= hops:
                _mark(node, TERMINAL_MAX_HOPS)
                continue
            if expanded >= MAX_NODES or time.monotonic() >= deadline:
                _mark(node, TERMINAL_BUDGET)
                result.truncated = True
                continue
            expanded += 1

            try:
                page = await transfers(
                    chain,
                    pending.address,
                    limit=TRANSFERS_PER_NODE,
                    since=pending.since,
                    until=pending.until,
                    client=client,
                )
            except (ProviderNotConfiguredError, ProviderRateLimited):
                # True of every address the walk would visit, not of this one.
                # Recording it per node would dress a total failure up as a
                # trail that happens to end early.
                raise
            except Exception:
                logger.warning(
                    "Trace could not read %s on %s", pending.address, chain.key, exc_info=True
                )
                # The reason stays a code, never the exception's text: an
                # upstream URL carries an API key and this field is rendered
                # to the user.
                _mark(node, TERMINAL_UNAVAILABLE)
                continue

            if page.saturated:
                _mark(node, page.saturated)
                continue

            followed = _pick(page.items, pending.address, direction, branches, min_amount)
            if not followed:
                # Three different silences, and only one of them means the
                # money stopped here. Claiming that for the other two would
                # exonerate an address the walk simply could not see past.
                if not page.complete:
                    _mark(node, TERMINAL_PARTIAL)
                elif page.items:
                    _mark(node, TERMINAL_NO_MATCH)
                else:
                    _mark(node, TERMINAL_NO_MOVEMENT)
                continue
            if not page.complete:
                result.truncated = True

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
                if other_id in nodes:
                    continue
                child = TraceNode(
                    id=other_id,
                    chain=chain.key,
                    address=other,
                    depth=pending.depth + 1,
                    symbol=chain.symbol,
                    balance=await _balance_or_none(chain, other, client),
                )
                nodes[other_id] = child
                result.nodes.append(child)
                queue.append(
                    _Pending(
                        address=other,
                        depth=pending.depth + 1,
                        # The hop inherits the transfer's instant as its own
                        # horizon — see ADR 0010.
                        since=transfer.occurred_at if direction == "out" else None,
                        until=None if direction == "out" else transfer.occurred_at,
                    )
                )

    for node in result.nodes:
        if node.terminal_reason is None and node.depth >= hops:
            _mark(node, TERMINAL_MAX_HOPS)
    return result


def _pick(
    items: list[Transfer],
    address: str,
    direction: Direction,
    branches: int,
    min_amount: Optional[Decimal],
) -> list[Transfer]:
    """The transfers worth following: right way, big enough, largest first."""
    candidates = [
        transfer
        for transfer in items
        if transfer.direction(address) == direction
        and transfer.counterparty(address) != address
        and (min_amount is None or transfer.amount >= min_amount)
    ]
    candidates.sort(key=lambda t: (t.amount, t.occurred_at), reverse=True)
    return candidates[:branches]


def _mark(node: TraceNode, reason: str) -> None:
    if node.terminal_reason is None:
        node.terminal_reason = reason


def _node_id(chain: Chain, address: str) -> str:
    return f"{chain.key}:{address}"


async def _balance_or_none(chain: Chain, address: str, client) -> Optional[Decimal]:
    """A node's balance is context, not the answer — never fail the trace for it."""
    try:
        return await native_balance(chain, address, client=client)
    except Exception:
        logger.debug("No balance for %s", address, exc_info=True)
        return None
