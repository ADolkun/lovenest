"""Source facts and reviewed links; decimal strings are part of the API contract."""
from datetime import date, datetime
from decimal import Decimal, DecimalException
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EvidenceLegInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=255)
    asset_symbol: str | None = Field(None, max_length=64)
    asset_id: UUID | None = None
    chain: str | None = Field(None, max_length=64)
    token_address: str | None = Field(None, max_length=255)
    isin: str | None = Field(None, max_length=32)
    provider_asset_id: str | None = Field(None, max_length=255)
    direction: Literal["in", "out", "unknown"] = "unknown"
    classification: str = Field("unknown", max_length=64)
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    execution_currency: str | None = Field(None, max_length=64)
    unit_price_origin: Literal["reported", "derived_execution", "derived_spot", "unknown"] = "unknown"
    subtotal: Decimal | None = None
    total: Decimal | None = None
    fee: Decimal | None = None
    fee_currency: str | None = Field(None, max_length=64)
    valuation_currency: str | None = Field(None, max_length=64)
    valuation_amount: Decimal | None = None
    external_funding_amount: Decimal | None = None
    external_funding_currency: str | None = Field(None, max_length=64)
    acquisition_basis: Decimal | None = None
    transaction_ref: str | None = Field(None, max_length=255)
    leg_ref: str | None = Field(None, max_length=255)
    execution_id: str | None = Field(None, max_length=255)

    @field_validator(
        "quantity", "unit_price", "subtotal", "total", "fee",
        "external_funding_amount", "acquisition_basis", "valuation_amount", mode="before",
    )
    @classmethod
    def exact_decimal(cls, value):
        if isinstance(value, float):
            raise ValueError("Financial amounts must be decimal strings, not JSON floats")
        if value is not None:
            try:
                number = Decimal(value)
            except (DecimalException, TypeError, ValueError):
                raise ValueError("Financial amounts must be valid decimal strings") from None
            if not number.is_finite():
                raise ValueError("Financial amounts must be finite")
            if number < 0:
                raise ValueError("Amounts are unsigned; direction records the sign")
            if len(number.as_tuple().digits) > 128 or (number and abs(number.adjusted()) > 128):
                raise ValueError("Source amount exceeds the supported 128-digit arithmetic range")
        return value


class EvidenceObservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1, max_length=512)
    source_reference: str | None = Field(None, max_length=512)
    source: str = Field(min_length=1, max_length=64)
    source_kind: Literal[
        "primary_activity", "balance_snapshot", "remaining_lots", "tax_workpaper", "recovery_notice"
    ] = "primary_activity"
    provider: str = Field(min_length=1, max_length=64)
    source_account_id: str | None = Field(None, max_length=255)
    account_external_id: str | None = Field(None, max_length=255)
    holding_external_id: str | None = Field(None, max_length=255)
    source_local_id: str | None = Field(None, max_length=255)
    source_locator: str = Field(min_length=1, max_length=512)
    observed_at: datetime | None = None
    event_time_raw: str | None = Field(None, max_length=255)
    event_date: date | None = None
    event_at: datetime | None = None
    timezone: str | None = Field(None, max_length=64)
    time_precision: Literal["unknown", "date", "minute", "second", "fractional"] = "unknown"
    provider_status: str | None = Field(None, max_length=64)
    network_status: str | None = Field(None, max_length=64)
    settlement_status: Literal["settled", "pending", "failed", "unknown"] = "unknown"
    order_ref: str | None = Field(None, max_length=255)
    historical_workspace_label: str | None = Field(None, max_length=255)
    coverage: list[str] = Field(default_factory=list, max_length=100)
    reason_codes: list[str] = Field(default_factory=list, max_length=100)
    source_fields: dict[str, str | None] = Field(default_factory=dict)
    legs: list[EvidenceLegInput] = Field(min_length=1, max_length=100)

    @field_validator("event_at", "observed_at")
    @classmethod
    def aware_time(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("An instant requires a timezone; retain unresolved time as event_time_raw")
        return value

    @model_validator(mode="after")
    def unique_legs(self):
        if len({leg.key for leg in self.legs}) != len(self.legs):
            raise ValueError("Source leg keys must be distinct within an observation")
        if self.time_precision in {"date", "unknown"} and self.event_at is not None:
            raise ValueError("Date-only and unknown observations cannot claim an instant")
        return self

    @field_validator("source_fields")
    @classmethod
    def bounded_source_fields(cls, value):
        allowed = {
            "acquisition_date", "disposal_date", "acquisition_basis", "proceeds",
            "quantity", "unit_price", "subtotal", "total", "fee", "fee_currency",
            "valuation_currency", "valuation_amount", "external_funding_amount",
            "external_funding_currency", "classification",
            "execution_currency", "unit_price_currency", "subtotal_currency", "total_currency", "unit_price_origin",
        }
        if value.keys() - allowed or any(v is not None and len(v) > 512 for v in value.values()):
            raise ValueError("Only bounded, named source evidence fields may be retained")
        return value


class EvidenceAllocation(BaseModel):
    leg_id: UUID
    quantity: Decimal | None = None

    @field_validator("quantity", mode="before")
    @classmethod
    def exact_decimal(cls, value):
        return EvidenceLegInput.exact_decimal(value)


class EvidenceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_ref: str
    leg_key: str
    action: Literal["retain", "apply", "link"] = "retain"
    allocations: list[EvidenceAllocation] = Field(default_factory=list, max_length=100)
    reason: str | None = Field(None, max_length=500)
    settlement_confirmed: bool = False


class EvidenceOpeningBoundary(BaseModel):
    as_of: date
    overlap_reviewed: bool
    assumption: str = Field(min_length=1, max_length=500)


class EvidenceTarget(BaseModel):
    workspace_id: UUID
    workspace_name: str
    group_id: UUID
    group_name: str
    account_id: UUID | None = None


class EvidenceSourceRef(BaseModel):
    observation_ref: str
    source: str
    source_local_id: str | None = None
    source_locator: str
    leg_key: str


class EvidenceCandidate(BaseModel):
    leg_id: UUID
    event_id: UUID
    asset_symbol: str | None = None
    direction: str
    classification: str
    quantity: Decimal | None = None
    event_date: date | None = None
    source_refs: list[EvidenceSourceRef] = Field(default_factory=list)


class EvidenceEffects(BaseModel):
    ledger_rows: int = 0
    units_delta: Decimal | None = Decimal("0")
    basis_delta: Decimal | None = None


class EvidenceLinkedLeg(BaseModel):
    link_id: UUID
    leg: EvidenceCandidate
    quantity: Decimal | None = None


class EvidenceRecord(BaseModel):
    observation_ref: str
    leg_key: str
    match_status: Literal["linked", "candidate", "conflicting", "unmatched"]
    application_status: Literal["already_applied", "eligible", "blocked", "not_applicable"]
    source_refs: list[EvidenceSourceRef] = Field(default_factory=list)
    candidate_legs: list[EvidenceCandidate] = Field(default_factory=list)
    link_ids: list[UUID] = Field(default_factory=list)
    links: list[EvidenceLinkedLeg] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    conflicting_fields: list[str] = Field(default_factory=list)
    effects: EvidenceEffects = Field(default_factory=EvidenceEffects)


class EvidenceReconciliation(BaseModel):
    asset_symbol: str | None = None
    chain: str | None = None
    token_address: str | None = None
    provider_asset_id: str | None = None
    isin: str | None = None
    opening_quantity: Decimal | None = None
    opening_assumption: str = "unknown"
    opening_as_of: date | None = None
    snapshot_quantity: Decimal | None = None
    snapshot_as_of: str | None = None
    settled_movement_quantity: Decimal = Decimal("0")
    expected_closing_quantity: Decimal | None = None
    discrepancy: Decimal | None = None
    missing_coverage: list[str] = Field(default_factory=list)
    unresolved_fee_semantics: bool = True
    unresolved_funding_semantics: bool = True
    basis_complete: bool = False
    history_complete: bool = False


class EvidencePreview(BaseModel):
    revision: str
    target: EvidenceTarget
    observations: list[EvidenceObservationInput]
    records: list[EvidenceRecord]
    reconciliation: list[EvidenceReconciliation] = Field(default_factory=list)


class EvidenceConfirmRequest(BaseModel):
    group_id: UUID
    decisions: list[EvidenceDecision]
    expected_revision: str
    opening_boundary: EvidenceOpeningBoundary | None = None
    allow_unpriced: bool = False


class EvidenceResult(BaseModel):
    import_log_id: UUID | None = None
    imported: int = 0
    retained: int = 0
    linked: int = 0
    evidence: EvidencePreview
