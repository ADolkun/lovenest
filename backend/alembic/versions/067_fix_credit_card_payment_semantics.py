"""repair credit-card payment rules and known false transfer matches

Revision ID: 067
Revises: 066
Create Date: 2026-07-19
"""

from alembic import op

revision = "067"
down_revision = "066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rewrite only the legacy auto-rule that targets this workspace's transfer
    # category. User-created rules may share the display name, so name alone is
    # not sufficient evidence.
    op.get_bind().exec_driver_sql(
        r"""
        UPDATE rules
        SET conditions = (
            SELECT (
                COALESCE(
                    jsonb_agg(condition.item::jsonb ORDER BY condition.ordinality)
                        FILTER (
                            WHERE NOT (
                                condition.item->>'field' = 'description'
                                AND condition.item->>'op' = 'contains'
                                AND upper(condition.item->>'value') = 'BILL PAYMENT'
                            )
                        ),
                    '[]'::jsonb
                )
                || CASE
                    WHEN rules.conditions::jsonb @> '[{"field":"description","op":"regex","value":"BILT CARD(?!.*(?:HOUSING|RENT)).*PMT"}]'::jsonb
                    THEN '[]'::jsonb
                    ELSE '[{"field":"description","op":"regex","value":"BILT CARD(?!.*(?:HOUSING|RENT)).*PMT"}]'::jsonb
                END
            )::json
            FROM json_array_elements(rules.conditions)
                WITH ORDINALITY AS condition(item, ordinality)
        )
        WHERE name = 'Auto: Credit Card Payment'
          AND conditions_op = 'or'
          AND EXISTS (
              SELECT 1
              FROM json_array_elements(rules.conditions) condition(item)
              WHERE condition.item->>'field' = 'description'
                AND condition.item->>'op' = 'contains'
                AND upper(condition.item->>'value') = 'BILL PAYMENT'
          )
          AND EXISTS (
              SELECT 1
              FROM json_array_elements(rules.actions) action(item)
              JOIN categories target
                ON action.item->>'value' ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               AND target.id = (action.item->>'value')::uuid
               AND target.workspace_id = rules.workspace_id
               AND target.name = 'Credit Card Payment'
               AND target.treat_as_transfer IS TRUE
              WHERE action.item->>'op' = 'set_category'
          )
        """
    )

    # Deterministically choose the system category when duplicate names exist.
    op.execute(
        r"""
        WITH target_categories AS (
            SELECT DISTINCT ON (workspace_id) workspace_id, id
            FROM categories
            WHERE name = 'Credit Card Payment'
            ORDER BY workspace_id, is_system DESC, id
        )
        UPDATE transactions transaction
        SET category_id = target.id
        FROM accounts account, categories current, target_categories target
        WHERE transaction.account_id = account.id
          AND transaction.category_id = current.id
          AND target.workspace_id = transaction.workspace_id
          AND current.name = 'Bills & Utilities'
          AND account.type <> 'credit_card'
          AND transaction.type = 'debit'
          AND upper(transaction.description) LIKE '%BILT CARD%'
          AND upper(transaction.description) LIKE '%PMT%'
          AND upper(transaction.description) NOT LIKE '%HOUSING%'
          AND upper(transaction.description) NOT LIKE '%RENT%'
        """
    )
    op.execute(
        r"""
        WITH target_categories AS (
            SELECT DISTINCT ON (workspace_id) workspace_id, id
            FROM categories
            WHERE name = 'Bills & Utilities'
            ORDER BY workspace_id, is_system DESC, id
        )
        UPDATE transactions transaction
        SET category_id = target.id
        FROM accounts account, categories current, target_categories target
        WHERE transaction.account_id = account.id
          AND transaction.category_id = current.id
          AND target.workspace_id = transaction.workspace_id
          AND current.name = 'Credit Card Payment'
          AND account.type = 'credit_card'
          AND transaction.type = 'debit'
          AND (
              upper(transaction.description) LIKE 'AT&T BILL PAYMENT%'
              OR upper(transaction.description) LIKE 'LEMONADE INSURANCE%'
          )
        """
    )

    # A pair id is transaction-to-transaction metadata. A singleton cannot be a
    # valid pair and previously hid real purchases and refunds from reporting.
    op.execute(
        r"""
        UPDATE transactions transaction
        SET transfer_pair_id = NULL
        WHERE transaction.transfer_pair_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM transactions counterpart
              WHERE counterpart.transfer_pair_id = transaction.transfer_pair_id
                AND counterpart.id <> transaction.id
          )
        """
    )

    # Amount/date-only matching also linked these unrelated two-row movements.
    op.execute(
        r"""
        UPDATE transactions
        SET transfer_pair_id = NULL
        WHERE transfer_pair_id IN (
            SELECT first.transfer_pair_id
            FROM transactions first
            JOIN transactions second
              ON second.transfer_pair_id = first.transfer_pair_id
             AND second.id <> first.id
            WHERE
                (
                    upper(first.description) LIKE 'LINK.COM* SIMPLEFIN%'
                    AND upper(second.description) LIKE 'ZELLE PAYMENT%'
                )
                OR (
                    upper(first.description) LIKE '%ACORNS%'
                    AND upper(second.description) LIKE 'AUTOMATIC PAYMENT%'
                )
                OR (
                    upper(second.description) LIKE 'LINK.COM* SIMPLEFIN%'
                    AND upper(first.description) LIKE 'ZELLE PAYMENT%'
                )
                OR (
                    upper(second.description) LIKE '%ACORNS%'
                    AND upper(first.description) LIKE 'AUTOMATIC PAYMENT%'
                )
        )
        """
    )

    # Known provider-confirmed refund/dispute credits were categorized as card
    # payments. Copy the original purchase category where the match is exact;
    # leave the unmatched Metropolis dispute uncategorized rather than lying.
    op.execute(
        r"""
        WITH refund_matches AS (
            SELECT refund.id AS refund_id, purchase.category_id,
                   row_number() OVER (
                       PARTITION BY refund.id
                       ORDER BY abs(refund.date - purchase.date), purchase.id
                   ) AS rank
            FROM transactions refund
            JOIN accounts account
              ON account.id = refund.account_id
             AND account.type = 'credit_card'
            JOIN categories current
              ON current.id = refund.category_id
             AND current.name = 'Credit Card Payment'
            JOIN transactions purchase
              ON purchase.account_id = refund.account_id
             AND purchase.type = 'debit'
             AND purchase.amount = refund.amount
             AND purchase.currency = refund.currency
             AND purchase.category_id IS NOT NULL
            WHERE refund.type = 'credit'
              AND (
                  upper(refund.description) LIKE 'EZ CONTACTS % (DISPUTE CREDIT)'
                  OR upper(refund.description) LIKE 'FAYE TRAVEL INSURANCE % (RETURN)'
              )
              AND split_part(upper(refund.description), ' (', 1)
                  = split_part(upper(purchase.description), ' (', 1)
        )
        UPDATE transactions refund
        SET category_id = refund_matches.category_id
        FROM refund_matches
        WHERE refund.id = refund_matches.refund_id
          AND refund_matches.rank = 1
        """
    )
    op.execute(
        r"""
        UPDATE transactions refund
        SET category_id = NULL
        FROM accounts account, categories current
        WHERE refund.account_id = account.id
          AND account.type = 'credit_card'
          AND refund.category_id = current.id
          AND current.name = 'Credit Card Payment'
          AND refund.type = 'credit'
          AND upper(refund.description) LIKE 'METROPOLIS PARKING % (DISPUTE'
        """
    )

    # Pair unambiguous Capital One payment legs that the old detector missed.
    op.execute(
        r"""
        CREATE TEMP TABLE capital_one_pairs_067 ON COMMIT DROP AS
        WITH candidates AS (
            SELECT debit.id AS debit_id, credit.id AS credit_id,
                   count(*) OVER (PARTITION BY debit.id) AS debit_matches,
                   count(*) OVER (PARTITION BY credit.id) AS credit_matches
            FROM transactions debit
            JOIN accounts debit_account
              ON debit_account.id = debit.account_id
             AND debit_account.type <> 'credit_card'
            JOIN transactions credit
              ON credit.workspace_id = debit.workspace_id
             AND credit.user_id = debit.user_id
             AND credit.type = 'credit'
             AND credit.amount = debit.amount
             AND credit.currency = debit.currency
             AND abs(credit.date - debit.date) <= 2
             AND credit.account_id <> debit.account_id
            JOIN accounts credit_account
              ON credit_account.id = credit.account_id
             AND credit_account.type = 'credit_card'
            WHERE debit.type = 'debit'
              AND trim(upper(debit.description)) = 'CAPITAL ONE'
              AND upper(credit.description) LIKE '%CAPITAL ONE MOBILE PYMT%'
              AND debit.transfer_pair_id IS NULL
              AND credit.transfer_pair_id IS NULL
        )
        SELECT debit_id, credit_id, gen_random_uuid() AS pair_id
        FROM candidates
        WHERE debit_matches = 1 AND credit_matches = 1
        """
    )
    op.execute(
        r"""
        UPDATE transactions transaction
        SET transfer_pair_id = pair.pair_id
        FROM capital_one_pairs_067 pair
        WHERE transaction.id = pair.debit_id OR transaction.id = pair.credit_id
        """
    )


def downgrade() -> None:
    # One-way data repair: restoring known-wrong categories and pair links
    # would reintroduce incorrect financial reporting.
    pass
