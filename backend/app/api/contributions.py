import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.workspace_context import (
    WorkspaceContext,
    current_workspace,
    current_writable_workspace,
)
from app.schemas.asset_contribution import (
    AssetContributionCreate,
    AssetContributionRead,
    AssetContributionUpdate,
    ContributionSummaryRead,
)
from app.schemas.contribution_import import (
    ContributionImportPreview,
    ContributionImportResult,
)
from app.services import contribution_import_service, contribution_service

router = APIRouter(prefix="/api/contributions", tags=["contributions"])


@router.get("", response_model=list[AssetContributionRead])
async def list_contributions(
    group_id: uuid.UUID | None = Query(None),
    tax_year: int | None = Query(None, ge=1900, le=2200),
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    return await contribution_service.list_contributions(
        session, ctx.workspace.id, group_id=group_id, tax_year=tax_year, as_of=date.today()
    )


@router.get("/summary", response_model=list[ContributionSummaryRead])
async def contribution_summary(
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """Net Contribution per wallet, with the employer and vesting split and the
    per-year totals annual limits are measured against."""
    return await contribution_service.summaries(
        session, ctx.workspace.id, ctx.user_id, as_of=date.today()
    )


@router.post("", response_model=AssetContributionRead, status_code=status.HTTP_201_CREATED)
async def create_contribution(
    data: AssetContributionCreate,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    return await contribution_service.create_contribution(
        session, ctx.workspace.id, data, as_of=date.today()
    )


@router.patch("/{contribution_id}", response_model=AssetContributionRead)
async def update_contribution(
    contribution_id: uuid.UUID,
    data: AssetContributionUpdate,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    row = await contribution_service.update_contribution(
        session, contribution_id, ctx.workspace.id, data, as_of=date.today()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Contribution not found"
        )
    return row


@router.delete("/{contribution_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contribution(
    contribution_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    deleted = await contribution_service.delete_contribution(
        session, contribution_id, ctx.workspace.id
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Contribution not found"
        )


@router.post("/import/preview", response_model=ContributionImportPreview)
async def preview_contribution_import(
    file: UploadFile = File(...),
    group_id: uuid.UUID = Form(...),
    account: str | None = Form(None),
    date_format: str | None = Form(None),
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """Read an account history and say what importing it would do. Writes nothing.

    Read-gated like the order-import preview: a viewer may look at a file
    without being able to commit it.
    """
    return await contribution_import_service.preview(
        session,
        ctx.workspace.id,
        await file.read(),
        group_id=group_id,
        account=account,
        date_format=date_format,
    )


@router.post("/import", response_model=ContributionImportResult)
async def import_contributions(
    file: UploadFile = File(...),
    group_id: uuid.UUID = Form(...),
    account: str | None = Form(None),
    date_format: str | None = Form(None),
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    return await contribution_import_service.import_contributions(
        session,
        ctx.workspace.id,
        ctx.user_id,
        await file.read(),
        group_id=group_id,
        account=account,
        filename=file.filename,
        date_format=date_format,
    )
