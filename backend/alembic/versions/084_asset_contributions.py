"""record money crossing a wallet's boundary, so a deposit is not read as growth

Revision ID: 084
Revises: 083
Create Date: 2026-08-25

A wallet's balance is Contributions plus gains (CONTEXT.md). Until now only the
gains half was recorded: `asset_transactions` says what was bought and sold
*inside* an account, and nothing at all says how much money went into it. So a
balance climbing through a year of heavy deposits read as performance, and a
Roth IRA could not answer the one question that governs whether a withdrawal
before retirement age is penalised — how much of the balance is the user's own
money.

One table for both directions. Net Contribution is Contributions minus
Distributions, which is a signed sum over one set of rows; splitting it in two
would make every read of it a union, and every write pick a table from a sign.
`amount` is therefore always positive and `kind` carries the direction, which
is also what lets the positivity constraint mean something.

`tax_year` is separate from `date` because an IRA contribution made before
April 15 may be designated for the prior year, and progress against an annual
limit is measured in the year it counts against. Fidelity's own export makes
the distinction in the action text ("CASH CONTRIBUTION CURRENT YEAR"), so a
file that says which one it is has somewhere to put it.

`party` and `vested_on` exist because employer money is not the user's until it
vests, and the annual limits that apply to it are not the limits that apply to
their own contributions — two figures that have to stay separable. The
constraints keep the combinations that mean nothing out of the table: only a
contribution has an employer behind it, and only employer money vests.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "084"
down_revision: Union[str, None] = "083"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "asset_contributions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.UUID(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "group_id",
            sa.UUID(),
            sa.ForeignKey("asset_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("party", sa.String(length=8), nullable=False, server_default="self"),
        sa.Column("amount", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("tax_year", sa.Integer(), nullable=False),
        sa.Column("vested_on", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="manual"),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column(
            "import_id",
            sa.UUID(),
            sa.ForeignKey("import_logs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('contribution', 'distribution')",
            name="ck_asset_contributions_kind",
        ),
        sa.CheckConstraint(
            "party IN ('self', 'employer')",
            name="ck_asset_contributions_party",
        ),
        sa.CheckConstraint("amount > 0", name="ck_asset_contributions_amount_positive"),
        sa.CheckConstraint(
            "party = 'self' OR kind = 'contribution'",
            name="ck_asset_contributions_employer_is_a_contribution",
        ),
        sa.CheckConstraint(
            "vested_on IS NULL OR party = 'employer'",
            name="ck_asset_contributions_only_employer_money_vests",
        ),
    )
    op.create_index(
        "ix_asset_contributions_workspace_id", "asset_contributions", ["workspace_id"]
    )
    op.create_index("ix_asset_contributions_group_id", "asset_contributions", ["group_id"])
    op.create_index("ix_asset_contributions_date", "asset_contributions", ["date"])
    op.create_index("ix_asset_contributions_tax_year", "asset_contributions", ["tax_year"])
    op.create_index("ix_asset_contributions_import_id", "asset_contributions", ["import_id"])


def downgrade() -> None:
    op.drop_index("ix_asset_contributions_import_id", table_name="asset_contributions")
    op.drop_index("ix_asset_contributions_tax_year", table_name="asset_contributions")
    op.drop_index("ix_asset_contributions_date", table_name="asset_contributions")
    op.drop_index("ix_asset_contributions_group_id", table_name="asset_contributions")
    op.drop_index("ix_asset_contributions_workspace_id", table_name="asset_contributions")
    op.drop_table("asset_contributions")
