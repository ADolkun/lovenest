import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal, Optional

from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_value import AssetValue
from app.models.bank_connection import BankConnection
from app.models.account import Account
from app.models.category import Category
from app.models.credit_card_bill import CreditCardBill
from app.models.payee import Payee, PayeeMapping
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.models.user import User
from app.providers import get_provider
from app.providers.base import (
    AccountData,
    HoldingData,
    ProviderNotConfiguredError,
    ProviderRateLimited,
    ProviderUserActionRequired,
    SessionExpiredError,
)
from app.schemas.bank_connection import ProviderAccountRead
from app.services import oauth_state
from app.services import admin_service
from app.services import recurring_match_service
from app.services.account_service import (
    _simplefin_to_internal_balance,
    sync_opening_balance_for_connected_account,
)
from app.services.asset_group_service import ensure_group_for_connection
from app.services.credit_card_service import apply_effective_date
from app.services.rule_service import apply_rules_to_transaction
from app.services.transfer_detection_service import detect_transfer_pairs
from app.services.fx_rate_service import stamp_primary_amount
from app.services.payee_service import get_or_create_payee
from app.services.transaction_match_service import find_unique_transaction_match

logger = logging.getLogger(__name__)

LOCAL_IMPORT_SOURCES = {"import", "csv", "ofx", "qif", "camt"}

settings = get_settings()


def _clean_logo_url(value: object) -> Optional[str]:
    """Normalize a provider-supplied logo to a non-empty string or None.

    Guards the DB column against anything a provider hands back that isn't a
    usable URL (None, empty string, or a non-string), so a misbehaving
    integration can never write junk into ``bank_connections.logo_url``.
    """
    return value if isinstance(value, str) and value.strip() else None

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


def _allowlist_ids(settings: Optional[dict]) -> Optional[set[str]]:
    """Read the tri-state `account_allowlist` out of a connection's settings.

    The difference between the first two states is the compatibility contract:

    - absent: sync every account the provider returns (what every connection
      did before this setting existed), signalled by None;
    - present: sync only the listed provider account ids;
    - present and empty: sync nothing. A valid state, not an error.

    A non-list value reads as absent — a malformed setting must not silently
    stop a connection from syncing.
    """
    raw = (settings or {}).get("account_allowlist")
    if not isinstance(raw, list):
        return None
    return {str(item) for item in raw}


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
    reviewed |= _allowlist_ids(updated) or set()
    updated["reviewed_account_ids"] = sorted(reviewed)
    connection.settings = updated


def _syncable_accounts(
    connection: BankConnection, accounts: list[AccountData]
) -> tuple[list[AccountData], Optional[set[str]]]:
    """Narrow a provider's account list to what this connection may sync.

    The one place the allowlist is enforced: every entity a sync creates is
    derived from the accounts this returns, so a new entity type can't bypass
    the check by adding its own code path.

    Also records every id the provider returned, before filtering, so the
    account-discovery endpoint can tell an account that appeared after the
    allowlist was configured from one the user deliberately unchecked, without
    spending a provider request.

    Returns the surviving accounts and their ids, or None for "no allowlist" —
    the signal holdings sync keys off.
    """
    updated = dict(connection.settings or {})
    updated["seen_account_ids"] = sorted({a.external_id for a in accounts})
    connection.settings = updated

    allowlist = _allowlist_ids(updated)
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


