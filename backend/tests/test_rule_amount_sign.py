from decimal import Decimal

import pytest

from app.models.transaction import Transaction
from app.services import rule_engine
from app.services.rule_amount_sign import to_magnitude_amounts, to_signed_amounts
from app.services.rule_engine import evaluate_conditions

DEBIT = {"field": "type", "op": "equals", "value": "debit"}
CREDIT = {"field": "type", "op": "equals", "value": "credit"}


def amount(op, value):
    return {"field": "amount", "op": op, "value": value}


TRANSACTIONS = [
    Transaction(type=kind, amount=Decimal(value), description="SHOP")
    for kind in ("debit", "credit")
    for value in ("0.00", "10.00", "49.99", "50.00", "50.01", "100.00", "250.00")
]


def matches(conditions_op, conditions, *, magnitude: bool, monkeypatch) -> list:
    """Indexes of TRANSACTIONS the rule matches, under the old magnitude
    comparison or the current signed one."""
    with monkeypatch.context() as patch:
        if magnitude:
            patch.setattr(rule_engine, "_signed_amount", lambda tx, raw: rule_engine._to_decimal(raw))
        return [
            i for i, tx in enumerate(TRANSACTIONS)
            if evaluate_conditions(conditions_op, conditions, tx)
        ]


SAME_MEANING_RULES = [
    ("and", [DEBIT, amount("gte", "50"), amount("lte", "100")]),
    ("and", [DEBIT, amount("gt", 49.99), amount("lt", 250)]),
    ("and", [DEBIT, amount("equals", "50.00")]),
    ("and", [DEBIT, amount("not_equals", 50)]),
    ("and", [DEBIT, amount("gt", 0)]),
    ("and", [{"op": "or", "conditions": [amount("lt", "10"), amount("gt", "200")]}, DEBIT]),
    ("or", [{"op": "and", "conditions": [DEBIT, amount("gte", "100")]},
            {"field": "description", "op": "contains", "value": "NOPE"}]),
]


@pytest.mark.parametrize(("conditions_op", "conditions"), SAME_MEANING_RULES)
def test_signed_rewrite_matches_what_the_magnitude_rule_matched(conditions_op, conditions, monkeypatch):
    before = matches(conditions_op, conditions, magnitude=True, monkeypatch=monkeypatch)
    assert before, "a rule that matches nothing proves nothing"

    result = to_signed_amounts(conditions_op, conditions)

    assert result.changed and not result.unresolved
    assert matches(conditions_op, result.conditions, magnitude=False, monkeypatch=monkeypatch) == before
    again = to_signed_amounts(conditions_op, result.conditions)
    assert not again.changed and again.conditions == result.conditions
    back = to_magnitude_amounts(conditions_op, result.conditions)
    assert back.changed and back.conditions == conditions


def test_rewrite_keeps_value_types_and_does_not_mutate_input():
    conditions = [DEBIT, amount("gte", "50.00"), amount("lt", 100), amount("lte", 2.5), amount("gt", "+7")]

    result = to_signed_amounts("and", conditions)

    assert result.conditions[1:] == [
        amount("lte", "-50.00"), amount("gt", -100), amount("gte", -2.5), amount("lt", "-7"),
    ]
    assert conditions[1] == amount("gte", "50.00")


@pytest.mark.parametrize(("conditions_op", "conditions"), [
    ("and", [DEBIT, amount("lte", "-50")]),
    ("and", [DEBIT, amount("lt", 0)]),
    ("and", [CREDIT, amount("gte", "50")]),
    ("and", [DEBIT, amount("equals", 0)]),
    ("and", [DEBIT, {"field": "description", "op": "contains", "value": "50"}]),
    ("and", [DEBIT, amount("contains", "50")]),
    ("and", [{"field": "type", "op": "equals", "value": "debit"}]),
])
def test_rules_already_meaning_the_same_are_left_alone(conditions_op, conditions):
    result = to_signed_amounts(conditions_op, conditions)

    assert not result.changed and not result.unresolved
    assert result.conditions == conditions


@pytest.mark.parametrize(("conditions_op", "conditions"), [
    ("and", [amount("gte", "50")]),
    ("or", [DEBIT, amount("gte", "50")]),
    ("and", [DEBIT, amount("gte", "50"), amount("lte", "-10")]),
    ("and", [DEBIT, amount("gte", "fifty")]),
])
def test_rules_that_cannot_keep_their_meaning_are_reported_not_rewritten(conditions_op, conditions):
    result = to_signed_amounts(conditions_op, conditions)

    assert result.unresolved and not result.changed
    assert result.conditions == conditions


def test_debit_group_is_rewritten_while_an_unscoped_leaf_is_reported():
    conditions = [
        {"op": "and", "conditions": [DEBIT, amount("gte", "100")]},
        amount("gt", "1000"),
    ]

    result = to_signed_amounts("or", conditions)

    assert result.changed and result.unresolved
    assert result.conditions == [
        {"op": "and", "conditions": [DEBIT, amount("lte", "-100")]},
        amount("gt", "1000"),
    ]
