import logging
import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import get_settings
from app.models.fx_rate import FxRate
from app.models.user import User
from app.providers.openexchangerates import OpenExchangeRatesProvider

logger = logging.getLogger(__name__)

_provider = OpenExchangeRatesProvider()


async def sync_rates(
    session: AsyncSession, target_date: Optional[date] = None
) -> int:
    """Fetch rates from the provider for the given date and upsert into fx_rates.

    Only saves rates for currencies in `supported_currencies`.
    Idempotent — existing rates for the same date are updated.
    Returns the number of rates synced.
    """
    target = target_date or date.today()
    supported = set(get_settings().supported_currencies.split(","))

    if target == date.today():
        rates = await _provider.fetch_latest()
    else:
        rates = await _provider.fetch_historical(target)

    count = 0
    for currency_code, rate in rates.items():
        if currency_code not in supported:
            continue
        stmt = pg_insert(FxRate).values(
            base_currency="USD",
            quote_currency=currency_code,
            date=target,
            rate=rate,
            source=_provider.name,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_fx_rate_base_quote_date",
            set_={"rate": rate, "source": _provider.name},
        )
        await session.execute(stmt)
        count += 1

    await session.commit()
    logger.info("Synced %d FX rates for %s", count, target)
    return count


async def get_rate(
    session: AsyncSession,
    from_currency: str,
    to_currency: str,
    target_date: Optional[date] = None,
) -> Decimal:
    """Get FX rate from from_currency to to_currency.

    Uses cross-rate through USD: rate = usd_to_target / usd_to_source.
    Priority: exact date → on-demand fetch → closest available → fallback 1:1.
    """
    if from_currency == to_currency:
        return Decimal("1")

    target = target_date or date.today()

    # Step 1: Try exact date
    usd_to_source = await _get_exact_date_rate(session, from_currency, target)
    usd_to_target = await _get_exact_date_rate(session, to_currency, target)

    # Step 2: If missing, fetch from provider for exact date
    if usd_to_source is None or usd_to_target is None:
        try:
            synced = await sync_rates(session, target)
            if synced > 0:
                logger.info("On-demand sync fetched %d rates for %s", synced, target)
                if usd_to_source is None:
                    usd_to_source = await _get_exact_date_rate(session, from_currency, target)
                if usd_to_target is None:
                    usd_to_target = await _get_exact_date_rate(session, to_currency, target)
        except Exception:
            logger.warning("On-demand FX rate sync failed for %s", target, exc_info=True)

    # Step 3: Fall back to closest available date
    if usd_to_source is None:
        usd_to_source = await _get_closest_rate(session, from_currency, target)
    if usd_to_target is None:
        usd_to_target = await _get_closest_rate(session, to_currency, target)

    if from_currency == "USD":
        usd_to_source = Decimal("1")
    if to_currency == "USD":
        usd_to_target = Decimal("1")

    if usd_to_source is None or usd_to_target is None:
        logger.warning(
            "No FX rate found for %s -> %s on %s, returning 1",
            from_currency, to_currency, target,
        )
        return Decimal("1")

    if usd_to_source == 0:
        return Decimal("1")

    return usd_to_target / usd_to_source


async def _get_exact_date_rate(session: AsyncSession, currency: str, target: date) -> Optional[Decimal]:
    """Get the rate for an exact date."""
    if currency == "USD":
        return Decimal("1")
    result = await session.scalar(
        select(FxRate.rate)
        .where(
            FxRate.base_currency == "USD",
            FxRate.quote_currency == currency,
            FxRate.date == target,
        )
    )
    return result


async def _get_closest_rate(session: AsyncSession, currency: str, target: date) -> Optional[Decimal]:
    """Get the closest available rate to a target date (preferring before, then after)."""
    if currency == "USD":
        return Decimal("1")
    # Try closest before or on target date
    result = await session.scalar(
        select(FxRate.rate)
        .where(
            FxRate.base_currency == "USD",
            FxRate.quote_currency == currency,
            FxRate.date <= target,
        )
        .order_by(desc(FxRate.date))
        .limit(1)
    )
    if result is not None:
        return result
    # Try closest after target date
    from sqlalchemy import asc
    result = await session.scalar(
        select(FxRate.rate)
        .where(
            FxRate.base_currency == "USD",
            FxRate.quote_currency == currency,
            FxRate.date > target,
        )
        .order_by(asc(FxRate.date))
        .limit(1)
    )
    return result


async def _get_latest_rate(session: AsyncSession, currency: str) -> Optional[Decimal]:
    """Get the most recent rate for a currency vs USD."""
    if currency == "USD":
        return Decimal("1")
    result = await session.scalar(
        select(FxRate.rate)
        .where(
            FxRate.base_currency == "USD",
            FxRate.quote_currency == currency,
        )
        .order_by(desc(FxRate.date))
        .limit(1)
    )
    return result


async def _get_month_closing_rate(
    session: AsyncSession, currency: str, target: date
) -> Optional[Decimal]:
    """Get the last available rate within the target's month."""
    if currency == "USD":
        return Decimal("1")
    month_start = target.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)

    result = await session.scalar(
        select(FxRate.rate)
        .where(
            FxRate.base_currency == "USD",
            FxRate.quote_currency == currency,
            FxRate.date >= month_start,
            FxRate.date < month_end,
        )
        .order_by(desc(FxRate.date))
        .limit(1)
    )
    return result


async def convert(
    session: AsyncSession,
    amount: Decimal,
    from_currency: str,
    to_currency: str,
    target_date: Optional[date] = None,
) -> tuple[Decimal, Decimal]:
    """Convert an amount from one currency to another.

    Returns (converted_amount, rate_used).
    """
    if from_currency == to_currency:
        return amount, Decimal("1")

    rate = await get_rate(session, from_currency, to_currency, target_date)
    converted = amount * rate
    return converted.quantize(Decimal("0.01")), rate


async def stamp_primary_amount(
    session: AsyncSession,
    user_id: uuid.UUID,
    obj,
    amount_field: str = "amount",
    primary_field: str = "amount_primary",
    rate_field: str = "fx_rate_used",
    date_field: str = "date",
    currency_field: str = "currency",
) -> None:
    """Set obj's primary amount and fx_rate_used based on user's primary currency.

    Works for Transaction, RecurringTransaction, etc.
    """
    user = await session.get(User, user_id)
    if not user:
        return

    primary_currency = user.primary_currency
    obj_currency = getattr(obj, currency_field, get_settings().default_currency)
    obj_amount = getattr(obj, amount_field, None)

    if obj_amount is None:
        return

    obj_date = getattr(obj, date_field, None)
    converted, rate = await convert(session, Decimal(str(obj_amount)), obj_currency, primary_currency, obj_date)

    setattr(obj, primary_field, converted)
    if hasattr(obj, rate_field):
        setattr(obj, rate_field, rate)
