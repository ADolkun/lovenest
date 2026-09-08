"""Reviewed owned-transfer actions on the existing Assets evidence surface."""
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.workspace_context import WorkspaceContext, current_workspace, current_writable_workspace
from app.schemas.owned_transfer import (
    IncidentCreate, IncidentRead, LotsRead, MovementApplicationRead, MovementConfirmRequest,
    MovementPreview, MovementPreviewRequest, OwnershipCreate, OwnershipRead, TransferConfirmRequest,
    TransferIndex, TransferPreview, TransferPreviewRequest, TransferRead,
)
from app.services import owned_transfer_service as service

router = APIRouter(prefix="/evidence")


@router.get("/transfers", response_model=TransferIndex)
async def list_transfers(ctx: WorkspaceContext = Depends(current_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.list_transfers(session, ctx.workspace.id)


@router.post("/ownership", response_model=OwnershipRead)
async def create_ownership(data: OwnershipCreate, ctx: WorkspaceContext = Depends(current_writable_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.create_ownership(session, ctx.workspace.id, ctx.user_id, data)


@router.delete("/ownership/{identifier}", response_model=OwnershipRead)
async def revoke_ownership(identifier: UUID, expected_revision: str, ctx: WorkspaceContext = Depends(current_writable_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.revoke_ownership(session, ctx.workspace.id, identifier, expected_revision)


@router.get("/transfers/lots", response_model=LotsRead)
async def available_lots(asset_id: UUID, before_leg_id: UUID | None = None, ctx: WorkspaceContext = Depends(current_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.available_lots(session, ctx.workspace.id, asset_id, before_leg_id)


@router.post("/transfers/preview", response_model=TransferPreview)
async def preview_transfer(data: TransferPreviewRequest, ctx: WorkspaceContext = Depends(current_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.preview_transfer(session, ctx.workspace.id, data)


@router.post("/transfers", response_model=TransferRead)
async def confirm_transfer(data: TransferConfirmRequest, ctx: WorkspaceContext = Depends(current_writable_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.confirm_transfer(session, ctx.workspace.id, ctx.user_id, data)


@router.get("/transfers/{identifier}", response_model=TransferRead)
async def get_transfer(identifier: UUID, ctx: WorkspaceContext = Depends(current_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.get_transfer(session, ctx.workspace.id, identifier)


@router.delete("/transfers/{identifier}", response_model=TransferRead)
async def reverse_transfer(identifier: UUID, expected_revision: str, ctx: WorkspaceContext = Depends(current_writable_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.reverse_transfer(session, ctx.workspace.id, identifier, expected_revision)


@router.post("/movements/preview", response_model=MovementPreview)
async def preview_movement(data: MovementPreviewRequest, ctx: WorkspaceContext = Depends(current_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.preview_movement(session, ctx.workspace.id, data)


@router.post("/movements", response_model=MovementApplicationRead)
async def apply_movement(data: MovementConfirmRequest, ctx: WorkspaceContext = Depends(current_writable_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.apply_movement(session, ctx.workspace.id, ctx.user_id, data)


@router.delete("/movements/{identifier}", response_model=MovementApplicationRead)
async def reverse_movement(identifier: UUID, expected_revision: str, ctx: WorkspaceContext = Depends(current_writable_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.reverse_movement(session, ctx.workspace.id, identifier, expected_revision)


@router.post("/incidents", response_model=IncidentRead)
async def create_incident(data: IncidentCreate, ctx: WorkspaceContext = Depends(current_writable_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.annotate_incident(session, ctx.workspace.id, ctx.user_id, data)


@router.put("/incidents/{identifier}", response_model=IncidentRead)
async def update_incident(identifier: UUID, data: IncidentCreate, ctx: WorkspaceContext = Depends(current_writable_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.annotate_incident(session, ctx.workspace.id, ctx.user_id, data, identifier)