async def _sync_holdings(
    session: AsyncSession,
    user_id: uuid.UUID,
    connection: BankConnection,
    credentials: dict,
    synced_account_ids: Optional[set[str]] = None,
) -> None:
    """Fetch investment holdings from the provider and upsert them as Assets.

    Each holding becomes one Asset (type="investment") keyed by
    (user_id, source, external_id). Every sync appends an AssetValue row
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
    """
    # Tolerate provider-side failures (e.g. Pluggy returning 500 for a
    # specific connector, a bank that doesn't expose /investments).
    # Storage errors below are intentionally not caught — they indicate
    # a schema/invariant bug we want to surface, not a hiccup to swallow.
    try:
        provider = get_provider(connection.provider)
        holdings = await provider.get_holdings(credentials)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to fetch holdings for connection %s", connection.id
        )
        return

    source = connection.provider
    today = date.today()

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

    # Find-or-create the wallet that will own this connection's holdings.
    # Name defaults to the institution; users can rename freely without
    # breaking future syncs (matching is by external_id).
    group: Optional[AssetGroup] = None
    if holdings:
        group = await ensure_group_for_connection(
            session,
            user_id=user_id,
            connection_id=connection.id,
            source=source,
            external_id=connection.external_id,
            default_name=connection.institution_name,
        )

    # Also pull orphans (connection_id IS NULL) with the same source —
    # those are assets archived by a prior disconnect. Re-matching on
    # external_id lets users re-link their investment history when they
    # re-add a connection without creating duplicate rows.
    existing_rows = await session.execute(
        select(Asset).where(
            Asset.user_id == user_id,
            Asset.source == source,
            or_(Asset.connection_id == connection.id, Asset.connection_id.is_(None)),
        )
    )
    existing_by_external: dict[str, Asset] = {
        a.external_id: a for a in existing_rows.scalars().all() if a.external_id
    }
    seen: set[str] = set(excluded_external_ids)

    for holding in holdings:
        seen.add(holding.external_id)
        existing = existing_by_external.get(holding.external_id)

        # Provider-reported closure (Pluggy TOTAL_WITHDRAWAL). Two cases:
        #   - New + withdrawn: skip entirely. A dead zero-balance asset
        #     with no history is noise; the user never saw this position
        #     while it was active, no reason to surface it closed.
        #   - Existing + withdrawn: mark sell_date (if not already set by
        #     the user) so it drops out of current totals but historical
        #     AssetValues remain visible in reports. No new AssetValue —
        #     appending today's zero would bury the real closing value.
        if holding.is_withdrawn:
            if existing is None:
                continue
            if existing.sell_date is None:
                existing.sell_date = today
            # Keep descriptive fields fresh in case the provider still
            # updates them post-closure, but don't touch valuation.
            existing.name = holding.name
            existing.external_metadata = holding.metadata
            existing.connection_id = connection.id
            continue

        asset = await _upsert_asset_from_holding(
            session, existing, holding, user_id, connection.id, source,
        )
        # Attach to the connection's wallet. We only set group_id when
        # it's currently null so a user who moved this holding to a
        # custom wallet ("US Stocks") doesn't get overridden back on
        # every sync.
        if group is not None and asset.group_id is None:
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

    for ext_id, asset in existing_by_external.items():
        if ext_id not in seen and not asset.is_archived:
            asset.is_archived = True


