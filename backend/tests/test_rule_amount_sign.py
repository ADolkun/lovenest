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
    ("and", [amount("gte", "-5")]),
    ("and", [{"field": "description", "op": "contains", "value": "SHOP"}, amount("lt", -1)]),
    ("and", [DEBIT, amount("gte", "50"), amount("lte", "-10")]),
    ("and", [DEBIT, amount("gte", "fifty")]),
])
def test_rules_that_cannot_keep_their_meaning_are_reported_not_rewritten(conditions_op, conditions):
    result = to_signed_amounts(conditions_op, conditions)

    assert result.unresolved and not result.changed
    assert result.conditions == conditions


def test_debit_group_is_rewritten_and_an_unscoped_leaf_gets_its_partner():
    conditions = [
        {"op": "and", "conditions": [DEBIT, amount("gte", "100")]},
        amount("gt", "1000"),
    ]

    result = to_signed_amounts("or", conditions)

    assert result.changed and not result.unresolved
    assert result.conditions == [
        {"op": "and", "conditions": [DEBIT, amount("lte", "-100")]},
        amount("gt", "1000"),
        amount("lt", "-1000"),
    ]


SHOP = {"field": "description", "op": "contains", "value": "SHOP"}
CAFE = {"field": "description", "op": "contains", "value": "CAFE"}

MIXED_TRANSACTIONS = [
    Transaction(type=kind, amount=Decimal(value), description=description)
    for kind in ("debit", "credit")
    for description in ("SHOP", "CAFE")
    for value in ("0.00", "10.00", "49.99", "50.00", "50.01", "100.00")
]


def mixed_matches(conditions_op, conditions, *, magnitude: bool, monkeypatch) -> list:
    with monkeypatch.context() as patch:
        if magnitude:
            patch.setattr(rule_engine, "_signed_amount", lambda tx, raw: rule_engine._to_decimal(raw))
        return [
            i for i, tx in enumerate(MIXED_TRANSACTIONS)
            if evaluate_conditions(conditions_op, conditions, tx)
        ]


# Every place an amount leaf can sit, by the operator of its container and of
# the rule. `flattened` shapes come back from the round trip without their group.
UNSCOPED_SHAPES = {
    "alone": (lambda leaf: ("and", [leaf]), False),
    "and_rule": (lambda leaf: ("and", [SHOP, leaf]), False),
    "or_rule": (lambda leaf: ("or", [CAFE, leaf]), False),
    "and_group_in_or_rule": (lambda leaf: ("or", [{"op": "and", "conditions": [SHOP, leaf]}, CAFE]), False),
    "or_group_in_and_rule": (lambda leaf: ("and", [{"op": "or", "conditions": [CAFE, leaf]}, SHOP]), False),
    "and_group_in_and_rule": (lambda leaf: ("and", [{"op": "and", "conditions": [SHOP, leaf]}]), True),
    "or_group_in_or_rule": (lambda leaf: ("or", [{"op": "or", "conditions": [CAFE, leaf]}]), True),
}


@pytest.mark.parametrize("shape", list(UNSCOPED_SHAPES))
@pytest.mark.parametrize("op", ["equals", "not_equals", "gt", "gte", "lt", "lte"])
@pytest.mark.parametrize("value", [0, "50", 50.0, "49.99", "+10"])
def test_unscoped_amounts_match_what_the_magnitude_rule_matched(shape, op, value, monkeypatch):
    build, flattened = UNSCOPED_SHAPES[shape]
    conditions_op, conditions = build(amount(op, value))
    before = mixed_matches(conditions_op, conditions, magnitude=True, monkeypatch=monkeypatch)

    result = to_signed_amounts(conditions_op, conditions)

    assert not result.unresolved
    assert result.changed == (op in ("gt", "gte", "lt", "lte") or value != 0)
    assert mixed_matches(conditions_op, result.conditions, magnitude=False, monkeypatch=monkeypatch) == before
    assert all(
        not isinstance(node.get("conditions"), list)
        or not any(isinstance(c.get("conditions"), list) for c in node["conditions"])
        for node in result.conditions
    ), "groups must not nest"

    again = to_signed_amounts(conditions_op, result.conditions)
    assert not again.changed and not again.unresolved and again.conditions == result.conditions

    back = to_magnitude_amounts(conditions_op, result.conditions)
    assert not back.unresolved
    assert mixed_matches(conditions_op, back.conditions, magnitude=True, monkeypatch=monkeypatch) == before
    # A zero bound stays in its `=`/`!=` form, which reads the same either way.
    if not flattened and not (value == 0 and op in ("gt", "gte", "lt", "lte")):
        assert back.conditions == conditions


