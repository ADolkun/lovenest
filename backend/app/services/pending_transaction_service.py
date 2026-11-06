"""Conservative, reversible lifecycle for provider-owned pending observations.

Absence is not a settlement match. A cancelled hold, an adjusted authorization,
and a split shipment all disappear the same way; retain the original row and
ignore it only after repeated complete, fresh observations of its date window.
"""
from datetime import date, datetime, timedelta, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import Transaction
from app.providers.base import TransactionCoverage, TransactionData, to_decimal

PENDING_MISSING_GRACE = timedelta(days=2)
PENDING_MIN_AGE = timedelta(days=7)
PENDING_RECHECK_DAYS = 365


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


def preserve_pending_override(transaction: Transaction, *, becoming_pending: bool = False) -> None:
    """A person's financial edit or ignore rule takes ownership from sync."""
    if transaction.source == "sync" and (transaction.status == "pending" or becoming_pending):
        transaction.sync_metadata = {
            **(transaction.sync_metadata or {}),
            "pending_manual_override": True,
            "pending_ignore_owned": False,
        }


def _source_date(transaction: Transaction) -> date | None:
    # Use immutable bank evidence, never the user's display/effective date.
    raw = transaction.raw_data or {}
    for key in ("posted", "transacted_at"):
        try:
            value = raw.get(key)
            if value is not None and not isinstance(value, bool) and int(value) > 0:
                return datetime.fromtimestamp(int(value), timezone.utc).date()
        except (ValueError, TypeError, OverflowError, OSError):
            pass
    return None


def pending_matches_bank_evidence(transaction: Transaction) -> bool:
    """Legacy rows have no edit marker: abstain when bank evidence disagrees."""
    raw = transaction.raw_data or {}
    amount = to_decimal(raw.get("amount"))
    currency = raw.get("currency")
    return (
        raw.get("pending") is True
        and amount is not None
        and amount.copy_abs() == transaction.amount
        and ("debit" if amount < 0 else "credit") == transaction.type
        and _source_date(transaction) == transaction.date
        and (currency is None or currency == transaction.currency)
        and transaction.effective_bill_date is None
        and not transaction.transfer_amount_explicit
    )


async def pending_recheck_since(
    session: AsyncSession, account_id: uuid.UUID, since: date | None, *, now: datetime,
) -> date | None:
    """Revisit older holds instead of letting them escape the rolling cursor.

    Cap requests at the provider's initial history horizon. Older/undated rows
    need review, not an age-based deletion or an unbounded backfill every sync.
    """
    if since is None:
        return None
    rows = (await session.scalars(select(Transaction).where(
        Transaction.account_id == account_id,
        Transaction.source == "sync",
        Transaction.status == "pending",
        Transaction.is_ignored.is_(False),
    ))).all()
    oldest = now.date() - timedelta(days=PENDING_RECHECK_DAYS)
    dates = [day for row in rows if not (row.sync_metadata or {}).get("pending_manual_override")
             if (day := _source_date(row)) is not None and oldest <= day]
    return max(oldest, min([since, *dates]))


async def reconcile_pending(
    session: AsyncSession, account_id: uuid.UUID,
    incoming: list[TransactionData], coverage: TransactionCoverage | None,
) -> None:
    rows = (await session.scalars(select(Transaction).where(
        Transaction.account_id == account_id,
        Transaction.source == "sync",
        Transaction.status == "pending",
        Transaction.external_id.is_not(None),
    ))).all()
    by_id = {txn.external_id: txn for txn in incoming}
    for row in rows:
        state = dict(row.sync_metadata or {})
        if state.get("pending_manual_override"):
            continue
        observed = by_id.get(row.external_id) if row.external_id else None
        if observed is not None:
            retired_at = _instant(state.get("pending_retired_at"))
            if state.get("pending_ignore_owned") and (
                observed.status == "posted"
                or (coverage is not None and retired_at is not None
                    and coverage.observed_at > retired_at)
            ):
                row.is_ignored = False
                state["pending_ignore_owned"] = False
                state["pending_restored_at"] = (
                    coverage.observed_at if coverage else datetime.now(timezone.utc)
                ).isoformat()
            # Positive presence interrupts a disappearance streak, even when
            # other rows/pages are missing. It never erases the audit history.
            state.pop("pending_missing_since", None)
            state.pop("pending_last_missing_at", None)
            row.sync_metadata = state or None
            continue
        if row.is_ignored or coverage is None or not pending_matches_bank_evidence(row):
            continue
        source_day = _source_date(row)
        if source_day is None or not coverage.start_date <= source_day < coverage.end_date:
            continue
        if coverage.observed_at.date() - source_day < PENDING_MIN_AGE:
            continue
        first_missing = _instant(state.get("pending_missing_since"))
        last_missing = _instant(state.get("pending_last_missing_at"))
        if last_missing is not None and coverage.observed_at <= last_missing:
            continue  # Repeated reads of one cached snapshot are one observation.
        if first_missing is None:
            state["pending_missing_since"] = coverage.observed_at.isoformat()
        elif coverage.observed_at - first_missing >= PENDING_MISSING_GRACE:
            row.is_ignored = True
            state["pending_ignore_owned"] = True
            state["pending_retired_at"] = coverage.observed_at.isoformat()
            state["pending_retirement_reason"] = "absent_from_complete_provider_windows"
        state["pending_last_missing_at"] = coverage.observed_at.isoformat()
        row.sync_metadata = state
