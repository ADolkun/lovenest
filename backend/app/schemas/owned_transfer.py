"""Reviewed movements and lineage: all exact amounts serialize as decimal strings."""
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.investment_evidence import EvidenceLegInput


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class OwnershipCreate(Contract):
    group_id: UUID
    beneficial_owner: str = Field(min_length=1, max_length=128)
    chain: str = Field(min_length=1, max_length=64)
    address: str | None = Field(None, max_length=255)
    source_account_id: str | None = Field(None, max_length=255)
    valid_from: date | None = None
    valid_until: date | None = None
    reason: str = Field(min_length=1, max_length=500)
    evidence_observation_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def scope(self):
        if not self.address and not self.source_account_id:
            raise ValueError("An address or source account must be explicitly identified")
        if self.valid_from and self.valid_until and self.valid_from > self.valid_until:
            raise ValueError("Ownership interval is reversed")
        return self


class OwnershipRead(OwnershipCreate):
    id: UUID
    workspace_id: UUID
    asserted_by: UUID | None
    asserted_at: datetime
    revoked_at: datetime | None = None


class LotSelection(Contract):
    lot_id: str = Field(min_length=1, max_length=1024)
    quantity: Decimal

    @field_validator("quantity", mode="before")
    @classmethod
    def exact(cls, value):
        return EvidenceLegInput.exact_decimal(value)

    @field_validator("quantity")
    @classmethod
    def positive(cls, value):
        if value <= 0:
            raise ValueError("Selected quantity must be positive")
        return value


class LotRead(Contract):
    lot_id: str
    asset_id: UUID
    root_transaction_id: UUID | None = None
    source_leg_id: UUID | None = None
    quantity: Decimal
    acquired: date | None = None
    acquisition_cost: Decimal | None = None
    basis_complete: bool
    lineage: list[UUID] = Field(default_factory=list)
    missing_links: list[str] = Field(default_factory=list)


class MovementSelection(Contract):
    leg_id: UUID
    asset_id: UUID
    ownership_id: UUID
    allocations: list[LotSelection] = Field(default_factory=list, max_length=100)
    reason: str = Field(min_length=1, max_length=500)


class TransferPreviewRequest(Contract):
    out_leg_id: UUID
    in_leg_id: UUID
    source_asset_id: UUID
    destination_asset_id: UUID
    source_ownership_id: UUID
    destination_ownership_id: UUID
    allocations: list[LotSelection] = Field(default_factory=list, max_length=100)
    fees: list[MovementSelection] = Field(default_factory=list, max_length=100)
    reason: str = Field(min_length=1, max_length=500)
    # Date-only source records may need an explicit chronological review.
    ordering_reviewed: bool = False


class TransferConfirmRequest(TransferPreviewRequest):
    expected_revision: str = Field(min_length=1, max_length=64)


class MovementPreviewRequest(MovementSelection):
    ordering_reviewed: bool = False


class MovementConfirmRequest(MovementPreviewRequest):
    expected_revision: str = Field(min_length=1, max_length=64)


class MovementRead(Contract):
    leg_id: UUID
    observation_ref: str
    leg_key: str
    observation_id: UUID
    group_id: UUID | None
    asset_id: UUID | None = None
    source: str
    source_local_id: str | None = None
    source_locator: str
    direction: str
    classification: str
    quantity: Decimal | None = None
    chain: str | None = None
    token_address: str | None = None
    token_program: str | None = None
    transaction_ref: str | None = None
    leg_ref: str | None = None
    source_address: str | None = None
    destination_address: str | None = None
    source_owner: str | None = None
    destination_owner: str | None = None
    raw_units: str | None = None
    decimals: int | None = None
    quantity_role: str | None = None
    fee_payer: str | None = None
    event_date: date | None = None
    event_at: datetime | None = None
    time_precision: str
    provider_status: str | None = None
    network_status: str | None = None
    settlement_status: str
    application_id: UUID | None = None
    application_status: Literal["unapplied", "applied", "reversed"] = "unapplied"
    reason_codes: list[str] = Field(default_factory=list)


