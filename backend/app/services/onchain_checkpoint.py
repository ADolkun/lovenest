"""Short-lived native trace snapshots; never a financial ledger or a bearer grant."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
from datetime import datetime, timezone
from collections.abc import Awaitable
from typing import Any, cast

from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core import redis as redis_store
from app.providers.onchain import CHAINS, rpc_url
from app.providers import onchain_reads

VERSION = 1
TTL_SECONDS = 15 * 60
MAX_BYTES = 4 * 1024 * 1024
MAX_PER_WORKSPACE = 5
STORE_TIMEOUT_SECONDS = 1.0
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")

# Keep count/eviction atomic across workers. Old tokens remain readable until
# their fixed expiry or bounded eviction; concurrent continuations never edit
# their shared source snapshot. Both Redis keys share the workspace hash tag.
_SAVE = """
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
for _, key in ipairs(redis.call('ZRANGE', KEYS[2], 0, -1)) do
  if redis.call('EXISTS', key) == 0 then redis.call('ZREM', KEYS[2], key) end
end
local latest = redis.call('ZREVRANGE', KEYS[2], 0, 0, 'WITHSCORES')
local sequence = tonumber(ARGV[3])
if #latest > 0 then sequence = math.max(sequence, tonumber(latest[2]) + 1) end
redis.call('ZADD', KEYS[2], sequence, KEYS[1])
local extra = redis.call('ZCARD', KEYS[2]) - tonumber(ARGV[4])
if extra > 0 then
  local expired = redis.call('ZRANGE', KEYS[2], 0, extra - 1)
  for _, key in ipairs(expired) do
    redis.call('DEL', key)
    redis.call('ZREM', KEYS[2], key)
  end
end
redis.call('EXPIRE', KEYS[2], ARGV[5])
return 1
"""


class CheckpointError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def assumptions(chain_key: str) -> str:
    """Internal source/decoder identity; never expose credential-bearing URLs."""
    chain = CHAINS[chain_key]
    identity = [
        VERSION, onchain_reads.READ_STATE_VERSION,
        chain.key, chain.kind, chain.explorer_chain_id, chain.decimals,
        rpc_url(chain), chain.token_index_url,
        get_settings().etherscan_api_key if chain.kind == "evm" else None,
    ]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _key(workspace_id: str, token: str) -> str:
    return f"onchain:trace:{{{workspace_id}}}:{token}"


async def save(workspace_id: str, token: str, snapshot: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).timestamp()
    expires = datetime.fromisoformat(snapshot["expires_at"]).timestamp()
    if expires <= now:
        raise CheckpointError("missing_or_expired")
    try:
        raw = json.dumps(snapshot, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise CheckpointError("storage_limit") from exc
    if len(raw.encode()) > MAX_BYTES:
        raise CheckpointError("storage_limit")
    try:
        async with asyncio.timeout(STORE_TIMEOUT_SECONDS):
            redis = await redis_store.get_redis()
            await cast(Awaitable[Any], redis.eval(
                _SAVE, 2, _key(workspace_id, token), _key(workspace_id, "index"),
                raw, str(max(1, math.ceil(expires - now))), str(now * 1000),
                str(MAX_PER_WORKSPACE), str(TTL_SECONDS),
            ))
    except (RedisError, TimeoutError, OSError) as exc:
        raise CheckpointError("storage_unavailable") from exc


async def load(workspace_id: str, token: str | None) -> dict[str, Any]:
    if not token or not _TOKEN.fullmatch(token):
        raise CheckpointError("missing_or_expired")
    try:
        async with asyncio.timeout(STORE_TIMEOUT_SECONDS):
            redis = await redis_store.get_redis()
            raw = await redis.get(_key(workspace_id, token))
    except (RedisError, TimeoutError, OSError) as exc:
        raise CheckpointError("storage_unavailable") from exc
    if not isinstance(raw, str) or len(raw.encode()) > MAX_BYTES:
        raise CheckpointError("missing_or_expired")
    try:
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict) or snapshot.get("workspace_id") != workspace_id:
            raise CheckpointError("missing_or_expired")
        expires = datetime.fromisoformat(snapshot["expires_at"])
        if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
            raise CheckpointError("missing_or_expired")
        if snapshot.get("version") != VERSION:
            raise CheckpointError("incompatible")
        chain_key = snapshot["request"]["chain"]
        if snapshot.get("assumptions") != assumptions(chain_key):
            raise CheckpointError("incompatible")
        if not all(isinstance(snapshot[key], dict) for key in ("request", "result", "state")):
            raise CheckpointError("incompatible")
        return snapshot
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise CheckpointError("incompatible") from exc
