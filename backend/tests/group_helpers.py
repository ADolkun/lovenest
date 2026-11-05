from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import GroupMember
from app.schemas.group import GroupMemberCreate
from app.services import group_service


async def create_member(
    session: AsyncSession,
    group_id: UUID,
    workspace_id: UUID,
    payload: GroupMemberCreate,
) -> GroupMember:
    member = await group_service.create_member(session, group_id, workspace_id, payload)
    assert member is not None
    return member
