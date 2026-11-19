"""Shared SQLAlchemy filter fragments for report/dashboard queries.

Centralizes the "what counts as real income/expense" definition so every
aggregation site agrees. Changes to the rule (e.g. adding a new exclusion
signal) only need to be made here.
"""
import uuid
from datetime import date
from typing import Optional

from sqlalchemy import (
    Numeric,
    and_,
    case,
    exists,
    func,
    literal,
    literal_column,
    not_,
    or_,
    select,
    type_coerce,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.app_clock import app_today
from app.core.shared_scope import in_bound_workspace, invited_group_member_ids
from app.models.account import Account
from app.models.asset import Asset
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.transaction_category_line import TransactionCategoryLine
from app.models.transaction_reimbursement import TransactionReimbursement
from app.services.transaction_semantics import (
    TransactionEntity,
    cached_expression,
    credit_card_payment_filter,
    credit_card_refund_filter,
)


@cached_expression
def has_valid_transfer_pair(entity: TransactionEntity = Transaction):
    """SQL predicate requiring a second transaction with the same pair id."""
    counterpart = aliased(Transaction)
    return and_(
        entity.transfer_pair_id.is_not(None),
        exists(
            select(1).where(
                counterpart.transfer_pair_id == entity.transfer_pair_id,
                counterpart.id != entity.id,
            )
        ),
    )


def _touches_reimbursement_link():
    return or_(
        TransactionReimbursement.reimbursement_id == Transaction.id,
        TransactionReimbursement.expense_id == Transaction.id,
    )


def has_reimbursement_links():
    """SQL predicate: the row is either side of a reimbursement link."""
    return exists(select(1).where(_touches_reimbursement_link()))


def reimbursement_link_side_counts(entity: TransactionEntity = Transaction):
    """SQL predicate: a reimbursement link may count on this side of it.

    A link is in effect only while both rows it joins are in user P&L
    (Lovenest ADR 0012). Otherwise one side would drop the linked amount while the
    other, no longer reported, could not give it back, and income minus
    expenses would move. A closed account's rows stay in period reports, so
    closing one does not switch its links off. A row split across categories
    is in P&L while any of its parts is.
    """
    return counts_as_user_pnl_row(entity)


@cached_expression
def reimbursement_unlinked_share():
    """The fraction of a row that reimbursement links leave in user P&L.

    A link takes its amount out of both rows it joins, each in its own month
    and category (Lovenest ADR 0012), so a row keeps `1 - linked / |amount|`, never
    below zero. Unitless, so it scales `amount`, `amount_primary`, a category
    line's share or any primary-currency expression alike: a row split across
    categories gives up the linked amount from every part in proportion.

    Only links whose other row still counts are summed. The row's own
    `counts_as_user_pnl` is not checked here: every reporting query already
    drops a row that fails it.
    """
    counterpart = aliased(Transaction)
    linked = func.sum(TransactionReimbursement.amount)
    magnitude = func.abs(Transaction.amount)
    # No link rows: SUM is NULL, so the ELSE yields NULL and the share is 1.
    linked_fraction = (
        select(case((linked >= magnitude, 1), else_=linked / magnitude))
        .select_from(TransactionReimbursement)
        .join(
            counterpart,
            or_(
                and_(
                    TransactionReimbursement.reimbursement_id == Transaction.id,
                    counterpart.id == TransactionReimbursement.expense_id,
                ),
                and_(
                    TransactionReimbursement.expense_id == Transaction.id,
                    counterpart.id == TransactionReimbursement.reimbursement_id,
                ),
            ),
        )
        .where(reimbursement_link_side_counts(counterpart))
        .correlate(Transaction)
        .scalar_subquery()
    )
    # Typed as Numeric so the helpers it scales keep returning Decimals.
    return type_coerce(1 - func.coalesce(linked_fraction, 0), Numeric())


@cached_expression
def spending_offset_filter(category_id=None):
    """SQL predicate for a credit (or one part of it) that gives money back
    on spending.

    The category the user chose decides: a credit filed under any category
    not flagged `is_income` (a refund, an employer reimbursement, a friend
    paying back their half) nets against that category. Uncategorized credits
    have no such signal, so only card refunds offset; the rest stay income.

    `category_id` is the one `counts_as_pnl` judges: `line_category_id()`
    after `with_category_lines`, so each part of a split credit is income or
    an offset by its own category, or `Transaction.category_id` where rows
    cannot be split. `None` means the row's own category.
    """
    if category_id is None:
        category_id = Transaction.category_id
    return and_(
        Transaction.type == "credit",
        ~credit_card_payment_filter(),
        or_(
            # The IS NOT NULL guard keeps this two-valued: a bare NULL IN (...)
            # is NULL, and negating it would drop the credit from income too.
            and_(
                category_id.is_not(None),
                category_id.in_(
                    select(Category.id).where(Category.is_income.is_(False))
                ),
            ),
            and_(category_id.is_(None), credit_card_refund_filter()),
        ),
    )


def user_pnl_income_amount(amount_expr, category_id=None):
    """Income amount, leaving out credits that offset spending and the part
    of a credit linked to the expenses it repays. `category_id` as in
    `spending_offset_filter`."""
    return case(
        (
            and_(Transaction.type == "credit", ~spending_offset_filter(category_id)),
            func.abs(amount_expr) * reimbursement_unlinked_share(),
        ),
        else_=0,
    )


def user_pnl_expense_amount(amount_expr, category_id=None):
    """Expense amount, with spending offsets reducing rather than inflating it
    and reimbursement links taken out of both the expense and the credit.
    `category_id` as in `spending_offset_filter`."""
    return case(
        (
            Transaction.type == "debit",
            func.abs(amount_expr) * reimbursement_unlinked_share(),
        ),
        (
            spending_offset_filter(category_id),
            -func.abs(amount_expr) * reimbursement_unlinked_share(),
        ),
        else_=0,
    )


def user_pnl_share_income(share_expr, category_id=None):
    """`user_pnl_income_amount` for a group-split share of the row, keeping
    the share's sign: an exact split may give a member a negative share, and
    taking its magnitude would move the owner's part the wrong way."""
    return share_expr * user_pnl_income_amount(literal(1), category_id)


def user_pnl_share_expense(share_expr, category_id=None):
    """`user_pnl_expense_amount` for a group-split share, signed as in
    `user_pnl_share_income`."""
    return share_expr * user_pnl_expense_amount(literal(1), category_id)


def user_pnl_group(category_id=None):
    """Map spending-offset credits into the expense group for category reporting.
    `category_id` as in `spending_offset_filter`."""
    return case(
        (spending_offset_filter(category_id), "expenses"),
        (Transaction.type == "credit", "income"),
        else_="expenses",
    )


def is_confirmed():
    """SQL filter: the charge is settled rather than merely authorized.

    One of the two independent axes a transaction sits on. This one is about
    *confirmation*: a pending row is real money already committed, it just
    has not cleared yet. It says nothing about when the row is dated.
    """
    return Transaction.status == "posted"


def is_not_future(as_of: date):
    """SQL filter: the transaction has already happened by ``as_of``.

    The other axis, and a pure date question. A future-dated row is forecast
    no matter how confirmed it is; a past-dated row has happened no matter
    whether the bank has cleared it.
    """
    return Transaction.date <= as_of


def is_inside_provider_snapshot():
    """SQL filter: the provider's balance already accounts for this row.

    A connected account's current balance is the number the provider sends,
    not a sum of our rows, and providers net out the pending charges they
    report. A row typed by hand is ambiguous the same way, since the user is
    usually copying a charge the bank is already showing them.

    A recurring placeholder is the one case we can be sure about: we invented
    the row from a schedule, so no provider has ever seen it. Treating it as
    already counted makes it cancel itself out, leaving a charge that shows up
    in the forecast totals but moves no balance.
    """
    return Transaction.source != "recurring"


def counts_in_current_balance(as_of: date):
    """SQL filter: the row belongs in the balance labelled "current".

    Composed from the two axes above so the definition lives in one place and
    moving the line later is a change here rather than at every query site.

    Today the line sits at "confirmed and not future", with one exception:
    a credit card's balance is the debt owed, and an authorized purchase is
    already owed, so pending card rows stay in. Without that carve-out the
    card's balance understates the debt while its own bill total includes it.
    """
    return and_(
        is_not_future(as_of),
        or_(is_confirmed(), Account.type == "credit_card"),
    )


def amount_in_account_currency(account_currency):
    """SQL expression: a row's amount in its account's currency.

    A row in another currency only has a usable account-currency value once
    ``amount_primary`` was stamped; an unconverted one contributes zero
    instead of being read 1:1 (#353, #1263). Falling back to the native amount
    there is how a 700,000 CLP purchase became 700,000 BRL in the ledger.

    The codes are compared upper-cased on both sides: a row and its account can
    name the same currency in different cases, because imports store whatever
    the file or adapter sent (OFX ``Ccy`` included) and the account keeps the
    code as given, while the transaction schema upper-cases what it accepts.

    Pass a currency code, or ``Account.currency`` from a query that joins the
    account, so a correlated aggregate uses each row's own account.
    """
    return case(
        (
            func.upper(Transaction.currency) == func.upper(account_currency),
            Transaction.amount,
        ),
        else_=func.coalesce(Transaction.amount_primary, 0),
    )


def signed_amount_expr(account_currency):
    """`amount_in_account_currency`, signed: credit → +, debit → −."""
    amount = amount_in_account_currency(account_currency)
    return case(
        (Transaction.type == "credit", amount),
        else_=-amount,
    )


def reporting_date_col(accounting_mode: str):
    """Return the SQL date expression used for period attribution.

    A user-supplied reporting override wins first while the provider `date`
    remains immutable bank truth. Credit-card bill overrides retain their
    existing priority over the cash/accrual base date.
    """
    base = (
        Transaction.effective_date
        if accounting_mode == "accrual"
        else Transaction.date
    )
    return func.coalesce(
        Transaction.reporting_date_override,
        Transaction.effective_bill_date,
        base,
    )


def reporting_date_value(transaction: Transaction, accounting_mode: str) -> date:
    """Python equivalent of :func:`reporting_date_col` for loaded rows."""
    return (
        transaction.reporting_date_override
        or transaction.effective_bill_date
        or (transaction.effective_date if accounting_mode == "accrual" else transaction.date)
    )


def is_not_ignored():
    """SQL filter: the row is not one the user told us to disregard.

    Only the ignore signal, without the transfer/settlement family that
    `counts_as_pnl` folds in, because hiding rows from a *list* is a
    different question from leaving them out of a *total*: a transfer still
    belongs in the ledger the user is reading.

    Matches what the UI badges as ignored, which is the transaction flag or
    its category's — see `TransactionRead.reflect_ignored_category`. A list
    that hid one but not the other would leave visibly-ignored rows behind
    and look broken.
    """
    return and_(
        Transaction.is_ignored.is_(False),
        or_(
            Transaction.category_id.is_(None),
            Transaction.category_id.not_in(
                select(Category.id).where(Category.is_ignored.is_(True))
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Category parts
#
# A transaction split across categories keeps one row and its full amount;
# `transaction_category_lines` says how much of it belongs to each category.
# Anything that totals money *by category*, or that drops a category from a
# total (transfer-like and ignored categories), has to read those lines, or
# a split row would land as "uncategorized" or be counted whole.
#
# Aggregations do that with one LEFT JOIN: a plain row stays one row, a split
# row becomes one row per line. Every amount summed after the join must then
# be the line's share (`line_amount`, `line_amount_primary`), every count
# must be `count(DISTINCT Transaction.id)`, and every category judgment
# (`counts_as_pnl`, `is_transfer`, `spending_offset_filter` and the
# `user_pnl_*` amounts) must be handed `line_category_id()`.
#
# Balances, bills, net worth and payee totals read the whole row and must
# never join the lines.
# ---------------------------------------------------------------------------

CATEGORY_LINE = aliased(TransactionCategoryLine, name="category_line")


def with_category_lines(stmt):
    """Join a statement over `transactions` to its category lines."""
    return stmt.outerjoin(CATEGORY_LINE, CATEGORY_LINE.transaction_id == Transaction.id)


@cached_expression
def line_category_id():
    """The category of a joined part: the line's, or the row's own.

    Cached so the filters cached on their `category_id` argument see the
    same expression on every call."""
    return func.coalesce(CATEGORY_LINE.category_id, Transaction.category_id)


def line_amount():
    """The joined part's amount, with the sign of its transaction."""
    return case(
        (CATEGORY_LINE.id.is_(None), Transaction.amount),
        (Transaction.amount < 0, -CATEGORY_LINE.amount),
        else_=CATEGORY_LINE.amount,
    )


def line_amount_primary():
    """The joined part's `amount_primary` (NULL when the row has none).

    Multiplying before dividing keeps the result exact whenever the part's
    share is (150 * 100 / 150 is 100, where 150 * (100 / 150) carries a
    rounded ratio into every total). The `* 1.0` keeps SQLite from dividing
    two integers.
    """
    return case(
        (CATEGORY_LINE.id.is_(None), Transaction.amount_primary),
        else_=(Transaction.amount_primary * CATEGORY_LINE.amount * literal_column("1.0"))
        / func.nullif(func.abs(Transaction.amount), 0),
    )


def line_primary_or_amount():
    """The joined part in primary currency, falling back to its own amount."""
    return func.coalesce(line_amount_primary(), line_amount())


def any_line_in(category_ids):
    """SQL filter: some category line of the row is in `category_ids`."""
    return exists().where(
        TransactionCategoryLine.transaction_id == Transaction.id,
        TransactionCategoryLine.category_id.in_(category_ids),
    )


def _transfer_category_ids():
    return select(Category.id).where(Category.treat_as_transfer.is_(True))


def _ignored_category_ids():
    return select(Category.id).where(Category.is_ignored.is_(True))


def _in_transfer_category(category_id, entity: TransactionEntity):
    """The category clause of `is_transfer`: a `treat_as_transfer` category,
    unless the row is a card credit that is not a repayment (a refund, which
    offsets spending under any category)."""
    return and_(
        category_id.is_not(None),
        category_id.in_(_transfer_category_ids()),
        not_(credit_card_refund_filter(entity)),
    )


@cached_expression
def is_transfer(category_id=None, entity: TransactionEntity = Transaction):
    """SQL predicate: the row (or one part of a split row) moves money
    between the user's own places, so P&L leaves it out of income and expense.

    The transfer clauses of `counts_as_pnl`, kept apart so the Transfer
    badge, the Type=Transfer filter and the calendar read the rule the
    totals apply. A row is a transfer when it is:
      - one leg of a valid pair. A `transfer_pair_id` whose second leg is
        gone is not a transfer,
      - either side of a credit-card repayment the payment classifier
        recognises, even unpaired,
      - in a `treat_as_transfer` category (a one-sided movement such as an
        investment application), unless it is a card credit that is not a
        repayment: that is a refund, and it offsets spending under any
        category.

    `category_id` is `line_category_id()` after `with_category_lines`, so a
    split row is judged part by part; `None` means `entity.category_id`.
    `entity` is an alias of `Transaction` when a query asks about a second
    row it joined in.

    Two-valued on every row. Without the IS NOT NULL guard an uncategorized
    row would read NULL in the category clause, `NOT` keeps NULL, and
    `counts_as_pnl` would drop the row from every total.
    """
    if category_id is None:
        category_id = entity.category_id
    return or_(
        has_valid_transfer_pair(entity),
        credit_card_payment_filter(entity),
        _in_transfer_category(category_id, entity),
    )


def is_not_transfer(category_id=None, entity: TransactionEntity = Transaction):
    """SQL filter: the negation of `is_transfer`, which is two-valued, so
    uncategorized rows survive it."""
    return not_(is_transfer(category_id, entity))


def is_transfer_row():
    """SQL filter for a transaction *list*: `is_transfer` for the whole row,
    or, for a row split across categories, for every one of its parts. A
    list shows whole rows, so it cannot join the lines.

    The row flag says the row is out of the P&L totals. A split row with a
    transfer part and a spending part still counts its spending part, so it
    is not a transfer row; its transfer part still leaves the totals on its
    own wherever amounts are summed part by part.
    """
    non_transfer_line = exists().where(
        TransactionCategoryLine.transaction_id == Transaction.id,
        or_(
            TransactionCategoryLine.category_id.is_(None),
            TransactionCategoryLine.category_id.not_in(_transfer_category_ids()),
        ),
    )
    return or_(
        is_transfer(Transaction.category_id),
        and_(
            Transaction.has_category_lines.is_(True),
            not_(non_transfer_line),
            not_(credit_card_refund_filter()),
        ),
    )


def has_transfer_part_row():
    """SQL filter for the Type=Transfer *list*: `is_transfer` for the whole
    row, or for any of its category lines. Wider than `is_transfer_row`, so a
    split row's transfer part can be found; the list's footer then sums only
    the parts that are transfers."""
    return or_(
        is_transfer(Transaction.category_id),
        and_(
            any_line_in(_transfer_category_ids()),
            not_(credit_card_refund_filter()),
        ),
    )


async def transfer_ids(
    session: AsyncSession, transaction_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """The rows among ``transaction_ids`` that `is_transfer_row` holds for."""
    if not transaction_ids:
        return set()
    result = await session.execute(
        select(Transaction.id).where(
            Transaction.id.in_(transaction_ids), is_transfer_row()
        )
    )
    return set(result.scalars())


@cached_expression
def counts_as_pnl(category_id=None, entity: TransactionEntity = Transaction):
    """SQL filter: True when a transaction (or one part of a split
    transaction) should contribute to income/expense totals.

    Pass `line_category_id()` after `with_category_lines`, so a split row's
    transfer-like or ignored parts drop out alone. The row's own category
    (`Transaction.category_id`, or `None` for `entity.category_id`) is only
    right where the rows cannot be split (group-split shares, for instance);
    the pair, card, ignored, `exclude_from_pnl` and settlement clauses always
    read the whole row.

    Excludes:
      - transfers, as `is_transfer` defines them,
      - transactions flagged `is_ignored=True` (user-marked as not to be reported),
      - transactions flagged `exclude_from_pnl=True` (kept in balance,
        omitted from income and expense calculations),
      - settlement debits,
      - parts in categories flagged `is_ignored=True` (user-marked as not to be reported).

    Does NOT exclude `source='opening_balance'` — callers that already
    filter those keep doing so; this helper only handles the transfer-like
    exclusion family so both rules stay visible at each call site.
    """
    if category_id is None:
        category_id = entity.category_id
    return and_(
        not_(is_transfer(category_id, entity)),
        entity.is_ignored.is_(False),
        entity.exclude_from_pnl.is_(False),
        # Settlement *debits* are repayments of debts that were already
        # booked as an expense via the share. Counting them would
        # double-count. Settlement *credits*, however, represent the
        # receiver actually getting the cash back — they offset the
        # over-recorded expense from when the receiver paid the full
        # parent transaction. So we keep credits, drop debits.
        ~and_(entity.source == "settlement", entity.type == "debit"),
        or_(category_id.is_(None), category_id.not_in(_ignored_category_ids())),
    )


def counts_on_bill(net_payments: bool = False):
    """SQL filter: True when a transaction belongs on a credit-card bill.

    A bill total is an *amount owed*, not a reporting figure, and the two
    answer to different authorities: the bill has to match what the bank
    says you owe, while P/L answers to how the user chose to categorize
    their spending. So the card's cycle total cannot reuse
    `counts_as_pnl`. Every judgment that helper makes about what counts
    as *spending* is a judgment the bank never made.

    Kept out, because they are genuinely not charges on this bill:
      - valid paired transfers, by default (the bill *payment* is not a
        purchase). With ``net_payments=True`` the *credit* leg of a valid
        pair counts instead: an intermediate payment into the card shrinks
        the amount owed, the way a refund does. Callers net only what is
        left after the payment settles any debt that was already
        outstanding (banks apply repayments oldest first), see
        `get_account_summary`.
      - settlement debits (a repayment of a share already booked),
      - rows the user flagged `is_ignored`, on the transaction or its
        category. Those leave the account balance too, so dropping them
        from the bill keeps the card's two numbers telling one story.

    Kept in, and this is the whole point of the helper:
      - `treat_as_transfer` categories. Buying an investment or paying a
        consortium installment with the card still lands on the statement,
        so a charge doesn't stop being owed to the bank because of how it
        was tagged afterwards (issue #647).
      - rows flagged `exclude_from_pnl`. Its canonical use is a work
        expense paid on a personal card and reimbursed later — and the
        bank bills the whole card either way.

    The rule both share: a bill honors "make this disappear" and
    ignores "report this differently".

    Credits the payment classifier recognizes are dropped even when
    unpaired (the payer's account isn't connected, the amount doesn't match
    exactly, or it was a partial payment): letting one through would net a
    repayment against new debt. Upstream (#649) keys this on the credit's
    `treat_as_transfer` category because it cannot tell a refund from an
    unpaired payment; Lovenest can, so a refund filed under a transfer-like
    category still shrinks the bill, exactly as it does in P&L.

    Deliberately spelled out rather than defined as "`counts_as_pnl`
    minus a clause": a filter for what a *report* excludes will keep
    growing as the product learns new ways to say "don't count this",
    and a bill total must not inherit those. Every clause here is one
    somebody chose for the bill.
    """
    ignored_category = Transaction.category_id.in_(
        select(Category.id).where(Category.is_ignored.is_(True))
    )
    valid_pair = has_valid_transfer_pair()
    if net_payments:
        # The credit leg of a valid pair is a payment by construction: the
        # pair proves the money moved between the user's own accounts, so
        # neither the pair nor the payment classifier keeps it off the bill.
        paired_payment = and_(Transaction.type == "credit", valid_pair)
        transfer_clause = or_(not_(valid_pair), paired_payment)
        payment_clause = or_(
            Transaction.type == "debit",
            not_(credit_card_payment_filter()),
            paired_payment,
        )
    else:
        transfer_clause = not_(valid_pair)
        payment_clause = or_(Transaction.type == "debit", not_(credit_card_payment_filter()))
    return and_(
        transfer_clause,
        Transaction.is_ignored.is_(False),
        ~and_(Transaction.source == "settlement", Transaction.type == "debit"),
        or_(Transaction.category_id.is_(None), ~ignored_category),
        payment_clause,
    )


@cached_expression
def counts_as_user_pnl(category_id=None, entity: TransactionEntity = Transaction):
    """SQL filter for *user-level* P/L (dashboard, reports, budgets).

    Stricter than `counts_as_pnl`: also drops settlement *credits*. Under
    the share-only model an owner's expense for a split tx is just their
    share, so the corresponding settlement credits would double-count.
    Per-account stats still use `counts_as_pnl` because account ledgers
    track real cash through the account, not user P/L.
    """
    return and_(
        counts_as_pnl(category_id, entity),
        entity.source != "settlement",
    )


@cached_expression
def counts_as_user_pnl_row(entity: TransactionEntity = Transaction):
    """SQL filter for a transaction *list*: the row counts toward the user's
    P/L, judging a split row by its parts (it counts when any part does).
    A list shows whole rows, so it cannot join the lines."""
    return and_(
        counts_as_user_pnl(entity.category_id, entity),
        or_(entity.has_category_lines.is_(False), _any_counted_line(entity)),
    )


def _any_counted_line(entity: TransactionEntity, *conditions):
    """Some category line of the row passes the category clauses of
    `counts_as_pnl` (and `conditions`); the row-level clauses are the
    caller's."""
    line_category = TransactionCategoryLine.category_id
    return exists().where(
        TransactionCategoryLine.transaction_id == entity.id,
        not_(_in_transfer_category(line_category, entity)),
        line_category.not_in(_ignored_category_ids()),
        *conditions,
    )


def in_user_pnl_group_row(pnl_group: str):
    """SQL filter for a transaction *list*: the row, or for a split row any
    of its counted parts, falls in the `user_pnl_group` `pnl_group`. A split
    credit can hold an income part and a spending-offset part, so it is
    listed under both. Pair with `counts_as_user_pnl_row`."""
    return or_(
        and_(
            Transaction.has_category_lines.is_(False),
            user_pnl_group(Transaction.category_id) == pnl_group,
        ),
        and_(
            Transaction.has_category_lines.is_(True),
            _any_counted_line(
                Transaction,
                user_pnl_group(TransactionCategoryLine.category_id) == pnl_group,
            ),
        ),
    )


def viewer_share_of_row(viewer_id: Optional[uuid.UUID] = None):
    """SQL expression: the fraction of a row that is the viewer's own cost.

    The one rule every user-P&L reader (dashboard, reports, category
    statement, budgets, cash-flow burn, transaction list footer) applies to
    a group-split row of the workspace being viewed:

    - the viewer owns the row: the row less every other member's share. The
      viewer's own members are those linked to them and the self-member of
      a group they created, so a payer in someone else's group keeps their
      own share, not the creator's;
    - someone else owns the row and the viewer holds a share in it, through
      the same members: that share alone;
    - anyone else, including every row without group splits: the whole row.

    A row of another workspace reaches a reader only as a Split Projection
    (`viewer_shared_pnl`, `viewer_shared_spending_by_category`), which is the
    second case; those helpers leave the viewed workspace out, so each row
    counts once per viewer, at their part of it. Readers scoped to some
    accounts drop the rule and count whole rows, since they follow cash.

    Without `viewer_id` the row's owner is the viewer, for workspace-level
    figures nobody in particular is looking at (recurring detection).

    Signed and unclamped: an exact split may give a member a negative share,
    and the owner's part then exceeds the row.
    """
    from app.models.group import Group, GroupMember
    from app.models.transaction_split import TransactionSplit

    viewer = Transaction.user_id if viewer_id is None else viewer_id
    viewer_member_ids = (
        select(GroupMember.id)
        .outerjoin(Group, Group.id == GroupMember.group_id)
        .where(
            or_(
                GroupMember.linked_user_id == viewer,
                and_(GroupMember.is_self.is_(True), Group.user_id == viewer),
            )
        )
        .correlate(Transaction)
    )
    magnitude = func.nullif(func.abs(Transaction.amount), 0)
    others = (
        select(func.sum(TransactionSplit.share_amount))
        .where(
            TransactionSplit.transaction_id == Transaction.id,
            TransactionSplit.group_member_id.not_in(viewer_member_ids),
        )
        .correlate(Transaction)
        .scalar_subquery()
    )
    owned = 1 - func.coalesce(others / magnitude, 0)
    if viewer_id is None:
        return type_coerce(owned, Numeric())
    viewer_share = (
        select(func.sum(TransactionSplit.share_amount))
        .where(
            TransactionSplit.transaction_id == Transaction.id,
            TransactionSplit.group_member_id.in_(viewer_member_ids),
        )
        .correlate(Transaction)
        .scalar_subquery()
    )
    return type_coerce(
        case(
            (Transaction.user_id == viewer_id, owned),
            else_=func.coalesce(viewer_share / magnitude, 1),
        ),
        Numeric(),
    )


def split_projection_ids(user_id: uuid.UUID, workspace_id: uuid.UUID):
    """Ids of the rows behind `viewer_shared_pnl`: rows of workspaces other
    than `workspace_id`, owned by someone else, that the user holds an
    invited share of. Their `viewer_share_of_row` is that share."""
    from app.models.transaction_split import TransactionSplit

    parent = aliased(Transaction)
    return (
        select(TransactionSplit.transaction_id)
        .join(parent, parent.id == TransactionSplit.transaction_id)
        .where(
            TransactionSplit.group_member_id.in_(invited_group_member_ids(user_id)),
            parent.workspace_id != workspace_id,
            parent.user_id != user_id,
            in_bound_workspace(parent.workspace_id),
        )
    )


async def viewer_shared_pnl(
    session: AsyncSession,
    user_id: uuid.UUID,
    month_start: date,
    month_end: date,
    use_effective_date: bool = False,
    primary_currency: Optional[str] = None,
    posted_only: bool = True,
    *,
    workspace_id: uuid.UUID,
) -> tuple[float, float]:
    """Return (income, expense) totals contributed by transactions of other
    workspaces that the viewer doesn't own but participates in via a group
    split: the Split Projection. Rows of `workspace_id`, the workspace being
    viewed, are left out; its readers count them through
    `viewer_share_of_row`.

    Concert tickets paid by a friend show up as the viewer's share in
    their own spending picture — without inflating account balances.
    Shares follow the parent's classification, so the viewer's part of a
    refund on that expense comes back as negative expense, not income.

    When `primary_currency` is given, mixed-currency shares are
    converted to that currency via the FX service. Otherwise, sums
    are taken in raw nominal terms (only safe when all shares are
    same-currency, e.g., during single-currency tests).
    """
    from app.models.transaction_split import TransactionSplit

    # Cross-workspace Splitwise projection: include only invitations
    # (linked_user_id matches but is_self is False). Self-memberships
    # represent the user in their own group and are already counted via
    # the workspace-scoped Transaction filter at the caller.
    member_ids = invited_group_member_ids(user_id)
    date_col = reporting_date_col("accrual" if use_effective_date else "cash")

    result = await session.execute(
        select(
            Transaction.currency,
            func.sum(user_pnl_share_income(TransactionSplit.share_amount, Transaction.category_id)),
            func.sum(user_pnl_share_expense(TransactionSplit.share_amount, Transaction.category_id)),
        )
        .select_from(TransactionSplit)
        .join(Transaction, TransactionSplit.transaction_id == Transaction.id)
        .where(
            TransactionSplit.group_member_id.in_(member_ids),
            in_bound_workspace(Transaction.workspace_id),
            Transaction.workspace_id != workspace_id,
            # Avoid double-counting if the viewer also owns the parent.
            Transaction.user_id != user_id,
            Transaction.source != "opening_balance",
            *(
                [Transaction.status == "posted", date_col <= app_today()]
                if posted_only
                else []
            ),
            date_col >= month_start,
            date_col < month_end,
            counts_as_user_pnl(Transaction.category_id),
        )
        .group_by(Transaction.currency)
    )

    if primary_currency is None:
        # Backward-compatible nominal sum (caller is responsible for
        # currency homogeneity). Acceptable when only one currency is
        # in play.
        income_total = 0.0
        expense_total = 0.0
        for row in result.all():
            income_total += float(row[1] or 0)
            expense_total += float(row[2] or 0)
        return income_total, expense_total

    # FX-aware: convert each currency bucket to primary.
    from decimal import Decimal as _Decimal

    from app.services.fx_rate_service import convert as _convert

    income_total = 0.0
    expense_total = 0.0
    for row in result.all():
        cur, inc_raw, exp_raw = row[0], row[1] or 0, row[2] or 0
        if inc_raw:
            inc_pri, _ = await _convert(session, _Decimal(str(inc_raw)), cur, primary_currency)
            income_total += float(inc_pri)
        if exp_raw:
            exp_pri, _ = await _convert(session, _Decimal(str(exp_raw)), cur, primary_currency)
            expense_total += float(exp_pri)
    return income_total, expense_total


async def viewer_shared_spending_by_category(
    session: AsyncSession,
    user_id: uuid.UUID,
    month_start: date,
    month_end: date,
    use_effective_date: bool = False,
    primary_currency: Optional[str] = None,
    posted_only: bool = True,
    *,
    workspace_id: uuid.UUID,
) -> dict:
    """Return {category_id (uuid|None): total_share_expense_float} for
    transactions of other workspaces where the viewer participates via a
    group split, leaving out `workspace_id` as `viewer_shared_pnl` does. A
    share of a spending-offset credit counts negative, as there.

    With `primary_currency`, totals are FX-converted; otherwise raw
    nominal sums (single-currency only)."""
    from app.models.transaction_split import TransactionSplit

    # Cross-workspace Splitwise projection: include only invitations
    # (linked_user_id matches but is_self is False). Self-memberships
    # represent the user in their own group and are already counted via
    # the workspace-scoped Transaction filter at the caller.
    member_ids = invited_group_member_ids(user_id)
    date_col = reporting_date_col("accrual" if use_effective_date else "cash")

    result = await session.execute(
        select(
            Transaction.category_id,
            Transaction.currency,
            func.sum(user_pnl_share_expense(TransactionSplit.share_amount, Transaction.category_id)),
        )
        .select_from(TransactionSplit)
        .join(Transaction, TransactionSplit.transaction_id == Transaction.id)
        .where(
            TransactionSplit.group_member_id.in_(member_ids),
            in_bound_workspace(Transaction.workspace_id),
            Transaction.user_id != user_id,
            user_pnl_group(Transaction.category_id) == "expenses",
            Transaction.source != "opening_balance",
            *(
                [Transaction.status == "posted", date_col <= app_today()]
                if posted_only
                else []
            ),
            date_col >= month_start,
            date_col < month_end,
            Transaction.workspace_id != workspace_id,
            counts_as_user_pnl(Transaction.category_id),
        )
        .group_by(Transaction.category_id, Transaction.currency)
    )

    out: dict = {}
    if primary_currency is None:
        for cat_id, _cur, total in result.all():
            out[cat_id] = out.get(cat_id, 0.0) + float(total or 0)
        return out

    from decimal import Decimal as _Decimal

    from app.services.fx_rate_service import convert as _convert

    for cat_id, cur, total in result.all():
        if not total:
            continue
        converted, _ = await _convert(session, _Decimal(str(total)), cur, primary_currency)
        out[cat_id] = out.get(cat_id, 0.0) + float(converted)
    return out


def holding_inside_account_balance(account_id: Optional[uuid.UUID] = None):
    """SQL predicate: a Holding its own Account's balance already carries.

    An Account's balance is the account's *total* — the Liquid Cash and the
    Holdings together, which is why Liquid Cash is derived by subtracting the
    Holdings from it (CONTEXT.md). Net worth adds Accounts and Assets, so a
    Holding attributed to a synced Account has to be counted on one side only,
    and the Account's side is the one a provider actually reports.

    Matched on the account the provider attributed the Holding to. A Holding
    the provider could not attribute (Pluggy files investments at the item, not
    the account) matches nothing and stays counted, as does one whose account
    is closed — a closed account is left out of the balance sum entirely, so
    its Holdings are the only place its value is left.

    `account_id` narrows the same join to one account, which is how a caller
    asks the question from the Account's side ("does this account's balance
    carry Holdings?"). The two readings have to stay one expression: an account
    net worth treats as carrying its Holdings is exactly an account whose
    transaction ledger cannot be reconciled against its balance
    (``account_service.sync_opening_balance_for_connected_account``).
    """
    conditions = [
        Account.connection_id == Asset.connection_id,
        Account.external_id == Asset.account_external_id,
        Account.workspace_id == Asset.workspace_id,
        Account.is_closed == False,  # noqa: E712 — SQL, not Python truth
    ]
    if account_id is not None:
        conditions.append(Account.id == account_id)
    return exists(select(1).where(*conditions))
