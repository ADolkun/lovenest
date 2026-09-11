"""Source declarations are interpretations; financial effects need a separate review."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.investment_evidence import EvidenceTarget

AmountField = Literal["unit_price", "subtotal", "total", "valuation_amount", "fee", "acquisition_basis"]
AmountMeaning = Literal["execution_unit_price", "execution_subtotal", "fee_inclusive_total", "valuation", "reported_fee", "reported_basis", "unknown"]


class SourceSemantics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clock_role: Literal["execution", "posted", "settled", "reported_unknown"]
    amount_field: AmountField
    amount_meaning: AmountMeaning
    decimal_places: int | None = Field(None, ge=0, le=128)


class SourceReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    group_id: UUID
    request_key: str = Field(min_length=1, max_length=128)
    action: Literal["associate", "revoke", "correct", "reverse"]
    reason: str = Field(min_length=1, max_length=500)
    source_leg_id: UUID | None = None
    target_leg_id: UUID | None = None
    source_semantics: SourceSemantics | None = None
    target_semantics: SourceSemantics | None = None
    same_execution_reviewed: bool = False
    review_id: UUID | None = None

    @model_validator(mode="after")
    def action_fields(self):
        if self.action == "associate":
            if not all((self.source_leg_id, self.target_leg_id, self.source_semantics, self.target_semantics)) or self.review_id:
                raise ValueError("Association requires two source legs and both declarations")
        elif not self.review_id or any((self.source_leg_id, self.target_leg_id, self.source_semantics, self.target_semantics, self.same_execution_reviewed)):
            raise ValueError("This action requires only its retained review ID and reason")
        return self


class SourceReviewConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: SourceReviewRequest
    expected_revision: str
    preview_digest: str


class SourceReviewRead(BaseModel):
    id: UUID
    request_key: str
    supersedes_id: UUID | None
    created_by: UUID | None
    created_at: datetime
    payload: dict


class SourceReviewLeg(BaseModel):
    leg_id: UUID
    observation_ref: str
    leg_key: str
    transaction_id: UUID | None


class SourceReviewPackage(BaseModel):
    revision: str
    target: EvidenceTarget
    legs: list[SourceReviewLeg]
    reviews: list[SourceReviewRead]


class SourceReviewPreview(BaseModel):
    revision: str
    preview_digest: str
    target: EvidenceTarget
    request: SourceReviewRequest
    supported: bool
    blockers: list[str]
    effects: dict
    sources: list[dict]
