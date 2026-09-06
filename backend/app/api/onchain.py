import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_async_session
from app.core.rate_limit import onchain_trace_rate_limit
from app.core.workspace_context import WorkspaceContext, current_workspace
from app.models.bank_connection import BankConnection
from app.providers.base import ProviderNotConfiguredError, ProviderRateLimited
from app.providers.onchain import CHAINS, WatchedAddress, parse_addresses
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
                )
            )
    return watched


@router.post(
    "/trace",
    response_model=TraceRead,
    dependencies=[Depends(onchain_trace_rate_limit)],
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
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except ProviderRateLimited as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="The chain node rate-limited this trace. Try again shortly, or set a "
            "dedicated RPC URL in ONCHAIN_RPC_URLS.",
        ) from exc
    return TraceRead(
        root=result.root,
        direction=result.direction,
        nodes=[node.__dict__ for node in result.nodes],
        edges=[edge.__dict__ for edge in result.edges],
        truncated=result.truncated,
    )
