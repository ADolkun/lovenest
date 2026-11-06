"""Synthetic pending lifecycle cases, including real sync orchestration."""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.account import Account
from app.models.transaction import Transaction
from app.providers.base import TransactionCoverage, TransactionData
from app.schemas.transaction import TransactionUpdate
from app.services.connection_service import sync_connection
from app.services.pending_transaction_service import (
    pending_recheck_since, preserve_pending_override, reconcile_pending,
)
from app.services.transaction_service import toggle_ignore_transaction, update_transaction
from tests.test_connection_service import (
    _cc_provider_mock, _make_simplefin_connection, _patch_sync_helpers,
)

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
DAY = date(2026, 9, 20)


def coverage(observed=NOW, start=date(2026, 9, 1), end=date(2026, 11, 1)):
    return TransactionCoverage(start, end, observed)


def incoming(key="hold", amount="100", status="pending", day=DAY):
    epoch = int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp())
    return TransactionData(
        external_id=key, description="Sample merchant", amount=Decimal(amount),
        date=day, type="debit", currency="BRL", status=status,
        raw_data={"id": key, "transacted_at": epoch, "posted": epoch if status == "posted" else 0,
                  "pending": status == "pending", "amount": f"-{amount}"},
    )


@pytest.fixture
async def pending_case(session, test_user, test_workspace):
    conn = await _make_simplefin_connection(session, test_user.id)
    account = Account(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        connection_id=conn.id, external_id="cc-acc-1", name="Credit Card",
        type="credit_card", balance=Decimal("0"), currency="BRL",
    )
    session.add(account)
    data = incoming()
    row = Transaction(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        account_id=account.id, external_id=data.external_id, description="My custom label",
        original_description=data.description, amount=data.amount, date=DAY, type="debit",
        source="sync", status="pending", currency="BRL", raw_data=data.raw_data,
        notes="Keep this annotation", is_ignored=False,
    )
    session.add(row)
    await session.commit()
    return conn, account, row


async def test_missing_requires_distinct_fresh_observations_and_grace(session, pending_case):
    _, account, row = pending_case
    original_raw = dict(row.raw_data)
    for stamp in [NOW, NOW, NOW - timedelta(hours=1), NOW + timedelta(hours=47)]:
        await reconcile_pending(session, account.id, [], coverage(stamp))
        assert not row.is_ignored
    await reconcile_pending(session, account.id, [], coverage(NOW + timedelta(days=2)))
    assert row.is_ignored and row.sync_metadata["pending_ignore_owned"]
    assert row.status == "pending" and row.external_id == "hold"
    assert row.amount == Decimal("100") and row.raw_data == original_raw
    assert row.description == "My custom label" and row.notes == "Keep this annotation"
    # Repeated retirement is idempotent and retains the original retirement date.
    retired = dict(row.sync_metadata)
    await reconcile_pending(session, account.id, [], coverage(NOW + timedelta(days=4)))
    assert row.sync_metadata == retired


@pytest.mark.parametrize("unsafe", ["no_coverage", "outside", "recent", "manual", "already_ignored", "posted", "no_source_date"])
async def test_unsafe_absence_never_retires(session, pending_case, unsafe):
    _, account, row = pending_case
    window = coverage()
    if unsafe == "no_coverage":
        window = None
    if unsafe == "outside":
        window = coverage(start=DAY + timedelta(days=1))
    if unsafe == "recent":
        window = coverage(observed=NOW.replace(day=22, month=9))
    if unsafe == "manual":
        row.source = "manual"
    if unsafe == "already_ignored":
        row.is_ignored = True
    if unsafe == "posted":
        row.status = "posted"
    if unsafe == "no_source_date":
        row.raw_data = {"pending": True}
    for _ in range(3):
        await reconcile_pending(session, account.id, [], window)
    assert row.is_ignored == (unsafe == "already_ignored")
    assert not (row.sync_metadata or {}).get("pending_retired_at")


async def test_present_long_hold_resets_missing_streak_and_is_never_age_expired(session, pending_case):
    _, account, row = pending_case
    await reconcile_pending(session, account.id, [], coverage())
    await reconcile_pending(session, account.id, [incoming()], None)
    assert not row.sync_metadata
    await reconcile_pending(session, account.id, [], coverage(NOW + timedelta(days=10)))
    assert not row.is_ignored
    await reconcile_pending(session, account.id, [incoming()], coverage(NOW + timedelta(days=200)))
    assert not row.is_ignored and not row.sync_metadata


