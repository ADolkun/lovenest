import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_active_user
from app.core.database import get_async_session
from app.models.user import User
from app.schemas.asset_group import AssetGroupCreate, AssetGroupRead, AssetGroupUpdate
from app.services import asset_group_service

router = APIRouter(prefix="/api/asset-groups", tags=["asset-groups"])


@router.get("", response_model=list[AssetGroupRead])
async def list_groups(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    return await asset_group_service.get_groups(session, user.id)


@router.post("", response_model=AssetGroupRead, status_code=status.HTTP_201_CREATED)
async def create_group(
    data: AssetGroupCreate,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    return await asset_group_service.create_group(session, user.id, data)


@router.patch("/{group_id}", response_model=AssetGroupRead)
async def update_group(
    group_id: uuid.UUID,
    data: AssetGroupUpdate,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    group = await asset_group_service.update_group(session, group_id, user.id, data)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return group


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    deleted = await asset_group_service.delete_group(session, group_id, user.id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )
