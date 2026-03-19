from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_active_user
from app.core.database import get_async_session
from app.models.user import User
from app.schemas.report import ReportResponse
from app.services import report_service

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/net-worth", response_model=ReportResponse)
async def get_net_worth(
    months: int = Query(12, ge=1, le=24),
    interval: str = Query("monthly", pattern="^(daily|weekly|monthly|yearly)$"),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    currency = getattr(user, "preferences", None)
    currency_display = "BRL"
    if currency and isinstance(currency, dict):
        currency_display = currency.get("currency_display", "BRL")

    return await report_service.get_net_worth_report(
        session, user.id, months, interval, currency_display
    )


@router.get("/income-expenses", response_model=ReportResponse)
async def get_income_expenses(
    months: int = Query(12, ge=1, le=24),
    interval: str = Query("monthly", pattern="^(daily|weekly|monthly|yearly)$"),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    currency = getattr(user, "preferences", None)
    currency_display = "BRL"
    if currency and isinstance(currency, dict):
        currency_display = currency.get("currency_display", "BRL")

    return await report_service.get_income_expenses_report(
        session, user.id, months, interval, currency_display
    )