class HoldingEffect(Contract):
    asset_id: UUID
    quantity: Decimal
    known_basis_quantity: Decimal
    unknown_basis_quantity: Decimal
    known_acquisition_cost: Decimal
    performance_basis: Decimal | None
    basis_complete: bool
    settlement_complete: bool
    realized_gain: Decimal | None
    known_realized_gain: Decimal
    unknown_disposition_quantity: Decimal
    missing_links: list[str] = Field(default_factory=list)
    lots: list[LotRead] = Field(default_factory=list)


class TransferPreview(Contract):
    workspace_id: UUID
    revision: str
    status: Literal["exact", "candidate", "conflicting"]
    can_confirm: bool
    reason_codes: list[str] = Field(default_factory=list)
    out_movement: MovementRead
    in_movement: MovementRead
    available_lots: list[LotRead]
    principal_quantity: Decimal | None
    acquisition_cost: Decimal | None
    known_acquisition_cost: Decimal = Decimal("0")
    performance_basis: Decimal | None
    unknown_basis_quantity: Decimal
    fee_movements: list[MovementRead] = Field(default_factory=list)
    effects: list[HoldingEffect] = Field(default_factory=list)


class TransferRead(Contract):
    id: UUID
    workspace_id: UUID
    revision: str
    status: Literal["confirmed", "unresolved", "reversed"]
    request: TransferPreviewRequest
    principal_quantity: Decimal
    acquisition_cost: Decimal | None
    known_acquisition_cost: Decimal = Decimal("0")
    performance_basis: Decimal | None
    unknown_basis_quantity: Decimal
    reason_codes: list[str] = Field(default_factory=list)
    created_at: datetime
    reversed_at: datetime | None = None
    effects: list[HoldingEffect] = Field(default_factory=list)


class MovementPreview(Contract):
    workspace_id: UUID
    revision: str
    can_confirm: bool
    reason_codes: list[str]
    movement: MovementRead
    available_lots: list[LotRead]
    selected_lots: list[LotRead] = Field(default_factory=list)
    effects: list[HoldingEffect] = Field(default_factory=list)


class MovementApplicationRead(Contract):
    id: UUID
    workspace_id: UUID
    revision: str
    status: Literal["applied", "unresolved", "reversed"]
    request: MovementPreviewRequest
    created_at: datetime
    reversed_at: datetime | None = None
    reason_codes: list[str] = Field(default_factory=list)
    selected_lots: list[LotRead] = Field(default_factory=list)
    effects: list[HoldingEffect] = Field(default_factory=list)


class IncidentCreate(Contract):
    leg_id: UUID
    allegation: Literal["reported_scam"] = "reported_scam"
    source_status: Literal["user_reported", "documented", "disputed"] = "user_reported"
    note: str = Field(min_length=1, max_length=2000)
    evidence_observation_ids: list[UUID] = Field(default_factory=list, max_length=100)
    related_fee_leg_ids: list[UUID] = Field(default_factory=list, max_length=100)


class IncidentRead(IncidentCreate):
    id: UUID
    workspace_id: UUID
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    tax_treatment: Literal["unresolved"] = "unresolved"


class TransferHolding(Contract):
    id: UUID
    group_id: UUID | None
    name: str
    ticker: str | None
    currency: str
    units: Decimal | None
    is_archived: bool


class TransferIndex(Contract):
    workspace_id: UUID
    revision: str
    transfers: list[TransferRead]
    movements: list[MovementRead]
    applications: list[MovementApplicationRead]
    ownership: list[OwnershipRead]
    holdings: list[TransferHolding]
    incidents: list[IncidentRead]


class LotsRead(Contract):
    revision: str
    asset_id: UUID
    lots: list[LotRead]
    missing_links: list[str] = Field(default_factory=list)
