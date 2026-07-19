"""enforce workspace-scoped normalized payee names

Revision ID: 066
Revises: 065
Create Date: 2026-07-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "066"
down_revision: Union[str, None] = "065"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Migration 052 made payees workspace-owned but left the original
    # user/name constraint behind. Collapse normalized duplicates before
    # replacing it with the workspace-scoped invariant used by the service.
    op.execute(
        """
        CREATE TEMP TABLE payee_dedupe_066 ON COMMIT DROP AS
        SELECT id AS source_id, target_id
        FROM (
            SELECT
                id,
                first_value(id) OVER (
                    PARTITION BY workspace_id, lower(btrim(name))
                    ORDER BY created_at, id
                ) AS target_id,
                row_number() OVER (
                    PARTITION BY workspace_id, lower(btrim(name))
                    ORDER BY created_at, id
                ) AS duplicate_rank
            FROM payees
        ) ranked
        WHERE duplicate_rank > 1
        """
    )
    op.execute(
        """
        UPDATE payees target
        SET
            is_favorite = merged.is_favorite,
            notes = left(merged.notes, 1000)
        FROM (
            SELECT
                duplicates.target_id,
                bool_or(payees.is_favorite) AS is_favorite,
                string_agg(
                    DISTINCT NULLIF(btrim(payees.notes), ''),
                    E'\n' ORDER BY NULLIF(btrim(payees.notes), '')
                ) AS notes
            FROM payee_dedupe_066 duplicates
            JOIN payees
              ON payees.id = duplicates.source_id
              OR payees.id = duplicates.target_id
            GROUP BY duplicates.target_id
        ) merged
        WHERE target.id = merged.target_id
        """
    )
    op.execute(
        """
        UPDATE transactions
        SET payee_id = duplicates.target_id
        FROM payee_dedupe_066 duplicates
        WHERE transactions.payee_id = duplicates.source_id
        """
    )
    op.execute(
        """
        UPDATE payee_mapping
        SET
            target_id = duplicates.target_id,
            workspace_id = target.workspace_id
        FROM payee_dedupe_066 duplicates
        JOIN payees target ON target.id = duplicates.target_id
        WHERE payee_mapping.target_id = duplicates.source_id
        """
    )
    op.execute(
        """
        INSERT INTO payee_mapping (id, user_id, workspace_id, target_id)
        SELECT source.id, source.user_id, source.workspace_id, duplicates.target_id
        FROM payee_dedupe_066 duplicates
        JOIN payees source ON source.id = duplicates.source_id
        ON CONFLICT (id) DO UPDATE
        SET
            target_id = EXCLUDED.target_id,
            workspace_id = EXCLUDED.workspace_id
        """
    )
    op.execute(
        """
        UPDATE rules
        SET actions = rewritten.actions
        FROM (
            SELECT
                rules_to_update.id,
                jsonb_agg(
                    CASE
                        WHEN duplicates.target_id IS NOT NULL THEN jsonb_set(
                            action.item::jsonb,
                            '{value}',
                            to_jsonb(duplicates.target_id::text)
                        )
                        ELSE action.item::jsonb
                    END
                    ORDER BY action.ordinality
                )::json AS actions
            FROM rules rules_to_update
            CROSS JOIN LATERAL json_array_elements(rules_to_update.actions)
                WITH ORDINALITY AS action(item, ordinality)
            LEFT JOIN payee_dedupe_066 duplicates
              ON action.item->>'op' = 'set_payee'
             AND action.item->>'value' = duplicates.source_id::text
            GROUP BY rules_to_update.id
            HAVING bool_or(duplicates.target_id IS NOT NULL)
        ) rewritten
        WHERE rules.id = rewritten.id
        """
    )
    op.execute(
        """
        UPDATE rules
        SET conditions = rewritten.conditions
        FROM (
            SELECT
                rules_to_update.id,
                jsonb_agg(
                    CASE
                        WHEN duplicates.target_id IS NOT NULL THEN jsonb_set(
                            condition.item::jsonb,
                            '{value}',
                            to_jsonb(duplicates.target_id::text)
                        )
                        ELSE condition.item::jsonb
                    END
                    ORDER BY condition.ordinality
                )::json AS conditions
            FROM rules rules_to_update
            CROSS JOIN LATERAL json_array_elements(rules_to_update.conditions)
                WITH ORDINALITY AS condition(item, ordinality)
            LEFT JOIN payee_dedupe_066 duplicates
              ON condition.item->>'field' = 'payee_id'
             AND condition.item->>'value' = duplicates.source_id::text
            GROUP BY rules_to_update.id
            HAVING bool_or(duplicates.target_id IS NOT NULL)
        ) rewritten
        WHERE rules.id = rewritten.id
        """
    )
    op.execute(
        """
        DELETE FROM payees
        USING payee_dedupe_066 duplicates
        WHERE payees.id = duplicates.source_id
        """
    )
    op.drop_constraint("uq_payees_user_id_name", "payees", type_="unique")
    op.create_index(
        "uq_payees_workspace_id_lower_name",
        "payees",
        ["workspace_id", sa.text("lower(btrim(name))")],
        unique=True,
    )


def downgrade() -> None:
    # The new schema permits the same exact name in different workspaces.
    # Refuse an incompatible rollback instead of merging or renaming user data.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM payees
                GROUP BY user_id, name
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade revision 066: identical payee names exist '
                    'for the same user across workspaces';
            END IF;
        END $$
        """
    )
    op.drop_index("uq_payees_workspace_id_lower_name", table_name="payees")
    op.create_unique_constraint(
        "uq_payees_user_id_name", "payees", ["user_id", "name"]
    )
