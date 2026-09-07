import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_async_session
from app.core.rate_limit import onchain_trace_rate_limit
from app.core.workspace_context import WorkspaceContext, current_workspace, current_writable_workspace
from app.models.bank_connection import BankConnection
from app.providers.base import ProviderNotConfiguredError, ProviderRateLimited
from app.providers.onchain import (
    CHAINS, OnchainRateLimited, WatchedAddress, address_is_valid, normalize_address, parse_addresses,
)
from app.schemas.onchain import (
    ChainRead, TraceContinuationRead, TraceRead, TraceRequest, WatchedAddressRead,
)
from app.services import onchain_checkpoint as checkpoints, onchain_trace
from app.schemas.onchain_history import HistoryRead, HistoryRequest, HistorySummary
from app.services import onchain_history

logger = logging.getLogger(__name__)
_state_adapter = TypeAdapter(onchain_trace.TraceState)

router = APIRouter(prefix="/api/onchain", tags=["onchain"])


@router.get("/chains", response_model=list[ChainRead])
async def list_chains(_: WorkspaceContext = Depends(current_workspace)):
    """Chains this deployment can read, and which of them it can trace."""
    has_explorer_key = bool(get_settings().etherscan_api_key)
    return [
        ChainRead(
            key=chain.key,
            display_name=chain.display_name,
            symbol=chain.symbol,
            kind=chain.kind,
            # Every family has a keyless history source; an Etherscan key only
            # buys the EVM ones a deeper page. A chain is untraceable here only
            # if it has neither.
            traceable=chain.kind != "evm"
            or has_explorer_key
            or bool(chain.token_index_url),
            historical_evidence="solana_owned_history" if chain.key == "solana" else "unsupported",
        )
        for chain in CHAINS.values()
    ]


@router.get("/addresses", response_model=list[WatchedAddressRead])
async def list_watched_addresses(
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """Addresses already connected in this workspace, so a trace can start from one."""
    result = await session.execute(
        select(BankConnection).where(
            BankConnection.workspace_id == ctx.id,
            BankConnection.provider == "onchain",
        )
    )
    watched: list[WatchedAddressRead] = []
    for connection in result.scalars().all():
        entries = (connection.credentials or {}).get("addresses") or []
        for entry in entries:
            try:
                parsed: WatchedAddress = parse_addresses(str(entry))[0]
            except Exception:
                logger.warning("Connection %s holds an unreadable address", connection.id)
                continue
            watched.append(
                WatchedAddressRead(
                    chain=parsed.chain.key,
                    address=parsed.address,
                    label=f"{parsed.chain.display_name} {parsed.short}",
                    connection_id=connection.id,
                    connection_name=connection.display_name or connection.institution_name,
                )
            )
    return watched


async def _trace_admission(request: Request) -> None:
    try:
        await onchain_trace_rate_limit(request)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_429_TOO_MANY_REQUESTS:
            raise
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": "trace_admission_limited",
                "message": "This server has reached its trace request limit. Wait before retrying.",
                "retry_after_seconds": int((exc.headers or {}).get("Retry-After", "60")),
            },
            headers=exc.headers,
        ) from exc


@router.post(
    "/trace",
    response_model=TraceRead,
    dependencies=[Depends(_trace_admission)],
)
async def trace_address(
    payload: TraceRequest,
    http_response: Response,
    ctx: WorkspaceContext = Depends(current_workspace),
):
    """Follow native-coin movement from an address, hop by hop.

    The address does not have to be connected — tracing stolen funds means
    walking addresses nobody in this workspace owns.
    """
    http_response.headers["Cache-Control"] = "no-store"
    canonical = _canonical_request(payload)
    workspace_id = str(ctx.id)
    started_at = datetime.now(timezone.utc)
    expires_at = started_at + timedelta(seconds=checkpoints.TTL_SECONDS)
    state = onchain_trace.TraceState()
    if payload.continuation_token is not None:
        snapshot = await _load_checkpoint(workspace_id, payload.continuation_token)
        try:
            saved_request = TraceRequest.model_validate(snapshot["request"])
            state = _state_adapter.validate_python(snapshot["state"])
            started_at = datetime.fromisoformat(snapshot["started_at"])
            expires_at = datetime.fromisoformat(snapshot["expires_at"])
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            raise _checkpoint_error(checkpoints.CheckpointError("incompatible")) from exc
        if (
            saved_request.model_dump(exclude={"continuation_token"}) != canonical.model_dump(exclude={"continuation_token"})
            or state.reads.get("limited")
        ):
            raise _checkpoint_error(checkpoints.CheckpointError("incompatible"))
        # Reopening or continuing completed bounded work never refreshes it.
        if not state.resumable:
            return TraceRead.model_validate(snapshot["result"])
    try:
        result = await onchain_trace.trace(
            canonical.chain,
            canonical.address,
            direction=canonical.direction,
            max_hops=canonical.max_hops,
            max_branches=canonical.max_branches,
            min_amount=canonical.min_amount,
            since=canonical.since,
            until=canonical.until,
            state=state,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ProviderNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "history_unavailable",
                "message": "Transfer history is not configured for this chain on this server.",
                "retry_after_seconds": None,
            },
        ) from exc
    except ProviderRateLimited as exc:
        retry_after = exc.retry_after_seconds if isinstance(exc, OnchainRateLimited) else None
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "upstream_rate_limited",
                "message": "The chain provider limited this trace. Wait before retrying.",
                "retry_after_seconds": retry_after,
            },
            headers={"Retry-After": str(retry_after)} if retry_after is not None else None,
        ) from exc
    response = TraceRead(
        **asdict(result), complete=result.complete, request=canonical,
        workspace_id=ctx.id, started_at=started_at, retrieved_at=datetime.now(timezone.utc),
        continuation=TraceContinuationRead(
            status="unavailable" if state.reads.get("limited") else (
                "available" if state.resumable else "not_needed"
            ),
            token=checkpoints.new_token(), expires_at=expires_at,
            reason="retention_limit" if state.reads.get("limited") else None,
        ),
    )
    snapshot = {
        "version": checkpoints.VERSION,
        "workspace_id": workspace_id,
        "assumptions": checkpoints.assumptions(canonical.chain),
        "request": canonical.model_dump(mode="json", exclude={"continuation_token"}),
        "started_at": started_at.isoformat(), "expires_at": expires_at.isoformat(),
        "result": response.model_dump(mode="json"),
        "state": _state_adapter.dump_python(state, mode="json"),
    }
    try:
        assert response.continuation.token is not None
        await checkpoints.save(workspace_id, response.continuation.token, snapshot)
    except checkpoints.CheckpointError as exc:
        response.continuation = TraceContinuationRead(status="unavailable", reason=exc.reason)
    return response


def _canonical_request(payload: TraceRequest) -> TraceRequest:
    try:
        chain = onchain_trace.resolve_chain(payload.chain)
        address = normalize_address(chain, payload.address)
        if not address_is_valid(chain, address):
            raise ValueError("Invalid address for the selected chain.")
        return payload.model_copy(update={"chain": chain.key, "address": address, "continuation_token": None})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _checkpoint_error(exc: checkpoints.CheckpointError) -> HTTPException:
    unavailable = exc.reason == "storage_unavailable"
    return HTTPException(
        status_code=503 if unavailable else 409,
        detail={
            "code": "trace_checkpoint_unavailable" if unavailable else "trace_restart_required",
            "reason": exc.reason,
            "message": (
                "Saved trace is temporarily unavailable. Keep this result or restart explicitly."
                if unavailable else "This saved trace cannot be continued. Restart explicitly."
            ),
            "retry_after_seconds": None,
        },
    )


async def _load_checkpoint(workspace_id: str, token: str | None) -> dict:
    try:
        snapshot = await checkpoints.load(workspace_id, token)
        canonical = TraceRead.model_validate(snapshot["result"])
        # Validate both copies before returning settings or evidence. Never
        # trust imported download fields as a financial result or workspace.
        if str(canonical.workspace_id) != workspace_id:
            raise checkpoints.CheckpointError("missing_or_expired")
        return snapshot
    except checkpoints.CheckpointError as exc:
        raise _checkpoint_error(exc) from exc
    except (ValidationError, ValueError, TypeError) as exc:
        raise _checkpoint_error(checkpoints.CheckpointError("incompatible")) from exc


@router.get("/trace/checkpoint", response_model=TraceRead)
async def reopen_trace(
    response: Response,
    ctx: WorkspaceContext = Depends(current_workspace),
    token: str | None = Header(default=None, alias="X-Trace-Continuation"),
):
    """Read an authorized saved snapshot. This route never accesses a chain node."""
    response.headers["Cache-Control"] = "no-store"
    snapshot = await _load_checkpoint(str(ctx.id), token)
    return TraceRead.model_validate(snapshot["result"])


@router.post("/history", response_model=HistoryRead, dependencies=[Depends(_trace_admission)])
async def collect_owned_history(
    payload: HistoryRequest,
    response: Response,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """Retain source evidence and progress only; collection never applies money."""
    response.headers["Cache-Control"] = "no-store"
    return await onchain_history.collect_history(session, ctx.id, ctx.user_id, payload)


@router.get("/history", response_model=list[HistorySummary])
async def list_owned_history(
    response: Response,
    connection_id: UUID | None = None,
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    response.headers["Cache-Control"] = "no-store"
    return await onchain_history.list_history(session, ctx.id, connection_id)


@router.get("/history/{collection_id}", response_model=HistoryRead)
async def reopen_owned_history(
    collection_id: UUID,
    response: Response,
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """Opening retained evidence never contacts a chain endpoint."""
    response.headers["Cache-Control"] = "no-store"
    return await onchain_history.read_history(session, ctx.id, collection_id)


@router.get("/history/{collection_id}/export")
async def export_owned_history(
    collection_id: UUID,
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """Server-serialized bytes preserve RPC u64 values beyond JavaScript precision."""
    saved = await onchain_history.read_history(session, ctx.id, collection_id)
    return Response(
        content=onchain_history._json_bytes(saved.model_dump(mode="json")),
        media_type="application/json",
        headers={"Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="owned-history-evidence.json"'},
    )
