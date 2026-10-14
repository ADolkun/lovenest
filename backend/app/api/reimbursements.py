import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.transactions import _attach_reimbursement_links, _tag_fx_fallback
from app.core.database import get_async_session
from app.core.workspace_context import (
    WorkspaceContext,
    current_workspace,
    current_writable_workspace,
)
from app.schemas.reimbursement import ReimbursementAllocationsUpdate, ReimbursementCandidate
from app.schemas.transaction import ReimbursementLinkRead, TransactionRead
from app.services import reimbursement_service

router = APIRouter(prefix="/api/transactions/{transaction_id}", tags=["reimbursements"])


@router.get("/reimbursements", response_model=list[ReimbursementLinkRead])
async def list_reimbursements(
    transaction_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        return await reimbursement_service.get_reimbursements(
            session, ctx.workspace.id, transaction_id
        )
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.put("/reimbursements", response_model=list[ReimbursementLinkRead])
async def replace_reimbursements(
    transaction_id: uuid.UUID,
    data: ReimbursementAllocationsUpdate,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        return await reimbursement_service.replace_reimbursements(
            session,
            ctx.workspace.id,
            transaction_id,
            [(a.expense_id, a.amount) for a in data.allocations],
        )
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete("/reimbursements/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_reimbursement(
    transaction_id: uuid.UUID,
    link_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        await reimbursement_service.delete_reimbursement(
            session, ctx.workspace.id, transaction_id, link_id
        )
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.get("/reimbursement-candidates", response_model=list[ReimbursementCandidate])
async def list_reimbursement_candidates(
    transaction_id: uuid.UUID,
    q: Optional[str] = Query(None, description="Search description or payee, ignoring the date window"),
    limit: int = Query(20, ge=1, le=100),
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        rows = await reimbursement_service.reimbursement_candidates(
            session, ctx.workspace.id, transaction_id, q=q, limit=limit
        )
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    primary_currency = ctx.user.primary_currency
    items = [
        _tag_fx_fallback(TransactionRead.model_validate(tx, from_attributes=True), primary_currency)
        for tx, _, _ in rows
    ]
    await _attach_reimbursement_links(session, ctx, items)
    return [
        ReimbursementCandidate(
            transaction=item, remaining=float(remaining), allocated_here=float(here)
        )
        for item, (_, remaining, here) in zip(items, rows)
    ]