def test_unscoped_amount_from_a_netflix_style_rule_matches_the_debit_again(monkeypatch):
    conditions = [SHOP, amount("equals", "50.00")]

    result = to_signed_amounts("and", conditions)

    assert result.conditions == [
        SHOP, {"op": "or", "conditions": [amount("equals", "50.00"), amount("equals", "-50.00")]},
    ]
    debits = [i for i in mixed_matches("and", result.conditions, magnitude=False, monkeypatch=monkeypatch)
              if MIXED_TRANSACTIONS[i].type == "debit"]
    assert debits


def test_several_leaves_in_one_group_are_copied_once_per_side(monkeypatch):
    conditions = [{"op": "and", "conditions": [SHOP, amount("gt", 5), amount("lt", "60"), amount("equals", 50)]}]
    before = mixed_matches("or", conditions, magnitude=True, monkeypatch=monkeypatch)

    result = to_signed_amounts("or", conditions)

    assert result.changed and not result.unresolved
    assert len(result.conditions) == 4
    assert mixed_matches("or", result.conditions, magnitude=False, monkeypatch=monkeypatch) == before
    assert not to_signed_amounts("or", result.conditions).changed
    assert to_magnitude_amounts("or", result.conditions).conditions == conditions


def test_unscoped_partners_keep_value_types():
    conditions = [amount("gte", "50.00"), amount("gt", 7), amount("lte", 2.5), amount("equals", "+3")]

    result = to_signed_amounts("or", conditions)

    assert result.conditions == [
        amount("gte", "50.00"), amount("lte", "-50.00"),
        amount("gt", 7), amount("lt", -7),
        {"op": "and", "conditions": [amount("lte", 2.5), amount("gte", -2.5)]},
        amount("equals", "+3"), amount("equals", "-3"),
    ]


@pytest.mark.parametrize(("op", "expected"), [
    ("lte", [amount("equals", "0.00")]),
    ("gt", [amount("not_equals", "0.00")]),
    ("lt", [amount("equals", "0.00"), amount("not_equals", "0.00")]),
    ("gte", [{"op": "or", "conditions": [amount("equals", "0.00"), amount("not_equals", "0.00")]}]),
])
def test_a_zero_bound_is_written_with_equality_only(op, expected):
    result = to_signed_amounts("and", [amount(op, "0.00")])

    assert result.changed and not result.unresolved
    assert result.conditions == expected


@pytest.mark.parametrize(("conditions_op", "conditions"), [
    ("and", [{"op": "or", "conditions": [DEBIT, CAFE]},
             {"op": "or", "conditions": [amount("lt", "0"), amount("lt", "0"), amount("gt", "0")]}]),
    ("and", [{"op": "or", "conditions": [amount("lt", "50"), amount("lt", "10"), CAFE, amount("lte", "0")]},
             amount("equals", "0")]),
    ("or", [{"op": "and", "conditions": [CAFE, amount("gte", "0")]},
            {"op": "and", "conditions": [amount("not_equals", "0"), amount("gt", "0"), amount("gt", "0")]}]),
])
def test_zero_bounds_beside_other_amounts_settle_and_keep_their_meaning(conditions_op, conditions, monkeypatch):
    before = mixed_matches(conditions_op, conditions, magnitude=True, monkeypatch=monkeypatch)

    result = to_signed_amounts(conditions_op, conditions)

    assert not result.unresolved
    assert mixed_matches(conditions_op, result.conditions, magnitude=False, monkeypatch=monkeypatch) == before
    assert not to_signed_amounts(conditions_op, result.conditions).changed
