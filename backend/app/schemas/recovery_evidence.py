"""Recovery observations and reviewed assertions, never financial instructions."""
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.schemas.investment_evidence import EvidenceLegInput, EvidenceObservationInput

def exact_amount(value):
    if isinstance(value, bool):
        raise ValueError('A boolean is not a financial amount')
    return EvidenceLegInput.exact_decimal(value)


Amount = Annotated[Decimal, BeforeValidator(exact_amount)]
Role = Literal['allowed_claim', 'platform_ledger', 'recovery_notice', 'receiving_receipt', 'disposition', 'cash_proceeds', 'equity_statement', 'tax_workpaper']
State = Literal['confirmed', 'candidate', 'conflict', 'missing']
AssertionStatus = Literal['reported', 'modeled', 'unverified', 'supported', 'conflict', 'missing']
Text = Annotated[str, Field(max_length=500)]


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', str_max_length=2000)


class RecoveryDetails(Contract):
    account_bucket: Text | None = None
    boundary_kind: Text | None = None
    claim_amount: Amount | None = None
    claim_currency: Text | None = None
    proceeds: Amount | None = None
    proceeds_currency: Text | None = None
    cash_credited: Amount | None = None
    cash_currency: Text | None = None
    acquisition_date: date | None = None
    statement_date: date | None = None
    reported_cost: Amount | None = None
    reported_cost_currency: Text | None = None
    provisional_allocation: Amount | None = None
    allocation_currency: Text | None = None


class RecoveryEntryInput(Contract):
    key: str = Field(min_length=1, max_length=255)
    observation_id: UUID | None = None
    observation: EvidenceObservationInput | None = None
    leg_key: str = Field(min_length=1, max_length=255)
    case_key: str = Field(min_length=1, max_length=255)
    round_key: Text | None = None
    round_asset_key: Text | None = None
    role: Role
    reported_state: State = 'candidate'
    details: RecoveryDetails = Field(default_factory=RecoveryDetails)
    missing_evidence: list[Text] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def source(self):
        if (self.observation_id is None) == (self.observation is None):
            raise ValueError('Supply an existing observation ID or a new observation, exclusively')
        if self.round_asset_key and not self.round_key:
            raise ValueError('A round asset requires a round key')
        return self


class RecoveryApplication(Contract):
    leg_id: UUID | None = None
    application_id: UUID | None = None
    status: Literal['unapplied', 'applied', 'reversed', 'unsupported'] = 'unsupported'
    reason_codes: list[str] = Field(default_factory=list)


class RecoveryEntryRead(Contract):
    source_group_id: UUID | None = None
    source_group_name: str | None = None
    id: UUID | None = None
    key: str
    observation_id: UUID | None = None
    observation: EvidenceObservationInput
    leg_key: str
    case_key: str
    round_key: str | None
    round_asset_key: str | None
    role: Role
    reported_state: State
    details: RecoveryDetails
    missing_evidence: list[str]
    application: RecoveryApplication = Field(default_factory=RecoveryApplication)
    reason_codes: list[str] = Field(default_factory=list)


