"""Shared helpers for serializing model rows into LLM-friendly dicts.

Keep payloads small and stable: a transaction returned to the LLM should
have a small set of obviously-named fields, not the full SQLAlchemy row.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from collections.abc import Sequence
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.transaction import Transaction


def parse_date(v: Any) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    return date.fromisoformat(str(v))


def parse_uuid(v: Any) -> Optional[uuid.UUID]:
    if v is None or v == "":
        return None
    if isinstance(v, uuid.UUID):
        return v
    return uuid.UUID(str(v))


def parse_uuid_list(v: Any) -> Optional[list[uuid.UUID]]:
    if v is None:
        return None
    values = v if isinstance(v, (list, tuple)) else [v]
    return [
        u
        for x in values
        if (u := parse_uuid(x)) is not None
    ] or None


def num(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, Decimal):
        return float(x)
    return float(x)


async def resolve_workspace_id(session, ctx) -> uuid.UUID:
    """Return the workspace the call operates in.

    Prefer the explicit `ws_id` claim from the JWT. Fall back to the
    caller's default (first) workspace — supports tokens minted before
    the workspace migration AND keeps single-workspace callers free of
    having to specify a workspace.
    """
    if ctx.workspace_id is not None:
        return ctx.workspace_id
    from app.services.workspace_service import get_default_workspace

    ws = await get_default_workspace(session, ctx.user_id)
    if ws is None:
        raise ValueError("No workspace available for this user")
    return ws.id


async def workspace_accounts(
    session: AsyncSession, workspace_id: uuid.UUID, txs: Sequence[Transaction]
) -> dict[uuid.UUID, Account]:
    """Batch-load the accounts of a set of transactions, in this workspace only.

    Keeps the payloads free of lazy loads: the service fetches do not all
    selectinload the account relationship. A leg whose account has since
    moved to another workspace gets no account here, so its name stays there.
    """
    ids = {t.account_id for t in txs if t.account_id}
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Account)
            .outerjoin(BankConnection, BankConnection.id == Account.connection_id)
            .where(
                Account.id.in_(ids),
                or_(
                    Account.workspace_id == workspace_id,
                    BankConnection.workspace_id == workspace_id,
                ),
            )
        )
    ).scalars().all()
    return {a.id: a for a in rows}
