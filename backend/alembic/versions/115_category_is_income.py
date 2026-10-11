"""a credit's category decides whether it is income

Adds `categories.is_income`. A credit filed under a category with the flag is
income; a credit filed under any other category is money back on that
category's spending and nets against it (ADR 0011).

Defaulting the column to false would turn every existing income category into
a spending offset overnight, so the backfill flags the categories that are
income by construction: everything in a group carrying a default income-group
name, and the default salary category wherever it was moved to. Both name
lists are every localized default, copied rather than imported so this
revision keeps its meaning when the app's defaults change.

Revision ID: 115
Revises: 114
"""
from alembic import op
import sqlalchemy as sa

revision = "115"
down_revision = "114"
branch_labels = None
depends_on = None


INCOME_GROUP_NAMES = (
    "Доходы",
    "Доходи",
    "Einkommen",
    "Revenus",
    "Entrate",
    "Przychody",
    "Ingresos",
    "Príjmy",
    "Income",
    "Renda",
    "Rendimentos",
)

SALARY_CATEGORY_NAMES = (
    "Salary & Income",
    "Salário & Renda",
    "Salário & Rendimentos",
    "Gehalt & Einnahmen",
    "Salaire & Revenus",
    "Salaris & Inkomen",
    "Mzda a príjmy",
)


def backfill(bind) -> int:
    """Flag the existing income categories. Returns how many were flagged.

    Split out from `upgrade` so a test can run it against a real session.
    """
    categories = sa.table(
        "categories",
        sa.column("name", sa.String),
        sa.column("group_id"),
        sa.column("is_income", sa.Boolean),
    )
    category_groups = sa.table(
        "category_groups",
        sa.column("id"),
        sa.column("name", sa.String),
    )
    income_groups = sa.select(category_groups.c.id).where(
        category_groups.c.name.in_(INCOME_GROUP_NAMES)
    )
    result = bind.execute(
        categories.update()
        .where(
            sa.or_(
                categories.c.group_id.in_(income_groups),
                categories.c.name.in_(SALARY_CATEGORY_NAMES),
            )
        )
        .values(is_income=True)
    )
    return result.rowcount


def upgrade() -> None:
    op.add_column(
        "categories",
        sa.Column(
            "is_income",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    backfill(op.get_bind())


def downgrade() -> None:
    op.drop_column("categories", "is_income")
