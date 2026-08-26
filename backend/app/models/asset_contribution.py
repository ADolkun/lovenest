import uuid
from datetime import date as _date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.asset_group import AssetGroup


class AssetContribution(Base):
    """Money crossing the boundary of a Wallet: a Contribution in, a
    Distribution out (CONTEXT.md).

    Not a Trade. A buy moves money *inside* the account and leaves the account
    total alone; a Contribution changes what the account holds of the user's
    own money. Keeping them in one table would make every ledger replay filter
    on kind, and `_recompute` has no meaning for a row with no ticker.

    One table for both directions, because Net Contribution is a signed sum and
    two tables double every query that computes it. `amount` is always
    positive; `kind` carries the sign.
    """

    __tablename__ = "asset_contributions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('contribution', 'distribution')",
            name="ck_asset_contributions_kind",
        ),
        CheckConstraint(
            "party IN ('self', 'employer')",
            name="ck_asset_contributions_party",
        ),
        CheckConstraint("amount > 0", name="ck_asset_contributions_amount_positive"),
        # Only an employer contributes employer money, and only employer money
        # vests. A distribution has no party — it is the account paying out,
        # whoever put the money there — so allowing one would invent a figure
        # ("employer distributions") that nothing can compute.
        CheckConstraint(
            "party = 'self' OR kind = 'contribution'",
            name="ck_asset_contributions_employer_is_a_contribution",
        ),
        CheckConstraint(
            "vested_on IS NULL OR party = 'employer'",
            name="ck_asset_contributions_only_employer_money_vests",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    #: The Wallet the money moved into or out of. A Wallet is exactly one
    #: brokerage account (CONTEXT.md), which is what makes "per account" a
    #: question this column can answer.
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("asset_groups.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(12))  # contribution, distribution
    party: Mapped[str] = mapped_column(String(8), default="self")  # self, employer
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=15, scale=2))
    date: Mapped[_date] = mapped_column(Date, index=True)
    #: The year the contribution counts against, which is not always the year
    #: it was paid: an IRA contribution made before April 15 may be designated
    #: for the prior year, and Fidelity's own export says so in as many words
    #: ("CASH CONTRIBUTION CURRENT YEAR"). Progress against an annual limit is
    #: measured in this year, never in `date`'s.
    tax_year: Mapped[int] = mapped_column(Integer, index=True)
    #: When employer money becomes the user's. Null means no vesting schedule
    #: applies — the reading an absent restriction deserves, and what an
    #: immediately-vested safe-harbour match actually is.
    vested_on: Mapped[Optional[_date]] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual, import
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    #: The import that wrote this row, so deleting that import can take it back
    #: out again — the same undo contract asset_transactions has.
    import_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_logs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    group: Mapped["AssetGroup"] = relationship()
