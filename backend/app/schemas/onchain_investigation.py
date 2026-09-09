"""Bounded research selects retained facts; it supplies no ownership or tax proof."""
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.investment_timeline import TimelineEvent


class InvestigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=512)
    leg_id: str = Field(min_length=1, max_length=1024)
    direction: Literal["in", "out"] = "out"
    since: datetime | None = None
    until: datetime | None = None
    max_hops: int = Field(3, ge=1, le=6)
    max_branches: int = Field(3, ge=1, le=5)
    minimums: dict[str, Decimal] = Field(default_factory=dict, max_length=64)

    @field_validator("since", "until")
    @classmethod
    def utc_bounds(cls, value):
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError("Research bounds require an explicit timezone")
            return value.astimezone(timezone.utc)
        return value

    @field_validator("minimums", mode="before")
    @classmethod
    def compatible_minimums(cls, value):
        if not isinstance(value, dict):
            raise ValueError("Minimum amounts require an asset-keyed object")
        if any(isinstance(quantity, float) for quantity in value.values()):
            raise ValueError("Minimum amounts must be exact decimal strings")
        try:
            value = {key: Decimal(quantity) for key, quantity in value.items()}
        except (DecimalException, TypeError, ValueError):
            raise ValueError("Minimum amounts must be valid decimal strings") from None
        if any(not quantity.is_finite() or quantity < 0 or not isinstance(key, str) or len(key) > 128 for key, quantity in value.items()):
            raise ValueError("Minimum amounts must be finite nonnegative quantities keyed by canonical asset")
        return value

    @model_validator(mode="after")
    def ordered_window(self):
        if self.since and self.until and self.since > self.until:
            raise ValueError("The end must be on or after the start")
        return self


class InvestigationContinue(InvestigationRequest):
    collection_id: UUID
    expected_revision: str = Field(min_length=1, max_length=64)
    frontier_key: str = Field(min_length=1, max_length=64)


class InvestigationRead(BaseModel):
    workspace_id: str
    request: InvestigationRequest
    collection_id: UUID | None = None
    revision: str | None = None
    events: list[TimelineEvent] = Field(default_factory=list)
    steps: list[dict] = Field(default_factory=list)
    frontier: list[dict] = Field(default_factory=list)
    boundaries: list[dict] = Field(default_factory=list)
    evidence: dict | None = None
    history_complete: Literal[False] = False


class BridgeReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection_id: UUID
    expected_revision: str = Field(min_length=1, max_length=64)
    source_event_id: str = Field(min_length=1, max_length=512)
    source_leg_id: str = Field(min_length=1, max_length=1024)
    destination_event_id: str = Field(min_length=1, max_length=512)
    destination_leg_id: str = Field(min_length=1, max_length=1024)
    source_id: str = Field(min_length=1, max_length=512)
    destination_source_id: str = Field(min_length=1, max_length=512)
    reviewed: Literal[True]
