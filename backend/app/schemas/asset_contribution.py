import uuid
from datetime import date as _date
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Which way the money crossed the wallet's boundary. `amount` is always
#: positive; this carries the sign.
ContributionKind = Literal["contribution", "distribution"]

#: Whose money it was. Employer contributions are tracked apart from the user's
#: own because the annual limits that apply to each differ, and because
#: employer money is not the user's until it vests.
ContributionParty = Literal["self", "employer"]


class AssetContributionBase(BaseModel):
    kind: ContributionKind
    party: ContributionParty = "self"
    amount: Decimal = Field(gt=0)
    date: _date
    #: CONTEXT.md's Tax Year, which is not always `date.year`. Defaults to
    #: `date.year` when the caller does not say.
    tax_year: Optional[int] = Field(default=None, ge=1900, le=2200)
    vested_on: Optional[_date] = None
    notes: Optional[str] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _reject_combinations_that_mean_nothing(self):
        if self.party == "employer" and self.kind != "contribution":
            raise ValueError("Only a contribution can come from an employer")
        if self.vested_on is not None and self.party != "employer":
            raise ValueError("Only employer money vests")
        return self


class AssetContributionCreate(AssetContributionBase):
    group_id: uuid.UUID


class AssetContributionUpdate(BaseModel):
    kind: Optional[ContributionKind] = None
    party: Optional[ContributionParty] = None
    amount: Optional[Decimal] = Field(default=None, gt=0)
    date: Optional[_date] = None
    tax_year: Optional[int] = Field(default=None, ge=1900, le=2200)
    vested_on: Optional[_date] = None
    notes: Optional[str] = Field(default=None, max_length=500)


class AssetContributionRead(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    kind: ContributionKind
    party: ContributionParty
    amount: float
    date: _date
    tax_year: int
    vested_on: Optional[_date] = None
    #: Derived, never stored: whether `vested_on` has arrived. A stored flag
    #: would be wrong the morning after it was written.
    is_vested: bool = True
    source: str = "manual"
    notes: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class ContributionYearRead(BaseModel):
    """One wallet's contribution totals for one tax year."""

    tax_year: int
    own: float
    employer: float
    distributions: float
    net: float


class ContributionSummaryRead(BaseModel):
    """Net Contribution for one wallet, and the parts it is made of.

    `net` is the figure CONTEXT.md calls Net Contribution: what the account's
    own money amounts to. `employer_unvested` is deliberately outside it.
    """

    group_id: uuid.UUID
    own_contributions: float
    employer_contributions: float
    employer_vested: float
    employer_unvested: float
    distributions: float
    net: float
    #: Wallet value now minus every dollar paid in (vested or not) less what
    #: was taken out, so a balance that rose only because money was deposited
    #: shows no gain. Null where the wallet's value is unknown.
    return_net_of_contributions: Optional[float] = None
    current_value: Optional[float] = None
    years: list[ContributionYearRead] = []
