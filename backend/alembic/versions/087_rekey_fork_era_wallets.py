"""re-key per-account wallets onto the "{connection}::{account}" shape

Revision ID: 087
Revises: 086
Create Date: 2026-08-26

Upstream v0.14.4 keys a synced wallet `"{connection}::{account}"`. This fork's
superseded `_wallet_for_account` wrote the account's own external id, bare. The
two shapes are not interchangeable: every guard upstream uses to tell a
per-account wallet from the pre-split connection-level one tests for the "::",
so a bare-keyed wallet reads as "legacy, free to adopt or drain". Left to the
runtime adoption path, the first sync after the upgrade can hand one account's
wallet to another account, or collapse every wallet into the connection default
and reap the emptied rows — taking the user-set `tax_treatment` with them.

Re-keying here instead means adoption never runs: the wallet is already at the
key the sync computes, so it matches outright.

`_wallet_for_account` wrote exactly one of two keys — the connection's own
external id for holdings the provider attributed to no account, or an account's
external id — so the re-key has to tell those apart. It asks `accounts`, rather
than comparing against the connection's current external id, because that id is
not stable: a reconnect rewrites it in place (`connection_service` resolves a
SimpleFIN claim URL to a fresh digest), which leaves a connection-level wallet
sitting on a *previous* connection key. Comparing would read that stale key as
an account id and mint "{new connection}::{old connection}", a key naming an
account that never existed and that no later sync can adopt.

A wallet orphaned by a disconnect (`connection_id` is NULL, via SET NULL) is out
of reach here — there is no connection left to build a key from. `_wallet_for`
adopts those by exact bare-key match instead; see the pool it builds.
"""
import hashlib
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "087"
down_revision: Union[str, None] = "086"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _wallet_external_id(connection_external_id: str, account_key: str) -> str:
    """Frozen copy of connection_service._wallet_external_id.

    Duplicated on purpose: a migration has to keep producing the keys that were
    correct the day it ran, even after the runtime helper changes.
    """
    if not account_key:
        return connection_external_id
    key = f"{connection_external_id}::{account_key}"
    if len(key) <= 255:
        return key
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{key[:214]}::{digest[:39]}"


def rekey(bind) -> int:
    """Re-key this database's fork-era wallets. Returns how many moved.

    Split out from `upgrade` so a test can drive it against a real session
    instead of asserting on the SQL by eye.
    """
    moved = 0
    rows = bind.execute(
        sa.text(
            """
            SELECT g.id, g.workspace_id, g.user_id, g.source, g.external_id,
                   c.external_id AS connection_external_id
            FROM asset_groups g
            JOIN bank_connections c ON c.id = g.connection_id
            WHERE g.source <> 'manual'
              AND g.external_id IS NOT NULL
              AND c.external_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM accounts a
                  WHERE a.connection_id = g.connection_id
                    AND a.external_id = g.external_id
              )
            """
        )
    ).fetchall()

    for row in rows:
        new_key = _wallet_external_id(row.connection_external_id, row.external_id)
        # The unique index is (workspace_id, user_id, source, external_id). A
        # row already sitting on the target key means a partially-migrated
        # database; leaving this one bare is recoverable, a failed migration on
        # every boot is not.
        taken = bind.execute(
            sa.text(
                """
                SELECT 1 FROM asset_groups
                WHERE workspace_id = :ws AND user_id = :uid
                  AND source = :src AND external_id = :key
                LIMIT 1
                """
            ),
            {"ws": row.workspace_id, "uid": row.user_id, "src": row.source, "key": new_key},
        ).first()
        if taken:
            continue
        bind.execute(
            sa.text("UPDATE asset_groups SET external_id = :key WHERE id = :id"),
            {"key": new_key, "id": row.id},
        )
        moved += 1
    return moved


def upgrade() -> None:
    moved = rekey(op.get_bind())
    logging.getLogger("alembic.runtime.migration").info(
        "087: re-keyed %d fork-era wallet(s)", moved
    )


def unkey(bind) -> int:
    """Return per-account wallets to the fork-era bare shape. Returns how many.

    Split out from `downgrade` for the same reason `rekey` is split out of
    `upgrade`, and written as a row loop rather than one UPDATE so the prefix
    test is a string comparison — a connection id containing `%` or `_` would
    otherwise widen a LIKE pattern.

    ponytail: matching is by shape, so this also returns wallets the merged
    runtime minted, not only the ones 087 moved. Nothing records which is which,
    and the bare shape is what a downgrade past 085 wants anyway.
    """
    moved = 0
    rows = bind.execute(
        sa.text(
            """
            SELECT g.id, g.workspace_id, g.user_id, g.source, g.external_id,
                   g.connection_id, c.external_id AS connection_external_id
            FROM asset_groups g
            JOIN bank_connections c ON c.id = g.connection_id
            WHERE g.source <> 'manual'
              AND g.external_id IS NOT NULL
              AND c.external_id IS NOT NULL
            """
        )
    ).fetchall()

    for row in rows:
        prefix = f"{row.connection_external_id}::"
        if not row.external_id.startswith(prefix):
            continue
        bare = row.external_id[len(prefix):]
        # A digest-suffixed key lost the account id it was built from, so it
        # names no account and is left alone rather than turned back into a key
        # that never existed.
        known = bind.execute(
            sa.text(
                "SELECT 1 FROM accounts WHERE connection_id = :cid "
                "AND external_id = :key LIMIT 1"
            ),
            {"cid": row.connection_id, "key": bare},
        ).first()
        if not known:
            continue
        # The mirror of the upgrade's collision skip: it leaves a wallet bare
        # when the target key is taken, and stripping the twin onto that same
        # key here would trip ux_asset_groups_workspace_source_external and
        # abort the whole downgrade.
        taken = bind.execute(
            sa.text(
                """
                SELECT 1 FROM asset_groups
                WHERE workspace_id = :ws AND user_id = :uid
                  AND source = :src AND external_id = :key
                LIMIT 1
                """
            ),
            {"ws": row.workspace_id, "uid": row.user_id, "src": row.source, "key": bare},
        ).first()
        if taken:
            continue
        bind.execute(
            sa.text("UPDATE asset_groups SET external_id = :key WHERE id = :id"),
            {"key": bare, "id": row.id},
        )
        moved += 1
    return moved


def downgrade() -> None:
    moved = unkey(op.get_bind())
    logging.getLogger("alembic.runtime.migration").info(
        "087: returned %d wallet(s) to the fork-era key", moved
    )
