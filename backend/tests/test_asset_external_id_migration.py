import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "090_asset_external_id_workspace.py"
)
_SPEC = importlib.util.spec_from_file_location("asset_external_id_migration", _MIGRATION_PATH)
assert _SPEC is not None and _SPEC.loader is not None
migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migration)


class _Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


def test_upgrade_aborts_before_schema_changes(monkeypatch):
    bind = Mock()
    bind.execute.return_value = _Result(
        {
            "scope_id": "scope-1",
            "source": "provider",
            "external_id": "asset-1",
            "row_count": 2,
        }
    )
    drop_index = Mock()
    create_index = Mock()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "drop_index", drop_index)
    monkeypatch.setattr(migration.op, "create_index", create_index)

    with pytest.raises(RuntimeError, match="workspace_id=scope-1"):
        migration.upgrade()

    statement = " ".join(str(bind.execute.call_args.args[0]).lower().split())
    assert "select workspace_id as scope_id" in statement
    assert "group by workspace_id, source, external_id" in statement
    drop_index.assert_not_called()
    create_index.assert_not_called()


def test_downgrade_restores_the_previous_lovenest_index(monkeypatch):
    drop_index = Mock()
    create_index = Mock()
    monkeypatch.setattr(migration.op, "drop_index", drop_index)
    monkeypatch.setattr(migration.op, "create_index", create_index)

    migration.downgrade()

    drop_index.assert_called_once_with(
        "ux_assets_workspace_source_external", table_name="assets"
    )
    assert create_index.call_args.args[2] == [
        "workspace_id", "user_id", "source", "external_id"
    ]
