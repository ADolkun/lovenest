"""Mint long-lived MCP tokens for external agents.

Lets a logged-in user generate a JWT they can paste into Claude Desktop,
n8n, or any other MCP client. The token is signed with the same
`AGENTS_MCP_JWT_SECRET` the internal runtime uses, scoped to the calling
user, with a configurable TTL (default 90 days) and an `ext: true`
claim. The MCP server already verifies any valid JWT — no auth changes
needed there.

Follows the AGENTS_ENABLED master switch: when agents are off, the
router isn't mounted at all so the endpoint 404s.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.agents.config import get_agent_settings
from app.agents.mcp.auth import mint_token
from app.core.auth import current_active_user
from app.models.user import User

router = APIRouter(prefix="/api/agents/mcp-tokens", tags=["agents"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_mcp_token(user: User = Depends(current_active_user)):
    s = get_agent_settings()
    ttl_seconds = max(s.mcp_external_ttl_days, 1) * 86400
    token = mint_token(user_id=user.id, ttl_seconds=ttl_seconds, external=True)
    return {
        "token": token,
        "expires_in_seconds": ttl_seconds,
        "expires_in_days": s.mcp_external_ttl_days,
    }
