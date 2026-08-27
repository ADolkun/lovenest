import uuid
from decimal import Decimal
from typing import Optional, cast

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_value import AssetValue, latest_value_first
from app.models.bank_connection import BankConnection
from app.models.user import User
from app.schemas.asset_group import (
    AssetGroupCreate,
    AssetGroupRead,
    AssetGroupUpdate,
    TaxTreatment,
)
from app.services.fx_rate_service import convert


async def ensure_group_in_workspace(
    session: AsyncSession, group_id: Optional[uuid.UUID], workspace_id: uuid.UUID
) -> None:
    """Validate a wallet an asset is about to be linked to.

    `Asset.group_id` is a bare foreign key with no workspace of its own, so
    every write path that takes one from a request has to check it here —
    otherwise an id belonging to another workspace is stored and honoured.
    Reports the same "not found" a wallet outside the workspace gets
    everywhere else, rather than confirming that the id exists elsewhere.
    """
    if group_id is None:
        return
    group = await session.get(AssetGroup, group_id)
    if group is None or group.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Group not found"
        )


async def _latest_value_amount(session: AsyncSession, asset_id: uuid.UUID) -> Optional[Decimal]:
    """Return the most recent AssetValue.amount for an asset, or None."""
    row = await session.execute(
        select(AssetValue.amount)
        .where(AssetValue.asset_id == asset_id)
        .order_by(*latest_value_first())
        .limit(1)
    )
    return row.scalar_one_or_none()


def _group_to_read(
    group: AssetGroup,
    asset_count: int,
    current_value: Decimal,
    current_value_primary: Decimal,
    currency: Optional[str] = None,
    institution_name: Optional[str] = None,
    account_type: Optional[str] = None,
    account_balance: Optional[Decimal] = None,
) -> AssetGroupRead:
    return AssetGroupRead(
        id=group.id,
        user_id=group.user_id,
        name=group.name,
        icon=group.icon,
        color=group.color,
        position=group.position,
        # The column is a plain String; the check constraint is what keeps it
        # inside the Literal's set.
        tax_treatment=cast(TaxTreatment, group.tax_treatment),
        source=group.source,
        connection_id=group.connection_id,
        institution_name=institution_name,
        account_type=account_type,
        asset_count=asset_count,
        # Decimal → round to 2dp → float at the API boundary. Precision is
        # preserved inside the sum; the float conversion is only for the
        # JSON response shape and is bounded to 2 decimals.
        current_value=float(current_value.quantize(Decimal("0.01"))),
        current_value_primary=float(current_value_primary.quantize(Decimal("0.01"))),
        account_balance=(
            None if account_balance is None else float(account_balance.quantize(Decimal("0.01")))
        ),
        currency=currency,
    )


async def _rollup(
    session: AsyncSession, group: AssetGroup, primary_currency: str
) -> tuple[int, Decimal, Decimal, Optional[str]]:
    """Compute (asset_count, current_value, current_value_primary, currency).

    `currency` is the one its holdings are denominated in, or None where they
    disagree or there are none — a wallet that has no single currency has no
    currency, and a caller that needs one has to say what it wants instead.

    Mirrors the filters used by dashboard net worth so wallet headers
    agree with the top-level total: excludes archived assets AND sold
    assets (sell_date is not null). Primary-currency totals use the
    same `fx_rate_service.convert` the dashboard does — no crude
    ratio tricks — so BRL/USD/EUR wallets all sum consistently.

    Aggregates in Decimal end-to-end to avoid float drift on portfolios
    with many small-value holdings (e.g. crypto) or when Pluggy
    publishes AssetValue amounts at 6-decimal precision.

    Scoped to the wallet's own workspace as well as its id: `group_id` is a
    bare foreign key, so a row written before the write paths validated it
    would otherwise be totalled in a workspace it does not belong to.
    """
    assets = await session.execute(
        select(Asset).where(
            Asset.group_id == group.id,
            Asset.workspace_id == group.workspace_id,
            Asset.is_archived == False,
            Asset.sell_date.is_(None),
        )
    )
    asset_list = list(assets.scalars().all())
    current_value, current_value_primary = await _sum_asset_values(
        session, asset_list, primary_currency
    )
    currencies = {asset.currency for asset in asset_list}
    return (
        len(asset_list),
        current_value,
        current_value_primary,
        currencies.pop() if len(currencies) == 1 else None,
    )


