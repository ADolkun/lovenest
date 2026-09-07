import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


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

    @field_validator("since", "until")
    @classmethod
    def utc_dates(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        try:
            return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)
        except (OverflowError, ValueError) as exc:
            raise ValueError("Date is outside the supported UTC range.") from exc

    @model_validator(mode="after")
    def ordered_window(self):
        if self.since is not None and self.until is not None and self.since > self.until:
            raise ValueError("The end date must be on or after the start date.")
        return self


class TraceWindowRead(BaseModel):
    since: Optional[datetime] = None
    until: Optional[datetime] = None


class UnfinishedWindowRead(TraceWindowRead):
    reason: str


class TransferCoverageRead(BaseModel):
    requested_since: Optional[datetime] = None
    requested_until: Optional[datetime] = None
    fetched_at: Optional[datetime] = None
    observed_oldest: Optional[datetime] = None
    observed_newest: Optional[datetime] = None
    examined_oldest: Optional[datetime] = None
    examined_newest: Optional[datetime] = None
    since_reached: Optional[bool] = None
    until_reached: Optional[bool] = None
    provider_exhausted: Optional[bool] = None
    pages_read: Optional[int] = Field(default=None, ge=0)
    rows_read: Optional[int] = Field(default=None, ge=0)
    signatures_read: Optional[int] = Field(default=None, ge=0)
    payloads_requested: Optional[int] = Field(default=None, ge=0)
    payloads_read: Optional[int] = Field(default=None, ge=0)
    missing_timestamps: Optional[int] = Field(default=None, ge=0)
    missing_payloads: Optional[int] = Field(default=None, ge=0)
    unsupported_payloads: Optional[int] = Field(default=None, ge=0)
    omitted_signatures: Optional[int] = Field(default=None, ge=0)
    omitted_transfers: Optional[int] = Field(default=None, ge=0)
    next_cursor: Optional[str] = None
    stop_reasons: list[str] = Field(default_factory=list)


class TraceNodeRead(BaseModel):
    id: str
    chain: str
    address: str
    depth: int
    symbol: str
    balance: Optional[Decimal] = None
    terminal_reason: Optional[str] = None
    effective_window: TraceWindowRead = Field(default_factory=TraceWindowRead)
    coverage: Optional[TransferCoverageRead] = None
    unfinished_windows: list[UnfinishedWindowRead] = Field(default_factory=list)
    stop_reasons: list[str] = Field(default_factory=list)
    branch_omitted_transfers: int = Field(default=0, ge=0)


class TraceEdgeRead(BaseModel):
    source: str
    target: str
    chain: str
    symbol: str
    amount: Decimal
    reference: str
    occurred_at: datetime


class TraceInterruptionRead(BaseModel):
    code: Literal["deadline_exceeded", "upstream_rate_limited"]
    phase: Literal["history", "balances"]
    retry_after_seconds: Optional[int] = Field(default=None, ge=0)


class TraceRead(BaseModel):
    root: str
    direction: str
    nodes: list[TraceNodeRead]
    edges: list[TraceEdgeRead]
    # True for any unread, unsupported or omitted history/traversal window.
    # Optional current balance reads do not change history completion.
    truncated: bool
    interruption: Optional[TraceInterruptionRead] = None
    scope: Literal["native_coin"] = "native_coin"
    root_window: TraceWindowRead = Field(default_factory=TraceWindowRead)
    complete: bool


class WatchedAddressRead(BaseModel):
    """An address already connected in this workspace, for one-click tracing."""

    chain: str
    address: str
    label: str
    connection_id: uuid.UUID
    connection_name: str
