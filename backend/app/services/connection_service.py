import hashlib
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, Optional

from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from app.core.app_clock import app_today
from app.core.config import Settings, get_settings
from app.core.connection_settings import (
    account_status,
    allowlist_ids,
    known_account_ids,
)
from app.models.asset import Asset
from app.models.asset_contribution import AssetContribution
from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.models.asset_value import AssetValue
from app.models.bank_connection import BankConnection
from app.models.account import Account
from app.models.account_reserved_balance import AccountReservedBalance
from app.models.category import Category
from app.models.institution import Institution
from app.models.invoice import Invoice
from app.models.invoice_schedule import InvoiceSchedule
from app.models.goal import Goal
from app.models.collection import collection_asset_groups
from app.models.credit_card_bill import CreditCardBill
from app.models.payee import Payee, PayeeMapping
from app.models.rule import Rule
from app.models.transaction import Transaction
from app.models.transaction_category_line import TransactionCategoryLine
from app.providers.base import TransactionCoverage
from app.services.pending_transaction_service import (
    pending_matches_bank_evidence, pending_recheck_since, reconcile_pending,
)
from app.models.user import User
from app.plugins.provider import is_plugin_provider
from app.providers import get_provider
from app.providers.base import (
    AccountData,
    BankProvider,
    HoldingData,
    HoldingTradeData,
    PartialHoldings,
    ProviderNotConfiguredError,
    ProviderRateLimited,
    ProviderUserActionRequired,
    SessionExpiredError,
    TransactionData,
)
from app.schemas.bank_connection import ProviderAccountRead
from app.services.provider_settings import resolve_settings
from app.services import asset_transaction_service
from app.services import oauth_state
from app.services import admin_service
from app.services import (
    category_line_service,
    reconciliation_service,
    recurring_match_service,
    reimbursement_service,
)
from app.services.text_similarity import normalize_words, token_overlap
from app.services.account_service import (
    LIABILITY_ACCOUNT_TYPES,
    _provider_to_internal_balance,
    keep_balance_as_manual,
    sync_opening_balance_for_connected_account,
)
from app.services.asset_group_service import (
    _unique_default_name,
    ensure_group_for_connection,
)
from app.services.cash_equivalent import CASH_EQUIVALENT_TYPE, is_cash_equivalent_ticker
from app.services.credit_card_service import apply_effective_date
from app.services.rule_engine import merge_notes
from app.services.rule_service import (
    apply_rules_to_transaction,
    preview_rules_for_transaction,
    rule_payee_ids,
)
from app.services.transfer_detection_service import detect_transfer_pairs
from app.services.fx_rate_service import FetchedRateCache, stamp_primary_amount
from app.services.payee_service import get_or_create_payee
from app.services.transaction_match_service import (
    find_unique_transaction_match,
    take_incoming_amount,
)

logger = logging.getLogger(__name__)

LOCAL_IMPORT_SOURCES = {"import", "csv", "ofx", "qif", "camt"}

# Providers whose holdings are crypto by construction: an exchange account and
# a watched wallet address can hold nothing else.
CRYPTO_SOURCES = {"coinbase", "onchain"}

# How far a replayed ledger may sit from the balance a provider reports and
# still count as the same number. Fees settled in kind and eighteen-decimal
# rounding move the last few digits; anything past this is a missing row.
LEDGER_RECONCILE_TOLERANCE = Decimal("0.0001")

settings = get_settings()

_PROVIDER_SELL_DATE_METADATA_KEY = "_securo_provider_sell_date"


def _clean_logo_url(value: object) -> Optional[str]:
    """Normalize a provider-supplied logo to a non-empty string or None.

    Guards the DB column against anything a provider hands back that isn't a
    usable URL (None, empty string, a non-string, or one longer than the
    column — truncating would store a broken URL, so it's dropped), so a
    misbehaving integration can never write junk into a ``logo_url`` column
    or abort a sync with a DataError.
    """
    if isinstance(value, str) and value.strip() and len(value) <= 500:
        return value
    return None


def _clean_institution_name(value: object) -> Optional[str]:
    """Same guard as _clean_logo_url, for the 255-char institution columns."""
    return value[:255] if isinstance(value, str) and value.strip() else None


def _wallet_external_id(connection_external_id: str, account_key: Optional[str]) -> str:
    """The per-account wallet key, squeezed into the 255-char column.

    Truncation alone could collide two accounts; over-long composites keep a
    deterministic digest suffix instead.
    """
    if not account_key:
        return connection_external_id
    key = f"{connection_external_id}::{account_key}"
    if len(key) <= 255:
        return key
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{key[:214]}::{digest[:39]}"


def _is_auto_wallet_name(name: str, institution_name: str) -> bool:
    """True while a wallet still carries its creation-time auto-name — the
    institution label, possibly with _unique_default_name's " N" suffix."""
    if name == institution_name:
        return True
    return bool(re.fullmatch(re.escape(institution_name) + r" \d+", name))


async def _resolve_institution(
    session: AsyncSession,
    connection_id: uuid.UUID,
    cache: dict[str, Institution],
    acc_data: AccountData,
) -> Optional[Institution]:
    """Get-or-create the Institution row an account's provider hint names.

    Matched by the provider's stable org id when it sends one, so a bank
    renamed on the provider side updates its row in place instead of minting
    a new one (review on #654); name identity is the fallback for servers
    that only send a name. Most Pluggy/Enable connections are one
    institution and never send this hint (returns None, serialization falls
    back to the connection's own fields) — the exception is a Pluggy
    connection spanning a banking group's brokerage arm (issue #723), which
    the provider detects and hints the same way SimpleFIN already does.
    """
    name = _clean_institution_name(acc_data.institution_name)
    if not name:
        return None
    ext = _clean_institution_name(acc_data.institution_external_id)
    key = f"id:{ext}" if ext else f"name:{name}"
    if key in cache:
        return cache[key]

    inst: Optional[Institution] = None
    if ext:
        inst = (
            await session.execute(
                select(Institution).where(
                    Institution.connection_id == connection_id,
                    Institution.external_id == ext,
                )
            )
        ).scalar_one_or_none()
        if inst is None:
            # Adopt a row created before the server sent org ids, so the
            # accounts pointing at it don't get orphaned onto a new row.
            inst = (
                await session.execute(
                    select(Institution).where(
                        Institution.connection_id == connection_id,
                        Institution.external_id.is_(None),
                        Institution.name == name,
                    )
                )
            ).scalar_one_or_none()
            if inst is not None:
                inst.external_id = ext
    else:
        # Name-only hint. Match any row with this name — id-keyed rows
        # included, so a payload that intermittently drops org ids doesn't
        # spawn duplicates. NULLS FIRST keeps the pick deterministic.
        inst = (
            await session.execute(
                select(Institution)
                .where(
                    Institution.connection_id == connection_id,
                    Institution.name == name,
                )
                .order_by(Institution.external_id.asc().nulls_first())
                .limit(1)
            )
        ).scalar_one_or_none()

    if inst is None:
        inst = Institution(
            connection_id=connection_id,
            external_id=ext,
            name=name,
            logo_url=_clean_logo_url(acc_data.institution_logo_url),
        )
        try:
            # Savepoint: a concurrent sync (scheduled + manual) can insert
            # the same identity between our SELECT and this flush. The
            # partial unique index rejects the loser — reuse the winner's
            # row instead of failing the whole sync.
            async with session.begin_nested():
                session.add(inst)
                await session.flush()
        except IntegrityError:
            inst = (
                await session.execute(
                    select(Institution)
                    .where(
                        Institution.connection_id == connection_id,
                        Institution.external_id == ext
                        if ext
                        else Institution.name == name,
                    )
                    .limit(1)
                )
            ).scalar_one()
    else:
        if inst.name != name:
            inst.name = name  # provider-side rename
        # The logo is favicon-derived from the org's website, never user-set,
        # so it follows the provider too — a rename that moves domains would
        # otherwise keep the old bank's icon forever.
        new_logo = _clean_logo_url(acc_data.institution_logo_url)
        if new_logo is not None and inst.logo_url != new_logo:
            inst.logo_url = new_logo
    cache[key] = inst
    return inst

PLUGGY_CATEGORY_MAP = {
    "Eating out": "Alimentação",
    "Restaurants": "Alimentação",
    "Food": "Alimentação",
    "Groceries": "Mercado",
    "Supermarkets": "Mercado",
    "Pharmacy": "Saúde",
    "Health": "Saúde",
    "Taxi and ride-hailing": "Transporte",
    "Transport": "Transporte",
    "Gas": "Transporte",
    "Travel": "Transporte",
    "Housing": "Moradia",
    "Rent": "Moradia",
    "Utilities": "Moradia",
    "Entertainment": "Lazer",
    "Leisure": "Lazer",
    "Education": "Educação",
    "Subscriptions": "Assinaturas",
    "Online services": "Assinaturas",
    "Transfer": "Transferências",
    "Transfers": "Transferências",
    "Wire transfers": "Transferências",
}


def _record_reviewed_accounts(connection: BankConnection, known_ids: set[str]) -> None:
    """Pin which provider accounts the user has already had a chance to see.

    An account is pending review when it "appears at the provider after the
    allowlist has been configured" (issue #46), so the comparison set has to be
    frozen at the moment the allowlist is written. Sync's `seen_account_ids`
    cannot serve as that set: it is rewritten on every run, so the first sync
    after a new account appears would absorb it and quietly demote it from
    pending to excluded before the user was ever shown it.
    """
    updated = dict(connection.settings or {})
    reviewed = {str(item) for item in updated.get("seen_account_ids") or []}
    reviewed |= known_ids
    reviewed |= allowlist_ids(updated) or set()
    updated["reviewed_account_ids"] = sorted(reviewed)
    connection.settings = updated