@pytest.mark.parametrize("status", ["pending", "posted"])
async def test_retired_id_can_return_without_duplicate_or_losing_audit(session, pending_case, status):
    _, account, row = pending_case
    await reconcile_pending(session, account.id, [], coverage())
    await reconcile_pending(session, account.id, [], coverage(NOW + timedelta(days=2)))
    await reconcile_pending(session, account.id, [incoming(status=status)],
                            coverage(NOW + timedelta(days=3)))
    assert not row.is_ignored
    assert row.sync_metadata["pending_ignore_owned"] is False
    assert row.sync_metadata["pending_retired_at"]
    assert row.sync_metadata["pending_restored_at"]


async def test_cached_return_cannot_resurrect_retired_hold(session, pending_case):
    _, account, row = pending_case
    await reconcile_pending(session, account.id, [], coverage())
    await reconcile_pending(session, account.id, [], coverage(NOW + timedelta(days=2)))
    await reconcile_pending(session, account.id, [incoming()], coverage())
    assert row.is_ignored
    await reconcile_pending(session, account.id, [incoming(status="posted")], None)
    assert not row.is_ignored  # Exact-ID posted evidence is positive, not absence.


async def test_manual_toggle_takes_ownership_from_sync(session, pending_case, test_workspace):
    _, account, row = pending_case
    await reconcile_pending(session, account.id, [], coverage())
    await reconcile_pending(session, account.id, [], coverage(NOW + timedelta(days=2)))
    await toggle_ignore_transaction(session, row.id, test_workspace.id)
    assert not row.is_ignored
    await reconcile_pending(session, account.id, [], coverage(NOW + timedelta(days=10)))
    assert not row.is_ignored and row.sync_metadata["pending_manual_override"]
    await toggle_ignore_transaction(session, row.id, test_workspace.id)
    await reconcile_pending(session, account.id, [incoming(status="posted")], None)
    assert row.is_ignored


async def test_financial_edit_preserves_manual_intent(session, pending_case, test_user, test_workspace):
    _, account, row = pending_case
    await update_transaction(session, row.id, test_workspace.id, test_user.id,
                             TransactionUpdate(date=DAY + timedelta(days=1)))
    assert row.sync_metadata["pending_manual_override"]
    await reconcile_pending(session, account.id, [], coverage())
    await reconcile_pending(session, account.id, [], coverage(NOW + timedelta(days=3)))
    assert not row.is_ignored


async def test_recheck_rewinds_to_bank_date_with_bounded_history(session, pending_case):
    _, account, row = pending_case
    since = NOW.date() - timedelta(days=14)
    assert await pending_recheck_since(session, account.id, since, now=NOW) == DAY
    row.date = DAY + timedelta(days=100)  # bank date remains the authority
    assert await pending_recheck_since(session, account.id, since, now=NOW) == DAY
    assert await pending_recheck_since(session, account.id, None, now=NOW) is None
    assert await pending_recheck_since(session, account.id, since, now=NOW + timedelta(days=400)) == (NOW + timedelta(days=35)).date()
    preserve_pending_override(row)
    assert await pending_recheck_since(session, account.id, since, now=NOW) == since


async def _sync(session, conn, user, workspace, provider):
    p1, p2, p3 = _patch_sync_helpers()
    with patch("app.services.connection_service.get_provider", return_value=provider), p1, p2, p3:
        await sync_connection(session, conn.id, workspace.id, user.id)


async def test_split_settlements_remain_distinct_then_absent_hold_retires(session, pending_case, test_user, test_workspace):
    conn, account, row = pending_case
    # Two shipments reuse the date-only timestamp. Neither is the whole hold.
    shipments = [incoming("shipment-a", "40", "posted"), incoming("shipment-b", "60", "posted")]
    provider = _cc_provider_mock(bills=[], transactions=shipments)
    provider.transaction_coverage = {"cc-acc-1": coverage()}
    await _sync(session, conn, test_user, test_workspace, provider)
    assert row.status == "pending" and not row.is_ignored
    provider.transaction_coverage = {"cc-acc-1": coverage(NOW + timedelta(days=2))}
    await _sync(session, conn, test_user, test_workspace, provider)
    rows = (await session.scalars(select(Transaction).where(Transaction.account_id == account.id, Transaction.source == "sync"))).all()
    assert len(rows) == 3
    assert row.is_ignored and row.notes == "Keep this annotation"
    assert sum(t.amount for t in rows if not t.is_ignored) == Decimal("100")
    # Repeat feed leaves the audit row and both shipments intact.
    await _sync(session, conn, test_user, test_workspace, provider)
    assert len((await session.scalars(select(Transaction).where(Transaction.account_id == account.id, Transaction.source == "sync"))).all()) == 3


