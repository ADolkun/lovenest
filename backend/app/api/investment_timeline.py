"""Authenticated local evidence reads; opening activity never starts collection."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.workspace_context import WorkspaceContext, current_workspace
from app.schemas.investment_timeline import TimelineEvent, TimelineRead, TimelineSourceDetail
from app.services import investment_timeline_service as service

router = APIRouter(prefix="/timeline")


@router.get("", response_model=TimelineRead)
async def list_timeline(
    response: Response,
    group_id: UUID | None = None,
    collection_id: UUID | None = None,
    asset_id: UUID | None = None,
    canonical_asset_key: str | None = Query(None, max_length=128),
    source: str | None = Query(None, max_length=64),
    status: str | None = Query(None, max_length=64),
    kind: str | None = Query(None, max_length=64),
    direction: Literal["in", "out", "unknown"] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    expected_revision: str | None = Query(None, max_length=64),
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    response.headers["Cache-Control"] = "no-store"
    return await service.list_timeline(
        session, ctx.workspace.id, group_id=group_id, collection_id=collection_id, asset_id=asset_id,
        canonical_asset_key=canonical_asset_key, source=source, status=status, kind=kind, direction=direction,
        since=since, until=until, limit=limit, offset=offset, expected_revision=expected_revision,
    )


@router.get("/sources/{source_id}", response_model=TimelineSourceDetail)
async def get_timeline_source(
    source_id: str,
    response: Response,
    group_id: UUID | None = None,
    collection_id: UUID | None = None,
    event_id: str | None = None,
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    response.headers["Cache-Control"] = "no-store"
    return await service.get_timeline_source(session, ctx.workspace.id, source_id, group_id=group_id, collection_id=collection_id, event_id=event_id)


@router.get("/{event_id}", response_model=TimelineEvent)
async def get_timeline_event(
    event_id: str,
    response: Response,
    group_id: UUID | None = None,
    collection_id: UUID | None = None,
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    response.headers["Cache-Control"] = "no-store"
    return await service.get_timeline_event(session, ctx.workspace.id, event_id, group_id=group_id, collection_id=collection_id)
