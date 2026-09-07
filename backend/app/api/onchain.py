import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_async_session
from app.core.rate_limit import onchain_trace_rate_limit
from app.core.workspace_context import WorkspaceContext, current_workspace
from app.models.bank_connection import BankConnection
from app.providers.base import ProviderNotConfiguredError, ProviderRateLimited
from app.providers.onchain import CHAINS, OnchainRateLimited, WatchedAddress, parse_addresses
from app.schemas.onchain import ChainRead, TraceRead, TraceRequest, WatchedAddressRead
from app.services import onchain_trace

logger = logging.getLogger(__name__)

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
    _: WorkspaceContext = Depends(current_workspace),
):
    """Follow native-coin movement from an address, hop by hop.

    The address does not have to be connected — tracing stolen funds means
    walking addresses nobody in this workspace owns.
    """
    try:
        result = await onchain_trace.trace(
            payload.chain,
            payload.address,
            direction=payload.direction,
            max_hops=payload.max_hops,
            max_branches=payload.max_branches,
            min_amount=payload.min_amount,
            since=payload.since,
            until=payload.until,
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
    return TraceRead(
        root=result.root,
        direction=result.direction,
        nodes=[node.__dict__ for node in result.nodes],
        edges=[edge.__dict__ for edge in result.edges],
        truncated=result.truncated,
        interruption=result.interruption.__dict__ if result.interruption else None,
    )