def _record_seen_accounts(
    connection: BankConnection,
    accounts: list[AccountData],
    throttled_ids: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Record every id the provider returned, before any allowlist filtering.

    Lets the account-discovery endpoint tell an account that appeared after the
    allowlist was configured from one the user deliberately unchecked, without
    spending a provider request. An account the provider left out because it
    was throttled stays seen.
    """
    updated = dict(connection.settings or {})
    previous = updated.get("seen_account_ids")
    kept = (
        {i for i in previous if i in throttled_ids}
        if isinstance(previous, list)
        else set()
    )
    updated["seen_account_ids"] = sorted({a.external_id for a in accounts} | kept)
    connection.settings = updated


def _record_balance_observation(
    connection: BankConnection, accounts: list[AccountData], provider: BankProvider,
    *, persisted_account_ids: Optional[set[str]] = None,
    account_types: Optional[dict[str, str]] = None,
    auto_invested_deducted: Optional[dict[str, Decimal]] = None,
) -> set[str]:
    """Only paired account/holdings persistence can heal an incomplete total.

    A snapshot's amount is the balance the account row stores, auto-invested
    deduction included, so the balance explanation can tell it unchanged.
    """
    updated = dict(connection.settings or {})
    unavailable = set(updated.get("unavailable_account_balance_ids") or [])
    unavailable_currencies = set(updated.get("unavailable_account_currency_ids") or [])
    snapshots = dict(updated.get("account_balance_observations") or {})
    allowed = allowlist_ids(connection.settings)
    observation = getattr(provider, "holdings_observation", None)
    account_observation = getattr(provider, "account_holdings_observation", None)
    changed = (
        isinstance(account_observation, dict)
        and isinstance(observation, dict)
        and observation is not account_observation
    )
    # An expired memo may refresh holdings after account persistence. Those
    # two complete observations still cannot establish a residual cash value.
    incomplete = changed or (
        isinstance(observation, dict) and observation.get("complete") is False
    )
    for account in accounts:
        metadata = account.balance_metadata or {}
        previous = snapshots.get(account.external_id) or {}
        recovered_validity = persisted_account_ids is not None and account.balance is not None and metadata.get("value_available") is True and (
            previous.get("value_available") is False
            or (previous.get("currency_available") is False and metadata.get("currency_available") is True)
        )
        if account.balance is None or metadata.get("value_available") is False or incomplete:
            unavailable.add(account.external_id)
        elif recovered_validity:
            unavailable.discard(account.external_id)
        paired = (
            account.balance is not None and not incomplete
            and persisted_account_ids and account.external_id in persisted_account_ids
        )
        if paired:
            if metadata.get("value_available") is not False:
                unavailable.discard(account.external_id)
            if isinstance(account_observation, dict) and account_observation.get("complete") is True:
                metadata = {
                    "observed_at": account_observation.get("observed_at"),
                    "observation_basis": "collection", "coverage": "complete",
                }
        if allowed is not None and account.external_id not in allowed:
            continue
        # Currency validity belongs to the current account read even if a
        # changed amount makes the retained T0 snapshot inapplicable.
        if persisted_account_ids is not None and account.balance is not None:
            if metadata.get("currency_available") is False:
                unavailable_currencies.add(account.external_id)
            elif metadata.get("currency_available") is True:
                unavailable_currencies.discard(account.external_id)
        if paired or recovered_validity or (not previous and metadata):
            # Even the first account-only read carries validity facts. Bind
            # them to the stored sign convention, including user card overrides.
            amount = _provider_to_internal_balance(
                connection.provider, (account_types or {}).get(account.external_id, account.type),
                _account_balance(account, account.balance, auto_invested_deducted or {}),
            ) if account.balance is not None else None
            snapshots[account.external_id] = {
                **metadata, "amount": str(amount.quantize(Decimal("0.01"))) if amount is not None else None,
                "currency": account.currency,
            }
        if not paired and account.external_id in snapshots:
            # Account writes can succeed before a holdings read fails or is
            # disabled. Preserve a retained T0 value/time; T1 cannot verify it.
            snapshots[account.external_id] = {
                **snapshots[account.external_id], "holdings_complete": False,
                "holdings_refresh_status": "unconfirmed",
            }
    if unavailable:
        updated["unavailable_account_balance_ids"] = sorted(unavailable)
    else:
        updated.pop("unavailable_account_balance_ids", None)
    if unavailable_currencies:
        updated["unavailable_account_currency_ids"] = sorted(unavailable_currencies)
    else:
        updated.pop("unavailable_account_currency_ids", None)
    if isinstance(observation, dict):
        updated["holdings_observation"] = dict(observation)
        if changed:
            updated["holdings_observation"]["account_balance_reason"] = "account_observation_changed"
    if snapshots:
        updated["account_balance_observations"] = snapshots
    connection.settings = updated
    return unavailable


def _syncable_accounts(
    connection: BankConnection, accounts: list[AccountData]
) -> tuple[list[AccountData], Optional[set[str]]]:
    """Narrow a provider's account list to what this connection may sync.

    The one place the allowlist is enforced: every entity a sync creates is
    derived from the accounts this returns, so a new entity type can't bypass
    the check by adding its own code path.

    Returns the surviving accounts and their ids, or None for "no allowlist" —
    the signal holdings sync keys off.
    """
    allowlist = allowlist_ids(connection.settings)
    if allowlist is None:
        return accounts, None
    surviving = [a for a in accounts if a.external_id in allowlist]
    return surviving, {a.external_id for a in surviving}


def _sync_assets_enabled(settings: Optional[dict]) -> bool:
    """Return whether provider investment holdings should sync for a connection.

    Missing settings keep the legacy behavior (enabled). Users can opt out per
    connection via Connection settings without disabling account/transaction sync.
    """
    return (settings or {}).get("sync_assets", True) is not False


def _account_balance(
    acc_data: AccountData, balance: Decimal, deducted: dict[str, Decimal]
) -> Decimal:
    """Balance to store for a synced account.

    Some banks auto-apply idle cash to an investment and the provider reports
    that money both inside the account balance and as a holding (issue #843);
    a SimpleFIN brokerage reports its whole portfolio as the balance and the
    positions as holdings (issue #343). The invested portion leaves the
    account balance only for the accounts in ``deducted``, by the amount
    recorded there (see ``_auto_invested_deductions``); every other account
    keeps the full reported balance so the money is never dropped from net
    worth. ``balance`` is ``acc_data.balance`` once the caller has ruled out
    an unknown total.
    """
    amount = deducted.get(acc_data.external_id)
    if not amount or acc_data.auto_invested_balance <= 0:
        return balance
    return balance - amount


@dataclass(frozen=True)
class _HoldingsFetch:
    """One provider holdings read.

    ``holdings`` is None when the provider call failed or did not cover
    holdings this run. ``unreadable`` names the scopes a partial read could
    not answer for (see ``PartialHoldings``).
    """

    holdings: Optional[list[HoldingData]]
    unreadable: tuple[str, ...] = ()


async def _sync_reserved_balances(
    session: AsyncSession, account: Account, acc_data: AccountData
) -> None:
    """Mirror the provider's reserved balances for one account.

    ``None`` from the provider means it did not report the collection, so the
    stored rows are left untouched (an unsupported connector must not wipe
    them). A reported list is authoritative: rows whose ``(external_id,
    currency)`` is absent are deleted, the rest are upserted — renames and
    amount changes update in place, so identity is the provider's
    identification, scoped to the parent account, never the label. Zero-amount
    reservations are stored: Open Finance requires them and "reserved nothing"
    is worth seeing.
    """
    if acc_data.reserved_balances is None or account.id is None:
        return

    existing = (
        await session.execute(
            select(AccountReservedBalance).where(
                AccountReservedBalance.account_id == account.id
            )
        )
    ).scalars().all()
    by_key = {(row.external_id, row.currency): row for row in existing}

    seen: set[tuple[str, str]] = set()
    for item in acc_data.reserved_balances:
        key = (item.external_id, item.currency)
        seen.add(key)
        row = by_key.get(key)
        if row is None:
            row = AccountReservedBalance(
                # The connection owner's workspace, matching the account
                # row, never the caller's (ADR 0002).
                workspace_id=account.workspace_id,
                account_id=account.id,
                external_id=item.external_id,
                name=item.name,
                currency=item.currency,
                amount=item.amount,
            )
            session.add(row)
            by_key[key] = row
        else:
            row.name = item.name
            row.amount = item.amount

    for key, row in by_key.items():
        if key not in seen:
            await session.delete(row)

    await session.flush()


async def _fetch_holdings(
    session: AsyncSession,
    connection: BankConnection,
    credentials: dict,
    provider: Optional[BankProvider] = None,
) -> _HoldingsFetch:
    """Fetch the connection's holdings once per sync.

    Provider-side failures are tolerated (not every connector exposes
    investment data, and a brokerage hiccup must not break the bank-account
    sync), so they are logged and reported as ``holdings=None``.

    Pass the sync's own ``provider``: a connector that memoizes what it
    observed (on-chain) reports it on that instance, and the balance
    observation reads it back from there.
    """
    try:
        provider = provider or get_provider(
            connection.provider, settings=await resolve_settings(session)
        )
        # A plugin's payload may leave holdings out of a run: nothing to sync.
        if getattr(provider, "holdings_fetched", True) is False:
            return _HoldingsFetch(None)
        return _HoldingsFetch(await provider.get_holdings(credentials))
    except PartialHoldings as partial:
        logger.warning(
            "Partial holdings for connection %s: %d scope(s) unreadable (%s)",
            connection.id,
            len(partial.unreadable),
            ", ".join(partial.unreadable),
        )
        return _HoldingsFetch(partial.holdings, tuple(partial.unreadable))
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to fetch holdings for connection %s", connection.id
        )
        return _HoldingsFetch(None)


@dataclass(frozen=True)
class _CountedHolding:
    """A holding that stays counted in net worth after this sync."""

    account_external_id: Optional[str]  # None: not attributable to an account
    value: Decimal
    currency: str


def _provider_sell_date_reverts(asset: Asset) -> bool:
    """Whether a live payload clears this asset's sell date.

    Only a sell date the sync itself set for a provider withdrawal
    (TOTAL_WITHDRAWAL) is reversible; a date the user entered stays.
    """
    if asset.sell_date is None:
        return False
    metadata = asset.external_metadata or {}
    return (
        str(metadata.get("status") or "").upper() == "TOTAL_WITHDRAWAL"
        and metadata.get(_PROVIDER_SELL_DATE_METADATA_KEY)
        == asset.sell_date.isoformat()
    )


def _stays_counted(
    existing: Optional[Asset], connection_id: uuid.UUID, *, excluded: bool = False
) -> bool:
    """Whether a live holding leaves its asset counted after ``_sync_holdings``.

    Mirrors what the assets sync keeps: a user-set sell date survives, and
    an archived asset is only unarchived when the holding moved to another
    connection. A holding the allowlist ``excluded``, or whose asset another
    live connection owns, is skipped by the assets sync: it creates nothing,
    and an existing asset stays exactly as stored.
    """
    if existing is None:
        return not excluded
    if excluded or existing.connection_id not in (None, connection_id):
        return not existing.is_archived and existing.sell_date is None
    if existing.sell_date is not None and not _provider_sell_date_reverts(existing):
        return False
    return not (existing.is_archived and existing.connection_id == connection_id)


async def _counted_holdings(
    session: AsyncSession,
    connection: BankConnection,
    holdings: Optional[list[HoldingData]],
    synced_account_ids: Optional[set[str]] = None,
) -> list[_CountedHolding]:
    """Holdings this sync leaves counted in net worth, with their owners.

    A fetched payload decides on its own: withdrawn positions are not
    counted, and neither are positions whose stored asset the user sold or
    archived, since the assets sync keeps that state. A holding outside
    ``synced_account_ids`` (the allowlist; unattributed holdings always fall
    outside it) only counts through an asset already stored. Without a payload
    (asset sync disabled, or the fetch failed) the assets sync does not run
    and whatever it stored last stays in place, so the answer comes from
    the database: every live (not archived, not sold) asset this
    connection's holdings sync owns, at its latest stored value. Its owner
    is the attribution stored on the asset and nothing else, because that is
    all net worth reads (holding_inside_account_balance): an asset filed in a
    per-account wallet before migration 073 stored attributions, and not
    re-synced since, is counted beside its account, so it is unattributed
    here too.
    """
    if holdings is not None:
        live = [h for h in holdings if not h.is_withdrawn]
        if not live:
            return []
        existing_by_external = {
            asset.external_id: asset
            for asset in (
                await session.scalars(
                    select(Asset).where(
                        Asset.workspace_id == connection.workspace_id,
                        Asset.source == connection.provider,
                        Asset.external_id.in_([h.external_id for h in live]),
                    )
                )
            ).all()
        }
        counted: list[_CountedHolding] = []
        for h in live:
            existing = existing_by_external.get(h.external_id)
            excluded = (
                synced_account_ids is not None
                and h.account_external_id not in synced_account_ids
            )
            if not _stays_counted(existing, connection.id, excluded=excluded):
                continue
            # A payload that names no account keeps the attribution already
            # stored (see _upsert_asset_from_holding).
            owner = h.account_external_id or (
                existing.account_external_id if existing is not None else None
            )
            counted.append(_CountedHolding(owner, h.current_value, h.currency))
        return counted

    latest_value = (
        select(AssetValue.amount)
        .where(AssetValue.asset_id == Asset.id)
        .order_by(AssetValue.date.desc())
        .limit(1)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            select(Asset.account_external_id, latest_value, Asset.currency).where(
                Asset.workspace_id == connection.workspace_id,
                Asset.source == connection.provider,
                Asset.connection_id == connection.id,
                Asset.external_id.is_not(None),
                Asset.is_archived.is_(False),
                Asset.sell_date.is_(None),
            )
        )
    ).all()
    return [
        _CountedHolding(
            owner, Decimal(value) if value is not None else Decimal(0), currency
        )
        for owner, value, currency in rows
    ]


# How far an unattributed holding's value may sit from an account's
# auto-invested balance and still count as the same money: the larger of
# 1% and 1.00 in the account currency. The two figures come from different
# provider endpoints and can drift by a day's yield.
_AUTO_INVEST_MATCH_RELATIVE = Decimal("0.01")
_AUTO_INVEST_MATCH_ABSOLUTE = Decimal("1")


def _mirrors_auto_invested(holding: _CountedHolding, acc: AccountData) -> bool:
    """Whether an unattributed holding looks like this account's auto-invested money."""
    if holding.currency != acc.currency:
        return False
    tolerance = max(
        acc.auto_invested_balance * _AUTO_INVEST_MATCH_RELATIVE,
        _AUTO_INVEST_MATCH_ABSOLUTE,
    )
    return abs(holding.value - acc.auto_invested_balance) <= tolerance


def _auto_invested_deductions(
    accounts_data: list[AccountData], counted: list[_CountedHolding]
) -> dict[str, Decimal]:
    """Amount deducted from each account's balance, by account external id.

    The deduction is per account: an account loses its auto-invested portion
    only when a counted holding is attributed to that same account, never
    because some other account of the connection has one.

    With attributed holdings the amount is what stays counted for the
    account (in its currency), capped at the reported portion. A holding the
    user archived or sold is not counted, so its share stays in the balance
    instead of vanishing from net worth; and the cash residual plus the
    counted holdings add up to exactly the balance the provider reported.

    Only positive, finite values are summed, the same rule the providers
    apply to the reported portion. A negative position (a short, a written
    option) then stays inside the cash residual, which is where the net
    worth report counts it: the report leaves non-positive assets out.

    Holdings without an owning account (Pluggy's investments payload carries
    the item but no account id, so this is the normal case there) cannot say
    which account they came from, or whether they are the auto-invested
    money at all (the same item can hold unrelated investments). Deducting
    on a guess would understate net worth, so an unattributed holding only
    allows the deduction when both hold:

    - exactly one account of the connection reports an auto-invested
      balance, so it is the only account the holding can mirror;
    - the holding's value matches that balance, in the same currency,
      within the tolerance above (in the case of issue #843 the two figures
      were identical).

    Otherwise the account keeps the full reported balance, which can count
    the money twice but never drops it.
    """
    candidates = [acc for acc in accounts_data if acc.auto_invested_balance > 0]
    deducted: dict[str, Decimal] = {}
    for acc in candidates:
        attributed = sum(
            (
                h.value
                for h in counted
                if h.account_external_id == acc.external_id
                and h.currency == acc.currency
                and h.value.is_finite()
                and h.value > 0
            ),
            Decimal(0),
        )
        if attributed > 0:
            deducted[acc.external_id] = min(attributed, acc.auto_invested_balance)
    if len(candidates) == 1:
        only = candidates[0]
        if only.external_id not in deducted and any(
            h.account_external_id is None and _mirrors_auto_invested(h, only)
            for h in counted
        ):
            deducted[only.external_id] = only.auto_invested_balance
    return deducted


async def _resolve_auto_invested_deductions(
    session: AsyncSession,
    connection: BankConnection,
    holdings: Optional[list[HoldingData]],
    accounts_data: list[AccountData],
    synced_account_ids: Optional[set[str]] = None,
) -> dict[str, Decimal]:
    """Amount each account's stored balance loses to counted holdings.

    ``accounts_data`` is every account the provider reports, allowlisted or
    not: an excluded account's auto-invested money still competes for an
    unattributed holding.

    Skips the database lookup when no account reports an auto-invested
    portion, which is every connection but the affected ones.
    """
    if not any(acc.auto_invested_balance > 0 for acc in accounts_data):
        return {}
    counted = await _counted_holdings(
        session, connection, holdings, synced_account_ids
    )
    # A holding attributed to an account is carried inside that account's
    # balance and left out of net worth (holding_inside_account_balance), so
    # deducting for it would drop the money. Only an unattributed holding is
    # counted beside the account and can count it twice.
    unattributed = [h for h in counted if h.account_external_id is None]
    return _auto_invested_deductions(accounts_data, unattributed)


async def _sync_holdings(
    session: AsyncSession,
    user_id: uuid.UUID,
    connection: BankConnection,
    credentials: dict,
    synced_account_ids: Optional[set[str]] = None,
    *,
    provider: Optional[BankProvider] = None,
    fetched: Optional[_HoldingsFetch] = None,
) -> bool:
    """Fetch investment holdings from the provider and upsert them as Assets.

    ``fetched`` lets a caller pass the read it already made with
    ``_fetch_holdings``; when omitted the provider is called here.

    Each holding becomes one Asset (type="investment") keyed by
    (workspace_id, source, external_id). Every sync appends an AssetValue row
    dated today; if a row for today already exists (same day re-sync) it
    is updated in place rather than creating a duplicate.

    Holdings that disappear from the provider response (e.g. fully
    redeemed fixed income) get archived rather than deleted so the user
    keeps their value history.

    ``synced_account_ids`` is the provider account set this sync survived the
    allowlist with, or None when no allowlist is configured (legacy: everything
    syncs).

    Failures here are swallowed: not all Pluggy connectors expose
    investment data, and we don't want a brokerage hiccup to break the
    bank-account sync that just succeeded.

    Return whether holdings persistence finished, so an account-only read
    cannot validate a total against retained holdings from an older read.
    """
    from app.services.owned_transfer_service import lock_workspace
    await lock_workspace(session, connection.workspace_id)
    # A cycle receipt, not a provider as-of time. Missing/failed/excluded reads
    # leave older holding receipts behind, even when cached units are retained.
    quantity_collected_at = datetime.now(timezone.utc).isoformat()
    connection.settings = {**(connection.settings or {}),
                           "holdings_quantity_attempt_at": quantity_collected_at}
    if fetched is None:
        fetched = await _fetch_holdings(session, connection, credentials, provider)
    if fetched.holdings is None:
        return False
    holdings = fetched.holdings
    # Scopes the provider read nothing for this run. Their assets are held
    # back from the archive sweep below — silence about a position is not a
    # report that it closed.
    unreadable = fetched.unreadable

    source = connection.provider
    today = app_today()

    # Deny by default: with an allowlist configured, a holding the provider
    # can't attribute to an account (Pluggy's item-level investments carry no
    # account id) cannot be shown to belong to a synced account, so it does not
    # sync. Excluding is not deleting, though — the provider still reports these
    # positions, so they are pre-seeded into `seen` below to keep the archive
    # sweep off assets imported before the account was excluded.
    excluded_external_ids: set[str] = set()
    if synced_account_ids is not None:
        excluded_external_ids = {
            h.external_id
            for h in holdings
            if h.account_external_id not in synced_account_ids
        }
        holdings = [
            h for h in holdings if h.account_external_id in synced_account_ids
        ]

    # Find-or-create the wallet(s) that own this connection's holdings. A
    # holding carrying its owning account (SimpleFIN — issue #345) gets one
    # wallet per investment account, named after it; the rest share the
    # connection-named wallet. Users can rename wallets freely without
    # breaking future syncs (matching is by external_id).
    groups_by_key: dict[Optional[str], AssetGroup] = {}
    holding_ids = {h.external_id for h in holdings if h.external_id}
    # How many accounts this payload spreads across. A wallet being split
    # between several of them cannot state any one account's tax character.
    payload_account_keys = {
        h.account_external_id for h in holdings if h.account_external_id
    }
    # Snapshots before any bucket runs, so payload order can't change them.
    # Once per-account wallets exist under the current connection key, the
    # plain-keyed wallet is the live connection-default, not a legacy row
    # awaiting its first split — adopting it would hijack it. A keyed
    # bucket applies the stricter test: any split wallet in scope (old
    # prefixes included) means past the legacy era, so a genuinely new
    # account must mint even right after a reconnect rotated the prefix —
    # only the keyless bucket may still adopt the stale default then.
    has_split_wallets = bool(
        await session.scalar(
            select(
                exists().where(
                    AssetGroup.user_id == user_id,
                    AssetGroup.workspace_id == connection.workspace_id,
                    AssetGroup.source == source,
                    AssetGroup.external_id.startswith(
                        f"{connection.external_id}::", autoescape=True
                    ),
                )
            )
        )
    )
    has_any_split_wallets = has_split_wallets or bool(
        await session.scalar(
            select(
                exists().where(
                    AssetGroup.user_id == user_id,
                    AssetGroup.workspace_id == connection.workspace_id,
                    AssetGroup.source == source,
                    or_(
                        AssetGroup.connection_id == connection.id,
                        AssetGroup.connection_id.is_(None),
                    ),
                    AssetGroup.external_id.contains("::"),
                )
            )
        )
    )

    async def _holds_synced_asset(group_id: uuid.UUID) -> bool:
        """Does this wallet already hold an asset this payload re-syncs?"""
        if not holding_ids:
            return False
        return bool(
            await session.scalar(
                select(
                    exists().where(
                        Asset.group_id == group_id,
                        Asset.external_id.in_(holding_ids),
                    )
                )
            )
        )

    async def _wallet_for(holding: HoldingData) -> AssetGroup:
        key = holding.account_external_id
        if key not in groups_by_key:
            # The owning account's institution backs the wallet's
            # "Synced from …" subtitle (issue #345).
            account = (
                await session.scalar(
                    select(Account).where(
                        Account.connection_id == connection.id,
                        Account.external_id == key,
                    )
                )
                if key
                else None
            )
            institution_id = account.institution_id if account else None
            wallet_key = _wallet_external_id(connection.external_id, key)
            default_name = (
                _clean_institution_name(holding.account_name)
                or (account.name if account else None)
                or connection.institution_name
            )
            # The first claim on a wallet key adopts the wallet these
            # holdings lived in before — the legacy connection-keyed one, or
            # one left on a stale key by a reconnect (plain or per-account:
            # a "…::{account}" suffix match is this very account's wallet
            # under an older connection key) — instead of minting a new
            # row, so an existing user's rename/icon/color/position
            # survive. Only an untouched auto-name is refreshed. Guards, in
            # order: never adopt when a wallet already owns the key
            # (re-keying a twin next to it would trip the unique
            # (workspace, user, source, external_id) index and fail every sync); never
            # adopt a wallet another bucket of this run claimed; never
            # reach into another workspace; a plain candidate only counts
            # before the first split — afterwards it is the live
            # connection-default wallet, not a legacy awaiting adoption; a
            # stale-keyed candidate must be provably this bank's — still
            # linked to this connection, or an orphan holding assets this
            # payload re-syncs (a deleted sibling connection's orphan is
            # neither); ambiguity means minting, not guessing.
            existing = await session.scalar(
                select(AssetGroup.id).where(
                    AssetGroup.user_id == user_id,
                    AssetGroup.workspace_id == connection.workspace_id,
                    AssetGroup.source == source,
                    AssetGroup.external_id == wallet_key,
                )
            )
            legacy: Optional[AssetGroup] = None
            per_account_ids: set[uuid.UUID] = set()
            if existing is None:
                claimed = {g.id for g in groups_by_key.values()}
                key_shape = ~AssetGroup.external_id.contains("::")
                if key:
                    key_shape = or_(
                        key_shape,
                        AssetGroup.external_id.endswith(
                            f"::{key}", autoescape=True
                        ),
                    )
                candidates = [
                    g
                    for g in (
                        await session.execute(
                            select(AssetGroup).where(
                                AssetGroup.user_id == user_id,
                                AssetGroup.workspace_id == connection.workspace_id,
                                AssetGroup.source == source,
                                or_(
                                    AssetGroup.connection_id == connection.id,
                                    AssetGroup.connection_id.is_(None),
                                ),
                                AssetGroup.external_id.isnot(None),
                                # Wallets keyed under the live prefix belong
                                # to their own accounts, never to this one.
                                ~AssetGroup.external_id.startswith(
                                    f"{connection.external_id}::",
                                    autoescape=True,
                                ),
                                key_shape,
                            )
                        )
                    ).scalars().all()
                    if g.id not in claimed
                ]
                # A stale key parses as exactly "{old prefix}::{key}"; a
                # leftover "::" in the remainder means the tail straddles
                # another account's key (ids may themselves contain "::").
                tail = f"::{key}" if key else None
                suffixed = [
                    g
                    for g in candidates
                    if tail
                    and g.external_id
                    and g.external_id.endswith(tail)
                    and g.external_id[: -len(tail)]
                    and "::" not in g.external_id[: -len(tail)]
                ]
                plain = [
                    g
                    for g in candidates
                    if g.external_id and "::" not in g.external_id
                ]
                # A bare key that *is* this account's id can only ever be this
                # account's wallet — the fork wrote exactly that shape, and a
                # connection-level wallet carries the connection's id instead.
                # It skips the gate below because migration 087 cannot re-key a
                # wallet orphaned by a disconnect (no connection left to build
                # the key from), while re-keying that orphan's live siblings is
                # itself what flips the gate. Gated, the orphan would strand
                # with the user's tax_treatment on it and a fresh `taxable`
                # wallet would take its holdings.
                exact_bare = [g for g in candidates if key and g.external_id == key]
                # Both pools above name one account: a suffixed key carries it,
                # and a bare key that equals it is the fork's own per-account
                # shape. Anything adopted outside them held the whole
                # connection, which is what the tax_treatment reset below turns
                # on.
                per_account_ids = {g.id for g in suffixed} | {g.id for g in exact_bare}
                pool = suffixed or exact_bare or (
                    []
                    if (has_any_split_wallets if key else has_split_wallets)
                    else plain
                )
                exact = [g for g in pool if key and g.external_id == key]
                if not exact:
                    exact = [
                        g for g in pool if g.external_id == connection.external_id
                    ]
                if exact:
                    legacy = exact[0]
                else:
                    ours = [g for g in pool if g.connection_id == connection.id]
                    if not ours:
                        ours = [
                            g for g in pool if await _holds_synced_asset(g.id)
                        ]
                    if len(ours) == 1:
                        legacy = ours[0]
            if legacy is not None:
                # A concurrent sync can win the key between the guard above
                # and this flush; fall through to the mint path, which
                # re-selects the winner's row.
                try:
                    async with session.begin_nested():
                        # A plainly-keyed wallet held every account's holdings
                        # at once, so the tax character on it was stated for the
                        # whole connection, not for the one account adopting it
                        # here — and which account that is comes down to payload
                        # order. Hand it back the default instead of letting one
                        # account inherit a setting made for several (#76): the
                        # user re-states it per wallet, and the direction that
                        # errs keeps gains reportable rather than hiding them.
                        if (
                            legacy.id not in per_account_ids
                            and len(payload_account_keys) > 1
                        ):
                            legacy.tax_treatment = "taxable"
                        legacy.external_id = wallet_key
                        legacy.connection_id = connection.id
                        if _is_auto_wallet_name(
                            legacy.name, connection.institution_name
                        ):
                            legacy.name = await _unique_default_name(
                                session,
                                connection.workspace_id,
                                default_name[:95],
                                exclude_group_id=legacy.id,
                            )
                        if institution_id is not None:
                            legacy.institution_id = institution_id
                        await session.flush()
                except IntegrityError:
                    await session.refresh(legacy)
                else:
                    groups_by_key[key] = legacy
                    return legacy
            groups_by_key[key] = await ensure_group_for_connection(
                session,
                user_id=user_id,
                connection_id=connection.id,
                source=source,
                external_id=wallet_key,
                default_name=default_name,
                institution_id=institution_id,
                workspace_id=connection.workspace_id,
            )
        return groups_by_key[key]

    # Wallets this sync owns: this connection's, plus orphans a prior
    # disconnect left behind (connection_id went NULL via SET NULL) — their
    # re-adopted assets must still split into per-account wallets instead of
    # staying stranded. An asset sitting in one may be re-attributed below;
    # a user's custom wallet (source "manual") and other workspaces' wallets
    # are never touched.
    sync_owned_rows = await session.execute(
        select(AssetGroup.id, AssetGroup.external_id).where(
            AssetGroup.user_id == user_id,
            AssetGroup.workspace_id == connection.workspace_id,
            AssetGroup.source == source,
            or_(
                AssetGroup.connection_id == connection.id,
                AssetGroup.connection_id.is_(None),
            ),
        )
    )
    sync_owned = sync_owned_rows.all()
    sync_owned_group_ids = {row[0] for row in sync_owned}
    # Per-account wallets, by their "…::…" keys. A holding that lost its
    # account hint must not drain one of these into the default bucket — a
    # single degraded payload would empty them and the reap would delete
    # the user's customization with them.
    split_group_ids = {row[0] for row in sync_owned if row[1] and "::" in row[1]}

    # Also pull orphans (connection_id IS NULL) with the same source —
    # those are assets archived by a prior disconnect. Re-matching on
    # external_id lets users re-link their investment history when they
    # re-add a connection without creating duplicate rows. Confined to the
    # connection's workspace: the same user can hold the same provider
    # external_id in another workspace, and adoption must not cross over.
    existing_rows = await session.execute(
        select(Asset).where(
            Asset.workspace_id == connection.workspace_id,
            Asset.source == source,
        )
    )
    existing_assets = list(existing_rows.scalars().all())
    existing_by_external: dict[str, Asset] = {
        asset.external_id: asset for asset in existing_assets if asset.external_id
    }
    archive_candidates: dict[str, Asset] = {
        asset.external_id: asset
        for asset in existing_assets
        if asset.external_id
        and (asset.connection_id == connection.id or asset.connection_id is None)
    }
    seen: set[str] = set(excluded_external_ids) | {
        ext_id
        for ext_id in archive_candidates
        if any(ext_id == scope or ext_id.startswith(f"{scope}:") for scope in unreadable)
    }
    # Every asset this sync wrote, for the holdings' trades below.
    synced_assets: dict[str, Asset] = {}

    for holding in holdings:
        seen.add(holding.external_id)
        existing = existing_by_external.get(holding.external_id)
        identity_verified = False

        if existing is None and not holding.is_withdrawn:
            from app.services.investment_evidence_service import evidence_holding_for_sync
            mapped_group = await _wallet_for(holding)
            existing, ambiguous = await evidence_holding_for_sync(session, connection, holding, mapped_group.id)
            identity_verified = existing is not None
            if ambiguous:
                # Retain the pending API evidence below instead of adding a
                # second snapshot beside a reviewed but unverified holding.
                logger.info("Holding identity needs source review in wallet %s", mapped_group.id)
                continue

        # A non-null connection owns the row until it is disconnected (the
        # FK then SET NULLs it). Provider ids are not guaranteed unique across
        # two live connections, so letting the latest sync adopt the row would
        # make its owner and connection flap while its wallet stays put.
        if (
            existing is not None
            and existing.connection_id is not None
            and existing.connection_id != connection.id
        ):
            if not holding.is_withdrawn:
                # Retain a destination for this connection's evidence while
                # leaving the other connection's holding and wallet alone.
                await _wallet_for(holding)
            logger.warning(
                "Skipping asset %s already owned by connection %s while syncing %s",
                existing.id,
                existing.connection_id,
                connection.id,
            )
            continue

        # Provider-reported closure (Pluggy TOTAL_WITHDRAWAL). Two cases:
        #   - New + withdrawn: skip entirely. A dead zero-balance asset
        #     with no history is noise; the user never saw this position
        #     while it was active, no reason to surface it closed.
        #   - Existing + withdrawn: mark sell_date (if not already set by
        #     the user) so it drops out of current totals but historical
        #     AssetValues remain visible in reports. No new AssetValue —
        #     appending today's zero would bury the real closing value.
        # A position opened and closed between two syncs still has a history
        # when its trades come along: it's imported, and its ledger sells it.
        closed_with_history = (
            holding.is_withdrawn
            and existing is None
            and bool(holding.trades)
            and _reconciled_trades(holding, list(holding.trades or [])) is not None
        )
        if holding.is_withdrawn and not closed_with_history:
            if existing is None:
                continue
            provider_sell_date: Optional[str] = None
            if existing.sell_date is None:
                existing.sell_date = today
                provider_sell_date = today.isoformat()
            else:
                # Preserve provenance across repeated withdrawn payloads, but
                # deliberately drop it once the user changes the date.
                previous_marker = (existing.external_metadata or {}).get(
                    _PROVIDER_SELL_DATE_METADATA_KEY
                )
                if previous_marker == existing.sell_date.isoformat():
                    provider_sell_date = previous_marker
            # Keep descriptive fields fresh in case the provider still
            # updates them post-closure, but don't touch valuation.
            existing.name = holding.name
            withdrawn_metadata = _holding_snapshot_metadata(
                existing, holding, connection.id, source, connection.workspace_id,
                quantity_collected_at,
            )
            if provider_sell_date is not None:
                withdrawn_metadata[_PROVIDER_SELL_DATE_METADATA_KEY] = provider_sell_date
            existing.external_metadata = withdrawn_metadata or None
            existing.connection_id = connection.id
            synced_assets[holding.external_id] = existing
            continue

        # A provider-reported closure is reversible. The prior raw status is
        # already persisted in external_metadata, so no separate schema field
        # is needed to distinguish TOTAL_WITHDRAWAL -> ACTIVE from a manually
        # entered sell date. Manual dates remain authoritative when there was
        # no provider withdrawal transition.
        if existing is not None and _provider_sell_date_reverts(existing):
            existing.sell_date = None

        asset = await _upsert_asset_from_holding(
            session, existing, holding, user_id, connection.id, source,
            workspace_id=connection.workspace_id,
            quantity_collected_at=quantity_collected_at,
            identity_verified=identity_verified,
        )
        synced_assets[holding.external_id] = asset
        if asset.group_id is not None:
            existing_group = await session.get(AssetGroup, asset.group_id)
            if (
                existing_group is not None
                and existing_group.workspace_id == connection.workspace_id
                and existing_group.source == source
                and (
                    existing_group.connection_id == connection.id
                    or existing_group.connection_id is None
                )
            ):
                existing_group.user_id = user_id
                sync_owned_group_ids.add(existing_group.id)
                if existing_group.external_id and "::" in existing_group.external_id:
                    split_group_ids.add(existing_group.id)
        # Attach to its institution's wallet. NOTE this deliberately moves
        # holdings between sync-owned wallets, not just out of a null group
        # like it used to: re-attribution corrects the sync's own earlier
        # bucketing (one-wallet-per-connection → per-account). A wallet the
        # user made themselves ("US Stocks", source "manual") is never
        # touched.
        group = await _wallet_for(holding)
        hint_lost = (
            holding.account_external_id is None
            and asset.group_id in split_group_ids
        )
        if (
            not hint_lost
            and (asset.group_id is None or asset.group_id in sync_owned_group_ids)
            and asset.group_id != group.id
        ):
            asset.group_id = group.id
        # Seed a historical value at purchase_date so users get a real
        # evolution curve from day one — not just today's snapshot.
        # Idempotent: skips if any AssetValue already exists at that date.
        if holding.purchase_date and holding.purchase_price is not None:
            await _ensure_historical_seed(
                session, asset, holding.purchase_date, holding.purchase_price
            )
        # Respect a user-set sell_date: if they've marked the asset as
        # sold we stop recording new values even when the provider still
        # reports the position. Historical values stay; current totals
        # already exclude it via the sell_date filter in rollups.
        if asset.sell_date is None:
            await _upsert_asset_value_for_today(session, asset, holding.current_value, today)

    for ext_id, asset in archive_candidates.items():
        if ext_id not in seen and not asset.is_archived:
            asset.is_archived = True

    await _sync_holding_trades(session, source, holdings, synced_assets)

    # Sync owns its wallets: drop any it emptied by re-attribution above
    # (e.g. the single connection-named wallet that predates per-institution
    # ones). Wallets still holding assets — or used this run — are kept, and
    # so is anything a goal tracks, a collection contains, or a contribution
    # was recorded against: deleting those would SET NULL the goal's target and
    # CASCADE the membership and the contribution history away, silently
    # breaking things the user built on the wallet. A wallet whose tax
    # character the user set is kept for the same reason — no provider reports
    # it, so a deleted row comes back `taxable` and quietly makes shielded
    # gains reportable.
    if holdings:
        await session.flush()
        used_ids = {g.id for g in groups_by_key.values()}
        for gid in sync_owned_group_ids - used_ids:
            has_assets = await session.scalar(
                select(func.count()).select_from(Asset).where(Asset.group_id == gid)
            )
            if has_assets:
                continue
            referenced = await session.scalar(
                select(
                    exists().where(Goal.asset_group_id == gid)
                    | exists().where(AssetContribution.group_id == gid)
                    | exists()
                    .select_from(collection_asset_groups)
                    .where(collection_asset_groups.c.asset_group_id == gid)
                )
            )
            if referenced:
                continue
            emptied = await session.get(AssetGroup, gid)
            # Orphans of *other* connections ride along in sync_owned so their
            # assets can be re-filed, but they are not this sync's to delete —
            # a connection removed long ago can leave a wallet the user keeps
            # on purpose, and this sync knows nothing about it.
            if emptied is None or emptied.connection_id != connection.id:
                continue
            if emptied.tax_treatment != "taxable":
                continue
            await session.delete(emptied)
    return True


async def _sync_trades(
    session: AsyncSession,
    connection: BankConnection,
    credentials: dict,
    synced_account_ids: Optional[set[str]] = None,
    *,
    provider: Optional[BankProvider] = None,
) -> None:
    """Append the provider's trade history to its holdings' ledgers.

    This is the first automatic writer to the trade ledger — every other one
    records something a person typed or uploaded — so re-running it has to be
    free. Deduplication keys on the provider's own transaction id, which is
    the only identifier stable across two walks of the same history: a second
    sync matches every row it already wrote and adds nothing.

    Runs after ``_sync_holdings``, and depends on it: the Assets it writes to
    are the ones that pass created or adopted, and it reads the quantity that
    pass recorded to decide whether the ledger is complete enough to be
    believed.

    ``synced_account_ids`` is the provider account set that survived the
    allowlist, or None when there is no allowlist. Deny by default, matching
    the holdings pass: an excluded account stops syncing, and a ledger row is
    a sync.

    Runs even when the provider reports no trades at all, because the
    reconciling recompute below has to happen on every sync, not only the ones
    that appended something.

    Provider failures are swallowed, matching ``_sync_holdings``: a history
    endpoint that errors should cost the user a cost basis, not the balances
    that synced fine a moment ago.
    """
    from app.services.owned_transfer_service import lock_workspace
    await lock_workspace(session, connection.workspace_id)
    try:
        provider = provider or get_provider(
            connection.provider, settings=await resolve_settings(session)
        )
        activity = await provider.get_investment_activity(credentials)
        trades = activity.trades
    except Exception:  # noqa: BLE001
        logger.exception("Failed to fetch trades for connection %s", connection.id)
        return

    rows = await session.execute(
        select(Asset).where(
            Asset.connection_id == connection.id,
            Asset.source == connection.provider,
            Asset.is_archived.is_(False),
        )
    )
    holdings: dict[str, Asset] = {
        a.external_id: a
        for a in rows.scalars().all()
        if a.external_id
        and (synced_account_ids is None or a.account_external_id in synced_account_ids)
    }
    if not holdings and not activity.observations:
        return

    by_id: dict[uuid.UUID, Asset] = {a.id: a for a in holdings.values()}
    existing = await session.execute(
        select(AssetTransaction.asset_id, AssetTransaction.external_id).where(
            AssetTransaction.asset_id.in_(by_id)
        )
    )
    # `written` is what a re-sync deduplicates against; `ledgered` is every
    # holding whose figures are the ledger's to state, which is a wider set —
    # it includes rows the user typed or imported themselves.
    written: set[tuple[uuid.UUID, str]] = set()
    ledgered: set[uuid.UUID] = set()
    for asset_id, external_id in existing.all():
        ledgered.add(asset_id)
        if external_id:
            written.add((asset_id, external_id))

    touched: dict[uuid.UUID, Asset] = {}
    if activity.observations:
        from app.services.investment_evidence_service import sync_evidence
        touched = await sync_evidence(
            session, connection, activity.observations, trades, holdings, synced_account_ids,
        )
        # The evidence-aware writer owns all proposals from this provider
        # read, including blocked ones. They must not fall through below.
        trades = []
    # Oldest first. `_recompute` replays by `(date, created_at)`, and rows
    # flushed together share a server-side `created_at` to the microsecond, so
    # left alone the tiebreak falls through to insertion order — which for a
    # provider that lists newest-first would replay a same-day buy and sell
    # backwards and book no gain at all.
    for trade in sorted(trades, key=lambda t: t.occurred_at):
        asset = holdings.get(trade.holding_external_id)
        # No holding means the account allowlist excluded this position, or
        # the provider reported history for one it no longer lists. Either way
        # there is no ledger to write to, and minting an Asset here would
        # resurrect what the holdings pass deliberately left out.
        if asset is None:
            continue
        key = (asset.id, trade.external_id)
        # Also catches a payload that repeats an id within one walk, which a
        # cursor that overlaps pages will do.
        if key in written:
            continue
        written.add(key)
        session.add(
            AssetTransaction(
                asset_id=asset.id,
                workspace_id=asset.workspace_id,
                kind=trade.kind,
                quantity=trade.quantity,
                price=trade.price,
                date=trade.occurred_at.date(),
                # The instant the trade happened, not the instant this row was
                # written: it is what orders two trades of one asset on one day.
                created_at=trade.occurred_at,
                source=connection.provider,
                external_id=trade.external_id,
                notes=trade.notes,
            )
        )
        touched[asset.id] = asset

    ledgered |= set(touched)
    if not ledgered:
        return
    await session.flush()
    # `_sync_holdings` ran a moment ago and wrote the provider's reported
    # balance onto `units`; for a holding whose ledger is complete that figure
    # is the cached one, not the authority (CONTEXT.md), so the replay has to
    # put it back on every sync. Recomputing only the holdings this run
    # appended to would leave a position flipping between the two figures on
    # alternate runs.
    for asset_id in ledgered:
        asset = by_id[asset_id]
        if not await _ledger_reconciles(session, asset):
            continue
        # Whether a Holding is closed is not the trade sync's call. The replay
        # clears `sell_date` whenever it lands on units, and sets it on a full
        # exit — either way it would overwrite a date the user put there
        # deliberately, which `_sync_holdings` goes out of its way to respect a
        # hundred lines above. Quantity and basis are what this pass derives.
        closed_on, closed_at = asset.sell_date, asset.sell_price
        await asset_transaction_service.recompute_and_cache(session, asset)
        asset.sell_date, asset.sell_price = closed_on, closed_at


async def _ledger_reconciles(session: AsyncSession, asset: Asset) -> bool:
    """Whether replaying the ledger reproduces the quantity the provider reports.

    A ledger is only the authority for a Holding when it is *complete*, and
    the provider's own balance is the one check available for that. Unreviewed
    or unqualified transfers between owned wallets may remain missing from
    the ledger; reviewed movements participate only while their evidence is
    qualified. Missing movements leave the replay short where coins arrived
    or long where they left. A position whose buys are missing but whose sell
    is not replays to zero,
    which `recompute_and_cache` reads as a full exit and stamps with a sell
    date. That drops a holding the exchange still reports a balance for out of
    the portfolio entirely.

    So a ledger that disagrees with the balance leaves the Holding as it was:
    provider quantity, no derived basis, still a Snapshot. The trades stay on
    the ledger — they are real, and they start counting the moment the history
    is whole, whether that comes from a later sync or from a cost-basis import
    filling in what the exchange never saw.

    Quantities are compared with a dust tolerance. Exchanges settle fees in
    kind and round at the eighteenth decimal, so a complete history routinely
    reproduces a balance to within a rounding error rather than exactly.
    """
    from app.services.owned_transfer_service import prepare_replay
    await prepare_replay(session, asset.workspace_id, if_movements=True)
    rows = await session.execute(
        select(AssetTransaction).where(AssetTransaction.asset_id == asset.id)
    )
    txs = list(rows.scalars().all())
    # A sell the ledger cannot cover is proof of a missing buy, whatever the
    # balance says — and it reconciles by coincidence whenever the position was
    # closed out, since a clamped replay and a spent position both land on
    # zero. Every other writer of this ledger refuses an over-sell outright
    # (asset_transaction_service._raise_if_oversell, asset_import_service);
    # sync is the one that cannot, because it does not get to choose what the
    # exchange reports.
    if asset_transaction_service._detect_oversell(txs, asset_type=asset.type) is not None:
        logger.info(
            "Ledger for asset %s over-sells; keeping the reported quantity and "
            "leaving cost basis underived",
            asset.id,
        )
        return False
    reported = asset.units
    if reported is None:
        return True
    position = asset_transaction_service._recompute(txs, asset_type=asset.type)
    if not position.get("settlement_complete", True):
        asset.average_price = asset.purchase_price = asset.realized_gain = None
        return False
    replayed = position["units"]
    reported = Decimal(str(reported))
    tolerance = abs(reported) * LEDGER_RECONCILE_TOLERANCE
    if abs(replayed - reported) <= tolerance:
        return True
    logger.info(
        "Ledger for asset %s replays to %s but %s reports %s; keeping the reported "
        "quantity and leaving cost basis underived",
        asset.id,
        replayed,
        asset.source,
        reported,
    )
    return False


def _holding_snapshot_metadata(asset, holding, connection_id, source, workspace_id, collected_at, *, identity_verified=False):
    """Replace provider facts; retain only compatible, explicitly reviewed identity."""
    metadata = dict(holding.metadata or {})
    # Providers cannot confer review. This namespace is written by evidence review.
    metadata.pop("evidence_asset_identity", None)
    metadata.pop("investment_evidence_created", None)
    metadata["provider_quantity_observation"] = {
        "quantity": str(holding.quantity) if holding.quantity is not None else None,
        "collected_at": collected_at,
    }
    if asset is None:
        return metadata
    reviewed = (asset.external_metadata or {}).get("evidence_asset_identity")
    if not isinstance(reviewed, dict):
        return metadata
    identity = {key: reviewed[key] for key in ("chain", "token_address", "provider_asset_id", "isin")
                if isinstance(reviewed.get(key), str) and reviewed[key]}
    same_scope = (
        asset.workspace_id == workspace_id and asset.source == source
        and asset.external_id == holding.external_id
        and (holding.account_external_id is None or asset.account_external_id in {None, holding.account_external_id})
        and (not holding.ticker or not asset.ticker or holding.ticker.upper() == asset.ticker.upper())
    )
    reported_identity = {**metadata, "isin": holding.isin}
    compatible = all(not reported_identity.get(key) or reported_identity[key] == value
                     for key, value in identity.items())
    # Same live connection tolerates sparse identity. A reconnect needs a
    # stable instrument identifier as well as the same scoped holding key.
    reconnect_verified = any(identity.get(key) and identity[key] == reported_identity.get(key)
                             for key in ("provider_asset_id", "isin")) or (
        identity.get("chain") and identity.get("token_address")
        and all(identity[key] == metadata.get(key) for key in ("chain", "token_address"))
    )
    if identity and same_scope and compatible and (asset.connection_id == connection_id or reconnect_verified or identity_verified):
        metadata["evidence_asset_identity"] = identity
    return metadata


def _trade_target(holding: HoldingData) -> Optional[Decimal]:
    """The quantity a holding's trades must add up to, when known."""
    return Decimal("0") if holding.is_withdrawn else holding.quantity


def _reconciled_trades(
    holding: HoldingData, trades: list[HoldingTradeData]
) -> Optional[list[HoldingTradeData]]:
    """`trades` in ledger order, if they add up to the holding's position.

    Replaying every buy and sell must land on the quantity the provider
    reports now (zero once withdrawn) without ever going below zero. Each
    trade's quantity may carry the provider's rounding, so the check allows
    one millionth of the largest lot (at least 0.0001).
    """
    target = _trade_target(holding)
    if target is None or not trades:
        return None
    ordered = sorted(trades, key=lambda t: (t.date, t.kind != "buy", t.external_id))
    largest = max([abs(target), *(trade.quantity for trade in trades)])
    tolerance = max(Decimal("0.0001"), largest * Decimal("0.000001"))
    position = Decimal("0")
    for trade in ordered:
        position += trade.quantity if trade.kind == "buy" else -trade.quantity
        if position < -tolerance:
            return None
    return ordered if abs(position - target) <= tolerance else None


def _trade_from_row(tx: AssetTransaction) -> HoldingTradeData:
    return HoldingTradeData(
        external_id=tx.external_id or "",
        kind="buy" if tx.kind == "buy" else "sell",
        date=tx.date,
        quantity=Decimal(str(tx.quantity)),
        price=Decimal(str(tx.price)),
        fee=Decimal(str(tx.fee or 0)),
    )


async def _sale_recorded_by_hand(session: AsyncSession, asset: Asset) -> bool:
    """Whether the asset's sale date is the user's, not one its ledger set by selling out."""
    from app.services.asset_transaction_service import _recompute

    if asset.sell_date is None:
        return False
    rows = (
        await session.execute(select(AssetTransaction).where(AssetTransaction.asset_id == asset.id))
    ).scalars().all()
    position = _recompute(list(rows))
    return not (rows and position["units"] <= 0 and position["last_sell"] == asset.sell_date)


async def _sync_holding_trades(
    session: AsyncSession,
    source: str,
    holdings: list[HoldingData],
    synced_assets: dict[str, Asset],
) -> None:
    """Mirror the trades a provider reports into each asset's ledger.

    The ledger lets performance count what was bought and sold instead of
    inferring it from value changes. Stored trades of this connection's
    source are overlaid with the reported ones by `external_id` (the reported
    one wins, it may be a correction), so a provider that only reports recent
    trades keeps the older ones; failing that, the reported trades alone are
    tried. A set that doesn't add up to the reported position is not used and
    the stored ledger stays. Trades of any other source (entered by hand,
    imported) are never touched, and `units` stays the reported quantity.
    """
    from app.services.asset_transaction_service import recompute_and_cache

    reported = [
        holding
        for holding in holdings
        if holding.trades is not None
        and (asset := synced_assets.get(holding.external_id)) is not None
        # A market-priced asset is valued from its own ledger (units x
        # quote): the user took over tracking it, so its trades stay theirs.
        and asset.valuation_method != "market_price"
        and _trade_target(holding) is not None
    ]
    if not reported:
        return
    await session.flush()
    for holding in reported:
        asset = synced_assets[holding.external_id]
        rows = (
            await session.execute(
                select(AssetTransaction).where(
                    AssetTransaction.asset_id == asset.id,
                    AssetTransaction.source == source,
                )
            )
        ).scalars().all()
        stored = {tx.external_id: tx for tx in rows if tx.external_id}
        merged = {external_id: _trade_from_row(tx) for external_id, tx in stored.items()}
        merged.update((trade.external_id, trade) for trade in holding.trades or [])
        reconciled = _reconciled_trades(holding, list(merged.values())) or _reconciled_trades(
            holding, list(holding.trades or [])
        )
        if reconciled is None:
            if holding.trades:
                logger.warning(
                    "Trades for asset %s don't add up to the reported position; keeping its ledger",
                    asset.id,
                )
            continue
        # A sale the user recorded on a position the provider still holds
        # stays recorded. One the ledger itself set (the position was sold
        # out) follows the ledger, so buying again reopens the position.
        sold = (
            (asset.sell_date, asset.sell_price)
            if not holding.is_withdrawn and await _sale_recorded_by_hand(session, asset)
            else None
        )
        wanted = {trade.external_id for trade in reconciled}
        for tx in rows:
            if tx.external_id not in wanted:
                await session.delete(tx)
        # The ledger replays same-day trades in creation order: stamp them in
        # the order they reconciled in, so both replays agree.
        stamp = datetime.now(timezone.utc)
        for position, trade in enumerate(reconciled):
            tx = stored.get(trade.external_id)
            if tx is None:
                tx = AssetTransaction(
                    asset_id=asset.id,
                    workspace_id=asset.workspace_id,
                    source=source,
                    external_id=trade.external_id,
                )
                session.add(tx)
            tx.kind = trade.kind
            tx.quantity = trade.quantity
            tx.price = trade.price
            tx.fee = trade.fee
            tx.date = trade.date
            tx.created_at = stamp + timedelta(microseconds=position)
        await session.flush()
        was_sold = asset.sell_date is not None
        # Average price, realized gain and cost basis come from the ledger;
        # the position stays what the provider reports.
        await recompute_and_cache(session, asset)
        if holding.quantity is not None and not holding.is_withdrawn:
            asset.units = holding.quantity
        if sold is not None:
            asset.sell_date, asset.sell_price = sold
        if was_sold and asset.sell_date is None and not holding.is_withdrawn:
            # Reopened by this sync: the value step above skipped it while it
            # still read as sold, so record today's value now.
            await _upsert_asset_value_for_today(session, asset, holding.current_value, app_today())


async def _upsert_asset_from_holding(
    session: AsyncSession,
    asset: Optional[Asset],
    holding: HoldingData,
    user_id: uuid.UUID,
    connection_id: uuid.UUID,
    source: str,
    workspace_id: uuid.UUID,
    *,
    quantity_collected_at: Optional[str] = None,
    identity_verified: bool = False,
) -> Asset:
    """Create or update an Asset from a HoldingData payload.

    Synced fields (name, currency, quantity, purchase_price, maturity,
    metadata) are always overwritten — the UI disables editing these on
    synced assets. Provider-reported withdrawal is handled by the caller
    via `sell_date`, not here, so this function only ever sees ACTIVE
    holdings and never flips `is_archived` on its own.
    """
    metadata = _holding_snapshot_metadata(
        asset, holding, connection_id, source, workspace_id,
        quantity_collected_at or datetime.now(timezone.utc).isoformat(),
        identity_verified=identity_verified,
    )
    if asset is None:
        asset = Asset(
            user_id=user_id,
            workspace_id=workspace_id,
            connection_id=connection_id,
            source=source,
            external_id=holding.external_id,
            account_external_id=holding.account_external_id,
            name=holding.name,
            # Seeded on create only — a later sync must not undo the user's
            # own classification (app/services/cash_equivalent.py). A provider
            # that reports nothing but crypto has its holdings typed as such
            # rather than landing in the generic bucket: asset class is what
            # decides whether the Wash Sale rule reaches a holding at all
            # (docs/adr/0004), and the rule does not reach crypto. A type the
            # provider names itself wins.
            type=holding.asset_type or (
                CASH_EQUIVALENT_TYPE
                if is_cash_equivalent_ticker(holding.ticker)
                else "crypto"
                if source in CRYPTO_SOURCES
                else "investment"
            ),
            currency=holding.currency,
            units=holding.quantity,
            purchase_price=holding.purchase_price,
            purchase_date=holding.purchase_date,
            isin=holding.isin,
            ticker=holding.ticker,
            maturity_date=holding.maturity_date,
            external_metadata=metadata,
            valuation_method="manual",
        )
        session.add(asset)
        await session.flush()
        return asset

    # Fields Pluggy consistently returns — safe to overwrite each sync.
    asset.name = holding.name
    # A type the provider names replaces only the generic default: a type
    # the user picked stays.
    if holding.asset_type and asset.type == "investment":
        asset.type = holding.asset_type
    asset.currency = holding.currency
    asset.user_id = user_id
    # Provider-owned while the provider states it. A payload that names no
    # account keeps the attribution already on the row: it is the join
    # `asset_group_service._account_type_and_balance_for` walks to reach the
    # account, so erasing it silently empties allocation-by-account-type and
    # Liquid Cash for the whole wallet until a healthy payload arrives. Same
    # reasoning as `hint_lost` in `_sync_holdings`, which refuses to re-file a
    # holding on the strength of a hint the provider stopped sending.
    if holding.account_external_id is not None:
        asset.account_external_id = holding.account_external_id
    # external_metadata is a snapshot blob: we want the latest every time.
    asset.external_metadata = metadata
    previous_connection_id = asset.connection_id
    asset.connection_id = connection_id
    # Only auto-unarchive when the holding moved to a different connection
    # (e.g. unlink + reconnect). This avoids overriding user-archived assets.
    if asset.is_archived and previous_connection_id != connection_id:
        asset.is_archived = False
    # Re-adopted across workspaces (bank deleted in one, re-added in the
    # other): the asset follows its connection; its old wallet stays behind
    # in the old workspace, so placement is redone by the caller.
    if asset.workspace_id != workspace_id:
        asset.workspace_id = workspace_id
        asset.group_id = None

    # Sparse fields — merge, don't clobber. Pluggy sometimes returns
    # these on first sync and null on later ones (e.g. amountOriginal
    # present at creation, missing on daily rebalances). Keeping the
    # first-seen value is better than wiping data we already have.
    if holding.quantity is not None:
        asset.units = holding.quantity
    if holding.purchase_price is not None:
        asset.purchase_price = holding.purchase_price
    if holding.purchase_date:
        asset.purchase_date = holding.purchase_date
    if holding.isin:
        asset.isin = holding.isin
    if holding.ticker:
        asset.ticker = holding.ticker
    if holding.maturity_date:
        asset.maturity_date = holding.maturity_date
    return asset


async def _ensure_historical_seed(
    session: AsyncSession,
    asset: Asset,
    purchase_date: date,
    purchase_price,
) -> None:
    """Insert a one-time AssetValue at purchase_date with purchase_price.

    Called on every sync but a no-op once the seed exists. Skips if ANY
    AssetValue already exists on that date (even a manual one) — we don't
    want to stomp a value the user may have entered themselves.
    """
    existing = await session.execute(
        select(AssetValue).where(
            AssetValue.asset_id == asset.id,
            AssetValue.date == purchase_date,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return
    session.add(
        AssetValue(
            asset_id=asset.id,
            amount=purchase_price,
            date=purchase_date,
            source="sync",
        )
    )


async def _upsert_asset_value_for_today(
    session: AsyncSession,
    asset: Asset,
    amount,
    today: date,
) -> None:
    """One sync-sourced AssetValue per asset per day.

    Re-syncing the same day updates the amount in place; a later day
    creates a new row so we build a daily valuation history over time.

    A day the user valued by hand is theirs: sync writes nothing for it
    (issue #68). Skipping the write rather than the row is what makes that
    true — appending a sync row alongside leaves the hand-set row intact but
    shadows it everywhere the latest value is read, which is the same loss
    with extra steps. Matches `_ensure_historical_seed`.
    """
    hand_set = await session.scalar(
        select(AssetValue.id).where(
            AssetValue.asset_id == asset.id,
            AssetValue.date == today,
            AssetValue.source == "manual",
        )
    )
    if hand_set is not None:
        return
    existing = await session.execute(
        select(AssetValue).where(
            AssetValue.asset_id == asset.id,
            AssetValue.date == today,
            AssetValue.source == "sync",
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        row.amount = amount
    else:
        session.add(
            AssetValue(
                asset_id=asset.id,
                amount=amount,
                date=today,
                source="sync",
            )
        )


async def _match_pluggy_category(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    pluggy_category: Optional[str],
    enabled: bool = True,
) -> Optional[uuid.UUID]:
    # `enabled` is the resolved value of the global `use_provider_categories`
    # admin setting. Off = sync skips the provider->user category mapping
    # entirely so transactions arrive uncategorized and Rules are the only
    # source of truth. Default keeps the historical behavior.
    if not enabled or not pluggy_category:
        return None
    # Try exact match first, then prefix before " - " (e.g. "Transfer - PIX" → "Transfer")
    app_name = PLUGGY_CATEGORY_MAP.get(pluggy_category)
    if not app_name and " - " in pluggy_category:
        app_name = PLUGGY_CATEGORY_MAP.get(pluggy_category.split(" - ")[0])
    if not app_name:
        return None
    # Scope to the connection's workspace: a user in multiple workspaces owns
    # the same default category names in each, so a user_id-only lookup returns
    # several rows. `.first()` is belt-and-suspenders — a category match must
    # never crash the whole sync even if a workspace somehow has name dupes.
    result = await session.execute(
        select(Category.id)
        .where(Category.workspace_id == workspace_id, Category.name == app_name)
        .limit(1)
    )
    return result.scalars().first()


def _bank_converted_amount(
    txn_data: TransactionData, account_currency: str, user_currency: str
) -> Optional[Decimal]:
    """The bank's own conversion of a foreign charge into the primary currency.

    Only when the account is held in the primary currency, so the figure the
    bank charged the account is the primary amount. None otherwise.
    """
    if (
        txn_data.amount_in_account_currency is not None
        and txn_data.amount
        and account_currency == user_currency
        and txn_data.currency != account_currency
    ):
        return txn_data.amount_in_account_currency
    return None


async def _stamp_synced_primary_amount(
    session: AsyncSession,
    user_id: uuid.UUID,
    transaction: Transaction,
    converted: Optional[Decimal],
    *,
    fetched_rates: FetchedRateCache,
) -> None:
    """Prefer the bank's conversion (see _bank_converted_amount); else use our FX rates.

    The transaction's amount must already be the charged amount. A row that
    was stamped before (a promoted pending row, an upgraded placeholder) is
    first rescaled at its earlier rate: ``stamp_primary_amount`` leaves the
    fields untouched when no rate is available, which would keep the primary
    amount of the old figure.
    """
    if converted is not None:
        transaction.amount_primary = converted
        transaction.fx_rate_used = converted / transaction.amount
        return
    if transaction.fx_rate_used is not None:
        transaction.amount_primary = (
            Decimal(transaction.amount) * Decimal(transaction.fx_rate_used)
        ).quantize(Decimal("0.01"))
    # OAuth import, pending promotion, and recurring sync all own the enclosing
    # transaction. Fetch into an import-local snapshot: never commit partial
    # data or acquire shared FX cache write locks inside that transaction.
    await stamp_primary_amount(
        session, user_id, transaction,
        persist_fetched_rates=False, fetched_rates=fetched_rates,
    )


async def _find_installment_category(
    session: AsyncSession,
    account_id: uuid.UUID,
    txn_data,
) -> Optional[uuid.UUID]:
    source = await _find_installment_source(session, account_id, txn_data)
    return source.category_id if source is not None else None


async def _find_installment_source(
    session: AsyncSession,
    account_id: uuid.UUID,
    txn_data,
) -> Optional[Transaction]:
    """Reuse the latest user-selected category from an earlier installment.

    Pluggy gives every parcel the original purchase date, installment count,
    and total purchase amount. Together with the card account and transaction
    type, those fields identify the purchase series without relying on the
    description (whose ``03/12`` suffix changes every month).

    Only earlier synced parcels qualify. This keeps manual installment series
    separate and lets a user's most recent category correction become the
    source of truth for the next imported parcel. A split parcel hands on its
    parts (see `_find_installment_source`).
    """
    if (
        txn_data.installment_number is None
        or txn_data.total_installments is None
        or txn_data.installment_total_amount is None
        or txn_data.installment_purchase_date is None
    ):
        return None

    result = await session.execute(
        select(Transaction)
        .where(
            Transaction.account_id == account_id,
            Transaction.source == "sync",
            # A split parcel counts as the latest choice too, and hands on its
            # parts rather than letting an older parcel's category win.
            or_(
                Transaction.category_id.is_not(None),
                Transaction.has_category_lines.is_(True),
            ),
            Transaction.installment_purchase_date == txn_data.installment_purchase_date,
            Transaction.total_installments == txn_data.total_installments,
            Transaction.installment_total_amount == txn_data.installment_total_amount,
            Transaction.type == txn_data.type,
            Transaction.installment_number < txn_data.installment_number,
        )
        .order_by(Transaction.installment_number.desc(), Transaction.date.desc())
        .limit(1)
    )
    return result.scalars().first()


async def get_connections(session: AsyncSession, workspace_id: uuid.UUID) -> list[BankConnection]:
    result = await session.execute(
        select(BankConnection)
        .where(BankConnection.workspace_id == workspace_id)
        .options(selectinload(BankConnection.accounts))
        .order_by(BankConnection.created_at.desc())
    )
    return list(result.scalars().all())


async def get_connection(
    session: AsyncSession,
    connection_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Optional[BankConnection]:
    statement = (
        select(BankConnection)
        .where(BankConnection.id == connection_id, BankConnection.workspace_id == workspace_id)
        .options(selectinload(BankConnection.accounts))
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    result = await session.execute(statement)
    return result.scalar_one_or_none()


class ConnectionSyncInProgressError(RuntimeError):
    """A connection mutation could not acquire the running sync's row lock."""

    def __init__(self) -> None:
        super().__init__("Connection sync in progress. Please try again shortly.")


async def _get_connection_for_mutation(
    session: AsyncSession, connection_id: uuid.UUID, workspace_id: uuid.UUID,
) -> Optional[BankConnection]:
    """Bound reconnect/delete lock waits without changing queued sync behavior.

    SET LOCAL is transaction-scoped, so pooled connections cannot retain the
    timeout. Restore the previous value after acquisition too: later import or
    cleanup queries must not inherit this short user-facing lock budget.
    """
    if session.get_bind().dialect.name != "postgresql":
        return await get_connection(session, connection_id, workspace_id, for_update=True)

    previous_timeout = await session.scalar(text("SHOW lock_timeout"))
    await session.execute(text("SET LOCAL lock_timeout = '1s'"))
    try:
        connection = await get_connection(session, connection_id, workspace_id, for_update=True)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        # A lock timeout aborts PostgreSQL's transaction. No provider calls or
        # mutation writes have happened yet, and rollback also resets SET LOCAL.
        await session.rollback()
        raise ConnectionSyncInProgressError() from exc
    await session.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": previous_timeout},
    )
    return connection


async def get_oauth_url(
    provider_name: str,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    flow_params: Optional[dict] = None,
    reconnect_connection_id: Optional[uuid.UUID] = None,
    session: AsyncSession | None = None,
) -> str:
    provider = get_provider(provider_name, settings=await resolve_settings(session))
    state = await oauth_state.store_state(
        {
            "user_id": str(user_id),
            "workspace_id": str(workspace_id),
            "provider": provider_name,
            "flow_params": flow_params or {},
            "reconnect_connection_id": (
                str(reconnect_connection_id) if reconnect_connection_id else None
            ),
        }
    )
    return await provider.get_oauth_url(provider.redirect_uri, state, flow_params)


async def get_reauth_url(
    session: AsyncSession,
    connection_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> str:
    connection = await get_connection(session, connection_id, workspace_id)
    if not connection:
        raise ValueError("Connection not found")
    provider = get_provider(connection.provider, settings=await resolve_settings(session))
    state = await oauth_state.store_state(
        {
            "user_id": str(user_id),
            "workspace_id": str(workspace_id),
            "provider": connection.provider,
            "flow_params": (connection.settings or {}).get("flow_params") or {},
            "reconnect_connection_id": str(connection.id),
        }
    )
    return await provider.reauth_url(
        connection.credentials or {},
        connection.settings or {},
        provider.redirect_uri,
        state,
    )


async def list_provider_institutions(
    provider_name: str, country: Optional[str] = None, session: AsyncSession | None = None
) -> dict:
    provider = get_provider(provider_name, settings=await resolve_settings(session))
    data = await provider.list_institutions(country)
    return {
        "countries": data.countries,
        "institutions": [
            {
                "name": i.name,
                "display_name": i.display_name,
                "country": i.country,
                "logo": i.logo,
                "bic": i.bic,
                "psu_types": i.psu_types,
                "max_consent_days": i.max_consent_days,
                "max_history_days": i.max_history_days,
            }
            for i in data.institutions
        ],
    }


async def create_connect_token(
    provider_name: str, user_id: uuid.UUID, item_id: str | None = None,
    session: AsyncSession | None = None,
) -> dict:
    provider = get_provider(provider_name, settings=await resolve_settings(session))
    token_data = await provider.create_connect_token(str(user_id), item_id=item_id)
    return {"access_token": token_data.access_token}


async def update_connection_settings(
    session: AsyncSession,
    connection_id: uuid.UUID,
    workspace_id: uuid.UUID,
    settings_update: dict,
) -> Optional[BankConnection]:
    # Settings are a single JSON blob and sync now writes to it too (the seen
    # account ids). Take the same row lock sync holds so a save landing mid-sync
    # waits and then merges onto fresh settings, instead of the two blob writes
    # clobbering each other.
    connection = await get_connection(
        session, connection_id, workspace_id, for_update=True
    )
    if not connection:
        return None

    if "display_name" in settings_update:
        raw = settings_update.pop("display_name")
        trimmed = raw.strip() if isinstance(raw, str) else raw
        connection.display_name = trimmed or None

    # Evidence about what the picker showed, not a setting of its own: it is
    # folded into the reviewed set below and never stored as sent.
    shown_ids = settings_update.pop("reviewed_account_ids", None)

    current = dict(connection.settings or {})
    for key, value in settings_update.items():
        if value is not None:
            current[key] = value
    connection.settings = current

    if settings_update.get("account_allowlist") is not None:
        # An account the provider started returning since the last sync is in
        # none of the sets on the connection, so unchecking it here would leave
        # it unreviewed and the next sync would put it back to pending. What the
        # picker had on screen is the only record that it was ever offered.
        reviewed = {a.external_id for a in connection.accounts if a.external_id}
        reviewed |= {str(item) for item in shown_ids or []}
        _record_reviewed_accounts(connection, reviewed)

    await session.commit()
    await session.refresh(connection)
    return connection


async def list_provider_accounts(
    connection: BankConnection,
    settings: Optional[Settings] = None,
) -> list[ProviderAccountRead]:
    """List every account the provider exposes, annotated with allowlist state.

    One call to the provider, filtered here. Asking it per account would burn a
    rate-limit budget measured in requests per day (SimpleFIN Bridge: 24 per
    token per day, with documented token disablement for sustained overuse) and
    SimpleFIN's per-account filter has its own bucket on top of that.

    One call is what this layer guarantees; a provider whose own account fetch
    is per-account (Enable Banking reads details and balances per uid, because
    its session payload carries neither) still fans out underneath. That is
    tolerable there and not on SimpleFIN: Enable Banking's PSD2 limits bind
    unattended polling, and this endpoint only ever runs with the user waiting.

    Read-only by design: nothing here writes the allowlist or the reviewed ids,
    so an account the provider stops returning keeps its place in the user's
    selection — but it is absent from this response, since it is a list of what
    the provider exposes. A caller rebuilding the allowlist from this response
    alone would drop it; the stored list on the connection read is the one to
    merge against.

    Takes a loaded connection rather than an id: tenancy is the caller's to
    enforce, and it keeps "not found" out of this function's error surface.
    """
    if not connection.credentials:
        raise ValueError("Credentials not found")

    # Resolved separately from the read below: an unregistered provider is a
    # server misconfiguration, not a provider outage, and the two answer with
    # different status codes.
    try:
        provider = get_provider(connection.provider, settings=settings)
    except ValueError as exc:
        raise ProviderNotConfiguredError(
            f"Provider '{connection.provider}' is not configured in this process."
        ) from exc

    # A validation gate here, not a rotation point — unlike sync, this read
    # persists nothing, so a provider that ever starts rotating credentials
    # needs sync to be the one that stores them.
    credentials = await provider.refresh_credentials(connection.credentials)
    accounts = await provider.get_accounts(credentials)

    conn_settings = connection.settings or {}
    known = known_account_ids(
        conn_settings, {a.external_id for a in connection.accounts if a.external_id}
    )

    return [
        ProviderAccountRead(
            external_id=account.external_id,
            name=account.name,
            balance=account.balance,
            currency=account.currency,
            has_holdings=account.has_holdings,
            status=account_status(conn_settings, account.external_id, known),
        )
        for account in accounts
    ]


async def handle_oauth_callback(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    code: str,
    provider_name: Optional[str] = None,
    state: Optional[str] = None,
    sync_assets: Optional[bool] = None,
    account_allowlist: Optional[list[str]] = None,
    reconnect_connection_id: Optional[uuid.UUID] = None,
) -> BankConnection:
    state_payload: dict = {}
    if state:
        consumed = await oauth_state.consume_state(state)
        if not consumed:
            raise ValueError("OAuth state is invalid or expired")
        # The state is authoritative — caller-supplied provider_name is a hint.
        if consumed.get("user_id") != str(user_id):
            raise ValueError("OAuth state user does not match authenticated user")
        if consumed.get("workspace_id") != str(workspace_id):
            raise ValueError("OAuth state workspace does not match active workspace")
        state_payload = consumed
        provider_name = consumed.get("provider") or provider_name
    reconnect_id = state_payload.get("reconnect_connection_id") or reconnect_connection_id
    existing_reconnect: BankConnection | None = None
    if reconnect_id:
        existing_reconnect = await _get_connection_for_mutation(
            session, uuid.UUID(str(reconnect_id)), workspace_id,
        )
        if not existing_reconnect:
            raise ValueError("Reconnect target connection not found")
        # Token reconnects do not carry OAuth state, so the request body may be
        # the only source of provider_name. Never allow a pasted token for one
        # provider to overwrite another provider's stored credentials.
        if provider_name and provider_name != existing_reconnect.provider:
            raise ValueError("Reconnect provider does not match target connection")
        provider_name = existing_reconnect.provider

    if not provider_name:
        raise ValueError("OAuth callback missing provider")

    provider = get_provider(provider_name, settings=await resolve_settings(session))
    connection_data = await provider.handle_oauth_callback(code)

    if existing_reconnect:
        existing_reconnect.external_id = connection_data.external_id
        existing_reconnect.institution_name = (
            connection_data.institution_name or existing_reconnect.institution_name
        )
        existing_reconnect.logo_url = _clean_logo_url(connection_data.logo_url) or existing_reconnect.logo_url
        existing_reconnect.credentials = connection_data.credentials
        unavailable = _record_balance_observation(
            existing_reconnect, connection_data.accounts, provider
        )
        existing_reconnect.status = "sync_error" if unavailable else "active"
        existing_reconnect.last_sync_error_account_id = None
        existing_reconnect.sync_state_version += 1
        # Re-sync from current data on next sync cycle.
        existing_reconnect.last_sync_at = None
        # New credentials end a backoff earned by failing ones, so the next
        # cycle syncs it. A rate-limit backoff stays: the bank's quota is the
        # same whatever the credentials.
        reconnect_settings = existing_reconnect.settings or {}
        backoff = reconnect_settings.get(SYNC_BACKOFF_KEY)
        if backoff is not None and not (
            isinstance(backoff, dict) and backoff.get("rate_limited") is True
        ):
            existing_reconnect.settings = {
                key: value
                for key, value in reconnect_settings.items()
                if key != SYNC_BACKOFF_KEY
            }
        await session.commit()
        await session.refresh(existing_reconnect)
        return existing_reconnect

    flow_params = dict(state_payload.get("flow_params") or {})
    flow_sync_assets = flow_params.pop("sync_assets", None)
    flow_allowlist = flow_params.pop("account_allowlist", None)
    initial_settings: dict[str, object] = {"flow_params": flow_params}
    if sync_assets is None and isinstance(flow_sync_assets, bool):
        sync_assets = flow_sync_assets
    if sync_assets is not None:
        initial_settings["sync_assets"] = sync_assets
    if account_allowlist is None and isinstance(flow_allowlist, list):
        account_allowlist = [str(item) for item in flow_allowlist]
    # Chosen during the connect flow, so the very first import already honors
    # it rather than creating accounts the user then has to clean up. An empty
    # one is the review-first connect: import nothing now, let the user pick
    # from the account picker. It only holds for a provider that enumerates
    # accounts at connect — with nothing to review the connection would sync
    # nothing forever, so fall back to the legacy default rather than error.
    review_first = False
    if account_allowlist is not None and (account_allowlist or connection_data.accounts):
        initial_settings["account_allowlist"] = [str(item) for item in account_allowlist]
        review_first = not account_allowlist
        if review_first:
            # Nothing has been shown to the user yet, so every account the
            # provider exposes must reach the picker as pending rather than as
            # an exclusion they chose.
            initial_settings["reviewed_account_ids"] = []

    connection = BankConnection(
        workspace_id=workspace_id,
        user_id=user_id,
        provider=provider_name,
        external_id=connection_data.external_id,
        institution_name=connection_data.institution_name,
        logo_url=_clean_logo_url(connection_data.logo_url),
        credentials=connection_data.credentials,
        settings=initial_settings,
        status="active",
    )
    session.add(connection)
    await session.flush()

    user = await session.get(User, user_id)
    user_currency = user.primary_currency if user else get_settings().default_currency
    new_tx_ids: list[uuid.UUID] = []
    fetched_rates: FetchedRateCache = {}

    use_provider_cats = await admin_service.use_provider_categories(session)

    _record_seen_accounts(connection, connection_data.accounts)
    syncable_accounts, synced_account_ids = _syncable_accounts(
        connection, connection_data.accounts
    )
    if synced_account_ids is not None and not review_first:
        # The connect widget showed this account list before the first sync, so
        # everything in it counts as reviewed — only accounts that turn up later
        # are new to the user. Skipped for a review-first connect, which imports
        # nothing precisely because the user has reviewed nothing.
        _record_reviewed_accounts(
            connection, {a.external_id for a in connection_data.accounts}
        )

    # Holdings are fetched before the accounts so each account balance knows
    # whether its auto-invested portion is counted as an asset (issue #843).
    # Without a payload (asset sync off or a failed fetch) the deduction is
    # decided from the live synced assets already stored.
    holdings_fetch: Optional[_HoldingsFetch] = None
    if _sync_assets_enabled(connection.settings):
        holdings_fetch = await _fetch_holdings(
            session, connection, connection_data.credentials, provider
        )
    auto_invested_deducted = await _resolve_auto_invested_deductions(
        session,
        connection,
        holdings_fetch.holdings if holdings_fetch is not None else None,
        connection_data.accounts,
        synced_account_ids,
    )

    institution_cache: dict[str, Institution] = {}
    created_accounts: list[Account] = []
    for acc_data in syncable_accounts:
        if acc_data.balance is None:
            # Keep the account in inventory/allowlists and sync its known
            # holdings, but do not create a numeric zero for an unknown total.
            continue
        is_cc = acc_data.type == "credit_card"
        institution = await _resolve_institution(
            session, connection.id, institution_cache, acc_data
        )
        account = Account(
            user_id=user_id,
            workspace_id=workspace_id,
            connection_id=connection.id,
            external_id=acc_data.external_id,
            name=acc_data.name,
            display_name=institution.name if institution else None,
            masked_number=acc_data.masked_number,
            type=acc_data.type,
            balance=_account_balance(acc_data, acc_data.balance, auto_invested_deducted),
            currency=acc_data.currency,
            credit_limit=acc_data.credit_limit if is_cc else None,
            statement_close_day=acc_data.statement_close_day if is_cc else None,
            payment_due_day=acc_data.payment_due_day if is_cc else None,
            minimum_payment=acc_data.minimum_payment if is_cc else None,
            card_brand=acc_data.card_brand if is_cc else None,
            card_level=acc_data.card_level if is_cc else None,
            shared_balance_group=(
                f"{connection.id}:{acc_data.shared_balance_group}"
                if acc_data.shared_balance_group else None
            ),
            institution_id=institution.id if institution else None,
        )
        session.add(account)
        await session.flush()
        created_accounts.append(account)

        await _sync_reserved_balances(session, account, acc_data)

        bills_by_external_id = await _sync_credit_card_bills(
            session, user_id, account, provider, connection_data.credentials
        )

        # Fetch initial transactions (since=None fetches all available history)
        transactions_data = await provider.get_transactions(
            connection_data.credentials, acc_data.external_id, None
        )
        incoming_external_ids = {txn.external_id for txn in transactions_data}
        seen_external_ids: set[str] = set()
        for txn_data in transactions_data:
            # Providers should return each external transaction once, but an
            # upstream pagination bug can repeat a page. This account was just
            # created, so batch-local tracking avoids both duplicate inserts
            # and an unnecessary SELECT for every imported transaction.
            if txn_data.external_id in seen_external_ids:
                continue
            seen_external_ids.add(txn_data.external_id)

            # Pending↔posted twin (and the credit-card installment variant).
            # When the same logical operation comes back under a new external
            # id with a different status, fingerprint match prevents the
            # second copy from landing.
            synced_dup = await _find_synced_duplicate(
                session, account.id, txn_data, incoming_external_ids
            )
            if synced_dup:
                if _sync_row_is_frozen(synced_dup, provider_name):
                    continue
                if synced_dup.status == "posted" and txn_data.status == "pending":
                    continue
                if synced_dup.original_description is None:
                    synced_dup.original_description = txn_data.description
                if synced_dup.status == "pending" and txn_data.status == "posted":
                    await _promote_synced_transaction(
                        session, user_id, synced_dup, txn_data,
                        account=account,
                        account_currency=acc_data.currency or user_currency,
                        user_currency=user_currency,
                        replace_external_id=True,
                        fetched_rates=fetched_rates,
                    )
                    new_tx_ids.append(synced_dup.id)
                    if (
                        txn_data.bill_external_id
                        and synced_dup.effective_bill_date is None
                    ):
                        bill = bills_by_external_id.get(txn_data.bill_external_id)
                        if bill is not None:
                            if synced_dup.bill_id != bill.id:
                                synced_dup.bill_id = bill.id
                            apply_effective_date(
                                synced_dup, account, bill_due_date=bill.due_date
                            )
                elif synced_dup.external_id != txn_data.external_id:
                    _merge_sync_metadata(
                        synced_dup,
                        txn_data,
                        replace_external_id=synced_dup.source == "sync",
                    )
                continue

            installment_source = await _find_installment_source(
                session, account.id, txn_data
            )
            category_id = installment_source.category_id if installment_source else None
            # The previous parcel was split: this one starts with the same parts.
            split_source = (
                installment_source
                if installment_source is not None and installment_source.has_category_lines
                else None
            )
            recurring_link_for_split = None
            if category_id is None:
                category_id = await _match_pluggy_category(
                    session,
                    workspace_id,
                    txn_data.pluggy_category,
                    enabled=use_provider_cats,
                )
            # Resolve payee entity from raw payee text
            payee_id = None
            if txn_data.payee:
                payee_entity = await get_or_create_payee(
                    session, user_id, txn_data.payee, workspace_id=workspace_id
                )
                payee_id = payee_entity.id

            bill = (
                bills_by_external_id.get(txn_data.bill_external_id)
                if txn_data.bill_external_id
                else None
            )
            transaction = Transaction(
                user_id=user_id,
                workspace_id=workspace_id,
                account_id=account.id,
                external_id=txn_data.external_id,
                description=txn_data.description,
                original_description=txn_data.description,
                amount=txn_data.amount,
                currency=txn_data.currency or acc_data.currency or user_currency,
                date=txn_data.date,
                type=txn_data.type,
                source="sync",
                status=txn_data.status,
                payee=txn_data.payee,
                payee_id=payee_id,
                raw_data=txn_data.raw_data,
                category_id=None,
                installment_number=txn_data.installment_number,
                total_installments=txn_data.total_installments,
                installment_total_amount=txn_data.installment_total_amount,
                installment_purchase_date=txn_data.installment_purchase_date,
                bill_id=bill.id if bill else None,
            )
            apply_effective_date(
                transaction, account, bill_due_date=bill.due_date if bill else None
            )
            if split_source is not None:
                category_line_service.copy_lines_to(transaction, split_source.category_lines)
            session.add(transaction)
            await session.flush()
            new_tx_ids.append(transaction.id)
            await apply_rules_to_transaction(
                session, user_id, transaction,
                skip_category_rules=split_source is not None,
            )
            if recurring_link_for_split is not None:
                # A split bill hands its parts to the charge it claims.
                await category_line_service.apply_template_split(
                    session, transaction, recurring_link_for_split
                )
            if transaction.category_id is None and not transaction.has_category_lines:
                transaction.category_id = category_id

            await _stamp_synced_primary_amount(
                session,
                user_id,
                transaction,
                _bank_converted_amount(
                    txn_data, acc_data.currency or user_currency, user_currency
                ),
                fetched_rates=fetched_rates,
            )

    # Detect transfer pairs among newly synced transactions
    await detect_transfer_pairs(session, workspace_id, candidate_ids=new_tx_ids)

    # And settle what this money was promised against, once the rows exist.
    if new_tx_ids:
        landed = list(
            (
                await session.execute(
                    select(Transaction).where(Transaction.id.in_(new_tx_ids))
                )
            )
            .scalars()
            .all()
        )
        await reconciliation_service.match_incoming(session, workspace_id, landed)

    # Investment holdings live on /investments — separate endpoint from
    # /accounts. Fetched before the account loop and stored here once the
    # accounts exist, so holdings are available on the Assets page
    # immediately after the widget closes.
    holdings_persisted = False
    if holdings_fetch is not None:
        holdings_persisted = await _sync_holdings(
            session, user_id, connection, connection_data.credentials,
            synced_account_ids, provider=provider, fetched=holdings_fetch,
        )
        await _sync_trades(
            session, connection, connection_data.credentials, synced_account_ids,
            provider=provider,
        )

    # Reconcile the opening balance so that SUM(all transactions) matches the
    # provider-reported balance: history outside the provider's lookback window
    # gets absorbed into a synthetic transaction. After the holdings sync, not
    # inside the account loop — an account whose balance carries Holdings gets
    # no opening balance at all, and on a first connect the holdings that say so
    # do not exist until `_sync_holdings` has run.
    unavailable = _record_balance_observation(
        connection, connection_data.accounts, provider,
        persisted_account_ids={a.external_id for a in created_accounts if a.external_id}
        if holdings_persisted else set(),
        account_types={a.external_id: a.type for a in created_accounts if a.external_id},
        auto_invested_deducted=auto_invested_deducted,
    )
    for account in created_accounts:
        if account.external_id not in unavailable:
            await sync_opening_balance_for_connected_account(session, account)

    if any(a.external_id in unavailable for a in syncable_accounts):
        connection.status = "sync_error"
    else:
        connection.last_sync_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(connection)
    return connection


def _normalized_account_name(name: str | None) -> str:
    """Normalize provider account names for fallback matching.

    Some SimpleFIN bridges re-key account ids, but the display name / masked
    suffix stays stable (e.g. "High Yield Savings Account (9402)").
    """
    return " ".join((name or "").casefold().split())


async def _find_existing_connected_account(
    session: AsyncSession,
    connection: BankConnection,
    acc_data: AccountData,
    institution: Optional[Institution],
    incoming_external_ids: set[str],
    incoming_institution_external_ids: frozenset[str] = frozenset(),
) -> Optional[Account]:
    """Find an existing account for incoming provider account data.

    Primary identity is provider external_id. For SimpleFIN only, fall back to
    stable account name + currency because some bridges emit fresh UUID-like
    account ids on each pull; when that happens, blindly keying by id creates a
    new app account on every sync.
    """
    result = await session.execute(
        select(Account).where(
            Account.connection_id == connection.id,
            Account.external_id == acc_data.external_id,
        )
    )
    account = result.scalar_one_or_none()
    if account is not None:
        return account

    # The provider re-keyed this account. Enable Banking issues a `uid` scoped to
    # one session, so reauthorising changes it while `stable_external_id` (the
    # identification_hash) survives. Rebind the existing row, keeping its
    # transactions, rules and name overrides, instead of inserting a duplicate.
    # EB does not guarantee the hash is unique, so require exactly one unclaimed
    # candidate: a row another incoming account already took in this run carries
    # an external_id that is in the batch, and adopting it a second time would
    # put both transaction feeds on one account.id.
    if acc_data.stable_external_id:
        stable_rows = (await session.execute(
            select(Account).where(
                Account.connection_id == connection.id,
                Account.stable_external_id == acc_data.stable_external_id,
            )
        )).scalars().all()
        stable_rows = [
            row for row in stable_rows if row.external_id not in incoming_external_ids
        ]
        if len(stable_rows) > 1:
            return None
        if len(stable_rows) == 1:
            stable_rows[0].external_id = acc_data.external_id
            return stable_rows[0]

    # Last resort for rows that predate `stable_external_id` (added by migration
    # 117, and only ever backfilled by a *successful* sync, which a user whose
    # bank was failing never had). The masked identifier is the closest thing to
    # an account number we keep. Two guards, both fail-closed: a single candidate
    # (a shared last-4 must never merge two accounts), and candidates limited to
    # rows with no stable id: a row that is already identified is a different
    # account, so adopting it would overwrite its identity and mix histories.
    if connection.provider == "enable_banking" and acc_data.masked_number:
        masked_rows = (await session.execute(
            select(Account).where(
                Account.connection_id == connection.id,
                Account.masked_number == acc_data.masked_number,
                Account.currency == acc_data.currency,
                Account.stable_external_id.is_(None),
            )
        )).scalars().all()
        masked_rows = [
            row for row in masked_rows if row.external_id not in incoming_external_ids
        ]
        if len(masked_rows) == 1:
            masked_rows[0].external_id = acc_data.external_id
            return masked_rows[0]

    if connection.provider != "simplefin":
        return None

    normalized_name = _normalized_account_name(acc_data.name)
    if not normalized_name:
        return None

    candidates_result = await session.execute(
        select(Account).where(
            Account.connection_id == connection.id,
            Account.currency == acc_data.currency,
        )
    )
    candidates = [
        candidate
        for candidate in candidates_result.scalars().all()
        if candidate.external_id not in incoming_external_ids
        and _normalized_account_name(candidate.name) == normalized_name
    ]
    if institution is not None:
        matched_institution = [
            candidate for candidate in candidates
            if candidate.institution_id == institution.id
        ]
        if not matched_institution and candidates:
            # Re-linking a bank in SimpleFIN Bridge can send it under a new
            # conn_id, so _resolve_institution just minted a fresh row while
            # the account still points at the old, same-named one.
            #
            # Only a conn_id this pull no longer carries can have been
            # re-linked. A bank still listed under its own conn_id was not,
            # so a same-named one arriving beside it is a second login at
            # that bank (SimpleFIN has no account number to tell them apart,
            # and two logins at one bank share its name). Adopting its
            # account would point the user's existing row, display name and
            # history at the other login's feed.
            same_name_ids = set((await session.execute(
                select(Institution.id).where(
                    Institution.connection_id == connection.id,
                    Institution.name == institution.name,
                    Institution.external_id.not_in(incoming_institution_external_ids),
                )
            )).scalars().all())
            matched_institution = [
                candidate for candidate in candidates
                if candidate.institution_id in same_name_ids
            ]
        if matched_institution:
            candidates = matched_institution
        else:
            candidates = [
                candidate for candidate in candidates
                if candidate.institution_id is None
            ]
    else:
        candidates = [
            candidate for candidate in candidates
            if candidate.institution_id is None
        ]
    # A duplicate the user closed must not make the surviving open account
    # ambiguous; a lone closed match still wins, so a re-keyed closed account
    # is not recreated as a new active one (#90).
    open_candidates = [candidate for candidate in candidates if not candidate.is_closed]
    if open_candidates:
        candidates = open_candidates
    if len(candidates) != 1:
        return None

    account = candidates[0]
    account.external_id = acc_data.external_id
    return account


def _sync_duplicate_description_match(a: str | None, b: str | None) -> bool:
    """Match stable merchant text across posting suffix/reference changes."""
    if not a or not b:
        return False
    left = " ".join(a.casefold().split())
    right = " ".join(b.casefold().split())
    if not left or not right:
        return False
    if token_overlap(left, right) >= 0.9:
        return True
    left_tokens = left.split()
    right_tokens = right.split()
    if (
        len(left_tokens) == len(right_tokens)
        and len(left_tokens) > 1
        and left_tokens[:-1] == right_tokens[:-1]
        and left_tokens[-1].removeprefix("#").isdigit()
        and right_tokens[-1].removeprefix("#").isdigit()
    ):
        return True
    shorter, longer = sorted((left, right), key=len)
    if not longer.startswith(shorter):
        return False
    suffix = longer.removeprefix(shorter).lstrip()
    return bool(suffix) and not suffix[0].isalnum()


async def _fuzzy_match_manual(
    session: AsyncSession,
    account_id: uuid.UUID,
    txn_data,
) -> Optional[Transaction]:
    """Try to find a manual transaction that matches the incoming synced one."""
    date_lo = txn_data.date - timedelta(days=3)
    date_hi = txn_data.date + timedelta(days=3)

    result = await session.execute(
        select(Transaction).where(
            Transaction.account_id == account_id,
            Transaction.external_id.is_(None),
            Transaction.source == "manual",
            Transaction.amount == txn_data.amount,
            Transaction.type == txn_data.type,
            Transaction.date >= date_lo,
            Transaction.date <= date_hi,
        )
    )
    candidates = result.scalars().all()
    if not candidates:
        return None

    best_match = None
    best_score = 0.0
    for candidate in candidates:
        score = token_overlap(
            candidate.original_description or candidate.description,
            txn_data.description,
        )
        if score > best_score:
            best_score = score
            best_match = candidate

    if best_match and best_score >= 0.6:
        return best_match
    return None


def _merge_sync_metadata(
    transaction: Transaction,
    txn_data,
    *,
    replace_external_id: bool = False,
) -> None:
    if transaction.source == "sync" and transaction.date.year <= 1970:
        transaction.date = txn_data.date
        if transaction.effective_date.year <= 1970:
            transaction.effective_date = txn_data.date
    if transaction.source in LOCAL_IMPORT_SOURCES:
        transaction.import_id = None
        replace_external_id = True
    if txn_data.external_id and (replace_external_id or not transaction.external_id):
        transaction.external_id = txn_data.external_id
    if txn_data.raw_data:
        if not transaction.raw_data:
            transaction.raw_data = txn_data.raw_data
        elif isinstance(transaction.raw_data, dict) and isinstance(txn_data.raw_data, dict):
            merged = {**transaction.raw_data, **txn_data.raw_data}
            if merged != transaction.raw_data:
                transaction.raw_data = merged
    provider_description = (txn_data.description or "").strip()
    if provider_description and transaction.source == "sync":
        previous_original = transaction.original_description
        if previous_original is None:
            # Migration 071 left provenance NULL for older rows. Without knowing
            # whether the visible text was user-edited, preserve it and store
            # the provider text only as provenance.
            transaction.original_description = provider_description
        elif provider_description != previous_original:
            if (
                not transaction.description_is_rule_managed
                and transaction.description == previous_original
            ):
                transaction.description = provider_description
            transaction.original_description = provider_description
    elif provider_description and transaction.original_description is None:
        transaction.original_description = provider_description
    if txn_data.payee and not transaction.payee:
        transaction.payee = txn_data.payee


def _sync_row_is_frozen(transaction: Transaction, provider_name: str) -> bool:
    """Re-keyed rows retain the same user ownership as exact-ID matches."""
    if transaction.is_ignored or (transaction.sync_metadata or {}).get("pending_manual_override"):
        return True
    if provider_name != "enable_banking" or transaction.source != "sync" or transaction.status != "pending":
        return False

    # Rows edited before ownership markers existed still carry immutable bank
    # evidence. A verified identity may suppress a duplicate without replacing
    # the person's financial fields with the later settlement's values.
    from app.providers.enable_banking import _parse_iso_date

    raw = transaction.raw_data
    if not isinstance(raw, dict):
        return True
    amount_data = raw.get("transaction_amount")
    if not isinstance(amount_data, dict):
        return True
    try:
        source_amount = Decimal(str(amount_data.get("amount", "0"))).copy_abs()
    except InvalidOperation:
        return True
    source_date = (
        _parse_iso_date(raw.get("booking_date"))
        or _parse_iso_date(raw.get("value_date"))
        or _parse_iso_date(raw.get("transaction_date"))
    )
    source_type = "debit" if str(raw.get("credit_debit_indicator") or "").upper() == "DBIT" else "credit"
    return (
        transaction.amount != source_amount
        or transaction.currency != (amount_data.get("currency") or "EUR")
        or transaction.type != source_type
        or transaction.date != source_date
        or transaction.effective_bill_date is not None
        or transaction.transfer_amount_explicit
    )


async def _promote_synced_transaction(
    session: AsyncSession,
    user_id: uuid.UUID,
    transaction: Transaction,
    txn_data,
    *,
    account: Account,
    account_currency: str,
    user_currency: str,
    replace_external_id: bool,
    fetched_rates: FetchedRateCache,
    keep_date: bool = False,
) -> None:
    """Post a pending row with the bank's settled values.

    ``keep_date`` is for a card hold a charge booked days later claimed
    (`_find_claimable_eb_hold`): the row keeps the hold's purchase date.
    """
    _merge_sync_metadata(
        transaction, txn_data, replace_external_id=replace_external_id
    )
    # Posted truth wins outright: a merge would keep pending-only keys.
    if replace_external_id and transaction.source == "sync" and txn_data.raw_data:
        transaction.raw_data = txn_data.raw_data
    if not keep_date:
        transaction.date = txn_data.date
    transaction.status = "posted"
    apply_effective_date(transaction, account)
    previous_amount = transaction.amount
    await take_incoming_amount(session, transaction, txn_data.amount)

    await _stamp_synced_primary_amount(
        session, user_id, transaction,
        _bank_converted_amount(txn_data, account_currency, user_currency),
        fetched_rates=fetched_rates,
    )
    await category_line_service.rescale_lines(session, transaction, previous_amount)


#: How many days an Enable Banking card hold can precede the booked entry that
#: settles it. PayPal books the charge a day after the hold; banks take one to
#: three banking days, which a weekend stretches to five calendar days.
_EB_HOLD_SETTLE_DAYS = 5


def _eb_counterparty(raw: dict) -> Optional[str]:
    """The party across from the account on an Enable Banking entry: the
    creditor of a debit, the debtor of a credit. The near side is usually the
    account holder, the same on every entry, so it never names a merchant."""
    indicator = raw.get("credit_debit_indicator")
    if not isinstance(indicator, str):
        return None
    party = {"DBIT": "creditor", "CRDT": "debtor"}.get(indicator.upper())
    if party is None:
        return None
    party_obj = raw.get(party)
    name = party_obj.get("name") if isinstance(party_obj, dict) else None
    if not isinstance(name, str) or not name.strip():
        name = raw.get(f"{party}_name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _eb_remittance(raw: dict) -> Optional[str]:
    """The remittance text of an Enable Banking entry, its parts joined."""
    remittance = raw.get("remittance_information")
    if isinstance(remittance, list):
        remittance = " ".join(str(part) for part in remittance if part)
    return remittance.strip() if isinstance(remittance, str) and remittance.strip() else None


def _eb_same_merchant(held: dict, booked: dict) -> bool:
    """Whether two Enable Banking payloads name the same merchant.

    Each field is compared only with the same field on the other entry: the
    counterparty with the counterparty, the remittance text with the
    remittance text. Every one of them that both entries carry has to match,
    so a counterparty on both decides even when the remittance text agrees,
    and the two have to share at least one. `additional_information` is left
    out: it often names the kind of entry ("VISA DEBIT PURCHASE") rather than
    the merchant, and would pair two purchases of the same amount at
    different shops.

    Read from the payloads rather than from the rows, so a description the
    user edited, or the provider's "Transaction" placeholder for an entry
    that names nothing, never counts as a merchant.
    """
    shared = False
    for read in (_eb_counterparty, _eb_remittance):
        held_text, booked_text = read(held), read(booked)
        if held_text is None or booked_text is None:
            continue
        if token_overlap(held_text, booked_text) < 0.7:
            return False
        shared = True
    return shared


#: Payload fields a bank may rewrite when it fills in a late entry_reference.
_EB_LATE_REF_REWRITTEN = frozenset({"entry_reference", "remittance_information"})


def _eb_reworded_entry(held: dict, booked: dict) -> bool:
    """Whether `booked` is `held` with a reference filled in and its
    remittance text reworded.

    A Spanish bank returned a transfer as "TRANSFERENCIA A FAVOR DE ...
    CONCEPTO <concept>" with no reference, then as "TRASPASO: <concept>" with
    one. The two texts share too few words for the description match, but
    every other field of the payload is the same. So the payloads have to
    agree on everything but the reference and the remittance text, and the
    two texts have to end in the same words, covering at least 80% of the
    shorter one: a bank that rewords an entry swaps the label in front and
    keeps the concept the payer typed at the end. Shared words elsewhere are
    not enough, since "CARD PAYMENT AT SHOP ALPHA" and "CARD PAYMENT AT SHOP
    BETA" are two purchases when no creditor field tells them apart.
    """
    def rest(raw: dict) -> dict:
        return {
            key: value for key, value in raw.items()
            if key not in _EB_LATE_REF_REWRITTEN and value is not None
        }

    if rest(held) != rest(booked):
        return False
    held_words = normalize_words(_eb_remittance(held))
    booked_words = normalize_words(_eb_remittance(booked))
    if not held_words or not booked_words:
        return False
    shared_tail = 0
    for held_word, booked_word in zip(reversed(held_words), reversed(booked_words)):
        if held_word != booked_word:
            break
        shared_tail += 1
    return shared_tail / min(len(held_words), len(booked_words)) >= 0.8


def _eb_exact_twin_in_batch(
    held: Transaction, txn_data, incoming_batch: Sequence[TransactionData],
) -> bool:
    """Whether another entry of this fetch matches `held` by description.

    A reworded match (`_eb_reworded_entry`) is weaker than a description
    match, and the entries of a fetch are matched one at a time. Without this
    check, "BETA COFFEE SHOP NEW YORK" processed first would take the held
    "ALPHA COFFEE SHOP NEW YORK" row, its category and its notes, while the
    referenced copy of ALPHA in the same fetch landed as a new row.
    """
    held_text = held.original_description or held.description
    return any(
        other.external_id != txn_data.external_id
        and other.amount == held.amount
        and other.type == held.type
        and other.currency == held.currency
        and other.date == held.date
        and other.status == held.status
        and token_overlap(held_text, other.description) >= 0.9
        for other in incoming_batch
    )


def _eb_entry_date(raw: dict) -> Optional[date]:
    """The date an Enable Banking payload gives its entry, read the way the
    provider reads it: the booking date, else the value date, else the
    transaction date."""
    for key in ("booking_date", "value_date", "transaction_date"):
        value = raw.get(key)
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                continue
    return None


async def _find_settled_eb_hold(
    session: AsyncSession,
    account_id: uuid.UUID,
    txn_data,
    live_external_ids: set[str],
) -> Optional[Transaction]:
    """The pending Enable Banking hold that a booked entry settles, if unique.

    Some institutions (PayPal among them) list a card authorization as a
    pending entry and the charge as a booked entry with a different
    entry_reference, booked a day or more later. The date and reference
    both differ, so neither the pending↔posted twin nor the re-key can pair
    them, and the hold stays behind next to the charge (#1232).

    A held pending row is the booked entry's hold only when it agrees on the
    amount, currency and type, names the same merchant (`_eb_same_merchant`),
    is dated up to `_EB_HOLD_SETTLE_DAYS` before the booked entry, and has
    left the provider's feed: a hold the provider still returns is a purchase
    of its own that has not booked yet. Rows the user ignored stay as they
    are. More than one candidate is ambiguous, so none is chosen.

    Only for Enable Banking connections: `sync_connection` decides that from
    the connection's provider.
    """
    raw = txn_data.raw_data if isinstance(txn_data.raw_data, dict) else {}
    if txn_data.status != "posted":
        return None
    if _eb_counterparty(raw) is None and _eb_remittance(raw) is None:
        return None
    result = await session.execute(
        select(Transaction).where(
            Transaction.account_id == account_id,
            Transaction.source == "sync",
            Transaction.status == "pending",
            Transaction.is_ignored.is_(False),
            Transaction.raw_data.is_not(None),
            Transaction.amount == txn_data.amount,
            Transaction.currency == txn_data.currency,
            Transaction.type == txn_data.type,
            Transaction.date >= txn_data.date - timedelta(days=_EB_HOLD_SETTLE_DAYS),
            Transaction.date <= txn_data.date,
            Transaction.external_id != txn_data.external_id,
        )
    )
    holds = [
        candidate
        for candidate in result.scalars()
        if candidate.external_id not in live_external_ids
        and isinstance(candidate.raw_data, dict)
        and _eb_same_merchant(candidate.raw_data, raw)
    ]
    return holds[0] if len(holds) == 1 else None


async def _find_synced_duplicate(
    session: AsyncSession,
    account_id: uuid.UUID,
    txn_data,
    incoming_external_ids: set[str],
    live_external_ids: Optional[set[str]] = None,
    incoming_batch: Sequence[TransactionData] = (),
) -> Optional[Transaction]:
    """Find an existing row that the incoming `txn_data` is a twin of.

    The `(account_id, external_id)` lookup only catches the case where a
    provider keeps the same id while a row's `status` flips pending→posted.
    It misses two patterns where the same logical operation comes back with
    two different external ids:

    1. The provider re-emits the operation with a new id when its state
       changes — e.g. a scheduled/pending row replaced by a posted row.
       Same account/date/amount/type with statuses differing.
    2. A credit-card installment that lands on the current bill but is also
       still scheduled against the next bill. Two different external ids
       and two different bills, but the same installment fingerprint
       `(purchase_date, number, total, amount, type)`.

    Returns the existing Transaction the caller should reuse; the caller
    decides whether to upgrade its status (pending→posted + swap external_id),
    enrich an imported row, or skip the incoming insert. Synthetic bill-charge rows
    (`bill_charge:*`) are excluded — they have their own idempotency keys.

    ``live_external_ids`` is every id the provider returned for the account
    in this fetch, pending ones included even when the connection does not
    import them. Pass it only for an Enable Banking connection, and leave it
    out when the fetch was cut short. ``incoming_batch`` is the fetch
    `txn_data` came from; it and ``live_external_ids`` let Path 4 match a
    reworded remittance text. A booked entry claiming the pending hold it
    settles is `_find_claimable_eb_hold`, which the caller runs after this.
    """
    # Enable Banking v0.16.3 refined its fallback ID. Find an old key only
    # when the stored bank payload independently produces the new identity;
    # same-date/amount rows with different counterparties must stay separate.
    raw = txn_data.raw_data
    legacy_account_uid: str | None = None
    if isinstance(raw, dict) and "transaction_amount" in raw:
        account = await session.get(Account, account_id)
        connection = (
            await session.get(BankConnection, account.connection_id)
            if account is not None and account.connection_id is not None
            else None
        )
        if connection is not None and connection.provider == "enable_banking" and account:
            from app.providers.enable_banking import (
                legacy_fallback_candidates,
                matches_legacy_fallback,
            )

            account_uid = account.external_id or ""
            old_ids = legacy_fallback_candidates(account_uid, txn_data)
            if old_ids:
                legacy_account_uid = account_uid
                # An old fingerprint can be shared by distinct counterparties
                # or duplicate copies. Verify the payload and claim the oldest
                # matching copy deterministically, without a transaction-date
                # filter: booking can arrive days after the pending row.
                legacy_rows = await session.scalars(
                    select(Transaction).where(
                        Transaction.account_id == account_id,
                        Transaction.source == "sync",
                        Transaction.external_id.in_(old_ids - incoming_external_ids),
                        Transaction.status.in_(
                            ["pending"] if txn_data.status == "pending" else ["pending", "posted"]
                        ),
                    ).order_by(Transaction.created_at, Transaction.id)
                )
                for candidate in legacy_rows:
                    if matches_legacy_fallback(
                        account_uid, candidate.external_id, candidate.raw_data, txn_data,
                        pending_to_posted=(
                            candidate.status == "pending" and txn_data.status == "posted"
                        ),
                    ):
                        return candidate

    def requires_legacy_match(candidate: Transaction) -> bool:
        # Once a stored row is recognized as legacy, only the payload-checked
        # compatibility path above can claim it. Generic description or
        # installment matches must not override its identity/status rejection.
        # Other synced rows and local imports retain their existing fallbacks.
        if legacy_account_uid is None or candidate.source != "sync":
            return False
        from app.providers.enable_banking import is_legacy_fallback

        return is_legacy_fallback(
            legacy_account_uid, candidate.external_id, candidate.raw_data,
        )

    account = await session.get(Account, account_id)
    connection = (
        await session.get(BankConnection, account.connection_id)
        if account is not None and account.connection_id is not None else None
    )
    simplefin = connection is not None and connection.provider == "simplefin"
    if simplefin:
        # Different pending and posted IDs are different observations. A bank
        # can reuse its date-only timestamps for split shipments or repeats.
        # Retire the absent authorization separately, without merging amounts,
        # user edits or annotations into an arbitrarily selected settlement.
        if txn_data.status == "pending":
            return None

    # Path 1: installment fingerprint. Highly specific, so we don't require a
    # description match on top.
    if (
        txn_data.installment_purchase_date is not None
        and txn_data.installment_number is not None
        and txn_data.total_installments is not None
    ):
        result = await session.execute(
            select(Transaction).where(
                Transaction.account_id == account_id,
                Transaction.source == "sync",
                Transaction.installment_purchase_date == txn_data.installment_purchase_date,
                Transaction.installment_number == txn_data.installment_number,
                Transaction.total_installments == txn_data.total_installments,
                Transaction.amount == txn_data.amount,
                Transaction.type == txn_data.type,
                Transaction.external_id != txn_data.external_id,
            )
        )
        for candidate in result.scalars():
            if requires_legacy_match(candidate):
                continue
            if candidate.external_id and candidate.external_id.startswith("bill_charge:"):
                continue
            return candidate

    # Path 2: pending↔posted twin on the same account/date/amount/type. The
    # status differential is the load-bearing signal — without it we'd risk
    # collapsing two genuinely separate transactions that happen to share a
    # day and amount. A light description-similarity check guards against
    # the residual false positive of two different merchants charging the
    # same amount the same day where one is pending and one is posted.
    result = await session.execute(
        select(Transaction).where(
            Transaction.account_id == account_id,
            Transaction.source == "sync",
            Transaction.date == txn_data.date,
            Transaction.amount == txn_data.amount,
            Transaction.type == txn_data.type,
            Transaction.status != txn_data.status,
            Transaction.external_id != txn_data.external_id,
            Transaction.is_ignored.is_(False),
        )
    )
    candidates = [] if simplefin else list(result.scalars())
    for candidate in candidates:
        if requires_legacy_match(candidate):
            continue
        if candidate.external_id and candidate.external_id.startswith("bill_charge:"):
            continue
        if _sync_duplicate_description_match(
            candidate.original_description or candidate.description,
            txn_data.description,
        ):
            return candidate

    # Path 3: exact transaction timestamp fingerprint. Some SimpleFIN
    # bridges finalize pending card charges with a new id, amount, or posted
    # date. The original bank transaction timestamp is the stable identity.
    raw = txn_data.raw_data if isinstance(txn_data.raw_data, dict) else {}
    transacted_at = raw.get("transacted_at")
    if transacted_at is not None and not simplefin:
        result = await session.execute(
            select(Transaction).where(
                Transaction.account_id == account_id,
                Transaction.source == "sync",
                Transaction.type == txn_data.type,
                Transaction.status != txn_data.status,
                Transaction.external_id != txn_data.external_id,
            )
        )
        for candidate in result.scalars():
            if requires_legacy_match(candidate):
                continue
            if candidate.external_id and candidate.external_id.startswith("bill_charge:"):
                continue
            candidate_raw = candidate.raw_data if isinstance(candidate.raw_data, dict) else {}
            if candidate_raw.get("transacted_at") == transacted_at and (
                _sync_duplicate_description_match(
                    candidate_raw.get("description") or candidate.description,
                    txn_data.description,
                )
            ):
                return candidate

    # Path 4: exact posted/transacted timestamp fingerprint. Some SimpleFIN
    # bridges re-key already-posted rows on later pulls, so status does not
    # differ. The raw bank timestamps plus same account/date/amount/type and a
    # near-identical description are specific enough to collapse the re-keyed
    # row while avoiding broad same-day/same-amount merchant dedupe.
    posted = raw.get("posted")
    if posted is not None or transacted_at is not None:
        result = await session.execute(
            select(Transaction).where(
                Transaction.account_id == account_id,
                or_(
                    Transaction.source == "sync",
                    (
                        Transaction.source.in_(LOCAL_IMPORT_SOURCES)
                        & Transaction.raw_data.is_not(None)
                    ),
                ),
                Transaction.date >= txn_data.date - timedelta(days=3),
                Transaction.date <= txn_data.date + timedelta(days=3),
                Transaction.amount == txn_data.amount,
                Transaction.type == txn_data.type,
                Transaction.status == txn_data.status,
                Transaction.external_id != txn_data.external_id,
            )
        )
        for candidate in result.scalars():
            if requires_legacy_match(candidate):
                continue
            if candidate.external_id and candidate.external_id.startswith("bill_charge:"):
                continue
            if candidate.external_id in incoming_external_ids:
                continue
            candidate_raw = candidate.raw_data if isinstance(candidate.raw_data, dict) else {}
            candidate_descriptions = (
                candidate_raw.get("description"), candidate.payee, candidate.description,
            )
            incoming_descriptions = (
                raw.get("description"), txn_data.payee, txn_data.description,
            )
            description_matches = any(
                _sync_duplicate_description_match(left, right)
                for left in candidate_descriptions
                for right in incoming_descriptions
            )
            if (
                candidate_raw.get("posted") == posted
                and candidate_raw.get("transacted_at") == transacted_at
                and description_matches
            ):
                return candidate

    # Path 4: Enable Banking re-key. Its id hashes the entry_reference with
    # the payload, so a richer remittance text or a settled date on a later
    # fetch changes the id of a row we already hold. The reference is not
    # unique on its own (#753), so it must also agree on amount and type
    # within a few days, and the old id must have dropped out of this batch:
    # a sibling sharing the reference that is still returned is its own
    # transaction. More than one candidate is ambiguous, so none is chosen.
    # A pending fetch never takes over a booked row.
    #
    # Some institutions return a booked row with no entry_reference on the day
    # it books and fill one in ("2026-11-03.1") on a later fetch. The first
    # copy was keyed by the payload fingerprint and the second by the
    # reference, so the held row carries no reference to compare. Such a row
    # is the same transaction only when it is a synced row that also agrees on
    # the currency, date, status and description, no row holding the reference
    # itself competes, and it is the single candidate: two identical purchases
    # on one day are ambiguous, and picking one would hang its category and
    # notes on the other. Some banks also reword the remittance text when they
    # fill in the reference; such a row matches when the rest of its payload is
    # unchanged (`_eb_reworded_entry`), the fetch was complete, and no other
    # entry of the fetch matches it by description (`_eb_exact_twin_in_batch`).
    # Known gap: a held row the bank deleted, next to a new entry that agrees
    # on every field and on the ending of its text, is still taken for it.
    #
    # A row that took over the pending hold it settled (Path 6) keeps the
    # hold's date, up to `_EB_HOLD_SETTLE_DAYS` before the booking. It is
    # compared by the date its stored booked payload carries as well, so a
    # later change to that payload still re-keys it.
    entry_ref = raw.get("entry_reference")
    entry_ref = entry_ref.strip() if isinstance(entry_ref, str) else ""
    if entry_ref and entry_ref != "0":
        statement = select(Transaction).where(
            Transaction.account_id == account_id,
            or_(
                Transaction.source == "sync",
                Transaction.source.in_(LOCAL_IMPORT_SOURCES),
            ),
            Transaction.raw_data.is_not(None),
            Transaction.date
            >= txn_data.date - timedelta(days=3 + _EB_HOLD_SETTLE_DAYS),
            Transaction.date <= txn_data.date + timedelta(days=3),
            Transaction.amount == txn_data.amount,
            Transaction.type == txn_data.type,
            Transaction.external_id != txn_data.external_id,
        )
        if txn_data.status == "pending":
            statement = statement.where(Transaction.status == "pending")
        result = await session.execute(statement)
        rekey_matches = []
        late_ref_matches = []
        for candidate in result.scalars():
            if candidate.external_id in incoming_external_ids:
                continue
            if not isinstance(candidate.raw_data, dict):
                continue
            stored_date = _eb_entry_date(candidate.raw_data)
            if abs((candidate.date - txn_data.date).days) > 3 and (
                stored_date is None or abs((stored_date - txn_data.date).days) > 3
            ):
                continue
            candidate_ref = candidate.raw_data.get("entry_reference")
            if isinstance(candidate_ref, str):
                candidate_ref = candidate_ref.strip()
            if candidate_ref == entry_ref:
                rekey_matches.append(candidate)
            elif (
                candidate_ref in (None, "", "0")
                and candidate.source == "sync"
                and candidate.currency == txn_data.currency
                and candidate.date == txn_data.date
                and candidate.status == txn_data.status
                and (
                    token_overlap(
                        candidate.original_description or candidate.description,
                        txn_data.description,
                    ) >= 0.9
                    or (
                        live_external_ids is not None
                        and _eb_reworded_entry(candidate.raw_data, raw)
                        and not _eb_exact_twin_in_batch(
                            candidate, txn_data, incoming_batch,
                        )
                    )
                )
            ):
                late_ref_matches.append(candidate)
        if len(rekey_matches) == 1:
            return rekey_matches[0]
        if not rekey_matches and len(late_ref_matches) == 1:
            return late_ref_matches[0]

    # Path 5: local import history from before the account was connected.
    # Posting lag and statement exports can shift the date or shorten the
    # merchant description, so accept only one exact normalized merchant/payee.
    local_match = await find_unique_transaction_match(
        session,
        account_id,
        txn_data,
        LOCAL_IMPORT_SOURCES,
        unclaimed_only=True,
        exclude_external_ids=incoming_external_ids,
    )
    return local_match


def _is_unverified_legacy_eb_row(
    account_uid: str, candidate: Transaction, txn_data: TransactionData,
) -> bool:
    """Whether `candidate` keeps a pre-v0.16.3 fallback key that only the
    payload-checked legacy match in `_find_synced_duplicate` may claim for
    `txn_data`, which is itself a fallback-keyed entry."""
    if candidate.source != "sync":
        return False
    from app.providers.enable_banking import is_legacy_fallback, legacy_fallback_candidates

    if not legacy_fallback_candidates(account_uid, txn_data):
        return False
    return is_legacy_fallback(account_uid, candidate.external_id, candidate.raw_data)


async def _find_claimable_eb_hold(
    session: AsyncSession,
    account: Account,
    txn_data: TransactionData,
    live_external_ids: set[str],
) -> Optional[Transaction]:
    """The pending hold a booked Enable Banking entry settles, when no other
    match found a row for it (see `_find_settled_eb_hold`). Run only after
    `_find_synced_duplicate` came back empty, so it never takes a row another
    path would have matched."""
    hold = await _find_settled_eb_hold(session, account.id, txn_data, live_external_ids)
    if hold is not None and _is_unverified_legacy_eb_row(
        account.external_id or "", hold, txn_data,
    ):
        return None
    return hold


def _relink_bill(
    transaction: Transaction,
    txn_data,
    account: Account,
    bills_by_external_id: dict[str, CreditCardBill],
) -> None:
    """Point a synced row at the bill the provider says it lands in.

    A manual cycle override (effective_bill_date) wins: bill_id and
    effective_date stay as the user set them. The effective date is
    recomputed even when the bill is unchanged, since a promotion to posted
    has just reset it from the account's cycle alone.
    """
    if not txn_data.bill_external_id or transaction.effective_bill_date is not None:
        return
    bill = bills_by_external_id.get(txn_data.bill_external_id)
    if bill is None:
        return
    if transaction.bill_id != bill.id:
        transaction.bill_id = bill.id
    apply_effective_date(transaction, account, bill_due_date=bill.due_date)


# Card purchases post within a bill cycle or two. A row still pending after
# this is one the provider keeps pending, and counting it would widen every sync.
_PENDING_REREAD_WINDOW = timedelta(days=90)

# A row can post on an earlier date than it had while pending: Itaú dates a
# scheduled installment on the bill's due day (the 15th) and the posted charge
# on the day it lands in the bill, the 9th of the month before. The re-read
# starts this far back so it still returns them; the widest gap seen is 37 days.
_PENDING_REREAD_LOOKBACK = timedelta(days=45)

# Rows older than the window can still post: a first sync may get the whole
# history as PENDING and see it corrected later. Once per interval, a
# connection's re-read also covers them. Some rows stay pending at the provider
# for good, so this is not done on every sync.
_PENDING_CATCH_UP_INTERVAL = timedelta(days=30)
PENDING_CATCH_UP_KEY = "pending_catch_up_at"


def _pending_catch_up_due(settings: Optional[dict], now: datetime) -> bool:
    value = (settings or {}).get(PENDING_CATCH_UP_KEY)
    if not isinstance(value, str):
        return True
    try:
        last_catch_up = datetime.fromisoformat(value)
    except ValueError:
        return True
    if last_catch_up.tzinfo is None:
        return True
    return now - last_catch_up >= _PENDING_CATCH_UP_INTERVAL


async def _unseen_pending_dates(
    session: AsyncSession,
    account_id: uuid.UUID,
    seen_external_ids: set[str],
    *,
    include_old: bool = False,
) -> dict[str, date]:
    """Pending synced rows the provider did not return, by external id.

    Future-dated rows are skipped (scheduled installments are pending by
    design), and so are ignored rows, which sync never changes. Rows older
    than the window are only included with ``include_old`` (the catch-up).
    """
    today = app_today()
    conditions = [
        Transaction.account_id == account_id,
        Transaction.source == "sync",
        Transaction.status == "pending",
        Transaction.is_ignored.is_(False),
        Transaction.date <= today,
    ]
    if not include_old:
        conditions.append(Transaction.date >= today - _PENDING_REREAD_WINDOW)
    result = await session.execute(
        select(Transaction.external_id, Transaction.date).where(*conditions)
    )
    return {
        external_id: tx_date
        for external_id, tx_date in result.all()
        if external_id not in seen_external_ids
    }


async def _promote_settled_pending(
    session: AsyncSession,
    account: Account,
    txn_data,
    incoming_external_ids: set[str],
    unseen_pending: dict[str, date],
    bills_by_external_id: dict[str, CreditCardBill],
    *,
    provider_name: str,
    user_id: uuid.UUID,
    account_currency: str,
    user_currency: str,
    fetched_rates: FetchedRateCache,
) -> Optional[str]:
    """Promote the pending row a posted `txn_data` settles, as the listing does.

    Only for the re-read by date, which also returns rows the user deleted or
    merged long ago, so this never inserts or merges. The twin match has no
    date bound, so only the rows the re-read was issued for are promoted.

    Returns the promoted row's external id, or None when nothing changed.
    """
    existing = await session.execute(
        select(Transaction)
        .where(
            Transaction.account_id == account.id,
            Transaction.external_id == txn_data.external_id,
        )
        .order_by(Transaction.created_at)
    )
    transaction = existing.scalars().first()
    rekeyed = transaction is None
    if rekeyed:
        transaction = await _find_synced_duplicate(
            session, account.id, txn_data, incoming_external_ids
        )
    if (
        transaction is None
        or transaction.source != "sync"
        or transaction.status != "pending"
        or transaction.external_id not in unseen_pending
        or _sync_row_is_frozen(transaction, provider_name)
    ):
        return None
    await _promote_synced_transaction(
        session, user_id, transaction, txn_data,
        account=account,
        account_currency=account_currency,
        user_currency=user_currency,
        replace_external_id=rekeyed,
        fetched_rates=fetched_rates,
    )
    _relink_bill(transaction, txn_data, account, bills_by_external_id)
    return transaction.external_id


async def _cleanup_phantom_duplicates(
    session: AsyncSession,
    account_ids: list[uuid.UUID],
    live_external_ids: Optional[dict[uuid.UUID, set[str]]] = None,
    skip_account_ids: Optional[set[uuid.UUID]] = None,
    promoted_external_ids: Optional[dict[uuid.UUID, set[str]]] = None,
) -> set[uuid.UUID]:
    """Delete synced transactions that are phantom duplicates.

    Some providers (or sandbox data) report the same payment twice with
    different external ids on adjacent days. Transfer detection matches the
    real one against the counterpart in another account; the phantom remains
    orphaned.

    We delete an unpaired synced tx when it has a *paired* sibling in the same
    account with: same amount, same type, near-identical description, dated
    within ±1 day. The pairing of the sibling is the safety signal that lets
    us distinguish the duplicate from a legitimate same-day repeat (e.g. two
    real Uber rides for the same fare).

    Scoped to the accounts the run actually synced, never every account on the
    connection: this deletes rows, and an account the allowlist excludes (or
    the user closed) must come out of a sync exactly as it went in.

    ``live_external_ids`` maps an account id to the transaction ids the
    provider returned for it in this sync. For an account in that map, a
    sibling with a different id marks the tx as a phantom only when exactly
    one of the two ids is still returned, which is what a re-keyed twin looks
    like. Both ids returned means two real payments (e.g. two same-day Pix of
    the same amount, #1038). Neither returned means the rows have left the
    provider's fetch window, so this sync has no evidence about them, and they
    were already checked while they were in it.

    ``skip_account_ids`` are accounts whose fetch this sync was cut short
    (e.g. an authorization failure mid-window). Their live set is partial,
    so a row from the missing part would look like a re-keyed twin of a
    returned sibling; they are left alone until a complete fetch.

    ``promoted_external_ids`` holds the ids of the rows the pending re-read
    promoted in this sync. The provider just reported them as posted, so they
    are never phantoms. The other rows the re-read returns get no such pass:
    a provider can keep a stale pending record next to the posted one it
    re-keyed, and the posted twin the regular listing inserted is still a
    phantom of the row already here.

    Returns the ids of the accounts that lost a row, so the caller can
    reconcile their opening balances against what is left.
    """
    if skip_account_ids:
        account_ids = [a for a in account_ids if a not in skip_account_ids]
    if not account_ids:
        return set()

    unmatched_result = await session.execute(
        select(Transaction).where(
            Transaction.account_id.in_(account_ids),
            Transaction.source == "sync",
            Transaction.transfer_pair_id.is_(None),
            Transaction.status == "posted",
            Transaction.is_ignored.is_(False),
        )
    )
    unmatched = list(unmatched_result.scalars().all())

    touched: set[uuid.UUID] = set()
    for tx in unmatched:
        live = (live_external_ids or {}).get(tx.account_id)
        promoted = (promoted_external_ids or {}).get(tx.account_id, set())
        date_lo = tx.date - timedelta(days=1)
        date_hi = tx.date + timedelta(days=1)
        sibling_result = await session.execute(
            select(Transaction).where(
                Transaction.account_id == tx.account_id,
                Transaction.source == "sync",
                Transaction.amount == tx.amount,
                Transaction.type == tx.type,
                Transaction.date >= date_lo,
                Transaction.date <= date_hi,
                Transaction.transfer_pair_id.is_not(None),
                Transaction.status == "posted",
                Transaction.is_ignored.is_(False),
                Transaction.id != tx.id,
            )
        )
        for sibling in sibling_result.scalars():
            if tx.external_id != sibling.external_id and tx.external_id in promoted:
                continue
            if (
                live is not None
                and tx.external_id != sibling.external_id
                and (tx.external_id in live) == (sibling.external_id in live)
            ):
                continue
            if token_overlap(
                sibling.original_description or sibling.description,
                tx.original_description or tx.description,
            ) >= 0.9:
                await session.delete(tx)
                touched.add(tx.account_id)
                break

    return touched


# Finance-charge `additionalInfo` strings that Pluggy emits but which would
# double-count if materialized as transactions:
#   - "Saldo em atraso" — the prior bill's unpaid balance carried into this
#     bill. It's an informational line, not part of bill.totalAmount.
#   - "Juros de dívida encerrada" — an aggregate that equals the sum of the
#     detailed late-charge items (IOF + LATE_PAYMENT_*) Pluggy ALSO lists
#     separately on the same bill.
# Matched case-insensitively after stripping whitespace. Issue #92.
_FINANCE_CHARGE_SKIP_INFO = {
    "saldo em atraso",
    "juros de dívida encerrada",
}


def _compute_bill_close_date(due_date: date, close_day: Optional[int]) -> date:
    """The cycle's close date — when the bank snapshots the bill and applies
    finance charges. We don't get this from the provider directly; we derive
    it as "the most recent statement_close_day on or before the bill's
    due_date" (a few days before due, the typical close-to-due gap). When
    the account has no close_day configured we fall back to due_date.

    Why this date, not due_date: charges accrue at close, before the user
    pays the bill. Stamping them at due_date makes them appear chronologically
    after the payment in the tx list, which doesn't match real bank semantics.
    """
    import calendar  # local — not used elsewhere in this file
    if not close_day:
        return due_date
    last = calendar.monthrange(due_date.year, due_date.month)[1]
    same_month = date(due_date.year, due_date.month, min(close_day, last))
    if same_month <= due_date:
        return same_month
    if due_date.month == 1:
        py, pm = due_date.year - 1, 12
    else:
        py, pm = due_date.year, due_date.month - 1
    plast = calendar.monthrange(py, pm)[1]
    return date(py, pm, min(close_day, plast))


def _describe_finance_charge(type_str: str, additional_info: Optional[str]) -> str:
    """User-facing description for a synthetic finance-charge transaction.

    Pluggy connectors emit human-readable Portuguese strings in
    `additionalInfo`; we prefer those because the bank's own wording is what
    the user expects to see. Fall back to a localized label keyed off the
    enumerated `type` when the info field is absent.
    """
    if additional_info:
        return additional_info.strip()
    return {
        "IOF": "IOF",
        "LATE_PAYMENT_FEE": "Multa por atraso",
        "LATE_PAYMENT_INTEREST": "Juros por atraso",
        "LATE_PAYMENT_REMUNERATIVE_INTEREST": "Juros remuneratórios",
    }.get(type_str, "Encargo")


async def _sync_bill_finance_charges(
    session: AsyncSession,
    user_id: uuid.UUID,
    account: Account,
    bill: CreditCardBill,
    raw_charges: list,
) -> None:
    """Materialize a bill's finance charges (IOF, juros, multa, etc.) as
    synthetic transactions linked to the bill.

    Without this, the cycle's tx sum can't reconcile to bill.total_amount —
    the bank charges these but the provider doesn't always emit them as
    standalone transactions.

    Each synthetic tx has a stable external_id of the form
    `bill_charge:{bill.external_id}:{charge.id}` so re-sync is idempotent and
    self-healing: removed charges are detected and deleted; updated charges
    overwrite in place. Charges matching the double-count patterns above
    (carry-over balance, aggregate of detailed lines) are skipped.
    """
    # date = close (when the bank applied the charge); effective_date stays
    # at bill.due_date so accrual-mode aggregations bucket the same as
    # regular CC purchases for this bill.
    charge_date = _compute_bill_close_date(bill.due_date, account.statement_close_day)

    desired_external_ids: set[str] = set()
    for raw in raw_charges:
        if not isinstance(raw, dict):
            continue
        info = (raw.get("additionalInfo") or "").strip().lower()
        if info in _FINANCE_CHARGE_SKIP_INFO:
            continue
        amount_raw = raw.get("amount")
        try:
            amount = Decimal(str(amount_raw))
        except (ValueError, TypeError, InvalidOperation):
            continue
        if amount == 0:
            continue
        charge_id = raw.get("id")
        if not charge_id:
            continue
        external_id = f"bill_charge:{bill.external_id}:{charge_id}"
        desired_external_ids.add(external_id)

        existing = (await session.execute(
            select(Transaction).where(
                Transaction.account_id == account.id,
                Transaction.external_id == external_id,
            )
        )).scalar_one_or_none()

        description = _describe_finance_charge(
            str(raw.get("type") or ""), raw.get("additionalInfo")
        )

        if existing:
            await take_incoming_amount(session, existing, abs(amount))
            existing.description = description
            existing.date = charge_date
            existing.effective_date = bill.due_date
            existing.bill_id = bill.id
            existing.raw_data = raw
        else:
            tx = Transaction(
                user_id=user_id,
                workspace_id=account.workspace_id,
                account_id=account.id,
                external_id=external_id,
                description=description,
                amount=abs(amount),
                currency=bill.currency,
                date=charge_date,
                effective_date=bill.due_date,
                type="debit",
                source="sync",
                status="posted",
                raw_data=raw,
                bill_id=bill.id,
            )
            session.add(tx)

    # Drop synthetic charges Pluggy no longer reports for this bill (e.g.
    # the bank reversed an erroneous fee on a re-sync). Real transactions
    # don't share the bill_charge: prefix so they're untouched.
    orphans = (await session.execute(
        select(Transaction).where(
            Transaction.account_id == account.id,
            Transaction.bill_id == bill.id,
            Transaction.external_id.like(f"bill_charge:{bill.external_id}:%"),
        )
    )).scalars().all()
    for tx in orphans:
        if tx.external_id not in desired_external_ids:
            await session.delete(tx)


async def _sync_credit_card_bills(
    session: AsyncSession,
    user_id: uuid.UUID,
    account: Account,
    provider,
    credentials: dict,
) -> dict[str, CreditCardBill]:
    """Fetch and upsert bills for a credit-card account.

    Returns a {external_id: bill} dict so the caller can resolve transaction
    bill_id without N+1 queries. For non-CC accounts or providers that don't
    expose bills, returns an empty dict — the read path then falls back to
    locally-computed cycle math via apply_effective_date.

    Failures are intentionally swallowed (logged at info): a non-regulado
    Pluggy connection 4xx'es here, a temporary API hiccup shouldn't fail
    the whole sync, and the cycle-math fallback already covers the gap.
    """
    if account.type != "credit_card":
        return {}

    try:
        bills_data = await provider.get_bills(credentials, account.external_id)
    except Exception as e:  # noqa: BLE001 — provider failures must not fail sync
        logger.info(
            "Skipping credit-card bills sync for account %s: %s", account.id, e
        )
        return {}

    if not bills_data:
        return {}

    existing = (
        await session.execute(
            select(CreditCardBill).where(CreditCardBill.account_id == account.id)
        )
    ).scalars().all()
    by_external_id: dict[str, CreditCardBill] = {b.external_id: b for b in existing}

    for bd in bills_data:
        bill = by_external_id.get(bd.external_id)
        if bill is None:
            bill = CreditCardBill(
                user_id=user_id,
                account_id=account.id,
                external_id=bd.external_id,
                due_date=bd.due_date,
                total_amount=bd.total_amount,
                currency=bd.currency,
                minimum_payment=bd.minimum_payment,
                raw_data=bd.raw_data,
            )
            session.add(bill)
            by_external_id[bd.external_id] = bill
        else:
            bill.due_date = bd.due_date
            bill.total_amount = bd.total_amount
            bill.currency = bd.currency
            bill.minimum_payment = bd.minimum_payment
            bill.raw_data = bd.raw_data

    await session.flush()

    # Materialize finance charges (IOF, juros, multa, etc.) as transactions
    # linked to each bill so the cycle sum reconciles to bill.total_amount.
    for bd in bills_data:
        bill = by_external_id.get(bd.external_id)
        if bill is None:
            continue
        raw_charges = (bd.raw_data or {}).get("financeCharges")
        if isinstance(raw_charges, list) and raw_charges:
            await _sync_bill_finance_charges(
                session, user_id, account, bill, raw_charges,
            )

    return by_external_id


async def _set_sync_status_if_current(
    session: AsyncSession,
    connection_id: uuid.UUID,
    starting_version: int,
    status: str,
    error_account_id: uuid.UUID | None = None,
    *,
    retry_after: Optional[float] = None,
) -> None:
    """Set the status a sync that ended early leaves.

    Only while no newer sync result has committed (`starting_version`), and
    never over a disconnect: that would bring back a connection the user
    archived and break its reconnect. `retry_after` is the provider's
    Retry-After on a throttled sync, noted as a floor on the scheduler backoff.
    """
    await session.execute(
        update(BankConnection)
        .where(
            BankConnection.id == connection_id,
            BankConnection.sync_state_version == starting_version,
            BankConnection.status != DISCONNECTED_STATUS,
        )
        .values(
            status=status,
            last_sync_error_account_id=error_account_id,
            sync_state_version=BankConnection.sync_state_version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    conn = await session.get(BankConnection, connection_id, populate_existing=True)
    if retry_after is not None and conn is not None and conn.status != DISCONNECTED_STATUS:
        conn.settings = _with_retry_after(conn.settings, retry_after)


# Outcome of a sync that completed without raising, reported to the caller
# so the UI can tell "data read" from "the provider throttled us and nothing
# was pulled" (issue #736). "refreshed" covers the normal path (the data may
# be the provider's cached copy; whether a fresh pull happened is the
# provider's own semantics). "partial" means the provider returned a 429 for
# some accounts: those were skipped, the others were imported and kept.
# "rate_limited" means nothing could be pulled. In both throttled cases
# last_sync_at is left untouched so the next sync retries the same window,
# and the scheduler counts the run as a rate-limited failure in its backoff
# (app.tasks.sync_tasks). Errors (expired session, user action required,
# generic failure) still propagate as exceptions.
SyncOutcome = Literal["refreshed", "partial", "rate_limited"]

# Key in BankConnection.settings where the scheduler keeps its backoff state
# for connections whose scheduled syncs keep failing (app.tasks.sync_tasks).
# Any fully successful sync clears it, so a manual sync that works ends the
# backoff; a throttled one ("partial" or "rate_limited") leaves it in place.
SYNC_BACKOFF_KEY = "sync_backoff"
# Inside the backoff state: the earliest time the provider's Retry-After lets
# the scheduler try again. A floor on the backoff, never a lock by itself: it
# only counts once a scheduled sync has failed.
RETRY_NOT_BEFORE_KEY = "retry_not_before"
# A bogus Retry-After must not park a connection for days.
RETRY_AFTER_MAX = timedelta(hours=24)
# Enable Banking advises resuming no earlier than 6 hours after a 429.
RATE_LIMITED_RETRY_DEFAULT = timedelta(hours=6)


def _with_retry_after(settings: Optional[dict], retry_after: Optional[float]) -> dict:
    """Settings with the provider's Retry-After noted in the backoff state."""
    current = dict(settings or {})
    if retry_after is None:
        return current
    # Clamp before building the timedelta: a huge finite Retry-After would
    # overflow timedelta itself and escape the sync as an OverflowError.
    seconds = min(max(retry_after, 0.0), RETRY_AFTER_MAX.total_seconds())
    not_before = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    previous = current.get(SYNC_BACKOFF_KEY)
    state = dict(previous) if isinstance(previous, dict) else {}
    # A shorter Retry-After (say, from a manual sync an hour later) must not
    # pull an earlier, longer floor forward: the bank's first answer stands.
    existing = _parse_aware_datetime(state.get(RETRY_NOT_BEFORE_KEY))
    if existing is not None and existing > not_before:
        not_before = existing
    state[RETRY_NOT_BEFORE_KEY] = not_before.isoformat()
    current[SYNC_BACKOFF_KEY] = state
    return current


def _parse_aware_datetime(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _throttle_retry_after(
    provider: object,
    throttled_accounts: set[str],
    retry_after: Optional[float],
    throttled_without_retry_after: bool = False,
) -> Optional[float]:
    """Retry-After to note when a sync fails for another reason after a 429
    on some account. The failure decides the status, but each 429 still sets
    a floor on the next scheduled try: its own Retry-After, or the usual 6
    hours when it gave none. A 429 can be noted by the provider (account
    discovery) or by the sync itself (transaction fetch); the longest wins,
    so a short Retry-After on one account never undercuts the 6 hours another
    account's bare 429 implies."""
    default = RATE_LIMITED_RETRY_DEFAULT.total_seconds()
    floors: list[float] = []
    if throttled_accounts:
        if retry_after is not None:
            floors.append(retry_after)
        if retry_after is None or throttled_without_retry_after:
            floors.append(default)
    throttled = getattr(provider, "rate_limited_account_ids", None)
    if isinstance(throttled, (set, frozenset, list, tuple)) and throttled:
        provider_retry_after = getattr(provider, "rate_limit_retry_after", None)
        has_header = isinstance(provider_retry_after, (int, float))
        if has_header:
            floors.append(float(provider_retry_after))
        if not has_header or (
            getattr(provider, "rate_limited_without_retry_after", False) is True
        ):
            floors.append(default)
    return max(floors) if floors else None


class ConnectionSyncValidationError(ValueError):
    """Required local connection state is missing; its message is safe for clients."""


async def _rollback_sync_attempt(
    session: AsyncSession, savepoint: AsyncSessionTransaction,
) -> None:
    if savepoint.is_active or session.in_nested_transaction():
        # Undo imported data while retaining the outer connection row lock.
        await savepoint.rollback()
    else:
        # A failure in the refresh after a successful commit has no savepoint;
        # the version guard still protects that newer committed result.
        await session.rollback()


async def sync_connection(
    session: AsyncSession,
    connection_id: uuid.UUID,
    workspace_id: uuid.UUID,
    requesting_user_id: uuid.UUID,
    trigger_provider_refresh: bool = False,
    provider: BankProvider | None = None,
) -> tuple[BankConnection, int, SyncOutcome]:
    """Sync a connection from its provider.

    `provider` is given for a plugin's connection: the plugin already fetched,
    and the provider serves what it returned (`app.plugins.provider`). Its
    credentials stay sealed with the plugin connection, so there are none here.
    """
    # Manual and scheduled syncs can overlap. Lock the connection row for the
    # transaction so every caller shares one serialization boundary.
    connection = await get_connection(
        session, connection_id, workspace_id, for_update=True
    )
    if not connection:
        raise ConnectionSyncValidationError("Connection not found")
    if connection.status == DISCONNECTED_STATUS:
        raise ConnectionDisconnectedError(
            "This connection was disconnected. Reconnect it to sync again."
        )
    if provider is None and is_plugin_provider(connection.provider):
        raise ProviderNotConfiguredError("This connection is synced by its plugin.")
    if provider is None and not connection.credentials:
        raise ConnectionSyncValidationError("Credentials not found")

    sync_start_status = connection.status
    sync_start_version = connection.sync_state_version
    sync_start_error_account_id = connection.last_sync_error_account_id

    # Authorization is workspace-scoped and happens before this service is
    # called. Data imported from a bank connection, however, must always be
    # owned by the user who owns that connection — not by whichever workspace
    # member clicked Sync. Mixing those identities creates duplicate holdings
    # and wallets because provider external IDs are unique per user.
    if requesting_user_id != connection.user_id:
        logger.info(
            "Connection %s sync requested by workspace member %s; importing as owner %s",
            connection.id,
            requesting_user_id,
            connection.user_id,
        )
    user_id = connection.user_id

    conn_settings = connection.settings or {}
    payee_source = conn_settings.get("payee_source", "auto")
    import_pending = conn_settings.get("import_pending", True)
    use_provider_cats = await admin_service.use_provider_categories(session)
    syncing_account_id: uuid.UUID | None = None

    # Resolve the provider before the error-handling block: an unregistered
    # provider is a server misconfiguration, and the catch-all below would
    # wrongly stamp the (healthy) connection with status="error".
    try:
        provider = provider or get_provider(connection.provider, settings=await resolve_settings(session))
    except ValueError as exc:
        raise ProviderNotConfiguredError(
            f"Provider '{connection.provider}' is not configured. "
            "Ask an administrator to configure it in Settings or through the environment."
        ) from exc

    sync_savepoint = await session.begin_nested()
    refresh_requires_user_action = False
    # Throttled accounts and the longest Retry-After seen, set before the try
    # so a later failure in the run still notes them as the retry floor.
    rate_limited_accounts: set[str] = set()
    retry_after: Optional[float] = None
    # Whether some 429 in this run came without a Retry-After: that throttle
    # still implies the 6-hour default, whatever the others asked for.
    throttled_without_retry_after = False
    try:
        # Refresh credentials if needed
        credentials = await provider.refresh_credentials(connection.credentials or {})
        connection.credentials = credentials

        # Backfill the institution logo for connections linked before logo
        # capture existed. Best-effort: a failure here must never break sync.
        if not connection.logo_url:
            try:
                logo = _clean_logo_url(await provider.get_institution_logo(credentials))
                if logo:
                    connection.logo_url = logo
            except Exception:
                logger.warning(
                    "Failed to backfill logo for connection %s", connection.id,
                    exc_info=True,
                )

        # When the caller asks for fresh data (typically a user-initiated
        # manual sync), ask the provider to pull from the bank before we
        # read. Providers that don't expose an on-demand refresh return
        # "skipped" via the default implementation and we proceed normally.
        if trigger_provider_refresh:
            connection.settings = {
                **conn_settings,
                "last_provider_refresh_at": datetime.now(timezone.utc).isoformat(),
            }
            outcome = await provider.trigger_refresh(credentials)
            if outcome == "needs_user_action":
                # Surfacing reconnect immediately is better than silently
                # reading stale data the user knows is stale.
                connection.status = "error"
                connection.last_sync_error_account_id = None
                connection.sync_state_version += 1
                await session.commit()
                # This early exit commits only refresh metadata under the lock.
                refresh_requires_user_action = True
                raise ProviderUserActionRequired(
                    "Provider needs the user to reconnect before fetching fresh data",
                    code="credentials_invalid",
                )
            # "refreshed", "skipped", or "failed" all fall through to a read.
            # On "failed" we read whatever cached copy the provider has —
            # better than aborting the entire sync over a transient hiccup.

        # Update accounts
        user = await session.get(User, user_id)
        user_currency = user.primary_currency if user else get_settings().default_currency
        new_tx_ids: list[uuid.UUID] = []
        fetched_rates: FetchedRateCache = {}
        synced_account_rows: list[Account] = []
        moved_account_ids: list[uuid.UUID] = []
        merged_count = 0
        live_txn_external_ids: dict[uuid.UUID, set[str]] = {}
        incomplete_fetch_account_ids: set[uuid.UUID] = set()
        unsafe_cleanup_account_ids: set[uuid.UUID] = set()
        promoted_txn_external_ids: dict[uuid.UUID, set[str]] = {}
        # `is True`: a provider without the property, or a test double, never
        # re-reads by date.
        rereads_by_date = getattr(provider, "rereads_by_date", False) is True
        pending_catch_up = (
            rereads_by_date
            and connection.last_sync_at is not None
            and _pending_catch_up_due(conn_settings, datetime.now(timezone.utc))
        )
        pending_catch_up_failed = False
        provider_accounts = await provider.get_accounts(credentials)
        # A provider may note what it learned about the accounts in the
        # credentials (Enable Banking caches account details for background
        # syncs). It updates the dict in place, which the JSON column does not
        # notice on its own.
        connection.credentials = credentials
        flag_modified(connection, "credentials")
        # Accounts the provider skipped because it was throttled (429) on
        # their details or balance. They are missing from accounts_data, so
        # their rows are left as they are; the sync checkpoint must not
        # advance past them, and the connection backs off before retrying.
        provider_rate_limited = getattr(provider, "rate_limited_account_ids", None)
        rate_limited_accounts = (
            set(provider_rate_limited)
            if isinstance(provider_rate_limited, (set, frozenset, list, tuple))
            else set()
        )
        provider_retry_after = getattr(provider, "rate_limit_retry_after", None)
        retry_after = (
            float(provider_retry_after)
            if isinstance(provider_retry_after, (int, float))
            else None
        )
        transactions_fetched = 0
        _record_seen_accounts(connection, provider_accounts, rate_limited_accounts)
        accounts_data, synced_account_ids = _syncable_accounts(connection, provider_accounts)
        # Built from every provider account, not the allowlisted subset: an
        # excluded account still owns its id, and a bank still served only for
        # excluded accounts was not re-linked.
        incoming_external_ids = {acc.external_id for acc in provider_accounts}
        incoming_institution_external_ids = frozenset(
            acc.institution_external_id
            for acc in provider_accounts
            if acc.institution_external_id
        )
        # Holdings are fetched before the accounts so each account balance
        # knows whether its auto-invested portion is counted as an asset
        # (issue #843). Without a payload (asset sync off or a failed fetch)
        # the deduction is decided from the live synced assets already stored.
        holdings_fetch: Optional[_HoldingsFetch] = None
        if _sync_assets_enabled(conn_settings):
            holdings_fetch = await _fetch_holdings(session, connection, credentials, provider)
        auto_invested_deducted = await _resolve_auto_invested_deductions(
            session,
            connection,
            holdings_fetch.holdings if holdings_fetch is not None else None,
            provider_accounts,
            synced_account_ids,
        )
        institution_cache: dict[str, Institution] = {}
        for acc_data in accounts_data:
            syncing_account_id = None
            institution = await _resolve_institution(
                session, connection.id, institution_cache, acc_data
            )
            account = await _find_existing_connected_account(
                session,
                connection,
                acc_data,
                institution,
                incoming_external_ids,
                incoming_institution_external_ids,
            )

            if account is not None:
                syncing_account_id = account.id
                account.user_id = user_id
                account.workspace_id = connection.workspace_id
                await session.execute(
                    update(Transaction)
                    .where(
                        Transaction.account_id == account.id,
                        Transaction.source == "sync",
                    )
                    .values(user_id=user_id, workspace_id=connection.workspace_id)
                )
                moved_account_ids.append(account.id)
                await session.execute(
                    update(TransactionCategoryLine)
                    .where(
                        TransactionCategoryLine.transaction_id.in_(
                            select(Transaction.id).where(
                                Transaction.account_id == account.id,
                                Transaction.source == "sync",
                            )
                        )
                    )
                    .values(workspace_id=connection.workspace_id)
                )
                await session.execute(
                    update(CreditCardBill)
                    .where(CreditCardBill.account_id == account.id)
                    .values(user_id=user_id, workspace_id=connection.workspace_id)
                )

            # Honor user intent: a closed connected account stays closed and is
            # not touched by sync. The row is left alone (no balance/name
            # rewrite, no new transactions) but the connection link is kept so
            # the next sync still finds it here instead of creating a duplicate
            # active account (issue #90). Its institution pointer still follows
            # the provider, though — otherwise a renamed org's abandoned row
            # would stay pinned forever and keep a single-bank link presenting
            # as multi-institution.
            if account and account.is_closed:
                if institution is not None and account.institution_id != institution.id:
                    account.institution_id = institution.id
                continue

            if account:
                # Normalize the provider sign using the account's CURRENT type,
                # which reflects any user override (sync never rewrites `type`).
                # SimpleFIN reports card debt as negative under a "checking"
                # label; once the user overrides the type to credit_card the
                # downstream sites negate it, so store positive-for-debt to keep
                # them provider-agnostic and avoid double-counting. A loan reads
                # the same way whatever the provider called the account.
                if acc_data.balance is not None:
                    account.balance = _provider_to_internal_balance(
                        connection.provider,
                        account.type,
                        _account_balance(acc_data, acc_data.balance, auto_invested_deducted),
                    )
                account.name = acc_data.name
                # Backfills existing accounts on their next sync. Only written
                # when the provider actually returns an identifier, so a payload
                # that intermittently omits it can't blank out a known mask.
                if acc_data.masked_number is not None:
                    account.masked_number = acc_data.masked_number
                # Backfills existing accounts on their next sync, same rule as
                # masked_number: provider-owned, and a payload that omits it
                # must not blank a value that is already known.
                if acc_data.stable_external_id is not None:
                    account.stable_external_id = acc_data.stable_external_id
                # Backfills existing accounts on next sync (issue #345).
                if institution is not None:
                    account.institution_id = institution.id
                    # Only when the user hasn't named the account themselves —
                    # never overwrite a manual display_name.
                    if account.display_name is None:
                        account.display_name = institution.name
                if acc_data.type == "credit_card":
                    # Preserve existing CC metadata when the provider doesn't
                    # expose it. Pluggy's creditData fields (limit, close/due
                    # dates, minimum payment, brand/level) are intermittently
                    # null even on connectors that have them elsewhere, and
                    # users may have filled them in manually via the edit
                    # dialog. Treat user input + previously-synced values as
                    # the higher source of truth than a fresh None.
                    if acc_data.credit_limit is not None:
                        account.credit_limit = acc_data.credit_limit
                    if acc_data.statement_close_day is not None:
                        account.statement_close_day = acc_data.statement_close_day
                    if acc_data.payment_due_day is not None:
                        account.payment_due_day = acc_data.payment_due_day
                    if acc_data.minimum_payment is not None:
                        account.minimum_payment = acc_data.minimum_payment
                    if acc_data.card_brand is not None:
                        account.card_brand = acc_data.card_brand
                    if acc_data.card_level is not None:
                        account.card_level = acc_data.card_level
                    account.shared_balance_group = (
                        f"{connection.id}:{acc_data.shared_balance_group}"
                        if acc_data.shared_balance_group else None
                    )
            else:
                if acc_data.balance is None:
                    continue
                is_cc = acc_data.type == "credit_card"
                account = Account(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    connection_id=connection.id,
                    external_id=acc_data.external_id,
                    stable_external_id=acc_data.stable_external_id,
                    name=acc_data.name,
                    display_name=institution.name if institution else None,
                    masked_number=acc_data.masked_number,
                    type=acc_data.type,
                    balance=_provider_to_internal_balance(
                        connection.provider,
                        acc_data.type,
                        _account_balance(acc_data, acc_data.balance, auto_invested_deducted),
                    ),
                    currency=acc_data.currency,
                    credit_limit=acc_data.credit_limit if is_cc else None,
                    statement_close_day=acc_data.statement_close_day if is_cc else None,
                    payment_due_day=acc_data.payment_due_day if is_cc else None,
                    minimum_payment=acc_data.minimum_payment if is_cc else None,
                    card_brand=acc_data.card_brand if is_cc else None,
                    card_level=acc_data.card_level if is_cc else None,
                    shared_balance_group=(
                        f"{connection.id}:{acc_data.shared_balance_group}"
                        if acc_data.shared_balance_group else None
                    ),
                    institution_id=institution.id if institution else None,
                )
                session.add(account)
                await session.flush()

            await _sync_reserved_balances(session, account, acc_data)

            syncing_account_id = account.id
            synced_account_rows.append(account)

            # Fetch the bills feed before transactions so transaction → bill
            # FK resolution happens in-memory (no N+1). Empty dict for non-CC
            # accounts or providers without /bills.
            bills_by_external_id = await _sync_credit_card_bills(
                session, user_id, account, provider, credentials
            )

            # Fetch and sync transactions. The 14-day rewind is on Pluggy's
            # `createdAt` (when their row was inserted), so it picks up rows
            # Pluggy ingested late but backdated. A row keeps its `createdAt`
            # when it posts, so pending rows older than the window are
            # re-read by date after this loop. Dedup on external_id below
            # handles overlap cheaply.
            since = (
                connection.last_sync_at.date() - timedelta(days=14)
                if connection.last_sync_at
                else None
            )
            if connection.provider == "simplefin":
                since = await pending_recheck_since(
                    session, account.id, since, now=datetime.now(timezone.utc),
                )
            try:
                transactions_data = await provider.get_transactions(
                    credentials, acc_data.external_id, since, payee_source=payee_source
                )
            except ProviderRateLimited as exc:
                # The provider throttled this account's transaction fetch
                # (PSD2 caps are per resource). Skip its transactions for this
                # run instead of rolling back the accounts already imported:
                # its existing transactions are left intact and the checkpoint
                # below does not advance, so the next sync retries the same
                # window. The balance above did come through; the opening
                # balance is reconciled with the other synced accounts after
                # the holdings sync.
                rate_limited_accounts.add(acc_data.external_id)
                if exc.retry_after is not None:
                    retry_after = max(retry_after or 0.0, exc.retry_after)
                else:
                    throttled_without_retry_after = True
                logger.warning(
                    "Sync %s: provider rate-limited transactions for account %s; "
                    "skipping it for this run",
                    connection_id,
                    acc_data.external_id,
                )
                continue
            transactions_fetched += 1
            if connection.provider == "simplefin":
                windows = getattr(provider, "transaction_coverage", None)
                coverage = windows.get(acc_data.external_id) if isinstance(windows, dict) else None
                if not isinstance(coverage, TransactionCoverage):
                    unsafe_cleanup_account_ids.add(account.id)
                await reconcile_pending(
                    session, account.id, transactions_data,
                    coverage if isinstance(coverage, TransactionCoverage) else None,
                )

            # Every id the provider returned, pending ones included, so the
            # phantom cleanup still sees a pending row the filter below skips.
            live_txn_external_ids[account.id] = {
                txn.external_id for txn in transactions_data
            }
            incomplete = getattr(provider, "incomplete_account_ids", None)
            if isinstance(incomplete, set) and acc_data.external_id in incomplete:
                incomplete_fetch_account_ids.add(account.id)
            if not import_pending:
                transactions_data = [t for t in transactions_data if t.status != "pending"]

            incoming_txn_external_ids = {txn.external_id for txn in transactions_data}
            # A booked entry can claim the pending hold it settles only on an
            # Enable Banking connection (Path 6 of `_find_synced_duplicate`).
            # A fetch cut short, or one whose pages stopped before the last,
            # can't tell a pending hold that left the feed from one it never
            # reached, so it claims no holds.
            truncated = getattr(provider, "truncated_account_ids", None)
            partial_feed = account.id in incomplete_fetch_account_ids or (
                isinstance(truncated, set) and acc_data.external_id in truncated
            )
            claims_holds = connection.provider == "enable_banking" and not partial_feed
            hold_live_ids = live_txn_external_ids[account.id] if claims_holds else None
            for txn_data in transactions_data:
                existing = await session.execute(
                    select(Transaction)
                    .where(
                        Transaction.account_id == account.id,
                        Transaction.external_id == txn_data.external_id,
                    )
                    .order_by(Transaction.created_at)
                )
                # `.first()` rather than `.scalar_one_or_none()`: a prior sync
                # race (two overlapping passes both select-then-insert the same
                # external_id before either commits) can leave two rows sharing
                # (account_id, external_id). scalar_one_or_none() would raise
                # MultipleResultsFound and abort the whole connection's sync;
                # we instead reconcile onto the oldest matching row and skip
                # re-inserting, so a stray duplicate is harmless and never grows.
                existing_tx = existing.scalars().first()
                if existing_tx:
                    # User-flagged rows are frozen: skip status/bill drift so
                    # a re-sync can't revive a transaction the user hid.
                    if _sync_row_is_frozen(existing_tx, connection.provider):
                        continue
                    if existing_tx.status == "posted" and txn_data.status == "pending":
                        continue
                    if connection.provider in {"simplefin", "enable_banking"} and existing_tx.status == "pending":
                        if connection.provider == "simplefin" and not pending_matches_bank_evidence(existing_tx):
                            continue  # A legacy financial edit must not be overwritten.
                        if txn_data.status == "pending":
                            # Keep the original authorization snapshot until it
                            # posts. Replacing only raw evidence on an adjusted
                            # hold would make its amount/date look user-edited.
                            continue
                    if (
                        connection.provider == "simplefin"
                        and existing_tx.status == "posted" and txn_data.status == "posted"
                        and txn_data.raw_data is not None
                    ):
                        # Refresh bank evidence rather than retaining pending-only
                        # keys from old snapshots; leave user accounting fields alone.
                        existing_tx.raw_data = txn_data.raw_data
                    if existing_tx.status == "pending" and txn_data.status == "posted":
                        await _promote_synced_transaction(
                            session, user_id, existing_tx, txn_data,
                            account=account,
                            account_currency=acc_data.currency or user_currency,
                            user_currency=user_currency,
                            replace_external_id=True,
                            fetched_rates=fetched_rates,
                        )
                        new_tx_ids.append(existing_tx.id)
                    else:
                        _merge_sync_metadata(existing_tx, txn_data)
                    # Self-heal bill linkage: a tx that pre-dates the bills
                    # feature (or whose bill we hadn't ingested last time)
                    # picks up bill_id + bank-truth effective_date on the
                    # first sync after the bill becomes available. Same
                    # branch covers re-bucketing if the bank moved a tx to
                    # a different bill (e.g. a chargeback).
                    _relink_bill(existing_tx, txn_data, account, bills_by_external_id)
                    continue

                # Pass 2: Fuzzy match against manual transactions
                fuzzy_match = await _fuzzy_match_manual(session, account.id, txn_data)
                if fuzzy_match:
                    if fuzzy_match.is_ignored:
                        continue
                    _merge_sync_metadata(fuzzy_match, txn_data)
                    fuzzy_match.source = "sync"
                    merged_count += 1
                    continue

                # Pass 3: pending↔posted twin (and the credit-card
                # installment variant). When the same logical operation
                # comes back under a new external id with a different
                # status, fingerprint match collapses it instead of letting
                # both rows land.
                synced_dup = await _find_synced_duplicate(
                    session,
                    account.id,
                    txn_data,
                    incoming_txn_external_ids,
                    live_external_ids=hold_live_ids,
                    incoming_batch=transactions_data,
                )
                claimed_hold = False
                if synced_dup is None and hold_live_ids is not None:
                    synced_dup = await _find_claimable_eb_hold(
                        session, account, txn_data, hold_live_ids,
                    )
                    claimed_hold = synced_dup is not None
                if synced_dup:
                    if _sync_row_is_frozen(synced_dup, connection.provider):
                        continue
                    if synced_dup.status == "posted" and txn_data.status == "pending":
                        continue
                    if synced_dup.original_description is None:
                        synced_dup.original_description = txn_data.description
                    if synced_dup.status == "pending" and txn_data.status == "posted":
                        # Posted truth wins: swap in the new id so subsequent
                        # syncs match by external_id and update raw_data.
                        await _promote_synced_transaction(
                            session, user_id, synced_dup, txn_data,
                            account=account,
                            account_currency=acc_data.currency or user_currency,
                            user_currency=user_currency,
                            replace_external_id=synced_dup.source == "sync",
                            fetched_rates=fetched_rates,
                            keep_date=claimed_hold,
                        )
                        new_tx_ids.append(synced_dup.id)
                        _relink_bill(synced_dup, txn_data, account, bills_by_external_id)
                    elif synced_dup.external_id != txn_data.external_id:
                        # Same logical posted row re-keyed by a provider such
                        # as SimpleFIN. Keep the user's row and move its
                        # idempotency key forward instead of inserting a twin.
                        _merge_sync_metadata(
                            synced_dup,
                            txn_data,
                            replace_external_id=synced_dup.source == "sync",
                        )
                    continue

                incoming_currency = (
                    txn_data.currency or acc_data.currency or user_currency
                )
                acct_currency = acc_data.currency or user_currency
                converted = _bank_converted_amount(
                    txn_data, acct_currency, user_currency
                )
                installment_source = await _find_installment_source(
                    session, account.id, txn_data
                )
                category_id = installment_source.category_id if installment_source else None
                # The previous parcel was split: this one starts with the same parts.
                split_source = (
                    installment_source
                    if installment_source is not None and installment_source.has_category_lines
                    else None
                )
                recurring_link_for_split = None
                if category_id is None:
                    category_id = await _match_pluggy_category(
                        session,
                        workspace_id,
                        txn_data.pluggy_category,
                        enabled=use_provider_cats,
                    )

                sync_payee_id = None
                if txn_data.payee:
                    sync_payee_entity = await get_or_create_payee(
                        session,
                        user_id,
                        txn_data.payee,
                        workspace_id=workspace_id,
                    )
                    sync_payee_id = sync_payee_entity.id

                bill = (
                    bills_by_external_id.get(txn_data.bill_external_id)
                    if txn_data.bill_external_id
                    else None
                )
                transaction = Transaction(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    account_id=account.id,
                    external_id=txn_data.external_id,
                    description=txn_data.description,
                    original_description=txn_data.description,
                    amount=txn_data.amount,
                    currency=incoming_currency,
                    date=txn_data.date,
                    type=txn_data.type,
                    source="sync",
                    status=txn_data.status,
                    payee=txn_data.payee,
                    payee_id=sync_payee_id,
                    raw_data=txn_data.raw_data,
                    category_id=None,
                    installment_number=txn_data.installment_number,
                    total_installments=txn_data.total_installments,
                    installment_total_amount=txn_data.installment_total_amount,
                    installment_purchase_date=txn_data.installment_purchase_date,
                    bill_id=bill.id if bill else None,
                )
                apply_effective_date(
                    transaction,
                    account,
                    bill_due_date=bill.due_date if bill else None,
                )
                if split_source is not None:
                    category_line_service.copy_lines_to(transaction, split_source.category_lines)
                preview = await preview_rules_for_transaction(
                    session, user_id, transaction
                )

                # Normalize before recurring reconciliation. A generated
                # placeholder is upgraded in place; otherwise the normalized
                # candidate may fulfill an active definition, which advances so
                # later generation cannot duplicate the occurrence.
                placeholder = (
                    await recurring_match_service.find_placeholder_for_incoming(
                        session,
                        account.id,
                        txn_data.amount,
                        incoming_currency,
                        txn_data.type,
                        txn_data.date,
                        preview.description,
                    )
                )
                if placeholder:
                    if placeholder.is_ignored:
                        continue
                    placeholder.external_id = txn_data.external_id
                    placeholder.source = "sync"
                    placeholder.status = txn_data.status
                    placeholder.raw_data = txn_data.raw_data
                    # Same shape as the import path: fold in the `preview` the
                    # rules already produced from the incoming charge instead of
                    # re-running them against the placeholder, whose description
                    # is the recurring definition's own wording. Existing values
                    # win, the charge fills the empty ones, and only its
                    # provenance is recorded outright.
                    placeholder.original_description = txn_data.description
                    if placeholder.amount != txn_data.amount or converted is not None:
                        # Matched under a tolerance, or the bank said what the
                        # charge cost in the primary currency: the row now
                        # stands for what was actually charged, not for what
                        # the bill expected.
                        expected_amount = placeholder.amount
                        await take_incoming_amount(
                            session, placeholder, txn_data.amount
                        )
                        await _stamp_synced_primary_amount(
                            session, user_id, placeholder, converted,
                            fetched_rates=fetched_rates,
                        )
                        # A split placeholder keeps its split, resized.
                        await category_line_service.rescale_lines(
                            session, placeholder, expected_amount
                        )
                    if placeholder.category_id is None and not placeholder.has_category_lines:
                        placeholder.category_id = preview.category_id or category_id
                    if txn_data.payee and not placeholder.payee:
                        placeholder.payee = txn_data.payee
                    if placeholder.payee_id is None:
                        placeholder.payee_id = preview.payee_id
                    placeholder.notes = merge_notes(
                        placeholder.notes, preview.notes
                    )
                    if preview.is_ignored:
                        placeholder.is_ignored = True
                    merged_count += 1
                    continue

                recurring_link = (
                    await recurring_match_service.find_bill_for_incoming(
                        session,
                        user_id,
                        account.id,
                        txn_data.amount,
                        incoming_currency,
                        txn_data.type,
                        txn_data.date,
                        preview.description,
                    )
                )
                transaction.recurring_transaction_id = (
                    recurring_link.id if recurring_link else None
                )
                recurring_link_for_split = recurring_link
                session.add(transaction)
                await session.flush()
                if recurring_link is not None:
                    recurring_match_service.advance_past(
                        recurring_link, txn_data.date
                    )
                new_tx_ids.append(transaction.id)
                await apply_rules_to_transaction(
                    session, user_id, transaction,
                    skip_category_rules=split_source is not None,
                )
                if recurring_link_for_split is not None:
                    # A split bill hands its parts to the charge it claims.
                    await category_line_service.apply_template_split(
                        session, transaction, recurring_link_for_split
                    )
                if transaction.category_id is None and not transaction.has_category_lines:
                    transaction.category_id = category_id

                await _stamp_synced_primary_amount(
                    session, user_id, transaction, converted,
                    fetched_rates=fetched_rates,
                )

            # Re-read by date the pending rows the listing above no longer returns.
            # The first sync lists everything, so it never needs this.
            seen_ids = live_txn_external_ids[account.id]
            unseen_pending = (
                await _unseen_pending_dates(
                    session, account.id, seen_ids, include_old=pending_catch_up
                )
                if since is not None and rereads_by_date
                else {}
            )
            reread: list[TransactionData] = []
            if unseen_pending:
                try:
                    reread = await provider.get_transactions_by_date(
                        credentials,
                        acc_data.external_id,
                        min(unseen_pending.values()) - _PENDING_REREAD_LOOKBACK,
                        payee_source=payee_source,
                    )
                except Exception as e:  # noqa: BLE001 - a failed re-read only delays promotion
                    pending_catch_up_failed = True
                    logger.warning(
                        "Skipping pending re-read for account %s: %s", account.id, e
                    )
            if reread:
                # Not added to live_txn_external_ids: a read from an arbitrary date can
                # return one twin of a pair that straddles it, and the phantom cleanup
                # would delete the other (#1038).
                reread_ids = {t.external_id for t in reread}
                promoted_ids = promoted_txn_external_ids.setdefault(account.id, set())
                incoming_ids = seen_ids | reread_ids
                # Skip rows already settled here: only a pending row, or a posted one
                # under an id we don't have (re-keyed by the provider), can need an update.
                known_ids = set(
                    (
                        await session.execute(
                            select(Transaction.external_id).where(
                                Transaction.account_id == account.id,
                                Transaction.external_id.in_(reread_ids),
                            )
                        )
                    ).scalars()
                )
                for txn_data in reread:
                    if txn_data.status != "posted" or txn_data.external_id in seen_ids:
                        continue
                    if (
                        txn_data.external_id in unseen_pending
                        or txn_data.external_id not in known_ids
                    ):
                        promoted_id = await _promote_settled_pending(
                            session,
                            account,
                            txn_data,
                            incoming_ids,
                            unseen_pending,
                            bills_by_external_id,
                            provider_name=connection.provider,
                            user_id=user_id,
                            account_currency=acc_data.currency or user_currency,
                            user_currency=user_currency,
                            fetched_rates=fetched_rates,
                        )
                        if promoted_id:
                            promoted_ids.add(promoted_id)

        syncing_account_id = None
        await reimbursement_service.rehome_links_for_accounts(session, moved_account_ids)

        # Detect transfer pairs among newly synced transactions
        if new_tx_ids:
            await detect_transfer_pairs(session, workspace_id, candidate_ids=new_tx_ids)

            # Then settle whatever this money was promised against. It runs
            # after the rows are written, not inside the loop: the recurring
            # match upgrades a placeholder in place, while an invoice link is
            # a row pointing at a transaction that has to exist first. It is
            # also a single batch, so the candidate invoices are loaded once
            # per sync rather than once per transaction.
            landed = list(
                (
                    await session.execute(
                        select(Transaction).where(Transaction.id.in_(new_tx_ids))
                    )
                )
                .scalars()
                .all()
            )
            await reconciliation_service.match_incoming(
                session, workspace_id, landed
            )

        # Clean up phantom duplicates: providers occasionally double-report the
        # same payment with different ids. Once transfer detection has paired
        # the real one, the orphan twin gets removed here.
        # A throttled account has no live id set this run (its transactions
        # were never read, or the account itself was skipped), so the cleanup
        # would fall back to the heuristic that can delete a real unpaired row.
        # Leave it alone until a run reads it, as for a cut-short fetch. An
        # account skipped on its details or balance is matched by its stored
        # row, whose external_id can still be the uid of an earlier session
        # (a reauthorisation re-keys it only when the account comes through),
        # so every stored row the provider did not return this run counts.
        throttled_account_ids: set[uuid.UUID] = set()
        if rate_limited_accounts:
            throttled_account_ids = set(
                (
                    await session.execute(
                        select(Account.id).where(
                            Account.connection_id == connection.id,
                            or_(
                                Account.external_id.in_(rate_limited_accounts),
                                Account.external_id.not_in(incoming_external_ids),
                            ),
                        )
                    )
                ).scalars()
            )
        touched_account_ids = await _cleanup_phantom_duplicates(
            session, [a.id for a in synced_account_rows], live_txn_external_ids,
            promoted_external_ids=promoted_txn_external_ids,
            skip_account_ids=(
                incomplete_fetch_account_ids
                | unsafe_cleanup_account_ids
                | throttled_account_ids
            ),
        )

        # The opening balances above were reconciled with the phantoms still
        # counted. Reconcile the open accounts that lost one again, so the
        # removed amount does not stay behind in their synthetic opening
        # transaction. Closed accounts stay out, as in the account loop.
        if touched_account_ids:
            await session.flush()
            touched_accounts = await session.execute(
                select(Account).where(
                    Account.id.in_(touched_account_ids),
                    Account.is_closed == False,
                )
            )
            for touched_account in touched_accounts.scalars():
                await sync_opening_balance_for_connected_account(session, touched_account)

        # Refresh investment holdings (brokerage, fixed income, funds,
        # etc.) when enabled for this connection. Errors here are logged but
        # don't fail the sync; a bank connector that doesn't expose
        # /investments shouldn't block the transaction sync that just succeeded.
        holdings_persisted = False
        if holdings_fetch is not None:
            holdings_persisted = await _sync_holdings(
                session, user_id, connection, credentials, synced_account_ids,
                provider=provider, fetched=holdings_fetch,
            )
            await _sync_trades(
                session, connection, credentials, synced_account_ids, provider=provider
            )

        # Reconcile the opening balance after any new transactions land so
        # SUM(all txs) keeps matching account.balance from the provider. Runs
        # after the holdings sync: an account whose balance carries Holdings is
        # not reconcilable against its cash ledger at all, and it is this run's
        # holdings that decide whether it does.
        unavailable = _record_balance_observation(
            connection, provider_accounts, provider,
            persisted_account_ids={a.external_id for a in synced_account_rows if a.external_id}
            if holdings_persisted else set(),
            account_types={a.external_id: a.type for a in synced_account_rows if a.external_id},
            auto_invested_deducted=auto_invested_deducted,
        )
        incomplete_balances = any(a.external_id in unavailable for a in accounts_data)
        for account in synced_account_rows:
            if account.external_id not in unavailable:
                await sync_opening_balance_for_connected_account(session, account)

        # Reap institution rows referenced by nothing. Id-carrying servers
        # never orphan a row (renames update in place), but a name-only
        # server that renames its bank mints a fresh row and repoints the
        # accounts — the abandoned row would otherwise keep a single-bank
        # link presenting as multi-institution forever. Requiring zero
        # account AND zero wallet references means a wallet's "Synced from"
        # label can never be taken away. Runs after the holdings sync so a
        # wallet repointed away from the old row frees it this sync, not next.
        orphaned_institutions = await session.execute(
            select(Institution).where(
                Institution.connection_id == connection.id,
                ~exists().where(Account.institution_id == Institution.id),
                ~exists().where(AssetGroup.institution_id == Institution.id),
            )
        )
        for orphan in orphaned_institutions.scalars().all():
            await session.delete(orphan)

        # An account whose fetch was cut short (issue #1077) is missing the
        # rest of the window. The next fetch starts from last_sync_at, so
        # moving it forward would leave that gap behind for good once the
        # user reauthorizes; keep it until every account fetched in full.
        # The healthy accounts just re-read a longer window, deduplicated.
        # The same holds when the provider throttled an account: the next
        # sync must re-read the same window for it (duplicates are dropped on
        # external_id). What was imported for the other accounts is
        # committed below either way. A balance this run could not confirm
        # keeps the checkpoint too.
        sync_outcome: SyncOutcome
        if not rate_limited_accounts:
            sync_outcome = "refreshed"
            if not incomplete_balances and not incomplete_fetch_account_ids:
                connection.last_sync_at = datetime.now(timezone.utc)
            # A successful read ends any scheduler backoff (app.tasks.sync_tasks).
            current_settings = connection.settings or {}
            if SYNC_BACKOFF_KEY in current_settings:
                connection.settings = {
                    key: value
                    for key, value in current_settings.items()
                    if key != SYNC_BACKOFF_KEY
                }
            # A failed re-read leaves the catch-up due, so the next sync retries it.
            if pending_catch_up and not pending_catch_up_failed:
                connection.settings = {
                    **(connection.settings or {}),
                    PENDING_CATCH_UP_KEY: datetime.now(timezone.utc).isoformat(),
                }
        else:
            # Throttled: the backoff stays (the scheduler counts this run as a
            # rate-limited failure) and each 429 sets its floor: its own
            # Retry-After, or 6 hours without one, the longest winning. The
            # pending catch-up stays due as well, since the throttled accounts
            # were not re-read.
            sync_outcome = "partial" if transactions_fetched else "rate_limited"
            connection.settings = _with_retry_after(
                connection.settings,
                _throttle_retry_after(
                    provider,
                    rate_limited_accounts,
                    retry_after,
                    throttled_without_retry_after,
                ),
            )
        action_required_warnings = getattr(provider, "action_required_warnings", None)
        if isinstance(action_required_warnings, list) and action_required_warnings:
            logger.warning(
                "Provider %s synced with %d user-action warning(s) for connection %s",
                connection.provider,
                len(action_required_warnings),
                connection.id,
            )
            connection.status = "error"
        elif incomplete_balances:
            connection.status = "sync_error"
        else:
            connection.status = "active"
        connection.last_sync_error_account_id = None
        connection.sync_state_version += 1
        if rate_limited_accounts:
            logger.warning(
                "Sync %s finished %s: %d account(s) rate-limited by the provider "
                "were skipped; checkpoint kept (Retry-After=%s)",
                connection_id,
                sync_outcome,
                len(rate_limited_accounts),
                retry_after,
            )
        await session.commit()
        await session.refresh(connection)
        return connection, merged_count, sync_outcome

    except SessionExpiredError:
        # Provider consent expired — distinct from a generic error so the UI
        # can show a clearer "reauthorize" prompt.
        await _rollback_sync_attempt(session, sync_savepoint)
        await _set_sync_status_if_current(
            session, connection_id, sync_start_version, "expired"
        )
        await session.commit()
        raise
    except ProviderUserActionRequired:
        # Stale/revoked provider credentials require a non-destructive
        # reconnect path. Mark the connection unhealthy so the accounts page
        # shows the reconnect banner, then let the API return a typed 409
        # instead of a generic 500.
        if refresh_requires_user_action:
            raise
        await _rollback_sync_attempt(session, sync_savepoint)
        await _set_sync_status_if_current(
            session, connection_id, sync_start_version, "error"
        )
        await session.commit()
        raise
    except ProviderRateLimited as exc:
        # The bank/aggregator is throttling data requests (PSD2 caps unattended
        # access, commonly ~4/day). This run learned nothing about connection
        # health, so preserve its prior status and diagnostic for the next retry,
        # leave last_sync_at untouched so the next sync retries the same window,
        # and note the provider's Retry-After as a floor on the scheduler backoff.
        logger.warning(
            "Sync %s: provider rate-limited the whole sync; nothing fetched",
            connection_id,
        )
        await _rollback_sync_attempt(session, sync_savepoint)
        await _set_sync_status_if_current(
            session,
            connection_id,
            sync_start_version,
            sync_start_status,
            sync_start_error_account_id,
            retry_after=exc.retry_after,
        )
        await session.commit()
        # The row can vanish if the connection was deleted mid-sync. Fall back
        # to the one we already hold rather than raising.
        refreshed = await session.get(BankConnection, connection_id)
        # Report "rate_limited" so the caller (manual sync endpoint, UI) knows
        # no fresh data was pulled and can say so instead of a success toast.
        return refreshed or connection, 0, "rate_limited"
    except Exception:
        # Generic sync failures do not imply invalid credentials. Preserve the
        # local account being processed, and do not erase known credential
        # problems until a sync or reconnect actually succeeds. If some
        # account was also throttled in this run, its Retry-After is kept as
        # the floor on the next scheduled try.
        await _rollback_sync_attempt(session, sync_savepoint)
        error_account_id = None
        if syncing_account_id is not None:
            error_account_id = await session.scalar(
                select(Account.id).where(
                    Account.id == syncing_account_id,
                    Account.connection_id == connection_id,
                )
            )
        await _set_sync_status_if_current(
            session,
            connection_id,
            sync_start_version,
            sync_start_status if sync_start_status in {"error", "expired"} else "sync_error",
            sync_start_error_account_id
            if sync_start_status in {"error", "expired"}
            else error_account_id,
            retry_after=_throttle_retry_after(
                provider,
                rate_limited_accounts,
                retry_after,
                throttled_without_retry_after,
            ),
        )
        await session.commit()
        raise


DISCONNECTED_STATUS = "disconnected"


class ConnectionDisconnectedError(Exception):
    """A sync was asked of a connection the user disconnected."""


async def unlink_connection(
    session: AsyncSession, connection_id: uuid.UUID, workspace_id: uuid.UUID
) -> bool:
    """End a connection but keep what it synced (not committed).

    Its accounts become manual ones with all their transactions: a synced
    account keeps an opening-balance transaction that makes its transactions
    add up to the provider's balance, so the balance carries over. Its assets
    are archived, as on a disconnect, and the connection and its institutions
    are deleted.
    """
    connection = await _get_connection_for_mutation(session, connection_id, workspace_id)
    if not connection:
        return False
    from app.services.owned_transfer_service import guard_scope_mutation
    await guard_scope_mutation(session, workspace_id, connection_ids={connection_id})
    await session.execute(
        update(Asset).where(Asset.connection_id == connection_id).values(is_archived=True)
    )
    accounts = (
        await session.scalars(select(Account).where(Account.connection_id == connection_id))
    ).all()
    for account in accounts:
        # The sync names an account after its institution; once manual, the
        # institution is gone and the account's own name tells it apart.
        if account.institution is not None and account.display_name == account.institution.name:
            account.display_name = None
    # What each account shows while synced: liabilities store debt as positive.
    shown = {
        account.id: -account.balance if account.type in LIABILITY_ACCOUNT_TYPES else account.balance
        for account in accounts
    }
    await session.execute(
        update(Account)
        .where(Account.connection_id == connection_id)
        .values(connection_id=None, institution_id=None)
    )
    for account in accounts:
        await keep_balance_as_manual(session, account, Decimal(str(shown[account.id])))
    # Bulk statements, so the relationship's delete-orphan cascade can't
    # reach the accounts just unlinked.
    await session.execute(delete(Institution).where(Institution.connection_id == connection_id))
    await session.execute(delete(BankConnection).where(BankConnection.id == connection_id))
    return True


async def delete_connection(
    session: AsyncSession,
    connection_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    keep_accounts: bool = False,
    commit: bool = True,
) -> bool:
    """Disconnect a connection.

    `keep_accounts=True` archives it and keeps its accounts and history (see
    below); otherwise its assets are archived and it is deleted with its
    accounts. `commit=False` leaves the commit to the caller (a plugin being
    removed disconnects its connections inside the removal's transaction).
    """
    connection = await _get_connection_for_mutation(session, connection_id, workspace_id)
    if not connection:
        return False

    if keep_accounts:
        # Disconnecting keeps the accounts, their history, payees and
        # recurrings. The connection is archived rather than deleted so its
        # accounts stay linked to it: a later reconnect goes through the
        # reconnect flow, which matches each account by the same
        # (connection_id, external_id) a normal sync uses, so the re-sync
        # updates the existing rows instead of importing them again.
        # Dropping the credentials ends the access, and the status keeps the
        # scheduler and manual sync away from it.
        connection.status = DISCONNECTED_STATUS
        connection.credentials = None
        if commit:
            await session.commit()
        else:
            await session.flush()
        return True

    # Only a deletion can take away the wallets that retained ownership
    # evidence points at; an archived connection keeps them.
    from app.services.owned_transfer_service import guard_scope_mutation
    await guard_scope_mutation(session, workspace_id, connection_ids={connection_id})

    # Archive synced investment assets rather than deleting them: the user
    # may still want to see their historical AssetValue trend, and if they
    # re-connect the same provider later we can un-archive by matching
    # (user_id, workspace_id, source, external_id) — re-adoption stays inside
    # this workspace. The FK's ON DELETE SET NULL will
    # then clear connection_id when the row is removed below.
    await session.execute(
        update(Asset)
        .where(Asset.connection_id == connection.id)
        .values(is_archived=True)
    )

    # Track payees referenced by this connection's transactions so we can
    # remove only newly-orphaned records after deleting the connection.
    affected_payee_ids = (
        await session.execute(
            select(Transaction.payee_id)
            .join(Account, Account.id == Transaction.account_id)
            .where(
                Account.connection_id == connection.id,
                Transaction.payee_id.isnot(None),
            )
            .distinct()
        )
    ).scalars().all()
    # A payee a rule assigns stays, so the rule keeps working when the bank is
    # connected again.
    rules = (
        await session.execute(select(Rule).where(Rule.workspace_id == workspace_id))
    ).scalars().all()
    affected_payee_ids = set(affected_payee_ids) - rule_payee_ids(rules)

    # transfer_pair_id has no foreign key, so a transfer partner on an account
    # outside this connection would stay paired with a deleted row.
    pair_ids = (
        select(Transaction.transfer_pair_id)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Account.connection_id == connection.id,
            Transaction.transfer_pair_id.isnot(None),
        )
    )
    await session.execute(
        update(Transaction)
        .where(
            Transaction.workspace_id == workspace_id,
            Transaction.transfer_pair_id.in_(pair_ids),
        )
        .values(transfer_pair_id=None)
    )

    await session.delete(connection)
    await session.flush()

    if affected_payee_ids:
        has_transactions = exists(
            select(Transaction.id).where(Transaction.payee_id == Payee.id)
        )
        has_external_mappings = exists(
            select(PayeeMapping.id).where(
                PayeeMapping.target_id == Payee.id,
                PayeeMapping.id != Payee.id,
            )
        )
        # Invoices and invoice schedules hold their payee under a RESTRICT
        # foreign key, so deleting it would fail the whole disconnect.
        has_invoices = exists(select(Invoice.id).where(Invoice.payee_id == Payee.id))
        has_invoice_schedules = exists(
            select(InvoiceSchedule.id).where(InvoiceSchedule.payee_id == Payee.id)
        )
        await session.execute(
            delete(Payee).where(
                Payee.workspace_id == workspace_id,
                Payee.id.in_(affected_payee_ids),
                ~has_transactions,
                ~has_external_mappings,
                ~has_invoices,
                ~has_invoice_schedules,
            )
        )

    if commit:
        await session.commit()
    else:
        await session.flush()
    return True