async def _upsert_asset_from_holding(
    session: AsyncSession,
    asset: Optional[Asset],
    holding: HoldingData,
    user_id: uuid.UUID,
    connection_id: uuid.UUID,
    source: str,
) -> Asset:
    """Create or update an Asset from a HoldingData payload.

    Synced fields (name, currency, quantity, purchase_price, maturity,
    metadata) are always overwritten — the UI disables editing these on
    synced assets. Provider-reported withdrawal is handled by the caller
    via `sell_date`, not here, so this function only ever sees ACTIVE
    holdings and never flips `is_archived` on its own.
    """
    if asset is None:
        asset = Asset(
            user_id=user_id,
            connection_id=connection_id,
            source=source,
            external_id=holding.external_id,
            name=holding.name,
            type="investment",
            currency=holding.currency,
            units=holding.quantity,
            purchase_price=holding.purchase_price,
            purchase_date=holding.purchase_date,
            isin=holding.isin,
            ticker=holding.ticker,
            maturity_date=holding.maturity_date,
            external_metadata=holding.metadata,
            valuation_method="manual",
        )
        session.add(asset)
        await session.flush()
        return asset

    # Fields Pluggy consistently returns — safe to overwrite each sync.
    asset.name = holding.name
    asset.currency = holding.currency
    # external_metadata is a snapshot blob: we want the latest every time.
    asset.external_metadata = holding.metadata
    previous_connection_id = asset.connection_id
    asset.connection_id = connection_id
    # Only auto-unarchive when the holding moved to a different connection
    # (e.g. unlink + reconnect). This avoids overriding user-archived assets.
    if asset.is_archived and previous_connection_id != connection_id:
        asset.is_archived = False

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
    """
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
        statement = statement.with_for_update()
    result = await session.execute(statement)
    return result.scalar_one_or_none()


async def get_oauth_url(
    provider_name: str,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    flow_params: Optional[dict] = None,
    reconnect_connection_id: Optional[uuid.UUID] = None,
) -> str:
    provider = get_provider(provider_name)
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
    provider = get_provider(connection.provider)
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
    provider_name: str, country: Optional[str] = None
) -> dict:
    provider = get_provider(provider_name)
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
    provider_name: str, user_id: uuid.UUID, item_id: str | None = None
) -> dict:
    provider = get_provider(provider_name)
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

    current = dict(connection.settings or {})
    for key, value in settings_update.items():
        if value is not None:
            current[key] = value
    connection.settings = current

    if settings_update.get("account_allowlist") is not None:
        _record_reviewed_accounts(
            connection, {a.external_id for a in connection.accounts if a.external_id}
        )

    await session.commit()
    await session.refresh(connection)
    return connection


async def list_provider_accounts(
    connection: BankConnection,
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
        provider = get_provider(connection.provider)
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
    allowlist = _allowlist_ids(conn_settings)
    reviewed = conn_settings.get("reviewed_account_ids")
    if reviewed is None:
        # An allowlist configured before the reviewed set was pinned. Sync's
        # rolling record and the account rows are the best evidence left of what
        # the user has already seen; both err towards excluded, which is the
        # quieter of the two wrong answers for an account they did unchecked.
        known = {str(item) for item in conn_settings.get("seen_account_ids") or []}
        known |= {a.external_id for a in connection.accounts if a.external_id}
    else:
        known = {str(item) for item in reviewed}

    def _status(external_id: str) -> Literal["included", "excluded", "pending"]:
        if allowlist is None or external_id in allowlist:
            return "included"
        return "excluded" if external_id in known else "pending"

    return [
        ProviderAccountRead(
            external_id=account.external_id,
            name=account.name,
            balance=account.balance,
            currency=account.currency,
            has_holdings=account.has_holdings,
            status=_status(account.external_id),
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
        existing_reconnect = await get_connection(
            session,
            uuid.UUID(str(reconnect_id)),
            workspace_id,
            for_update=True,
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

    provider = get_provider(provider_name)
    connection_data = await provider.handle_oauth_callback(code)

    if existing_reconnect:
        existing_reconnect.external_id = connection_data.external_id
        existing_reconnect.institution_name = (
            connection_data.institution_name or existing_reconnect.institution_name
        )
        existing_reconnect.logo_url = _clean_logo_url(connection_data.logo_url) or existing_reconnect.logo_url
        existing_reconnect.credentials = connection_data.credentials
        existing_reconnect.status = "active"
        existing_reconnect.last_sync_error_account_id = None
        existing_reconnect.sync_state_version += 1
        # Re-sync from current data on next sync cycle.
        existing_reconnect.last_sync_at = None
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
    # Chosen during the connect flow, so the very first import already honors
    # it rather than creating accounts the user then has to clean up.
    if isinstance(flow_allowlist, list):
        initial_settings["account_allowlist"] = [str(item) for item in flow_allowlist]

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

    use_provider_cats = await admin_service.use_provider_categories(session)

    syncable_accounts, synced_account_ids = _syncable_accounts(
        connection, connection_data.accounts
    )
    if synced_account_ids is not None:
        # The connect widget showed this account list before the first sync, so
        # everything in it counts as reviewed — only accounts that turn up later
        # are new to the user.
        _record_reviewed_accounts(
            connection, {a.external_id for a in connection_data.accounts}
        )

    for acc_data in syncable_accounts:
        is_cc = acc_data.type == "credit_card"
        account = Account(
            user_id=user_id,
            workspace_id=workspace_id,
            connection_id=connection.id,
            external_id=acc_data.external_id,
            name=acc_data.name,
            masked_number=acc_data.masked_number,
            type=acc_data.type,
            balance=acc_data.balance,
            currency=acc_data.currency,
            credit_limit=acc_data.credit_limit if is_cc else None,
            statement_close_day=acc_data.statement_close_day if is_cc else None,
            payment_due_day=acc_data.payment_due_day if is_cc else None,
            minimum_payment=acc_data.minimum_payment if is_cc else None,
            card_brand=acc_data.card_brand if is_cc else None,
            card_level=acc_data.card_level if is_cc else None,
        )
        session.add(account)
        await session.flush()

        bills_by_external_id = await _sync_credit_card_bills(
            session, user_id, account, provider, connection_data.credentials
        )

        # Fetch initial transactions (since=None fetches all available history)
        transactions_data = await provider.get_transactions(
            connection_data.credentials, acc_data.external_id, None
        )
        incoming_external_ids = {txn.external_id for txn in transactions_data}
        for txn_data in transactions_data:
            # Pending↔posted twin (and the credit-card installment variant).
            # When the same logical operation comes back under a new external
            # id with a different status, fingerprint match prevents the
            # second copy from landing.
            synced_dup = await _find_synced_duplicate(
                session, account.id, txn_data, incoming_external_ids
            )
            if synced_dup:
                if synced_dup.status == "posted" and txn_data.status == "pending":
                    continue
                if synced_dup.status == "pending" and txn_data.status == "posted":
                    await _promote_synced_transaction(
                        session, user_id, synced_dup, txn_data,
                        account=account,
                        account_currency=acc_data.currency or user_currency,
                        user_currency=user_currency,
                        replace_external_id=True,
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

            category_id = await _match_pluggy_category(
                session, workspace_id, txn_data.pluggy_category, enabled=use_provider_cats
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
                amount=txn_data.amount,
                currency=txn_data.currency or acc_data.currency or user_currency,
                date=txn_data.date,
                type=txn_data.type,
                source="sync",
                status=txn_data.status,
                payee=txn_data.payee,
                payee_id=payee_id,
                raw_data=txn_data.raw_data,
                category_id=category_id,
                installment_number=txn_data.installment_number,
                total_installments=txn_data.total_installments,
                installment_total_amount=txn_data.installment_total_amount,
                installment_purchase_date=txn_data.installment_purchase_date,
                bill_id=bill.id if bill else None,
            )
            apply_effective_date(
                transaction, account, bill_due_date=bill.due_date if bill else None
            )
            session.add(transaction)
            await session.flush()
            new_tx_ids.append(transaction.id)
            if not category_id:
                await apply_rules_to_transaction(session, user_id, transaction)

            await _stamp_synced_amount(
                session, user_id, transaction, txn_data,
                account_currency=acc_data.currency or user_currency,
                user_currency=user_currency,
            )

        # After importing the initial batch, reconcile the opening balance so
        # that SUM(all transactions) matches the provider-reported balance. Any
        # history that falls outside the provider's lookback window gets
        # absorbed into this synthetic transaction.
        await sync_opening_balance_for_connected_account(session, account)

    # Detect transfer pairs among newly synced transactions
    await detect_transfer_pairs(session, workspace_id, candidate_ids=new_tx_ids)

    # Investment holdings live on /investments — separate endpoint from
    # /accounts. Pulled after account setup when enabled so holdings are
    # available on the Assets page immediately after the widget closes.
    if _sync_assets_enabled(connection.settings):
        await _sync_holdings(
            session, user_id, connection, connection_data.credentials,
            synced_account_ids,
        )

    connection.last_sync_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(connection)
    return connection


def _description_similarity(a: str | None, b: str | None) -> float:
    """Token overlap ratio between two descriptions."""
    if not a or not b:
        return 0.0
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    return len(intersection) / max(len(tokens_a), len(tokens_b))


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
    if account or connection.provider != "simplefin":
        return account

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
        if not candidate.is_closed
        and _normalized_account_name(candidate.name) == normalized_name
    ]
    if not candidates:
        return None

    # Prefer the user's established/customized account over a freshly-created
    # duplicate: it usually has a display name, corrected type, and history.
    candidates.sort(
        key=lambda candidate: (
            candidate.display_name is not None,
            candidate.type != acc_data.type,
            candidate.external_id is not None,
        ),
        reverse=True,
    )
    account = candidates[0]
    account.external_id = acc_data.external_id
    return account


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
        score = _description_similarity(candidate.description, txn_data.description)
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
        if replace_external_id and transaction.source == "sync":
            transaction.raw_data = txn_data.raw_data
        elif not transaction.raw_data:
            transaction.raw_data = txn_data.raw_data
        elif isinstance(transaction.raw_data, dict) and isinstance(txn_data.raw_data, dict):
            merged = {**txn_data.raw_data, **transaction.raw_data}
            if merged != transaction.raw_data:
                transaction.raw_data = merged
    if txn_data.payee and not transaction.payee:
        transaction.payee = txn_data.payee


async def _stamp_synced_amount(
    session: AsyncSession,
    user_id: uuid.UUID,
    transaction: Transaction,
    txn_data,
    *,
    account_currency: str,
    user_currency: str,
) -> None:
    if (
        txn_data.amount_in_account_currency is not None
        and txn_data.amount
        and account_currency == user_currency
        and txn_data.currency != account_currency
    ):
        transaction.amount_primary = txn_data.amount_in_account_currency
        transaction.fx_rate_used = txn_data.amount_in_account_currency / txn_data.amount
    else:
        if transaction.fx_rate_used is not None:
            transaction.amount_primary = (
                Decimal(txn_data.amount) * Decimal(transaction.fx_rate_used)
            ).quantize(Decimal("0.01"))
        await stamp_primary_amount(session, user_id, transaction)


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
) -> None:
    old_amount = Decimal(transaction.amount)
    pair_id = transaction.transfer_pair_id

    _merge_sync_metadata(
        transaction, txn_data, replace_external_id=replace_external_id
    )
    transaction.amount = txn_data.amount
    transaction.date = txn_data.date
    transaction.status = "posted"
    apply_effective_date(transaction, account)

    if old_amount != txn_data.amount:
        splits = (await session.execute(
            select(TransactionSplit)
            .where(TransactionSplit.transaction_id == transaction.id)
            .order_by(TransactionSplit.created_at, TransactionSplit.id)
        )).scalars().all()
        if splits:
            new_total = abs(Decimal(txn_data.amount)).quantize(Decimal("0.01"))
            old_total = sum((split.share_amount for split in splits), Decimal("0"))
            allocated = Decimal("0")
            for split in splits[:-1]:
                split.share_amount = (
                    split.share_amount * new_total / old_total
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                allocated += split.share_amount
            splits[-1].share_amount = new_total - allocated

        if pair_id is not None:
            await session.execute(
                update(Transaction)
                .where(Transaction.transfer_pair_id == pair_id)
                .values(transfer_pair_id=None)
            )
            transaction.transfer_pair_id = None

    await _stamp_synced_amount(
        session, user_id, transaction, txn_data,
        account_currency=account_currency,
        user_currency=user_currency,
    )


async def _find_synced_duplicate(
    session: AsyncSession,
    account_id: uuid.UUID,
    txn_data,
    incoming_external_ids: set[str],
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
    """
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
        )
    )
    for candidate in result.scalars():
        if candidate.external_id and candidate.external_id.startswith("bill_charge:"):
            continue
        if _description_similarity(candidate.description, txn_data.description) >= 0.7:
            return candidate

    # Path 3: exact transaction timestamp fingerprint. Some SimpleFIN
    # bridges finalize pending card charges with a new id, amount, or posted
    # date. The original bank transaction timestamp is the stable identity.
    raw = txn_data.raw_data if isinstance(txn_data.raw_data, dict) else {}
    transacted_at = raw.get("transacted_at")
    if transacted_at is not None:
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
            if candidate.external_id and candidate.external_id.startswith("bill_charge:"):
                continue
            candidate_raw = candidate.raw_data if isinstance(candidate.raw_data, dict) else {}
            if (
                candidate_raw.get("transacted_at") == transacted_at
                and _description_similarity(
                    candidate_raw.get("description") or candidate.description,
                    txn_data.description,
                ) >= 0.9
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
                left and right and _description_similarity(left, right) >= 0.9
                for left in candidate_descriptions
                for right in incoming_descriptions
            )
            if (
                candidate_raw.get("posted") == posted
                and candidate_raw.get("transacted_at") == transacted_at
                and description_matches
            ):
                return candidate

    # Path 5: local import history from before the account was connected.
    # Posting lag and statement exports can shift the date or shorten the
    # merchant description, so accept only one exact normalized merchant/payee.
    return await find_unique_transaction_match(
        session,
        account_id,
        txn_data,
        LOCAL_IMPORT_SOURCES,
        unclaimed_only=True,
        exclude_external_ids=incoming_external_ids,
    )


