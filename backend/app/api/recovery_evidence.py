"""Read-only recovery projection and explicitly reviewed evidence retention."""
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.workspace_context import WorkspaceContext, current_workspace, current_writable_workspace
from app.schemas.recovery_evidence import AssertionStatus, RecoveryPackage, RecoveryPreviewRequest, RecoveryRetainRequest, RecoveryReviewsRequest, Role, State
from app.services import recovery_evidence_service as service

router = APIRouter(prefix='/api/assets/recovery', tags=['recovery evidence'])


def filters(group_id: UUID, case_key: Annotated[str | None, Query(max_length=255)] = None,
            round_key: Annotated[str | None, Query(max_length=500)] = None, role: Role | None = None,
            state: State | AssertionStatus | None = None, q: Annotated[str | None, Query(max_length=500)] = None):
    return dict(group_id=group_id, case_key=case_key, round_key=round_key, role=role, relation_state=state, q=q)


@router.get('', response_model=RecoveryPackage)
async def list_recovery(scope: dict = Depends(filters), ctx: WorkspaceContext = Depends(current_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.list_recovery(session, ctx.workspace.id, **scope)


@router.post('/preview', response_model=RecoveryPackage)
async def preview_recovery(data: RecoveryPreviewRequest, ctx: WorkspaceContext = Depends(current_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.preview_recovery(session, ctx.workspace.id, data)


@router.post('/retain', response_model=RecoveryPackage)
async def retain_recovery(data: RecoveryRetainRequest, ctx: WorkspaceContext = Depends(current_writable_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.retain_recovery(session, ctx.workspace.id, ctx.user_id, data)


@router.post('/reviews', response_model=RecoveryPackage)
async def review_recovery(data: RecoveryReviewsRequest, ctx: WorkspaceContext = Depends(current_writable_workspace), session: AsyncSession = Depends(get_async_session)):
    return await service.review_recovery(session, ctx.workspace.id, ctx.user_id, data)


@router.get('/export')
async def export_recovery(format: Literal['json', 'csv'], scope: dict = Depends(filters), expected_revision: str | None = None,
                          ctx: WorkspaceContext = Depends(current_workspace), session: AsyncSession = Depends(get_async_session)):
    package = await service.list_recovery(session, ctx.workspace.id, **scope)
    if expected_revision is not None and expected_revision != package.revision:
        raise HTTPException(409, 'Recovery evidence changed; refresh before exporting')
    content = service.export_recovery(package, format, {key: str(value) if isinstance(value, UUID) else value for key, value in scope.items()})
    return Response(content, media_type='application/json' if format == 'json' else 'text/csv',
                    headers={'Content-Disposition': f'attachment; filename="recovery-evidence.{format}"'})
