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
external id. Anything that is not the former is therefore the latter, which is
why no lookup against `accounts` is needed.
"""
import hashlib
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
              AND g.external_id NOT LIKE '%::%'
              AND c.external_id IS NOT NULL
              AND g.external_id <> c.external_id
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
    rekey(op.get_bind())


def downgrade() -> None:
    # Only the un-truncated keys round-trip; a digest-suffixed one has lost the
    # account id it was built from. Those stay as they are rather than being
    # turned back into a key that never existed.
    op.execute(
        """
        UPDATE asset_groups AS g
        SET external_id = substr(g.external_id, length(c.external_id) + 3)
        FROM bank_connections AS c
        WHERE c.id = g.connection_id
          AND g.external_id LIKE c.external_id || '::%'
          AND substr(g.external_id, length(c.external_id) + 3) NOT LIKE '%::%'
        """
    )