class RecoveryReviewInput(Contract):
    key: str = Field(min_length=1, max_length=255)
    kind: Literal['relation', 'assertion', 'correction', 'allocation']
    entry_id: UUID
    target_entry_id: UUID | None = None
    supersedes_id: UUID | None = None
    relation_kind: Literal['claim_notice', 'notice_receipt', 'receipt_disposition', 'disposition_proceeds', 'candidate_acquisition', 'owned_transfer_reference'] | None = None
    relation_state: State | None = None
    assertion_kind: Literal['reported_cost', 'provisional_allocation', 'valuation', 'account_mapping', 'lot_mapping', 'accounting_assumption', 'filing_assertion'] | None = None
    assertion_status: AssertionStatus | None = None
    value: Amount | None = None
    currency: Text | None = None
    field: Text | None = None
    proposed_value: Text | None = None
    source_locator: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=2000)
    supporting_observation_ids: list[UUID] = Field(default_factory=list, max_length=100)
    required_entry_ids: list[UUID] = Field(default_factory=list, max_length=100)
    required_review_ids: list[UUID] = Field(default_factory=list, max_length=100)
    missing_evidence: list[Text] = Field(default_factory=list, max_length=100)
    conflicting_fields: list[Text] = Field(default_factory=list, max_length=100)
    owned_transfer_id: UUID | None = None
    account_mapping_evidence: Text | None = None
    timing_evidence: Text | None = None
    quantity_adjustment: Decimal | None = None
    adjustment_evidence: Text | None = None

    @model_validator(mode='before')
    @classmethod
    def signed_adjustment(cls, data):
        if isinstance(data, dict) and data.get('quantity_adjustment') is not None:
            raw = data['quantity_adjustment']
            if isinstance(raw, (float, bool)):
                raise ValueError('Quantity adjustment must be an exact decimal string')
            try:
                number = Decimal(raw)
            except (ValueError, TypeError, ArithmeticError):
                raise ValueError('Quantity adjustment must be a valid decimal') from None
            EvidenceLegInput.exact_decimal(number.copy_abs())
        return data

    @model_validator(mode='after')
    def semantics(self):
        if self.kind == 'relation':
            if not self.relation_kind or not self.relation_state:
                raise ValueError('A relation requires a kind and independent relation state')
            if self.target_entry_id is None and self.relation_state not in {'missing', 'candidate'}:
                raise ValueError('A confirmed or conflicting relation needs a target')
            if self.assertion_kind or self.assertion_status or self.field or self.proposed_value is not None:
                raise ValueError('Relation and assertion/correction fields are separate')
        else:
            if self.relation_kind or self.relation_state or self.owned_transfer_id:
                raise ValueError('Only relations may carry relationship fields')
            if not self.assertion_status:
                raise ValueError('Assertions, corrections and models require a status')
            if self.kind == 'assertion' and not self.assertion_kind:
                raise ValueError('An assertion requires its kind')
            if self.kind == 'correction' and not self.field:
                raise ValueError('A correction requires a named field/reference')
            if self.kind == 'allocation' and self.assertion_status not in {'modeled', 'unverified', 'conflict', 'missing'}:
                raise ValueError('An allocation is a review model, not a finalized tax or basis decision')
        if self.assertion_status == 'supported' and not self.supporting_observation_ids:
            raise ValueError('A supported assertion requires source observations')
        if self.quantity_adjustment is not None and not self.adjustment_evidence:
            raise ValueError('A quantity adjustment requires documented rounding or fee evidence')
        for values in (self.supporting_observation_ids, self.required_entry_ids, self.required_review_ids):
            if len(values) != len(set(values)):
                raise ValueError('Repeated evidence references are not allowed')
        return self


class RecoveryReviewRead(RecoveryReviewInput):
    id: UUID
    created_at: datetime
    created_by: UUID | None = None
    is_current: bool
    blockers: list[str] = Field(default_factory=list)
    ready_for_review: bool = False


class RecoveryPackage(Contract):
    workspace_id: UUID
    group_id: UUID
    revision: str
    entries: list[RecoveryEntryRead]
    reviews: list[RecoveryReviewRead]
    round_count: int = 0
    asset_record_count: int = 0
    missing_evidence: list[str] = Field(default_factory=list)
    allocation_blockers: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)


class RecoveryPreviewRequest(Contract):
    group_id: UUID
    entries: list[RecoveryEntryInput] = Field(default_factory=list, max_length=100)


class RecoveryRetainRequest(RecoveryPreviewRequest):
    expected_revision: str = Field(min_length=1, max_length=64)


class RecoveryReviewsRequest(Contract):
    group_id: UUID
    reviews: list[RecoveryReviewInput] = Field(min_length=1, max_length=100)
    expected_revision: str = Field(min_length=1, max_length=64)
