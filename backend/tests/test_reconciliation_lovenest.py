"""Lovenest adjustments to upstream's reconciliation module."""
import pytest

from app.models.reconciliation import (
    ReconciliationEvent,
    ReconciliationRule,
    ReconciliationSuggestion,
)
from app.services import reconciliation_rule_service as rule_service


def test_a_rule_can_target_lovenest_cash_accounts():
    """Lovenest's account type is `cash`; upstream names it `wallet`."""
    clean = rule_service.validate_config(
        {"outcome": "link", "when": {"account_types": ["cash", "checking"]}},
        whole=False,
    )
    assert clean["when"]["account_types"] == ["cash", "checking"]

    with pytest.raises(rule_service.RuleError):
        rule_service.validate_config(
            {"outcome": "link", "when": {"account_types": ["wallet"]}}, whole=False
        )


@pytest.mark.parametrize(
    "column",
    [
        ReconciliationRule.__table__.c.user_id,
        ReconciliationSuggestion.__table__.c.resolved_by,
        ReconciliationEvent.__table__.c.user_id,
    ],
    ids=["rule-author", "suggestion-resolver", "event-actor"],
)
def test_deleting_a_user_does_not_block_on_reconciliation_audit_columns(column):
    """`admin_service.delete_user` leaves workspace-owned rows in place."""
    (foreign_key,) = column.foreign_keys
    assert foreign_key.column.table.name == "users"
    assert foreign_key.ondelete == "SET NULL"
