"""Migration 140 on real PostgreSQL: debit rules keep matching what they did."""
import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.models.rule import Rule
from app.models.transaction import Transaction
from app.services import rule_engine
from app.services.rule_engine import evaluate_conditions

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "140_sign_debit_rule_amounts.py"
)
_SPEC = importlib.util.spec_from_file_location("sign_debit_rule_amounts", _MIGRATION_PATH)
assert _SPEC is not None and _SPEC.loader is not None
migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migration)

DEBIT = {"field": "type", "op": "equals", "value": "debit"}

TRANSACTIONS = [
    Transaction(type=kind, amount=Decimal(value), description="SHOP")
    for kind in ("debit", "credit")
    for value in ("5.00", "20.00", "50.00", "75.00", "120.00")
]


@pytest_asyncio.fixture
async def session(postgres_sessions):
    async with postgres_sessions() as session:
        yield session
        await session.rollback()


@pytest.fixture
def clean_db(postgres_sessions):
    """The reused user fixture starts in a fresh per-test PostgreSQL schema."""


def _matches(conditions_op, conditions, *, magnitude: bool, monkeypatch) -> list[int]:
    with monkeypatch.context() as patch:
        if magnitude:
            patch.setattr(rule_engine, "_signed_amount", lambda tx, raw: rule_engine._to_decimal(raw))
        return [
            i for i, tx in enumerate(TRANSACTIONS)
            if evaluate_conditions(conditions_op, conditions, tx)
        ]


async def _run(session, monkeypatch, step: str) -> None:
    schema = session.bind.get_execution_options()["schema_translate_map"][None]
    conn = await session.connection()
    # The migration's raw SQL bypasses schema_translate_map.
    await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))

    def run(sync_conn):
        monkeypatch.setattr(migration.op, "get_bind", lambda: sync_conn)
        getattr(migration, step)()

    await conn.run_sync(run)


async def _conditions(session, rule_id) -> list:
    session.expire_all()
    return await session.scalar(select(Rule.conditions).where(Rule.id == rule_id))


def _rule(user_id, workspace_id, conditions, *, conditions_op="and", source=None) -> Rule:
    return Rule(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=f"r-{uuid.uuid4().hex[:6]}",
        conditions_op=conditions_op, conditions=conditions,
        actions=[{"op": "append_notes", "value": "#x"}], source=source,
    )


async def test_upgrade_signs_debit_rules_and_downgrade_restores_them(
    session, test_user, test_workspace, monkeypatch, caplog
):
    band = [
        DEBIT,
        {"field": "amount", "op": "gte", "value": "20"},
        {"field": "amount", "op": "lte", "value": 75},
    ]
    grouped = [
        DEBIT,
        {"op": "or", "conditions": [
            {"field": "amount", "op": "lt", "value": "10.00"},
            {"field": "amount", "op": "gt", "value": 100.0},
        ]},
    ]
    already_signed = [DEBIT, {"field": "amount", "op": "lte", "value": "-50"}]
    both_directions = [{"field": "amount", "op": "gte", "value": "50"}]
    from_pack = [DEBIT, {"field": "amount", "op": "gte", "value": "20"}]

    rules = {
        "band": _rule(test_user.id, test_workspace.id, band),
        "grouped": _rule(test_user.id, test_workspace.id, grouped),
        "already_signed": _rule(test_user.id, test_workspace.id, already_signed),
        "both_directions": _rule(test_user.id, test_workspace.id, both_directions),
        "from_pack": _rule(test_user.id, test_workspace.id, from_pack, source="pack:test"),
    }
    session.add_all(rules.values())
    await session.flush()
    ids = {name: rule.id for name, rule in rules.items()}

    for original in (band, grouped):
        before = _matches("and", original, magnitude=True, monkeypatch=monkeypatch)
        assert before
        assert _matches("and", original, magnitude=False, monkeypatch=monkeypatch) != before

    caplog.set_level("INFO", logger="alembic.runtime.migration")
    await _run(session, monkeypatch, "upgrade")

    assert await _conditions(session, ids["band"]) == [
        DEBIT,
        {"field": "amount", "op": "lte", "value": "-20"},
        {"field": "amount", "op": "gte", "value": -75},
    ]
    for name, original in (("band", band), ("grouped", grouped)):
        signed = await _conditions(session, ids[name])
        assert _matches("and", signed, magnitude=False, monkeypatch=monkeypatch) == _matches(
            "and", original, magnitude=True, monkeypatch=monkeypatch
        )
    assert await _conditions(session, ids["already_signed"]) == already_signed
    assert await _conditions(session, ids["both_directions"]) == both_directions
    assert await _conditions(session, ids["from_pack"]) == from_pack
    assert str(ids["both_directions"]) in caplog.text

    await _run(session, monkeypatch, "upgrade")
    assert await _conditions(session, ids["band"]) == [
        DEBIT,
        {"field": "amount", "op": "lte", "value": "-20"},
        {"field": "amount", "op": "gte", "value": -75},
    ]

    await _run(session, monkeypatch, "downgrade")

    assert await _conditions(session, ids["band"]) == band
    assert await _conditions(session, ids["grouped"]) == grouped
    # Saved signed under the new engine, so the old one needs it in magnitudes.
    assert await _conditions(session, ids["already_signed"]) == [
        DEBIT, {"field": "amount", "op": "gte", "value": "50"},
    ]
    assert await _conditions(session, ids["from_pack"]) == from_pack
