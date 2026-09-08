"""Read-only valuation provenance; absent source facts stay unknown."""
import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class BalanceHolding(BaseModel):
    asset_id: uuid.UUID
    wallet_id: uuid.UUID | None = None
    name: str
    ticker: str | None = None
    type: str
    quantity: float | None = None
    value: float | None = None
    currency: str
    observed_at: datetime | None = None
    observation_basis: Literal["quote", "recorded", "source", "unknown"] = "unknown"
    value_date: date | None = None


class BalanceExplanation(BaseModel):
    workspace_id: uuid.UUID
    wallet_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    connection_id: uuid.UUID | None = None
    association: Literal["resolved", "missing_or_ambiguous"] = "missing_or_ambiguous"
    basis: Literal[
        "reported_account_total", "connector_calculated_subtotal", "cash_ledger", "unknown",
    ] = "unknown"
    amount: float | None = None
    currency: str | None = None
    observed_at: datetime | None = None
    observation_basis: Literal["source", "collection", "unknown"] = "unknown"
    last_successful_sync_at: datetime | None = None
    refresh_status: str = "unknown"
    refresh_observed_at: datetime | None = None
    coverage: Literal["complete", "partial", "unknown"] = "unknown"
    reason_codes: list[str] = Field(default_factory=list)
    unreadable_count: int | None = None
    omitted_count: int | None = None
    holdings: list[BalanceHolding] = Field(default_factory=list)
    holdings_value: float | None = None
    holdings_currency: str | None = None
    holdings_coverage: Literal["complete", "partial", "unavailable"] = "unavailable"
    residual_cash: float | None = None
    reconciliation: Literal[
        "matched", "residual_derived", "difference", "not_comparable", "shared_derivation", "separate_cash_ledger",
    ] = "not_comparable"
    difference: float | None = None