async def _sum_asset_values(
    session: AsyncSession, asset_list: list[Asset], primary_currency: str
) -> tuple[Decimal, Decimal]:
    current_value = Decimal("0")
    current_value_primary = Decimal("0")
    for asset in asset_list:
        latest = await _latest_value_amount(session, asset.id)
        if latest is None:
            # Fall back to purchase_price if no value history yet — same
            # logic _compute_current_value uses on the asset read path.
            latest = asset.purchase_price
        if latest is None:
            continue

        current_value += latest
        if asset.currency == primary_currency:
            current_value_primary += latest
        else:
            converted, _ = await convert(
                session, latest, asset.currency, primary_currency
            )
            current_value_primary += converted
    return current_value, current_value_primary


async def ungrouped_value(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> Decimal:
    """Primary-currency total of holdings sitting in no Wallet.

    A Wallet is where tax character lives, so a holding outside one has none —
    it cannot be bucketed, and any total built from tax character has to say
    how much it left behind rather than quietly shrink.
    """
    primary = await _primary_currency_for(session, user_id)
    result = await session.execute(
        select(Asset).where(
            Asset.workspace_id == workspace_id,
            Asset.group_id.is_(None),
            Asset.is_archived == False,
            Asset.sell_date.is_(None),
        )
    )
    _, primary_total = await _sum_asset_values(session, list(result.scalars().all()), primary)
    return primary_total


async def _institution_name_for(
    session: AsyncSession, group: AssetGroup
) -> Optional[str]:
    """Fetch the bank/broker institution name for a synced wallet.

    Prefers the wallet's own institution (issue #345), falling back to the
    connection's label. Returns None for manual wallets (no connection) or
    if the connection was deleted — callers render the subtitle
    conditionally on this.
    """
    if group.institution is not None:
        return group.institution.name
    if group.connection_id is None:
        return None
    row = await session.execute(
        select(BankConnection.institution_name).where(BankConnection.id == group.connection_id)
    )
    return row.scalar_one_or_none()


async def _account_type_and_balance_for(
    session: AsyncSession, group: AssetGroup, primary_currency: str
) -> tuple[Optional[str], Optional[Decimal]]:
    """The (type, balance) of the provider account a synced wallet mirrors.

    One wallet per provider account (#76), reached through the holdings it
    owns: the wallet's own `external_id` is "{connection}::{account}" and is
    digest-truncated when that overflows the column, so it does not parse back
    into an account id. `Asset.account_external_id` carries the attribution
    unchanged. This is the join that lets allocation be grouped by account
    type, and that carries the balance Liquid Cash is derived against.

    Both are None for manual wallets, for a connection-level wallet holding
    positions the provider attributed to no account, and whenever the wallet's
    holdings name more than one account — an unsplit legacy wallet has no
    single balance to subtract from, and guessing one would invent cash.

    The balance comes back in the primary currency, because the only thing that
    subtracts from it is `current_value_primary`. `Account.balance_primary` is
    not stored — the accounts API converts on read — so this converts the same
    way rather than trusting a column nothing fills.
    """
    rows = (
        await session.execute(
            select(Account.id, Account.type, Account.balance, Account.currency)
            .join(Asset, Asset.account_external_id == Account.external_id)
            .where(
                Asset.group_id == group.id,
                Account.workspace_id == group.workspace_id,
            )
            .distinct()
            .limit(2)
        )
    ).all()
    if len(rows) != 1:
        return None, None
    _, account_type, balance, currency = rows[0]
    if balance is None:
        return account_type, None
    if currency == primary_currency:
        return account_type, balance
    converted, _ = await convert(session, balance, currency, primary_currency)
    return account_type, converted


async def _primary_currency_for(session: AsyncSession, user_id: uuid.UUID) -> str:
    user = await session.get(User, user_id)
    if user:
        return user.primary_currency
    return get_settings().default_currency


async def get_groups(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> list[AssetGroupRead]:
    result = await session.execute(
        select(AssetGroup)
        .where(AssetGroup.workspace_id == workspace_id)
        .order_by(AssetGroup.position, AssetGroup.name)
    )
    groups = list(result.scalars().all())
    if not groups:
        return []
    primary = await _primary_currency_for(session, user_id)
    reads = []
    for g in groups:
        count, cv, cvp, ccy = await _rollup(session, g, primary)
        # Synced wallets are auto-generated by providers. Keep manual wallets
        # visible even when empty, but hide empty synced wallets (connected or
        # orphaned) to avoid duplicate provider placeholders like
        # "MeuPluggy 4 · 0 items" after reconnects/migrations.
        if g.source != "manual" and count == 0:
            continue
        institution = await _institution_name_for(session, g)
        account_type, balance = await _account_type_and_balance_for(session, g, primary)
        reads.append(_group_to_read(g, count, cv, cvp, ccy, institution, account_type, balance))
    return reads


async def get_group(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[AssetGroupRead]:
    result = await session.execute(
        select(AssetGroup).where(AssetGroup.id == group_id, AssetGroup.workspace_id == workspace_id)
    )
    group = result.scalar_one_or_none()
    if not group:
        return None
    primary = await _primary_currency_for(session, user_id)
    count, cv, cvp, ccy = await _rollup(session, group, primary)
    institution = await _institution_name_for(session, group)
    account_type, balance = await _account_type_and_balance_for(session, group, primary)
    return _group_to_read(group, count, cv, cvp, ccy, institution, account_type, balance)


async def _next_position(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    row = await session.execute(
        select(func.coalesce(func.max(AssetGroup.position), -1) + 1).where(
            AssetGroup.workspace_id == workspace_id
        )
    )
    return int(row.scalar() or 0)


async def create_group(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    data: AssetGroupCreate,
) -> AssetGroupRead:
    # Let the caller supply a position, otherwise append to the end so new
    # groups don't fight with existing drag-ordered ones.
    position = data.position
    if position == 0:
        position = await _next_position(session, workspace_id)
    group = AssetGroup(
        user_id=user_id,
        workspace_id=workspace_id,
        name=data.name,
        icon=data.icon,
        color=data.color,
        position=position,
        tax_treatment=data.tax_treatment,
        source="manual",
    )
    session.add(group)
    await session.commit()
    await session.refresh(group)
    return _group_to_read(group, 0, Decimal("0"), Decimal("0"), None, None)


async def update_group(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    data: AssetGroupUpdate,
) -> Optional[AssetGroupRead]:
    result = await session.execute(
        select(AssetGroup).where(AssetGroup.id == group_id, AssetGroup.workspace_id == workspace_id)
    )
    group = result.scalar_one_or_none()
    if not group:
        return None
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(group, key, value)
    await session.commit()
    await session.refresh(group)
    primary = await _primary_currency_for(session, user_id)
    count, cv, cvp, ccy = await _rollup(session, group, primary)
    institution = await _institution_name_for(session, group)
    account_type, balance = await _account_type_and_balance_for(session, group, primary)
    return _group_to_read(group, count, cv, cvp, ccy, institution, account_type, balance)


async def delete_group(session: AsyncSession, group_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
    result = await session.execute(
        select(AssetGroup).where(AssetGroup.id == group_id, AssetGroup.workspace_id == workspace_id)
    )
    group = result.scalar_one_or_none()
    if not group:
        return False
    await session.delete(group)
    await session.commit()
    return True


async def ensure_group_for_connection(
    session: AsyncSession,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
    source: str,
    external_id: Optional[str],
    default_name: str,
    institution_id: Optional[uuid.UUID] = None,
) -> AssetGroup:
    """Return (creating if absent) the group that owns a connection's assets.

    Called during sync before holdings are upserted so newly-created
    Assets can attach to the group in the same transaction. Matching
    prefers (user_id, workspace_id, source, external_id) when external_id is
    set; otherwise (user_id, workspace_id, source, connection_id). The name is
    only applied on creation — users are free to rename synced groups later.

    `workspace_id` is load-bearing: one user can link the same provider item
    once per workspace, so (user_id, source, external_id) is not unique and
    would otherwise match the other workspace's wallet. `user_id` is kept
    alongside it, so two members of a shared workspace who each link the same
    institution get a wallet apiece rather than sharing one row.
    """

    def _align(g: AssetGroup) -> AssetGroup:
        # Re-link if the connection was recreated. Name and workspace are
        # preserved; the query below is already scoped to this workspace.
        if g.connection_id != connection_id:
            g.connection_id = connection_id
        # Backfills groups that predate institution tracking (issue #345).
        if institution_id is not None and g.institution_id != institution_id:
            g.institution_id = institution_id
        return g

    # Prefer matching by external_id (Pluggy item id). Falls back to
    # connection_id which is less stable (connection can be deleted/recreated).
    query = select(AssetGroup).where(
        AssetGroup.user_id == user_id,
        AssetGroup.workspace_id == workspace_id,
        AssetGroup.source == source,
    )
    if external_id:
        query = query.where(AssetGroup.external_id == external_id)
    else:
        query = query.where(AssetGroup.connection_id == connection_id)

    result = await session.execute(query)
    group = result.scalar_one_or_none()
    if group:
        return _align(group)

    position = await _next_position(session, workspace_id)
    # Disambiguate when the user has multiple connections from the same
    # institution (common with Pluggy sandbox, where every item comes back
    # as "MeuPluggy"). Appends " 2", " 3", etc. until we find a free name.
    # User can rename freely afterwards without affecting sync matching,
    # which keys on external_id, not on name. Clamped so the disambiguating
    # suffix still fits the 100-char name column.
    unique_name = await _unique_default_name(session, workspace_id, default_name[:95])
    group = AssetGroup(
        user_id=user_id,
        workspace_id=workspace_id,
        name=unique_name,
        icon="wallet",
        color="#0EA5E9",
        position=position,
        source=source,
        connection_id=connection_id,
        external_id=external_id,
        institution_id=institution_id,
    )
    # A concurrent sync (scheduled + manual) can mint the same key; the
    # savepoint keeps the loser's IntegrityError from poisoning the
    # session — re-select and use the winner's row.
    try:
        async with session.begin_nested():
            session.add(group)
            await session.flush()
    except IntegrityError:
        result = await session.execute(query)
        group = _align(result.scalar_one())
    return group


async def _unique_default_name(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    base: str,
    exclude_group_id: Optional[uuid.UUID] = None,
) -> str:
    """Return `base` or the first free `base N` in this workspace.

    A group being renamed in place passes its own id so its current name
    doesn't count as taken.
    """
    query = select(AssetGroup.name).where(AssetGroup.workspace_id == workspace_id)
    if exclude_group_id is not None:
        query = query.where(AssetGroup.id != exclude_group_id)
    existing_rows = await session.execute(query)
    taken = {row[0] for row in existing_rows.all()}
    if base not in taken:
        return base
    # Start at 2 — "Nubank" and "Nubank 2" read naturally; "Nubank 1" looks wrong.
    i = 2
    while f"{base} {i}" in taken:
        i += 1
    return f"{base} {i}"
