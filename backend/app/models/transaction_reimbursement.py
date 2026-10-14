"""A reimbursement credit booked against the expense debits it repays (ADR 0012).

N:N with an amount: one employer payment covers a trip's meals, and one
family-plan subscription is repaid by several Zelles. Reporting reads the
links through `reimbursement_unlinked_share` in `_query_filters.py`, which
takes the linked part out of both rows in their own months.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TransactionReimbursement(Base):
    __tablename__ = "transaction_reimbursements"
    __table_args__ = (
        UniqueConstraint(
            "reimbursement_id", "expense_id", name="uq_transaction_reimbursements_pair"
        ),
        CheckConstraint("amount > 0", name="ck_transaction_reimbursements_amount_positive"),
        CheckConstraint(
            "reimbursement_id <> expense_id", name="ck_transaction_reimbursements_distinct"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    # CASCADE at the database, not the ORM: bulk delete removes transactions
    # with a SQL DELETE that never loads the rows, and a link left pointing at
    # nothing would keep shrinking the surviving side's figures.
    reimbursement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    expense_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    # In the currency both rows share; linking across currencies is refused.
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=15, scale=2))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
