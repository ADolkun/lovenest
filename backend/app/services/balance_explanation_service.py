"""Explain existing balances without repairing or converting financial records."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_value import AssetValue, latest_value_first
from app.models.bank_connection import BankConnection
from app.schemas.balance_explanation import BalanceExplanation, BalanceHolding
from app.services.asset_valuation import current_value_amount


def _timestamp(value) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    # Stored database clocks are UTC; source strings must declare their zone.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc) if isinstance(value, datetime) else None
    return parsed.astimezone(timezone.utc)


def _number(value) -> float | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
        return float(amount) if amount.is_finite() else None
    except ArithmeticError:
        return None


def _same_amount(left, right) -> bool:
    try:
        first, second = Decimal(str(left)), Decimal(str(right))
        return first.is_finite() and second.is_finite() and first == second
    except ArithmeticError:
        return False


def _reconcile(detail: BalanceExplanation, scope_matches: bool) -> None:
    reasons = detail.reason_codes
    if detail.basis == "cash_ledger":
        detail.reconciliation = "separate_cash_ledger"
        reasons.append("separate_cash_ledger")
        return
    if detail.basis == "connector_calculated_subtotal":
        detail.reconciliation = "shared_derivation"
        reasons.append("shared_derivation")
        return
    if detail.basis != "reported_account_total":
        reasons.append("metadata_unknown")
        return
    if not scope_matches:
        reasons.append("account_scope_mismatch")
    if detail.currency != detail.holdings_currency:
        reasons.append("currency_mismatch")
    if detail.observed_at is None or detail.observation_basis != "source" or any(
        h.observed_at is None or h.observation_basis not in {"source", "quote"}
        for h in detail.holdings
    ):
        reasons.append("observation_time_unknown")
    elif any(h.observed_at != detail.observed_at for h in detail.holdings):
        reasons.append("observation_time_mismatch")
    if (
        not scope_matches or detail.currency != detail.holdings_currency
        or detail.coverage != "complete" or detail.holdings_coverage != "complete"
        or detail.refresh_status != "active" or detail.amount is None
        or detail.holdings_value is None
        or "observation_time_unknown" in reasons or "observation_time_mismatch" in reasons
    ):
        return
    difference = Decimal(str(detail.amount)) - Decimal(str(detail.holdings_value))
    detail.difference = float(difference)
    detail.residual_cash = float(max(Decimal("0"), difference))
    detail.reconciliation = "residual_derived" if difference >= 0 else "difference"
    if difference < 0:
        reasons.append("balance_difference")


async def explain_balance(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    account: Account | None = None,
    connection: BankConnection | None = None,
    current_balance: Decimal | float | None = None,
    group: AssetGroup | None = None,
) -> BalanceExplanation:
    """One account or wallet scope, joined only through persisted identities."""
    if account is not None and account.workspace_id != workspace_id:
        account = None
    if connection is not None and (
        connection.workspace_id != workspace_id
        or (account is not None and connection.id != account.connection_id)
    ):
        connection = None
    detail = BalanceExplanation(
        workspace_id=workspace_id,
        wallet_id=group.id if group is not None else None,
        account_id=account.id if account is not None else None,
        connection_id=group.connection_id if group is not None else (
            account.connection_id if account is not None else None
        ),
    )
    settings = (connection.settings or {}) if connection is not None else {}
    refresh = settings.get("holdings_observation") or {}
    snapshot = (settings.get("account_balance_observations") or {}).get(
        account.external_id if account is not None else None,
    ) or {}
    holdings_unconfirmed = snapshot.get("holdings_refresh_status") == "unconfirmed"
    if account is not None and snapshot and (
        snapshot.get("currency") != account.currency
        or not _same_amount(snapshot.get("amount"), account.balance)
    ):
        snapshot = {}
        detail.reason_codes.append("account_observation_changed")
    if connection is not None:
        detail.refresh_status = connection.status or "unknown"
        detail.last_successful_sync_at = _timestamp(connection.last_sync_at)
        detail.refresh_observed_at = _timestamp(refresh.get("observed_at"))
        count = refresh.get("unreadable_count")
        detail.unreadable_count = count if type(count) is int and count >= 0 else None
        if detail.refresh_status in {"error", "sync_error"}:
            detail.reason_codes.append("refresh_failed")
    if account is None:
        detail.reason_codes.append("no_account_association")
    else:
        detail.association = "resolved"
        detail.currency = account.currency
        detail.amount = _number(account.balance if account.connection_id else current_balance)
        if account.connection_id and account.type == "credit_card" and detail.amount is not None:
            detail.amount = -detail.amount
        if account.connection_id is None:
            detail.basis = "cash_ledger"
            detail.coverage = "complete" if current_balance is not None else "unknown"
            detail.refresh_status = "active"
        elif connection is not None:
            if connection.provider == "onchain" or (
                connection.provider == "coinbase" and account.type == "investment"
            ):
                detail.basis = "connector_calculated_subtotal"
            elif connection.provider in {"pluggy", "simplefin", "enable_banking", "coinbase"}:
                detail.basis = "reported_account_total"
            detail.observed_at = _timestamp(snapshot.get("observed_at"))
            if snapshot.get("observation_basis") in {"source", "collection"}:
                detail.observation_basis = snapshot["observation_basis"]
            if snapshot.get("coverage") in {"complete", "partial"}:
                detail.coverage = snapshot["coverage"]
            if snapshot.get("value_available") is False:
                detail.amount = None
                detail.reason_codes.append("account_balance_unavailable")
            if snapshot.get("currency_available") is False:
                detail.currency = None
                detail.reason_codes.append("currency_unknown")
            count = snapshot.get("omitted_count")
            detail.omitted_count = count if type(count) is int and count >= 0 else None
            if detail.omitted_count:
                detail.reason_codes.append("connector_omissions")
            if account.external_id in settings.get("unavailable_account_balance_ids", []):
                detail.coverage = "partial"
                detail.reason_codes.append("account_balance_unavailable")
                if not snapshot and account.balance == 0:
                    detail.amount = None
    if refresh.get("complete") is False:
        detail.coverage = "partial"
        detail.reason_codes.append("partial_refresh")
    if holdings_unconfirmed:
        detail.coverage = "partial"
        detail.reason_codes.append("holdings_refresh_unconfirmed")
    detail.reason_codes.extend(sorted(set(refresh.get("reasons") or []) & {
        "native_unavailable", "untrusted_price", "time_budget", "token_position_limit",
        "token_inventory_unavailable", "token_inventory_unreadable", "token_price_unavailable",
        "token_request_limit", "token_rate_limited", "token_index_unavailable", "token_index_unreadable",
    }))
    if refresh.get("account_balance_reason") == "account_observation_changed":
        detail.reason_codes.append("account_observation_changed")
    if detail.coverage == "unknown":
        detail.reason_codes.append("coverage_unknown")
    if detail.observed_at is None:
        detail.reason_codes.append("observation_time_unknown")

    account_filter = false()
    if account is not None:
        account_filter = and_(
            Asset.connection_id == account.connection_id,
            Asset.account_external_id == account.external_id,
        ) if account.connection_id is not None and account.external_id is not None else (
            Asset.group_id.in_(select(AssetGroup.id).where(
                AssetGroup.workspace_id == workspace_id, AssetGroup.account_id == account.id,
            )) if account.connection_id is None else false()
        )
    assets = list((await session.execute(select(Asset).where(
        Asset.workspace_id == workspace_id, Asset.is_archived == False,  # noqa: E712
        Asset.sell_date.is_(None),
        or_(account_filter, Asset.group_id == group.id if group is not None else false()),
    ))).scalars().all())
    linked_ids = set((await session.execute(select(AssetGroup.id).where(
        AssetGroup.workspace_id == workspace_id, AssetGroup.account_id == account.id,
    ))).scalars().all()) if account is not None and account.connection_id is None else set()
    account_assets = [a for a in assets if account is not None and (
        a.group_id in linked_ids if account.connection_id is None else (
            a.connection_id == account.connection_id
            and account.external_id is not None and a.account_external_id == account.external_id
        )
    )]
    selected = [a for a in assets if a.group_id == group.id] if group is not None else account_assets
    values: list[Decimal] = []
    currencies = {a.currency for a in selected}
    for asset in selected:
        latest = (await session.execute(select(AssetValue).where(
            AssetValue.asset_id == asset.id, AssetValue.workspace_id == workspace_id,
        ).order_by(*latest_value_first()).limit(1))).scalar_one_or_none()
        value = current_value_amount(asset, latest.amount if latest is not None else None)
        quote = asset.valuation_method == "market_price" and asset.last_price is not None and asset.units is not None
        moment = asset.last_price_at if quote else (
            latest.recorded_at if latest is not None and latest.source != "rule" else None
        )
        observation_basis = "quote" if quote else "recorded" if moment is not None else "unknown"
        source_observation = (asset.external_metadata or {}).get("valuation_observation")
        # An optional provider observation describes a specific sync value.
        # Neither same-day manual edits nor a later changed amount inherit it.
        if (
            not quote and latest is not None and latest.source == "sync"
            and isinstance(source_observation, dict)
            and source_observation.get("currency") == asset.currency
            and _same_amount(source_observation.get("amount"), value)
            and _timestamp(source_observation.get("observed_at")) is not None
        ):
            moment = _timestamp(source_observation["observed_at"])
            observation_basis = "source"
        holding = BalanceHolding(
            asset_id=asset.id, wallet_id=asset.group_id, name=asset.name,
            ticker=asset.ticker, type=asset.type, quantity=_number(asset.units),
            value=_number(value), currency=asset.currency, observed_at=_timestamp(moment),
            observation_basis=observation_basis,
            # A sync row's date is its local daily bucket, not the source's
            # valuation date. Only explicitly dated app valuations expose it.
            value_date=latest.date if latest is not None and latest.source != "sync" and not quote else None,
        )
        detail.holdings.append(holding)
        if holding.value is not None and value is not None:
            values.append(value)
    missing = len(selected) - len(values)
    detail.holdings_currency = currencies.pop() if len(currencies) == 1 else (
        account.currency if not selected and account is not None else None
    )
    detail.holdings_value = float(sum(values, Decimal("0"))) if (
        detail.holdings_currency is not None and (values or not selected)
    ) else None
    detail.holdings_coverage = "complete" if not missing else "partial" if values else "unavailable"
    if missing:
        detail.reason_codes.append("missing_holding_value")
    # Complete valuation of loaded rows is not proof that the provider returned
    # the entire account inventory. An explicit source count must agree too.
    scope_matches = (
        account is not None and snapshot.get("holdings_count") == len(account_assets)
        and snapshot.get("holdings_complete") is True
        and {a.id for a in selected} == {a.id for a in account_assets}
        and len({a.group_id for a in account_assets}) <= 1
    )
    _reconcile(detail, scope_matches)
    detail.reason_codes = list(dict.fromkeys(detail.reason_codes))
    return detail
