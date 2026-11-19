"""sign rule amount conditions

Rule amount conditions now compare a signed amount (credits positive, debits
negative) instead of the stored positive magnitude. A user rule that requires
`type equals debit` and compares `amount >= 50` could therefore never match
again; it becomes `amount <= -50`. A rule with no direction, such as
`description contains NETFLIX and amount equals 15.99`, would only match the
credit; its amount condition gains the mirrored one (`amount equals 15.99 or
amount equals -15.99`). See `app.services.rule_amount_sign` for the rewrites.
Rules from packs and plugins (`source` set) already ship signed and are
skipped. Amount conditions that cannot keep their meaning (mixed signs in a
debit rule, a negative or non-numeric bound in a rule with no direction) are
left alone and their rule ids logged.

Every rule present at this revision was written for the magnitude engine, so
a direction-free amount condition is read as a magnitude unless it already has
its mirrored partner, which is what a second run finds.

Downgrade applies the inverse rewrite to every debit-only rule in signed
form, not only those this upgrade touched. That is what the older engine
needs: a signed rule saved after the upgrade means the same there only once
it is back in magnitude form. It also drops each mirrored partner the upgrade
added. A direction-free signed condition saved after the upgrade, such as
`amount < 0`, has no magnitude equivalent and is logged. Both directions skip
rules already in the target form, so either can run twice.

Revision ID: 140
Revises: 139
"""

import json
import logging
from collections.abc import Callable

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op
from app.services.rule_amount_sign import (
    AmountSignRewrite,
    to_magnitude_amounts,
    to_signed_amounts,
)

revision = "140"
down_revision = "139"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def rewrite_rules(bind: sa.Connection, convert: Callable[[str, list], AmountSignRewrite]) -> int:
    """Apply `convert` to every user rule; returns how many rules changed."""
    rows = bind.execute(
        text("SELECT id, conditions_op, conditions FROM rules WHERE source IS NULL ORDER BY id")
    ).fetchall()
    update = text("UPDATE rules SET conditions = :conditions WHERE id = :id").bindparams(
        sa.bindparam("conditions", type_=sa.JSON())
    )
    changed = 0
    unresolved = []
    for row in rows:
        result = convert(row.conditions_op or "and", _json(row.conditions) or [])
        if result.unresolved:
            unresolved.append(str(row.id))
        if result.changed:
            bind.execute(update, {"conditions": result.conditions, "id": row.id})
            changed += 1
    logger.info("Rewrote amount conditions of %d rule(s)", changed)
    if unresolved:
        logger.warning(
            "Amount conditions of %d rule(s) could not keep their meaning; review: %s",
            len(unresolved),
            ", ".join(unresolved),
        )
    return changed


def upgrade() -> None:
    rewrite_rules(op.get_bind(), to_signed_amounts)


def downgrade() -> None:
    rewrite_rules(op.get_bind(), to_magnitude_amounts)
