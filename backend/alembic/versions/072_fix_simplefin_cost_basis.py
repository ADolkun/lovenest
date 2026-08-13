"""recover total SimpleFIN cost bases (issue #72)

Revision ID: 072
Revises: 071
Create Date: 2026-08-13

The SimpleFIN mapping wrote the provider's per-share ``purchase_price`` into
``assets.purchase_price``, which is a total everywhere else — every synced
holding's cost basis is off by its share count. The real total was fetched all
along and mirrored into ``external_metadata['cost_basis']``, so it is recovered
from there rather than left for the next sync: holdings the provider stopped
reporting (archived) or that were marked withdrawn are never synced again and
would otherwise keep the wrong figure forever.

Holdings with no usable ``cost_basis`` in the blob end up with none rather than
a figure derived from shares — the provider never reported a total for them.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "072"
down_revision: Union[str, None] = "071"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The blob holds whatever string the provider sent, so anything that isn't a
# plain number is treated as absent — a bad row must not fail the migration.
# Zero is absent too: bridges send 0.00 for "not populated", and storing it
# would report the holding's whole market value as unrealized gain.
_TOTAL = (
    "NULLIF(CASE WHEN external_metadata->>'cost_basis' ~ '^-?[0-9]+(\\.[0-9]+)?$' "
    "THEN (external_metadata->>'cost_basis')::numeric END, 0)"
)


def upgrade() -> None:
    # purchase_price_primary is the same figure in the user's primary currency.
    # Rescaling by total/per-share reuses the FX rate already baked into it, so
    # it stays consistent without a conversion pass — backfill_primary_amounts
    # is a manual task and only fills nulls.
    op.execute(
        "UPDATE assets a SET purchase_price = t.total, purchase_price_primary = CASE "
        "WHEN t.total IS NOT NULL AND a.purchase_price <> 0 AND a.purchase_price_primary IS NOT NULL "
        "THEN a.purchase_price_primary * t.total / a.purchase_price END "
        "FROM (SELECT id, " + _TOTAL + " AS total FROM assets) t "
        "WHERE t.id = a.id AND a.source = 'simplefin' AND a.purchase_price IS NOT NULL "
        # Ledger-backed assets are excluded: `recompute_and_cache` owns their
        # purchase_price and already caches a real total.
        "AND NOT EXISTS (SELECT 1 FROM asset_transactions x WHERE x.asset_id = a.id)"
    )


def downgrade() -> None:
    """No-op — the replaced values were wrong by the share count."""
