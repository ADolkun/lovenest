import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_active_user
from app.core.database import get_async_session
from app.models.user import User
from app.schemas.goal import GoalCreate, GoalRead, GoalSummary, GoalUpdate
from app.services import goal_service

router = APIRouter(prefix="/api/goals", tags=["goals"])


@router.get("", response_model=list[GoalRead])
async def list_goals(
    status: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    return await goal_service.get_goals(session, user.id, status)


@router.get("/summary", response_model=list[GoalSummary])
async def goal_summary(
    limit: int = Query(3, ge=1, le=10),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    return await goal_service.get_goal_summary(session, user.id, limit)


@router.post("", response_model=GoalRead, status_code=status.HTTP_201_CREATED)
async def create_goal(
    data: GoalCreate,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    try:
        return await goal_service.create_goal(session, user.id, data)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/{goal_id}", response_model=GoalRead)
async def get_goal(
    goal_id: uuid.UUID,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    goal = await goal_service.get_goal(session, goal_id, user.id)
    if not goal:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Goal not found")
    return goal


@router.patch("/{goal_id}", response_model=GoalRead)
async def update_goal(
    goal_id: uuid.UUID,
    data: GoalUpdate,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    goal = await goal_service.update_goal(session, goal_id, user.id, data)
    if not goal:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Goal not found")
    return goal


@router.delete("/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_goal(
    goal_id: uuid.UUID,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
):
    deleted = await goal_service.delete_goal(session, goal_id, user.id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Goal not found")