async def test_same_id_posting_restores_retired_row_refreshes_raw_and_keeps_edits(session, pending_case, test_user, test_workspace):
    conn, account, row = pending_case
    await reconcile_pending(session, account.id, [], coverage())
    await reconcile_pending(session, account.id, [], coverage(NOW + timedelta(days=2)))
    data = incoming(amount="105", status="posted", day=DAY + timedelta(days=1))
    provider = _cc_provider_mock(bills=[], transactions=[data])
    provider.transaction_coverage = {}
    await _sync(session, conn, test_user, test_workspace, provider)
    assert row.status == "posted" and row.amount == Decimal("105") and not row.is_ignored
    assert row.raw_data == data.raw_data and row.description == "My custom label"
    # A later posted refresh removes obsolete pending-only evidence.
    row.raw_data = {**row.raw_data, "pending": True, "obsolete": True}
    await session.commit()
    await _sync(session, conn, test_user, test_workspace, provider)
    assert row.raw_data == data.raw_data
    provider.get_transactions.return_value = [incoming()]
    await _sync(session, conn, test_user, test_workspace, provider)
    assert row.status == "posted" and row.raw_data == data.raw_data


async def test_import_pending_off_still_reconciles_existing_holds(session, pending_case, test_user, test_workspace):
    conn, _, row = pending_case
    conn.settings = {"import_pending": False}
    provider = _cc_provider_mock(bills=[], transactions=[incoming()])
    provider.transaction_coverage = {"cc-acc-1": coverage()}
    await _sync(session, conn, test_user, test_workspace, provider)
    assert not row.is_ignored and not row.sync_metadata
    provider.get_transactions.return_value = []
    await _sync(session, conn, test_user, test_workspace, provider)
    provider.transaction_coverage = {"cc-acc-1": coverage(NOW + timedelta(days=2))}
    await _sync(session, conn, test_user, test_workspace, provider)
    assert row.is_ignored


@pytest.mark.parametrize("edit", [
    {"amount_primary": Decimal("123")}, {"fx_rate_used": Decimal("1.23")},
    {"effective_bill_date": date(2026, 10, 25)},
])
async def test_other_financial_edits_take_ownership(session, pending_case, test_user, test_workspace, edit):
    _, account, row = pending_case
    await update_transaction(session, row.id, test_workspace.id, test_user.id, TransactionUpdate(**edit))
    assert row.sync_metadata["pending_manual_override"]
    await reconcile_pending(session, account.id, [], coverage())
    await reconcile_pending(session, account.id, [], coverage(NOW + timedelta(days=3)))
    assert not row.is_ignored


async def test_posted_to_pending_manual_override_survives_sync(session, pending_case, test_user, test_workspace):
    conn, _, row = pending_case
    row.status = "posted"
    await session.commit()
    await update_transaction(session, row.id, test_workspace.id, test_user.id, TransactionUpdate(status="pending"))
    assert row.sync_metadata["pending_manual_override"]
    provider = _cc_provider_mock(bills=[], transactions=[incoming(status="posted")])
    provider.transaction_coverage = {}
    await _sync(session, conn, test_user, test_workspace, provider)
    assert row.status == "pending"


@pytest.mark.parametrize("field,value", [
    ("amount", Decimal("80")), ("date", DAY + timedelta(days=1)),
    ("type", "credit"), ("effective_bill_date", date(2026, 10, 25)),
])
async def test_legacy_financial_changes_are_not_retired(session, pending_case, field, value):
    _, account, row = pending_case
    setattr(row, field, value)
    assert row.sync_metadata is None
    await reconcile_pending(session, account.id, [], coverage())
    await reconcile_pending(session, account.id, [], coverage(NOW + timedelta(days=3)))
    assert not row.is_ignored and not row.sync_metadata


async def test_cascaded_transfer_edit_protects_pending_partner(session, pending_case, test_user, test_workspace):
    _, _, row = pending_case
    row.transfer_pair_id = uuid.uuid4()
    other = Account(id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
                    name="Other", type="checking", currency="BRL", balance=Decimal("0"))
    session.add(other)
    partner = Transaction(id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
                          account_id=other.id, description="Other leg", amount=row.amount, date=DAY,
                          type="credit", currency="BRL", status="posted", source="manual",
                          transfer_pair_id=row.transfer_pair_id)
    session.add(partner)
    await session.commit()
    await update_transaction(session, partner.id, test_workspace.id, test_user.id,
                             TransactionUpdate(amount=Decimal("120")))
    assert row.amount == Decimal("120")
    assert row.sync_metadata["pending_manual_override"]


