"""Move rule amount conditions between magnitude and signed form.

Rule amount conditions used to compare the stored amount, which is positive
for both directions. They now compare a signed amount: credits positive,
debits negative (see `rule_engine._signed_amount`). Used by fork migration 140
and by imports of rule files exported before the change.

Debit-only conditions keep their old meaning when every amount value is
negated and each ordering operator flipped: `|x| >= 50` is `x <= -50` for a
debit. Which form a debit-only rule is in is read from its values, so applying
a conversion twice changes nothing: values that are all non-negative (or all
zero with only `>`/`>=`) are magnitudes, all non-positive (or all zero with
only `<`/`<=`) are signed. A debit rule mixing both signs is left alone and
reported. Credit-only conditions mean the same under both readings.

An amount condition that sees both directions compared the magnitude, which
takes two signed conditions to say: `|x| = X` is `x = X or x = -X`, `|x| < X`
is `x < X and x > -X` (see `_JOIN`). The partner joins the leaf's own
container when that container already has the join it needs. Otherwise a
top-level leaf is wrapped in a new group, and a grouped leaf, since groups
cannot nest, has its group flattened into a top level of the same operator, or
copied once per side when the top level has the needed join:
`or [A and |x| = X]` becomes `or [A and x = X, A and x = -X]`.

A leaf and its partner mean the same under either reading, so the pair is
the converted form, recognised on a later run and left alone. Collapsing it
back (dropping the partner, or the copied group) is the inverse.

A zero bound is written with `=` and `!=` only, which mean the same under
both readings: `|x| <= 0` is `x = 0`, `|x| > 0` is `x != 0`, `|x| < 0` is
`x = 0 and x != 0`, `|x| >= 0` is `x = 0 or x != 0`. A value of 0 tells
neither form apart, so any other form is misread later: a direction-free
partner of 0 reads as one more magnitude leaf needing a partner, and the
rewrite never settles; a debit-only `x < 0` reads back as the magnitude
`|x| < 0`.
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
# For X >= 0: `|x| op X` is `x op X <join> x _FLIPPED_OP[op] -X`.
_JOIN = {
    "equals": "or",
    "gt": "or",
    "gte": "or",
    "not_equals": "and",
    "lt": "and",
    "lte": "and",
}
# `|x| op 0` as one fixed-point leaf, where one says it.
_ZERO_BOUND_OP = {"lte": "equals", "gt": "not_equals"}
_UPWARD_OPS = frozenset({"gt", "gte"})
_DOWNWARD_OPS = frozenset({"lt", "lte"})

Form = Literal["magnitude", "signed"]


@dataclass
class AmountSignRewrite:
    conditions: list
    changed: bool = False
    # Amount conditions whose meaning changed with signing and could not be
    # carried over: they mix signs or hold a non-number.
    unresolved: bool = False


@dataclass
class _Pair:
    leaf: dict
    partner: dict
    container: list
    # The copy holding `partner` when the pair spans two groups.
    copy: dict | None = None


def _is_group(node: Any) -> bool:
    return isinstance(node, dict) and isinstance(node.get("conditions"), list)


def _join_of(op: Any) -> str:
    """The engine reads anything but "or" as "and"."""
    return "or" if op == "or" else "and"


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


def _classify(conditions_op: str, conditions: list) -> tuple[list[dict], list[dict]]:
    """Debit-only and direction-free amount leaves of `conditions`."""
    debit_leaves: list[dict] = []
    unscoped: list[dict] = []
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
        elif direction is None:
            unscoped.extend(leaves)
    return debit_leaves, unscoped


def _flip_debit_leaves(debit_leaves: list[dict], source: Form, result: AmountSignRewrite) -> None:
    if not debit_leaves:
        return
    numbers = [_number(leaf["value"]) for leaf in debit_leaves]
    if any(n is None for n in numbers):
        result.unresolved = True
        return
    known = [n for n in numbers if n is not None]
    form = _form(debit_leaves, known)
    if form == "mixed":
        result.unresolved = True
        return
    if form != source:
        return
    for leaf, number in zip(debit_leaves, known):
        leaf["op"] = _FLIPPED_OP[leaf["op"]]
        leaf["value"] = _negated(leaf["value"], number)
    result.changed = True


def _magnitude(leaf: dict) -> Decimal | None:
    """The bound of a leaf in magnitude form, or None when it is not one."""
    number = _number(leaf["value"])
    return number if number is not None and number >= 0 else None


def _is_partner(leaf: dict, candidate: Any, bound: Decimal) -> bool:
    return (
        _is_sign_sensitive_amount(candidate)
        and candidate["op"] == _FLIPPED_OP[leaf["op"]]
        and _number(candidate["value"]) == -bound
    )


def _is_fixed_point(leaf: dict, bound: Decimal) -> bool:
    """`|x| = 0` is `x = 0`, and likewise for `!=`."""
    return bound == 0 and leaf["op"] in ("equals", "not_equals")


def _index(nodes: list, node: Any) -> int:
    return next(i for i, candidate in enumerate(nodes) if candidate is node)


def _pairs(conditions_op: str, conditions: list, unscoped: list[dict]) -> list[_Pair]:
    """Converted leaf/partner pairs among the direction-free amount leaves."""
    candidates = {id(leaf) for leaf in unscoped}
    used: set[int] = set()
    pairs: list[_Pair] = []
    top_join = _join_of(conditions_op)
    groups = [node for node in conditions if _is_group(node)]
    containers = [(top_join, conditions)] + [
        (_join_of(group.get("op")), group["conditions"]) for group in groups
    ]
    for join, nodes in containers:
        leaves = [node for node in nodes if id(node) in candidates]
        for leaf in leaves:
            bound = _magnitude(leaf)
            if id(leaf) in used or bound is None or _JOIN[leaf["op"]] != join:
                continue
            partner = next(
                (c for c in leaves if c is not leaf and id(c) not in used and _is_partner(leaf, c, bound)),
                None,
            )
            if partner is not None:
                used |= {id(leaf), id(partner)}
                pairs.append(_Pair(leaf, partner, nodes))
    for group in groups:
        for i, leaf in enumerate(group["conditions"]):
            bound = _magnitude(leaf) if id(leaf) in candidates else None
            if id(leaf) in used or bound is None or _JOIN[leaf["op"]] != top_join:
                continue
            for other in groups:
                if (
                    other is group
                    or _join_of(other.get("op")) != _join_of(group.get("op"))
                    or len(other["conditions"]) != len(group["conditions"])
                ):
                    continue
                partner = other["conditions"][i]
                if id(partner) in used or not _is_partner(leaf, partner, bound):
                    continue
                if all(
                    mine == theirs
                    for j, (mine, theirs) in enumerate(zip(group["conditions"], other["conditions"]))
                    if j != i
                ):
                    used |= {id(leaf), id(partner)}
                    pairs.append(_Pair(leaf, partner, group["conditions"], copy=other))
                    break
    return pairs


def _expand(conditions_op: str, conditions: list, leaf: dict, partner: dict, join: str) -> None:
    """Place `partner` beside `leaf`, joined to it by `join`."""
    top_join = _join_of(conditions_op)
    group = next(
        (node for node in conditions if _is_group(node) and any(c is leaf for c in node["conditions"])),
        None,
    )
    if group is None:
        i = _index(conditions, leaf)
        if join == top_join:
            conditions.insert(i + 1, partner)
        else:
            conditions[i] = {"op": join, "conditions": [leaf, partner]}
        return
    members = group["conditions"]
    group_join = _join_of(group.get("op"))
    g = _index(conditions, group)
    if join == group_join:
        members.insert(_index(members, leaf) + 1, partner)
    elif group_join == top_join:
        conditions[g:g + 1] = members
        _expand(conditions_op, conditions, leaf, partner, join)
    else:
        sibling = copy.deepcopy(group)
        sibling["conditions"][_index(members, leaf)] = partner
        conditions.insert(g + 1, sibling)


def _fix_zero_bounds(conditions_op: str, conditions: list, result: AmountSignRewrite) -> None:
    while True:
        debit_leaves, unscoped = _classify(conditions_op, conditions)
        zero = [leaf for leaf in debit_leaves + unscoped
                if _magnitude(leaf) == 0 and not _is_fixed_point(leaf, Decimal(0))]
        if not zero:
            return
        result.changed = True
        leaf = zero[0]
        if leaf["op"] in _ZERO_BOUND_OP:
            leaf["op"] = _ZERO_BOUND_OP[leaf["op"]]
            continue
        join = _JOIN[leaf["op"]]
        leaf["op"] = "equals"
        _expand(conditions_op, conditions, leaf, {**leaf, "op": "not_equals"}, join)


def _sign_unscoped(conditions_op: str, conditions: list, result: AmountSignRewrite) -> None:
    while True:
        _, unscoped = _classify(conditions_op, conditions)
        settled = {id(node) for pair in _pairs(conditions_op, conditions, unscoped)
                   for node in (pair.leaf, pair.partner)}
        pending: list[tuple[dict, Decimal]] = []
        for leaf in unscoped:
            if id(leaf) in settled:
                continue
            bound = _magnitude(leaf)
            if bound is None:
                # Negative or non-numeric: a magnitude never compared that way.
                result.unresolved = True
            elif not _is_fixed_point(leaf, bound):
                pending.append((leaf, bound))
        if not pending:
            return
        # Partners joining their own container go first, so a group copied
        # for another leaf carries them and the copies stay identical.
        joins = {id(leaf): _join_of(conditions_op) for leaf in unscoped}
        for node in conditions:
            if _is_group(node):
                joins.update((id(leaf), _join_of(node.get("op"))) for leaf in node["conditions"])
        leaf, bound = next(
            (item for item in pending if _JOIN[item[0]["op"]] == joins[id(item[0])]), pending[0]
        )
        partner = {**leaf, "op": _FLIPPED_OP[leaf["op"]], "value": _negated(leaf["value"], bound)}
        _expand(conditions_op, conditions, leaf, partner, _JOIN[leaf["op"]])
        result.changed = True


def _collapse_unscoped(conditions_op: str, conditions: list, result: AmountSignRewrite) -> None:
    _, unscoped = _classify(conditions_op, conditions)
    pairs = _pairs(conditions_op, conditions, unscoped)
    settled = {id(node) for pair in pairs for node in (pair.leaf, pair.partner)}
    if any(
        id(leaf) not in settled
        and not ((bound := _magnitude(leaf)) is not None and _is_fixed_point(leaf, bound))
        for leaf in unscoped
    ):
        result.unresolved = True
    if not pairs:
        return
    copies = {id(pair.copy) for pair in pairs if pair.copy is not None}
    conditions[:] = [node for node in conditions if id(node) not in copies]
    shrunk: set[int] = set()
    for pair in pairs:
        if pair.copy is None:
            del pair.container[_index(pair.container, pair.partner)]
            shrunk.add(id(pair.container))
    for i, node in enumerate(conditions):
        if _is_group(node) and id(node["conditions"]) in shrunk and len(node["conditions"]) == 1:
            conditions[i] = node["conditions"][0]
    result.changed = True


def to_signed_amounts(conditions_op: str, conditions: list) -> AmountSignRewrite:
    """Rewrite magnitude-form amount conditions as signed ones."""
    rewritten = copy.deepcopy(conditions or [])
    result = AmountSignRewrite(conditions=rewritten)
    _fix_zero_bounds(conditions_op, rewritten, result)
    debit_leaves, _ = _classify(conditions_op, rewritten)
    _flip_debit_leaves(debit_leaves, "magnitude", result)
    _sign_unscoped(conditions_op, rewritten, result)
    return result


def to_magnitude_amounts(conditions_op: str, conditions: list) -> AmountSignRewrite:
    """Rewrite signed amount conditions back to magnitudes."""
    rewritten = copy.deepcopy(conditions or [])
    result = AmountSignRewrite(conditions=rewritten)
    debit_leaves, _ = _classify(conditions_op, rewritten)
    _flip_debit_leaves(debit_leaves, "signed", result)
    _collapse_unscoped(conditions_op, rewritten, result)
    return result
