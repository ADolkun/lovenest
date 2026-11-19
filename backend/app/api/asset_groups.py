import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.workspace_context import (
    WorkspaceContext,
    current_workspace,
    current_writable_workspace,
)
from app.schemas.asset_group import AssetGroupCreate, AssetGroupRead, AssetGroupUpdate
from app.services import asset_group_service

router = APIRouter(prefix="/api/asset-groups", tags=["asset-groups"])


# A wallet's tax treatment decides what the tax planner counts, and the tax
# routes are closed to access tokens (`_CLOSED_TO_TOKENS`). A token with
# `assets` scopes may manage wallets, but neither sets nor reads the treatment.
def _by_token(request: Request) -> bool:
    return getattr(request.state, "access_token", None) is not None


def _refuse_tax_treatment(request: Request, data: AssetGroupCreate | AssetGroupUpdate) -> None:
    if _by_token(request) and "tax_treatment" in data.model_fields_set:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An access token can't set a wallet's tax treatment.",
        )


def _for_caller(
    request: Request,
    read: AssetGroupRead | list[AssetGroupRead],
    status_code: int = status.HTTP_200_OK,
):
    if not _by_token(request):
        return read
    # Returned as-is: the response model would fill the field back in.
    reads = read if isinstance(read, list) else [read]
    payload = [group.model_dump(exclude={"tax_treatment"}) for group in reads]
    return JSONResponse(
        jsonable_encoder(payload if isinstance(read, list) else payload[0]),
        status_code=status_code,
    )


@router.get("", response_model=list[AssetGroupRead])
async def list_groups(
    request: Request,
    include_empty: bool = False,
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    groups = await asset_group_service.get_groups(
        session, ctx.workspace.id, ctx.user_id, include_empty=include_empty
    )
    return _for_caller(request, groups)


@router.post("", response_model=AssetGroupRead, status_code=status.HTTP_201_CREATED)
async def create_group(
    request: Request,
    data: AssetGroupCreate,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    _refuse_tax_treatment(request, data)
    group = await asset_group_service.create_group(session, ctx.workspace.id, ctx.user_id, data)
    return _for_caller(request, group, status.HTTP_201_CREATED)


@router.patch("/{group_id}", response_model=AssetGroupRead)
async def update_group(
    request: Request,
    group_id: uuid.UUID,
    data: AssetGroupUpdate,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    _refuse_tax_treatment(request, data)
    group = await asset_group_service.update_group(session, group_id, ctx.workspace.id, ctx.user_id, data)
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return _for_caller(request, group)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    deleted = await asset_group_service.delete_group(session, group_id, ctx.workspace.id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )
