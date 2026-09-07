"""Owned history admission. Exported source quantities remain decimal strings."""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.investment_evidence import EvidenceObservationInput


class HistoricalTokenAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(min_length=1, max_length=255)
    owner: str = Field(min_length=1, max_length=255)
    reviewed: Literal[True]


class HistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: UUID
    chain: str = Field("solana", max_length=64)
    address: str = Field(min_length=1, max_length=255)
    ownership_confirmed: Literal[True]
    since: datetime | None = None
    until: datetime | None = None
    supplied_accounts: list[HistoricalTokenAccount] = Field(default_factory=list, max_length=64)
    collection_id: UUID | None = None
    expected_revision: str | None = Field(None, max_length=64)
    reobserve: bool = False

    @field_validator("since", "until")
    @classmethod
    def utc_time(cls, value):
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError("History bounds require an explicit timezone")
            return value.astimezone(timezone.utc)
        return value

    @model_validator(mode="after")
    def valid_window(self):
        if self.since and self.until and self.since > self.until:
            raise ValueError("The end must be on or after the start")
        if bool(self.collection_id) != bool(self.expected_revision):
            raise ValueError("Resuming requires a saved collection and its revision")
        if len({item.address for item in self.supplied_accounts}) != len(self.supplied_accounts):
            raise ValueError("Historical token accounts must be distinct")
        return self


class HistoryRead(BaseModel):
    collection_id: UUID
    revision: str
    request: dict
    evidence: dict
    observations: list[EvidenceObservationInput]
    group_id: UUID | None = None
    updated_at: datetime


class HistorySummary(BaseModel):
    collection_id: UUID
    revision: str
    request: dict
    updated_at: datetime
    coverage: dict
    transaction_count: int
