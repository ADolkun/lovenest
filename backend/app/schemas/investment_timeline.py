"""Read-only activity: source facts, qualified relationships and bounded coverage."""
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.investment_evidence import EvidenceLegInput


class TimelineTime(BaseModel):
    event_at: datetime | None = None
    event_date: date | None = None
    event_time_raw: str | None = None
    timezone: str | None = None
    time_precision: str = "unknown"
    ordering: Literal["exact", "within_day_unknown", "unknown", "conflicting"] = "unknown"


class TimelineAsset(BaseModel):
    canonical_asset_key: str
    asset_symbol: str | None = None
    chain: str | None = None
    token_address: str | None = None
    token_program: str | None = None
    identity_status: Literal["canonical", "holding", "unresolved"]
    asset_ids: list[str] = Field(default_factory=list)


class TimelineAccount(BaseModel):
    group_id: str | None = None
    group_name: str | None = None
    account_id: str | None = None
    connection_id: str | None = None


class TimelineSource(BaseModel):
    source_id: str
    source: str
    provider: str
    source_kind: str
    source_local_id: str | None = None
    source_locator: str | None = None
    source_reference: str | None = None
    observed_at: datetime | None = None
    time: TimelineTime = Field(default_factory=TimelineTime)
    original_type: str | None = None
    provider_status: str | None = None
    network_status: str | None = None
    settlement_status: str = "unknown"
    is_current: bool = True
    availability: Literal["available", "unavailable"] = "available"
    unavailable_reason: str | None = None
    detail_url: str
    collection_id: str | None = None
    payload_digest: str | None = None
    decoder_version: str | None = None
    account: TimelineAccount = Field(default_factory=TimelineAccount)


class TimelineLeg(EvidenceLegInput):
    leg_id: str
    canonical_asset_key: str
    group_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    settlement_status: str = "unknown"
    execution_status: str | None = None
    interpretation: str | None = None
    non_additive: bool = False
    is_current: bool = True
    reason_codes: list[str] = Field(default_factory=list)


class TimelineRelationship(BaseModel):
    kind: str
    state: str
    event_id: str | None = None
    source_id: str | None = None
    review_id: str | None = None
    leg_id: str | None = None
    quantity: Decimal | None = None
    reason_codes: list[str] = Field(default_factory=list)
    conflicting_fields: list[str] = Field(default_factory=list)
    review_url: str | None = None


class TimelineBasis(BaseModel):
    state: Literal["known", "partial", "unknown"] = "unknown"
    acquisition_cost: Decimal | None = None
    known_acquisition_cost: Decimal | None = None
    unknown_basis_quantity: Decimal | None = None
    reason_codes: list[str] = Field(default_factory=lambda: ["acquisition_evidence_unknown"])


class TimelineCoverage(BaseModel):
    coverage_id: str
    source: str
    group_id: str | None = None
    connection_id: str | None = None
    collection_id: str | None = None
    chain: str | None = None
    requested: dict = Field(default_factory=dict)
    observed: dict = Field(default_factory=dict)
    last_successful_collection: datetime | None = None
    inventory: str = "unknown"
    retrieval: str = "unknown"
    interpretation: str = "unknown"
    settlement: str = "unknown"
    gaps: list[str] = Field(default_factory=list)
    streams: dict = Field(default_factory=dict)
    source_url: str | None = None
    history_complete: Literal[False] = False


class TimelineEvent(BaseModel):
    event_id: str
    kind: str
    status: str
    linkage: Literal["confirmed", "candidate", "conflicting", "unresolved"] = "unresolved"
    time: TimelineTime = Field(default_factory=TimelineTime)
    accounts: list[TimelineAccount] = Field(default_factory=list)
    assets: list[TimelineAsset] = Field(default_factory=list)
    legs: list[TimelineLeg] = Field(default_factory=list)
    sources: list[TimelineSource] = Field(default_factory=list)
    relationships: list[TimelineRelationship] = Field(default_factory=list)
    basis: TimelineBasis = Field(default_factory=TimelineBasis)
    tax_treatment: Literal["unresolved"] = "unresolved"
    native_trace_url: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    conflicting_fields: list[str] = Field(default_factory=list)
    coverage: list[TimelineCoverage] = Field(default_factory=list)
    transfers: list[dict] = Field(default_factory=list)
    recovery: list[dict] = Field(default_factory=list)
    incidents: list[dict] = Field(default_factory=list)


class TimelineRead(BaseModel):
    workspace_id: str
    revision: str
    events: list[TimelineEvent]
    assets: list[TimelineAsset]
    coverage: list[TimelineCoverage]
    total: int
    limit: int
    offset: int
    has_more: bool
    all_available_records_loaded: bool
    history_complete: Literal[False] = False
    errors: list[dict] = Field(default_factory=list)


class TimelineSourceDetail(BaseModel):
    workspace_id: str
    source: TimelineSource
    observation: dict | None = None
    raw_payload: dict | None = None
    transaction: dict | None = None
    coverage: list[TimelineCoverage] = Field(default_factory=list)
