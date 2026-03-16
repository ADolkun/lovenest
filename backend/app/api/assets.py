import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_active_user
from app.core.database import get_async_session
from app.models.user import User
from app.schemas.asset import AssetCreate, AssetRead, AssetUpdate, AssetValueCreate, AssetValueRead
from app.services import asset_service

router = APIRouter(prefix="/api/assets", tags=["assets"])


@router.get("", response_model=list[AssetRead])
async def list_assets(
    include_archived: bool = False,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    return await asset_service.get_assets(session, user.id, include_archived=include_archived)


@router.get("/portfolio-trend")
async def portfolio_trend(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    return await asset_service.get_portfolio_trend(session, user.id)


@router.get("/{asset_id}", response_model=AssetRead)
async def get_asset(
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    asset = await asset_service.get_asset(session, asset_id, user.id)
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    return asset


@router.post("", response_model=AssetRead, status_code=status.HTTP_201_CREATED)
async def create_asset(
    data: AssetCreate,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    return await asset_service.create_asset(session, user.id, data)


@router.patch("/{asset_id}", response_model=AssetRead)
async def update_asset(
    asset_id: uuid.UUID,
    data: AssetUpdate,
    regenerate_growth: bool = Query(False),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    asset = await asset_service.update_asset(session, asset_id, user.id, data, regenerate_growth=regenerate_growth)
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    return asset


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset(
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    deleted = await asset_service.delete_asset(session, asset_id, user.id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")


@router.get("/{asset_id}/values", response_model=list[AssetValueRead])
async def list_asset_values(
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    values = await asset_service.get_asset_values(session, asset_id, user.id)
    if values is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    return values


@router.get("/{asset_id}/value-trend")
async def get_asset_value_trend(
    asset_id: uuid.UUID,
    months: int = Query(12, ge=1, le=120),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    trend = await asset_service.get_asset_value_trend(session, asset_id, user.id, months=months)
    if trend is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    return trend


@router.post("/{asset_id}/values", response_model=AssetValueRead, status_code=status.HTTP_201_CREATED)
async def add_asset_value(
    asset_id: uuid.UUID,
    data: AssetValueCreate,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    value = await asset_service.add_asset_value(session, asset_id, user.id, data)
    if value is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    return value


@router.delete("/values/{value_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset_value(
    value_id: uuid.UUID,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    deleted = await asset_service.delete_asset_value(session, value_id, user.id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Value not found")
