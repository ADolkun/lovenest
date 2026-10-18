import uuid
from decimal import Decimal

from pydantic import BaseModel

from app.schemas.transaction import TransactionRead


class ReimbursementAllocationInput(BaseModel):
    expense_id: uuid.UUID
    amount: Decimal


class ReimbursementAllocationsUpdate(BaseModel):
    """The whole set of expenses a credit repays. Replaces what was there;
    an empty list unlinks the credit from everything."""

    allocations: list[ReimbursementAllocationInput]


class ReimbursementCandidate(BaseModel):
    transaction: TransactionRead
    #: The expense amount not yet claimed by other credits.
    remaining: float
    #: What the credit being edited already links to this expense.
    allocated_here: float
