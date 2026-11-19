"""Move debit-only amount conditions between magnitude and signed form.

Rule amount conditions used to compare the stored amount, which is positive
for both directions. They now compare a signed amount: credits positive,
debits negative (see `rule_engine._signed_amount`). A rule that only ever
sees debits keeps its old meaning when every amount value is negated and
each ordering operator flipped: `|x| >= 50` is `x <= -50` for a debit.
Credit-only conditions mean the same under both readings. Amount conditions
that can see both directions compared the magnitude of either, which a
single signed condition cannot say; those are reported, never rewritten.

Which form a debit-only rule is in is read from its values, so applying a
conversion twice changes nothing: values that are all non-negative (or all
zero with only `>`/`>=`) are magnitudes, all non-positive (or all zero with
only `<`/`<=`) are signed. A rule mixing both signs is left alone and
reported. Used by fork migration 140 and by imports of rule files exported
before the change.
"""
import copy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.services.rule_engine import _normalize

_FLIPPED_OP = {
    "gt": "lt",
    "lt": "gt",
    "gte": "lte",
    "lte": "gte",
    "equals": "equals",
    "not_equals": "not_equals",
}
_UPWARD_OPS = frozenset({"gt", "gte"})
_DOWNWARD_OPS = frozenset({"lt", "lte"})

Form = Literal["magnitude", "signed"]


@dataclass
class AmountSignRewrite:
    conditions: list
    changed: bool = False
    # Amount conditions whose meaning changed with signing and could not be
    # carried over: they see both directions, mix signs, or hold a non-number.
    unresolved: bool = False


def _is_group(node: Any) -> bool:
    return isinstance(node, dict) and isinstance(node.get("conditions"), list)


def _is_sign_sensitive_amount(node: Any) -> bool:
    if not isinstance(node, dict) or _is_group(node):
        return False
    if node.get("field") != "amount" or node.get("op") not in _FLIPPED_OP:
        return False
    value = node.get("value")
    # The engine never matches a blank value, under either reading.
    return not (value is None or (isinstance(value, str) and not value.strip()))


def _direction(nodes: list) -> str | None:
    """The transaction type a conjunction of `nodes` requires, if any."""
    for node in nodes:
        if (
            isinstance(node, dict)
            and not _is_group(node)
            and node.get("field") == "type"
            and node.get("op") == "equals"
            and node.get("value") is not None
        ):
            normalized = _normalize(str(node["value"]))
            if normalized in ("DEBIT", "CREDIT"):
                return normalized.lower()
    return None


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _negated(value: Any, number: Decimal) -> Any:
    """`value` negated, keeping its JSON type and, for strings, its digits."""
    if number == 0:
        return value
    if isinstance(value, (int, float)):
        return -value
    text = str(value).strip()
    if text.startswith("-"):
        return text[1:]
    return "-" + text.removeprefix("+")


def _form(leaves: list[dict], numbers: list[Decimal]) -> Form | Literal["neutral", "mixed"]:
    has_positive = any(n > 0 for n in numbers)
    has_negative = any(n < 0 for n in numbers)
    if has_positive and has_negative:
        return "mixed"
    if has_positive:
        return "magnitude"
    if has_negative:
        return "signed"
    ops = {leaf["op"] for leaf in leaves}
    upward, downward = bool(ops & _UPWARD_OPS), bool(ops & _DOWNWARD_OPS)
    if upward and not downward:
        return "magnitude"
    if downward and not upward:
        return "signed"
    # Zero under equality, or both directions at zero: the same either way.
    return "neutral"


def _classify(conditions_op: str, conditions: list) -> tuple[list[dict], bool]:
    """Debit-only amount leaves of `conditions` (from a copy) and whether any
    amount leaf can see both directions."""
    debit_leaves: list[dict] = []
    unscoped = False
    rule_direction = _direction(conditions) if conditions_op != "or" else None
    for node in conditions:
        if _is_group(node):
            direction = rule_direction or (
                _direction(node["conditions"]) if node.get("op") != "or" else None
            )
            leaves = [leaf for leaf in node["conditions"] if _is_sign_sensitive_amount(leaf)]
        else:
            direction = rule_direction
            leaves = [node] if _is_sign_sensitive_amount(node) else []
        if direction == "debit":
            debit_leaves.extend(leaves)
        elif direction is None and leaves:
            unscoped = True
    return debit_leaves, unscoped


def _convert(conditions_op: str, conditions: list, source: Form) -> AmountSignRewrite:
    rewritten = copy.deepcopy(conditions or [])
    debit_leaves, unscoped = _classify(conditions_op, rewritten)
    result = AmountSignRewrite(conditions=rewritten, unresolved=unscoped)
    if not debit_leaves:
        return result
    numbers = [_number(leaf["value"]) for leaf in debit_leaves]
    if any(n is None for n in numbers):
        result.unresolved = True
        return result
    known = [n for n in numbers if n is not None]
    form = _form(debit_leaves, known)
    if form == "mixed":
        result.unresolved = True
        return result
    if form != source:
        return result
    for leaf, number in zip(debit_leaves, known):
        leaf["op"] = _FLIPPED_OP[leaf["op"]]
        leaf["value"] = _negated(leaf["value"], number)
    result.changed = True
    return result


def to_signed_amounts(conditions_op: str, conditions: list) -> AmountSignRewrite:
    """Rewrite magnitude-form debit-only amount conditions as signed ones."""
    return _convert(conditions_op, conditions, "magnitude")


def to_magnitude_amounts(conditions_op: str, conditions: list) -> AmountSignRewrite:
    """Rewrite signed debit-only amount conditions back to magnitudes."""
    return _convert(conditions_op, conditions, "signed")
