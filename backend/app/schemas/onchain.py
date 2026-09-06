import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ChainRead(BaseModel):
    key: str
    display_name: str
    symbol: str
    kind: str
    # False when the deployment has no history source for the chain — EVM
    # tracing needs an Etherscan key that balances do not. The UI reads this
    # to disable the chain rather than let a trace fail on submit.
    traceable: bool


class TraceRequest(BaseModel):
    chain: str
    address: str
    direction: Literal["out", "in"] = "out"
    max_hops: int = Field(default=3, ge=1, le=6)
    max_branches: int = Field(default=3, ge=1, le=5)
    min_amount: Optional[Decimal] = Field(default=None, ge=0)
    since: Optional[datetime] = None
    until: Optional[datetime] = None


class TraceNodeRead(BaseModel):
    id: str
    chain: str
    address: str
    depth: int
    symbol: str
    balance: Optional[Decimal] = None
    terminal_reason: Optional[str] = None


class TraceEdgeRead(BaseModel):
    source: str
    target: str
    chain: str
    symbol: str
    amount: Decimal
    reference: str
    occurred_at: datetime


class TraceRead(BaseModel):
    root: str
    direction: str
    nodes: list[TraceNodeRead]
    edges: list[TraceEdgeRead]
    # True when the node budget ran out before the walk did, so the result is
    # a prefix of the trail rather than all of it.
    truncated: bool


class WatchedAddressRead(BaseModel):
    """An address already connected in this workspace, for one-click tracing."""

    chain: str
    address: str
    label: str
    connection_id: uuid.UUID
    connection_name: str