async def test_complete_but_stale_snapshot_advances_cursor_without_cleanup(session, pending_case, test_user, test_workspace):
    conn, account, hold = pending_case
    previous_sync = datetime.now(timezone.utc) - timedelta(days=10)
    conn.last_sync_at = previous_sync
    for key, pair in [("orphan", None), ("paired", uuid.uuid4())]:
        session.add(Transaction(
            id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
            account_id=account.id, external_id=key, description="Sample merchant",
            original_description="Sample merchant", amount=Decimal("100"), date=DAY,
            type="debit", source="sync", status="posted", currency="BRL",
            transfer_pair_id=pair,
        ))
    await session.commit()
    provider = _cc_provider_mock(bills=[], transactions=[incoming("paired", status="posted")])
    # Structural fetch succeeded, but source timestamp wasn't fresh enough for
    # an absence claim. Do not freeze the cursor or perform either cleanup.
    provider.transaction_coverage = {}
    provider.incomplete_account_ids = set()
    await _sync(session, conn, test_user, test_workspace, provider)
    assert conn.last_sync_at.replace(tzinfo=timezone.utc) > previous_sync
    assert await session.scalar(select(Transaction).where(Transaction.external_id == "orphan"))
    assert not hold.is_ignored and hold.sync_metadata is None


async def test_same_id_pending_adjustment_stays_reconcilable(session, pending_case, test_user, test_workspace):
    conn, account, row = pending_case
    # The bank moves its date and omits the earlier posted key. The old key
    # must not survive raw-data refresh and invalidate future reconciliation.
    row.raw_data = {**row.raw_data, "posted": row.raw_data["transacted_at"]}
    await session.commit()
    adjusted = incoming(amount="95", day=DAY + timedelta(days=1))
    assert adjusted.raw_data is not None
    adjusted.raw_data.pop("posted")
    provider = _cc_provider_mock(bills=[], transactions=[adjusted])
    provider.transaction_coverage = {}
    await _sync(session, conn, test_user, test_workspace, provider)
    assert row.amount == Decimal("100") and row.date == DAY
    assert row.raw_data["posted"] == row.raw_data["transacted_at"]
    assert row.description == "My custom label"
    await reconcile_pending(session, account.id, [], coverage())
    await reconcile_pending(session, account.id, [], coverage(NOW + timedelta(days=2)))
    assert row.is_ignored


async def test_legacy_manual_amount_is_not_overwritten_on_posting(session, pending_case, test_user, test_workspace):
    conn, _, row = pending_case
    row.amount = Decimal("80")
    await session.commit()
    provider = _cc_provider_mock(bills=[], transactions=[incoming(status="posted")])
    provider.transaction_coverage = {}
    await _sync(session, conn, test_user, test_workspace, provider)
    assert row.amount == Decimal("80") and row.status == "pending"


async def test_bulk_split_assignment_protects_pending_ownership(session, pending_case, client, auth_headers, test_user, test_workspace):
    from app.services.transaction_service import bulk_add_to_group
    from tests.test_transactions_bulk_add_to_group import _create_group_with_members

    _, account, row = pending_case
    group, _ = await _create_group_with_members(client, auth_headers, "Me", "Friend")
    result = await bulk_add_to_group(session, test_workspace.id, test_user.id, [row.id], uuid.UUID(group["id"]))
    assert result == {"updated": 1, "skipped": 0}
    assert row.sync_metadata["pending_manual_override"]
    await reconcile_pending(session, account.id, [], coverage())
    await reconcile_pending(session, account.id, [], coverage(NOW + timedelta(days=2)))
    assert not row.is_ignored


@pytest.mark.parametrize("retired", [False, True])
async def test_pending_transfer_sibling_cannot_delete_real_settlement(session, pending_case, test_user, test_workspace, retired):
    conn, account, hold = pending_case
    hold.transfer_pair_id = uuid.uuid4()
    hold.type = "credit"
    hold.raw_data = {**hold.raw_data, "amount": "100"}
    await session.commit()
    if retired:
        await reconcile_pending(session, account.id, [], coverage())
        await reconcile_pending(session, account.id, [], coverage(NOW + timedelta(days=2)))
    posted = incoming("actual-settlement", status="posted")
    posted.type = "credit"
    assert posted.raw_data is not None
    posted.raw_data["amount"] = "100"
    provider = _cc_provider_mock(bills=[], transactions=[posted])
    provider.transaction_coverage = {"cc-acc-1": coverage(NOW + timedelta(days=3))}
    for _ in range(2):
        await _sync(session, conn, test_user, test_workspace, provider)
        settlement = await session.scalar(select(Transaction).where(Transaction.external_id == "actual-settlement"))
        assert settlement is not None and settlement.status == "posted" and not settlement.is_ignored
        assert await session.get(Transaction, hold.id) is not None
