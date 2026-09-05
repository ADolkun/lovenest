import importlib.util
from pathlib import Path
from unittest.mock import Mock

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_migrations_have_single_head():
    script = ScriptDirectory.from_config(Config("alembic.ini"))

    assert len(script.get_heads()) == 1


def test_invoice_attachment_downgrade_skips_numbers_that_overflow_integer(monkeypatch):
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "093_invoice_attachments.py"
    )
    spec = importlib.util.spec_from_file_location("invoice_attachment_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    execute = Mock()
    monkeypatch.setattr(migration.op, "execute", execute)
    for operation in (
        "drop_index",
        "drop_table",
        "drop_constraint",
        "create_check_constraint",
        "drop_column",
    ):
        monkeypatch.setattr(migration.op, operation, Mock())

    migration.downgrade()

    statement = " ".join(str(execute.call_args.args[0]).split())
    assert "external_number::numeric <= 2147483647" in statement
