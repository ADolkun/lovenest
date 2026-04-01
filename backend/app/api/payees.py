import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_active_user
from app.core.database import get_async_session
from app.models.user import User
from app.schemas.payee import PayeeCreate, PayeeMergeRequest, PayeeRead, PayeeSummary, PayeeUpdate
from app.schemas.category import CategoryRead
from app.services import payee_service

router = APIRouter(prefix="/api/payees", tags=["payees"])


@router.get("", response_model=list[PayeeRead])
async def list_payees(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    return await payee_service.get_payees(session, user.id)


@router.get("/{payee_id}", response_model=PayeeRead)
async def get_payee(
    payee_id: uuid.UUID,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    payee = await payee_service.get_payee(session, payee_id, user.id)
    if not payee:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payee not found")
    return payee


@router.get("/{payee_id}/summary", response_model=PayeeSummary)
async def get_payee_summary(
    payee_id: uuid.UUID,
    start_date: Optional[date] = Query(None, alias="from"),
    end_date: Optional[date] = Query(None, alias="to"),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    try:
        result = await payee_service.get_payee_summary(session, payee_id, user.id, start_date, end_date)
        return PayeeSummary(
            payee=PayeeRead.model_validate(result["payee"], from_attributes=True),
            total_spent=result["total_spent"],
            total_received=result["total_received"],
            transaction_count=result["transaction_count"],
            most_common_category=CategoryRead.model_validate(result["most_common_category"], from_attributes=True) if result["most_common_category"] else None,
            last_transaction_date=result["last_transaction_date"],
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("", response_model=PayeeRead, status_code=status.HTTP_201_CREATED)
async def create_payee(
    data: PayeeCreate,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    try:
        return await payee_service.create_payee(session, user.id, data)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.patch("/{payee_id}", response_model=PayeeRead)
async def update_payee(
    payee_id: uuid.UUID,
    data: PayeeUpdate,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    try:
        payee = await payee_service.update_payee(session, payee_id, user.id, data)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not payee:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payee not found")
    return payee


@router.delete("/{payee_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_payee(
    payee_id: uuid.UUID,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    deleted = await payee_service.delete_payee(session, payee_id, user.id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payee not found")


@router.post("/merge")
async def merge_payees(
    data: PayeeMergeRequest,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    try:
        reassigned = await payee_service.merge_payees(session, user.id, data.target_id, data.source_ids)
        return {"merged": len(data.source_ids), "transactions_reassigned": reassigned}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
