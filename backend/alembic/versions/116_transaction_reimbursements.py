"""a reimbursement is booked against the expense it repays

Adds `transaction_reimbursements`: a reimbursement credit linked to the
expense debits it repays, with an amount per expense (ADR 0012). The link
lives in its own table so `transactions` is not touched, and both foreign
keys cascade in the database because bulk delete removes transactions with
a SQL DELETE that never loads them.

Downgrade drops the table; nothing else references it.

Revision ID: 116
Revises: 115
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "116"
down_revision = "115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transaction_reimbursements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reimbursement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expense_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reimbursement_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["expense_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reimbursement_id", "expense_id", name="uq_transaction_reimbursements_pair"
        ),
        sa.CheckConstraint("amount > 0", name="ck_transaction_reimbursements_amount_positive"),
        sa.CheckConstraint(
            "reimbursement_id <> expense_id", name="ck_transaction_reimbursements_distinct"
        ),
    )
    op.create_index(
        "ix_transaction_reimbursements_workspace_id",
        "transaction_reimbursements",
        ["workspace_id"],
    )
    op.create_index(
        "ix_transaction_reimbursements_reimbursement_id",
        "transaction_reimbursements",
        ["reimbursement_id"],
    )
    op.create_index(
        "ix_transaction_reimbursements_expense_id",
        "transaction_reimbursements",
        ["expense_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_transaction_reimbursements_expense_id", table_name="transaction_reimbursements"
    )
    op.drop_index(
        "ix_transaction_reimbursements_reimbursement_id",
        table_name="transaction_reimbursements",
    )
    op.drop_index(
        "ix_transaction_reimbursements_workspace_id", table_name="transaction_reimbursements"
    )
    op.drop_table("transaction_reimbursements")