async def _cleanup_phantom_duplicates(
    session: AsyncSession,
    account_ids: list[uuid.UUID],
) -> int:
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
    """
    if not account_ids:
        return 0

    unmatched_result = await session.execute(
        select(Transaction).where(
            Transaction.account_id.in_(account_ids),
            Transaction.source == "sync",
            Transaction.transfer_pair_id.is_(None),
        )
    )
    unmatched = list(unmatched_result.scalars().all())

    deleted = 0
    for tx in unmatched:
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
                Transaction.id != tx.id,
            )
        )
        for sibling in sibling_result.scalars():
            if _description_similarity(sibling.description, tx.description) >= 0.9:
                await session.delete(tx)
                deleted += 1
                break

    return deleted


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
            existing.amount = abs(amount)
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
) -> None:
    await session.execute(
        update(BankConnection)
        .where(
            BankConnection.id == connection_id,
            BankConnection.sync_state_version == starting_version,
        )
        .values(
            status=status,
            last_sync_error_account_id=error_account_id,
            sync_state_version=BankConnection.sync_state_version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    await session.get(BankConnection, connection_id, populate_existing=True)


async def sync_connection(
    session: AsyncSession,
    connection_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    trigger_provider_refresh: bool = False,
) -> tuple[BankConnection, int]:
    # Manual and scheduled syncs can overlap. Lock the connection row for the
    # transaction so every caller shares one serialization boundary.
    connection = await get_connection(
        session, connection_id, workspace_id, for_update=True
    )
    if not connection:
        raise ValueError("Connection not found")
    if not connection.credentials:
        raise ValueError("Credentials not found")

    sync_start_status = connection.status
    sync_start_version = connection.sync_state_version
    sync_start_error_account_id = connection.last_sync_error_account_id
    conn_settings = connection.settings or {}
    payee_source = conn_settings.get("payee_source", "auto")
    import_pending = conn_settings.get("import_pending", True)
    use_provider_cats = await admin_service.use_provider_categories(session)
    syncing_account_id: uuid.UUID | None = None

    # Resolve the provider before the error-handling block: an unregistered
    # provider is a server misconfiguration, and the catch-all below would
    # wrongly stamp the (healthy) connection with status="error".
    try:
        provider = get_provider(connection.provider)
    except ValueError as exc:
        raise ProviderNotConfiguredError(
            f"Provider '{connection.provider}' is not configured in this process. "
            "If connecting from the web app works but background sync fails, the "
            "worker service is likely not loading the environment (.env) that "
            "enables this provider."
        ) from exc

    try:
        # Refresh credentials if needed
        credentials = await provider.refresh_credentials(connection.credentials)
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
            outcome = await provider.trigger_refresh(credentials)
            if outcome == "needs_user_action":
                # Surfacing reconnect immediately is better than silently
                # reading stale data the user knows is stale.
                connection.status = "error"
                connection.last_sync_error_account_id = None
                connection.sync_state_version += 1
                await session.commit()
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
        synced_account_row_ids: list[uuid.UUID] = []
        merged_count = 0
        accounts_data = await provider.get_accounts(credentials)
        accounts_data, synced_account_ids = _syncable_accounts(connection, accounts_data)
        for acc_data in accounts_data:
            syncing_account_id = None
            account = await _find_existing_connected_account(
                session, connection, acc_data
            )

            # Honor user intent: a closed connected account stays closed and is
            # not touched by sync. The row is left alone (no balance/name
            # rewrite, no new transactions) but the connection link is kept so
            # the next sync still finds it here instead of creating a duplicate
            # active account (issue #90).
            if account and account.is_closed:
                continue

            if account:
                # Normalize the provider sign using the account's CURRENT type,
                # which reflects any user override (sync never rewrites `type`).
                # SimpleFIN reports card debt as negative under a "checking"
                # label; once the user overrides the type to credit_card the
                # downstream sites negate it, so store positive-for-debt to keep
                # them provider-agnostic and avoid double-counting.
                account.balance = _simplefin_to_internal_balance(
                    connection.provider, account.type, acc_data.balance
                )
                account.name = acc_data.name
                # Backfills existing accounts on their next sync. Only written
                # when the provider actually returns an identifier, so a payload
                # that intermittently omits it can't blank out a known mask.
                if acc_data.masked_number is not None:
                    account.masked_number = acc_data.masked_number
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
            else:
                is_cc = acc_data.type == "credit_card"
                account = Account(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    connection_id=connection.id,
                    external_id=acc_data.external_id,
                    name=acc_data.name,
                    masked_number=acc_data.masked_number,
                    type=acc_data.type,
                    balance=acc_data.balance,
                    currency=acc_data.currency,
                    credit_limit=acc_data.credit_limit if is_cc else None,
                    statement_close_day=acc_data.statement_close_day if is_cc else None,
                    payment_due_day=acc_data.payment_due_day if is_cc else None,
                    minimum_payment=acc_data.minimum_payment if is_cc else None,
                    card_brand=acc_data.card_brand if is_cc else None,
                    card_level=acc_data.card_level if is_cc else None,
                )
                session.add(account)
                await session.flush()

            syncing_account_id = account.id
            synced_account_row_ids.append(account.id)

            # Fetch the bills feed before transactions so transaction → bill
            # FK resolution happens in-memory (no N+1). Empty dict for non-CC
            # accounts or providers without /bills.
            bills_by_external_id = await _sync_credit_card_bills(
                session, user_id, account, provider, credentials
            )

            # Fetch and sync transactions. The 14-day rewind is on Pluggy's
            # `createdAt` (when their row was inserted), so it covers two
            # cases: (1) PENDING transactions that POSTED since last sync,
            # (2) any rows Pluggy ingested late but backdated. Dedup on
            # external_id below handles overlap cheaply.
            since = (
                connection.last_sync_at.date() - timedelta(days=14)
                if connection.last_sync_at
                else None
            )
            transactions_data = await provider.get_transactions(
                credentials, acc_data.external_id, since, payee_source=payee_source
            )

            if not import_pending:
                transactions_data = [t for t in transactions_data if t.status != "pending"]

            incoming_external_ids = {txn.external_id for txn in transactions_data}
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
                    if existing_tx.is_ignored:
                        continue
                    if existing_tx.status == "pending" and txn_data.status == "posted":
                        await _promote_synced_transaction(
                            session, user_id, existing_tx, txn_data,
                            account=account,
                            account_currency=acc_data.currency or user_currency,
                            user_currency=user_currency,
                            replace_external_id=True,
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
                    #
                    # User's manual override wins: if effective_bill_date is
                    # set, we don't touch bill_id or effective_date — the
                    # user has explicitly overridden the auto bucketing.
                    if (
                        txn_data.bill_external_id
                        and existing_tx.effective_bill_date is None
                    ):
                        bill = bills_by_external_id.get(txn_data.bill_external_id)
                        if bill is not None:
                            if existing_tx.bill_id != bill.id:
                                existing_tx.bill_id = bill.id
                            apply_effective_date(
                                existing_tx, account, bill_due_date=bill.due_date
                            )
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
                    session, account.id, txn_data, incoming_external_ids
                )
                if synced_dup:
                    if synced_dup.status == "posted" and txn_data.status == "pending":
                        continue
                    if synced_dup.status == "pending" and txn_data.status == "posted":
                        # Posted truth wins: swap in the new id so subsequent
                        # syncs match by external_id and update raw_data.
                        await _promote_synced_transaction(
                            session, user_id, synced_dup, txn_data,
                            account=account,
                            account_currency=acc_data.currency or user_currency,
                            user_currency=user_currency,
                            replace_external_id=synced_dup.source == "sync",
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
                        # Same logical posted row re-keyed by a provider such
                        # as SimpleFIN. Keep the user's row and move its
                        # idempotency key forward instead of inserting a twin.
                        _merge_sync_metadata(
                            synced_dup,
                            txn_data,
                            replace_external_id=synced_dup.source == "sync",
                        )
                    continue

                # Pass 4: recurring bill placeholder. If generate_pending
                # already materialized this bill's occurrence, merge the incoming
                # charge into that placeholder instead of duplicating (issue
                # #116). The recurring link is preserved by upgrading in place.
                incoming_currency = txn_data.currency or acc_data.currency or user_currency
                placeholder = await recurring_match_service.find_placeholder_for_incoming(
                    session, account.id, txn_data.amount, incoming_currency,
                    txn_data.type, txn_data.date, txn_data.description,
                )
                if placeholder:
                    if placeholder.is_ignored:
                        continue
                    placeholder.external_id = txn_data.external_id
                    placeholder.source = "sync"
                    placeholder.status = txn_data.status
                    placeholder.raw_data = txn_data.raw_data
                    if txn_data.payee:
                        if not placeholder.payee:
                            placeholder.payee = txn_data.payee
                        placeholder.payee_id = (
                            await get_or_create_payee(
                                session, user_id, txn_data.payee, workspace_id=workspace_id
                            )
                        ).id
                    merged_count += 1
                    continue

                category_id = await _match_pluggy_category(
                    session, workspace_id, txn_data.pluggy_category, enabled=use_provider_cats
                )

                # Resolve payee entity from raw payee text
                sync_payee_id = None
                if txn_data.payee:
                    sync_payee_entity = await get_or_create_payee(
                        session, user_id, txn_data.payee, workspace_id=workspace_id
                    )
                    sync_payee_id = sync_payee_entity.id

                # No placeholder existed: if this charge fulfills an active bill's
                # next occurrence, link it and advance the bill so a later
                # generate_pending won't create a duplicate placeholder.
                recurring_link = await recurring_match_service.find_bill_for_incoming(
                    session, user_id, account.id, txn_data.amount, incoming_currency,
                    txn_data.type, txn_data.date, txn_data.description,
                )

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
                    amount=txn_data.amount,
                    currency=txn_data.currency or acc_data.currency or user_currency,
                    date=txn_data.date,
                    type=txn_data.type,
                    source="sync",
                    status=txn_data.status,
                    payee=txn_data.payee,
                    payee_id=sync_payee_id,
                    raw_data=txn_data.raw_data,
                    category_id=category_id,
                    installment_number=txn_data.installment_number,
                    total_installments=txn_data.total_installments,
                    installment_total_amount=txn_data.installment_total_amount,
                    installment_purchase_date=txn_data.installment_purchase_date,
                    bill_id=bill.id if bill else None,
                    recurring_transaction_id=recurring_link.id if recurring_link else None,
                )
                apply_effective_date(
                    transaction, account, bill_due_date=bill.due_date if bill else None
                )
                session.add(transaction)
                await session.flush()
                if recurring_link is not None:
                    recurring_match_service.advance_past(recurring_link, txn_data.date)
                new_tx_ids.append(transaction.id)
                if not category_id:
                    await apply_rules_to_transaction(session, user_id, transaction)

                await _stamp_synced_amount(
                    session, user_id, transaction, txn_data,
                    account_currency=acc_data.currency or user_currency,
                    user_currency=user_currency,
                )

            # Reconcile the opening balance after any new transactions land so
            # SUM(all txs) keeps matching account.balance from the provider.
            await sync_opening_balance_for_connected_account(session, account)

        syncing_account_id = None

        # Detect transfer pairs among newly synced transactions
        if new_tx_ids:
            await detect_transfer_pairs(session, workspace_id, candidate_ids=new_tx_ids)

        # Clean up phantom duplicates: providers occasionally double-report the
        # same payment with different ids. Once transfer detection has paired
        # the real one, the orphan twin gets removed here.
        await _cleanup_phantom_duplicates(session, synced_account_row_ids)

        # Refresh investment holdings (brokerage, fixed income, funds,
        # etc.) when enabled for this connection. Errors here are logged but
        # don't fail the sync; a bank connector that doesn't expose
        # /investments shouldn't block the transaction sync that just succeeded.
        if _sync_assets_enabled(conn_settings):
            await _sync_holdings(
                session, user_id, connection, credentials, synced_account_ids
            )

        connection.last_sync_at = datetime.now(timezone.utc)
        action_required_warnings = getattr(provider, "action_required_warnings", None)
        if isinstance(action_required_warnings, list) and action_required_warnings:
            logger.warning(
                "Provider %s synced with %d user-action warning(s) for connection %s",
                connection.provider,
                len(action_required_warnings),
                connection.id,
            )
            connection.status = "error"
        else:
            connection.status = "active"
        connection.last_sync_error_account_id = None
        connection.sync_state_version += 1
        await session.commit()
        await session.refresh(connection)
        return connection, merged_count

    except SessionExpiredError:
        # Provider consent expired — distinct from a generic error so the UI
        # can show a clearer "reauthorize" prompt.
        await session.rollback()
        async with session.begin():
            await _set_sync_status_if_current(
                session, connection_id, sync_start_version, "expired"
            )
        raise
    except ProviderUserActionRequired:
        # Stale/revoked provider credentials require a non-destructive
        # reconnect path. Mark the connection unhealthy so the accounts page
        # shows the reconnect banner, then let the API return a typed 409
        # instead of a generic 500.
        await session.rollback()
        async with session.begin():
            await _set_sync_status_if_current(
                session, connection_id, sync_start_version, "error"
            )
        raise
    except ProviderRateLimited:
        # The bank/aggregator is throttling data requests (PSD2 caps unattended
        # access, commonly ~4/day). This run learned nothing about connection
        # health, so preserve its prior status and diagnostic for the next retry.
        await session.rollback()
        async with session.begin():
            await _set_sync_status_if_current(
                session,
                connection_id,
                sync_start_version,
                sync_start_status,
                sync_start_error_account_id,
            )
        # The row can vanish if the connection was deleted mid-sync. Fall back
        # to the one we already hold rather than raising: re-raising here would
        # escape as a 500, which is exactly what this handler exists to avoid.
        refreshed = await session.get(BankConnection, connection_id)
        return refreshed or connection, 0
    except Exception:
        # Generic sync failures do not imply invalid credentials. Preserve the
        # local account being processed so the accounts page can identify it.
        await session.rollback()
        async with session.begin():
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
                "sync_error",
                error_account_id,
            )
        raise


async def delete_connection(
    session: AsyncSession, connection_id: uuid.UUID, workspace_id: uuid.UUID
) -> bool:
    connection = await get_connection(
        session, connection_id, workspace_id, for_update=True
    )
    if not connection:
        return False

    # Archive synced investment assets rather than deleting them: the user
    # may still want to see their historical AssetValue trend, and if they
    # re-connect the same provider later we can un-archive by matching
    # (user_id, source, external_id). The FK's ON DELETE SET NULL will
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
        await session.execute(
            delete(Payee).where(
                Payee.workspace_id == workspace_id,
                Payee.id.in_(affected_payee_ids),
                ~has_transactions,
                ~has_external_mappings,
            )
        )

    await session.commit()
    return True
