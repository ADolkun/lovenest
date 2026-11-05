"""P&L predicate builders hand back one shared expression per entity.

Report queries nest these builders dozens of times, so building each tree
once is what keeps a report from spending its time in SQLAlchemy instead of
the database.
"""

import gc
import weakref

from sqlalchemy.orm import aliased

from app.models.transaction import Transaction
from app.services._query_filters import counts_as_user_pnl, reimbursement_unlinked_share
from app.services.transaction_semantics import credit_card_payment_filter


def test_a_builder_returns_the_same_expression_for_the_same_entity():
    own = aliased(Transaction)

    assert counts_as_user_pnl() is counts_as_user_pnl()
    assert counts_as_user_pnl(own) is counts_as_user_pnl(own)
    assert counts_as_user_pnl(own) is not counts_as_user_pnl()
    assert credit_card_payment_filter() is credit_card_payment_filter()
    assert reimbursement_unlinked_share() is reimbursement_unlinked_share()


def _build_for_a_fresh_alias() -> weakref.ref:
    own = aliased(Transaction)
    counts_as_user_pnl(own)
    return weakref.ref(own)


def test_per_query_aliases_do_not_accumulate():
    refs = [_build_for_a_fresh_alias() for _ in range(50)]
    gc.collect()

    assert sum(ref() is not None for ref in refs) < 20
