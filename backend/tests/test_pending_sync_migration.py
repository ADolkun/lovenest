"""Exercise migration DDL and JSON ownership semantics in disposable PostgreSQL."""
import importlib.util
import os
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine


async def test_pending_migration_roundtrip_preserves_user_ignores():
    configured = os.environ.get("EVIDENCE_TEST_DATABASE_URL")
    if not configured:
        if os.environ.get("CI"):
            pytest.fail("CI must supply EVIDENCE_TEST_DATABASE_URL for migration integration")
        pytest.skip("isolated PostgreSQL not configured")
    schema = f"pending_migration_{uuid.uuid4().hex}"
    engine = create_async_engine(configured)
    path = Path(__file__).resolve().parents[1] / "alembic/versions/119_pending_sync_metadata.py"
    spec = importlib.util.spec_from_file_location("pending_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def migrate(connection, direction):
        setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        getattr(migration, direction)()

    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
            await connection.execute(sa.text("CREATE TABLE transactions (id integer PRIMARY KEY, is_ignored boolean NOT NULL)"))
            await connection.execute(sa.text("INSERT INTO transactions VALUES (1, false), (2, true), (3, true), (4, false)"))
            await connection.run_sync(migrate, "upgrade")
            assert await connection.scalar(sa.text("SELECT count(*) FROM transactions WHERE sync_metadata IS NULL")) == 4
            await connection.execute(sa.text("""UPDATE transactions SET is_ignored=true,
                sync_metadata='{"pending_ignore_owned": true, "pending_retired_at": "2026-10-10T12:00:00+00:00"}'
                WHERE id=1"""))
            await connection.execute(sa.text("""UPDATE transactions SET
                sync_metadata='{"pending_ignore_owned": false, "pending_manual_override": true}' WHERE id=3"""))
            await connection.run_sync(migrate, "downgrade")
            assert (await connection.execute(sa.text("SELECT id, is_ignored FROM transactions ORDER BY id"))).all() == [
                (1, False), (2, True), (3, True), (4, False),
            ]
            await connection.run_sync(migrate, "upgrade")
            assert await connection.scalar(sa.text("SELECT count(*) FROM transactions WHERE sync_metadata IS NULL")) == 4
            assert await connection.scalar(sa.text("SELECT count(*) FROM transactions WHERE is_ignored")) == 2
    finally:
        async with engine.begin() as connection:
            await connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
