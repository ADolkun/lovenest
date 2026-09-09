"""Owned history admission. Exported source quantities remain decimal strings."""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.investment_evidence import EvidenceObservationInput


class HistoricalTokenAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str | None = Field(None, min_length=1, max_length=255)
    scriptpubkey: str | None = Field(None, min_length=2, max_length=20000, pattern=r"^(?:[0-9a-fA-F]{2})+$")
    owner: str = Field(min_length=1, max_length=255)
    reviewed: Literal[True]

    @model_validator(mode="after")
    def one_endpoint(self):
        if bool(self.address) == bool(self.scriptpubkey):
            raise ValueError("Supply exactly one address or script")
        return self


class HistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: UUID
    chain: str = Field("solana", max_length=64)
    address: str = Field(min_length=1, max_length=255)
    ownership_confirmed: Literal[True]
    since: datetime | None = None
    until: datetime | None = None
    start_block: int | None = Field(None, ge=0)
    end_block: int | None = Field(None, ge=0)
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
        if self.start_block is not None and self.end_block is not None and self.start_block > self.end_block:
            raise ValueError("The end block must be on or after the start")
        if len({(item.address, item.scriptpubkey) for item in self.supplied_accounts}) != len(self.supplied_accounts):
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
