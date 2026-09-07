"""Bound on-chain reads across API and Celery processes using their shared Redis."""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, cast

import httpx
import redis.asyncio as redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.providers.base import ProviderRateLimited

REDIS_TIMEOUT_SECONDS = 1.0
CLEANUP_TIMEOUT_SECONDS = 0.25
RPC_BACKOFF_MAX_SECONDS = 10.0
OVERFLOW_COOLDOWN_SECONDS = 60.0
# Milliseconds, even after adding the current epoch, remain exact in Lua doubles.
MAX_COOLDOWN_SECONDS = 2**42

_ACQUIRE_SCRIPT = """
local clock = redis.call('TIME')
local now = clock[1] * 1000 + math.floor(clock[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local cooldown = redis.call('PTTL', KEYS[2])
if cooldown > 0 then return {0, cooldown} end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return {0, 0} end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[1])
if redis.call('PTTL', KEYS[1]) < tonumber(ARGV[3]) then
    redis.call('PEXPIRE', KEYS[1], ARGV[3])
end
return {1, 0}
"""

_FINISH_SCRIPT = """
local cooldown = math.max(0, redis.call('PTTL', KEYS[2]), tonumber(ARGV[2]))
if tonumber(ARGV[2]) > 0 and redis.call('PTTL', KEYS[2]) < tonumber(ARGV[2]) then
    redis.call('SET', KEYS[2], '1', 'PX', ARGV[2])
end
redis.call('ZREM', KEYS[1], ARGV[1])
return cooldown
"""


class OnchainDeadlineExceeded(TimeoutError):
    """The caller's monotonic budget has elapsed; start no further reads."""


class OnchainRequestLimitExceeded(RuntimeError):
    """The collection's aggregate upstream attempt allowance is exhausted."""


@dataclass
class RequestBudget:
    max_attempts: int
    attempts: int = 0

    def consume(self) -> None:
        # No await between checking and consuming: concurrent reads share this
        # invocation's allowance, including each transport retry.
        if self.attempts >= self.max_attempts:
            raise OnchainRequestLimitExceeded("On-chain request limit reached")
        self.attempts += 1


class OnchainRateLimited(ProviderRateLimited):
    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _redis_client() -> redis.Redis:
    # Celery creates a new asyncio.run loop per job. A module-cached async
    # client would outlive its loop; keep this pool local to one logical call.
    return redis.from_url(
        get_settings().redis_url,
        decode_responses=True,
        socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
        socket_timeout=REDIS_TIMEOUT_SECONDS,
        retry=Retry(NoBackoff(), 0),
    )


def _endpoint_key(endpoint: str) -> str:
    # Callers supply a stable source base, without address/pagination params.
    # Credential-bearing paths stay part of identity, but never reach Redis.
    digest = hashlib.sha256(endpoint.encode()).hexdigest()
    return f"onchain:rpc:{{{digest}}}"


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OnchainDeadlineExceeded("On-chain read deadline exceeded")
    return remaining


def _retry_after(header: str | None) -> tuple[float | None, bool]:
    """RFC 9110 delay/date, and whether unusably large guidance forbids a retry."""
    if header is None:
        return None, False
    value = header.strip()
    if value and value.isascii() and value.isdecimal():
        digits = value.lstrip("0") or "0"
        if len(digits) > 13 or int(digits) > MAX_COOLDOWN_SECONDS:
            # ponytail: bounded recovery for unrepresentable guidance; no
            # retry in this call or permanent operator-managed Redis blocker.
            return OVERFLOW_COOLDOWN_SECONDS, True
        return float(int(digits)), False
    try:
        moment = parsedate_to_datetime(value)
        if moment.tzinfo is None:
            # Obsolete HTTP-date asctime syntax has no explicit GMT suffix.
            moment = moment.replace(tzinfo=timezone.utc)
        delay = max(0.0, moment.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None, False
    if not math.isfinite(delay) or delay > MAX_COOLDOWN_SECONDS:
        return OVERFLOW_COOLDOWN_SECONDS, True
    return delay, False


def _is_rpc_throttle(payload: Any) -> bool:
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    if code == 429:
        return True
    message = error.get("message")
    if not isinstance(message, str):
        return False
    # QuickNode documents these gateway errors, but also uses -32007 for a
    # skipped Solana slot and -32011 for an EVM network error. Qualify messages.
    # https://www.quicknode.com/docs/solana/error-references
    # https://www.quicknode.com/docs/ethereum/error-references
    messages = {
        -32007: (
            "per-second request limit reached",
            "you have exceeded your plan's per-second request limit",
        ),
        -32008: (
            "per-minute request limit reached",
            "you have exceeded your plan's per-minute request limit",
        ),
        -32011: (
            "method rate limit reached",
            "you have exceeded a method-specific rate limit configured for the endpoint",
        ),
    }
    if not isinstance(code, int):
        return False
    normalized = message.strip().casefold()
    return any(
        normalized == prefix or normalized.startswith(prefix + ".")
        for prefix in messages.get(code, ())
    )


async def _finish(client: redis.Redis, key: str, token: str, cooldown: float) -> float:
    async with asyncio.timeout(REDIS_TIMEOUT_SECONDS):
        milliseconds = await cast(
            Awaitable[Any],
            client.eval(
                _FINISH_SCRIPT,
                2,
                key + ":leases",
                key + ":cooldown",
                token,
                str(math.ceil(cooldown * 1000)),
            ),
        )
    return int(milliseconds) / 1000


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    endpoint: str,
    label: str,
    deadline: float | None,
    attempts: int,
    backoff: float,
    timeout: float,
    concurrency: int,
    json_body: dict | None = None,
    params: dict | None = None,
    rpc: bool = False,
    budget: RequestBudget | None = None,
) -> Any:
    """One bounded read, with atomic per-endpoint permits and shared cooldown.

    HTTP 429 and documented JSON-RPC throttles share this retry owner. HTTP
    errors and exception strings never expose upstream URLs or response bodies.
    """
    deadline = deadline if deadline is not None else time.monotonic() + timeout * attempts
    _remaining(deadline)
    key = _endpoint_key(endpoint)
    coordinator = _redis_client()
    try:
        for attempt in range(attempts):
            token = uuid.uuid4().hex
            finished = False
            cooldown = 0.0
            try:
                while True:
                    # The hard attempt deadline starts before its grant round
                    # trip, so delayed Redis replies cannot outlive the lease.
                    attempt_deadline = min(deadline, time.monotonic() + timeout)
                    async with asyncio.timeout(min(_remaining(deadline), REDIS_TIMEOUT_SECONDS)):
                        granted, wait_ms = await cast(
                            Awaitable[Any],
                            coordinator.eval(
                                _ACQUIRE_SCRIPT,
                                2,
                                key + ":leases",
                                key + ":cooldown",
                                token,
                                str(concurrency),
                                str(math.ceil((timeout + 5.0) * 1000)),
                            ),
                        )
                    remaining = _remaining(deadline)
                    if granted:
                        break
                    wait = int(wait_ms) / 1000
                    if wait >= remaining:
                        raise OnchainRateLimited(
                            f"{label} rate-limited the request", math.ceil(wait)
                        )
                    # A wake is not a permit: atomically recheck both limits.
                    await asyncio.sleep(min(wait or random.uniform(0.1, 0.2), remaining))

                try:
                    async with asyncio.timeout(_remaining(attempt_deadline)):
                        if budget is not None:
                            budget.consume()
                        response = await client.request(
                            method,
                            url,
                            json=json_body,
                            params=params,
                            timeout=min(timeout, _remaining(attempt_deadline)),
                        )
                except (TimeoutError, httpx.TimeoutException):
                    _remaining(deadline)
                    raise RuntimeError(f"{label} request timed out") from None
                except httpx.HTTPError:
                    _remaining(deadline)
                    raise RuntimeError(f"{label} request failed") from None

                payload = None
                throttled = response.status_code == 429
                if not throttled:
                    if not response.is_success:
                        _remaining(deadline)
                        raise RuntimeError(f"{label} returned HTTP {response.status_code}")
                    try:
                        payload = response.json()
                    except ValueError:
                        _remaining(deadline)
                        raise RuntimeError(f"{label} returned a non-JSON response") from None
                    throttled = rpc and _is_rpc_throttle(payload)
                    if rpc and isinstance(payload, dict) and payload.get("error") and not throttled:
                        _remaining(deadline)
                        raise RuntimeError(f"{label} returned a JSON-RPC error")
                if not throttled:
                    _remaining(deadline)
                    return payload

                guidance, overflow = _retry_after(response.headers.get("Retry-After"))
                fallback = min(
                    RPC_BACKOFF_MAX_SECONDS, backoff * 2**attempt + random.uniform(0, backoff / 4)
                )
                cooldown = guidance if guidance is not None else fallback
                # Publish before release, including the last failed attempt.
                wait = await _finish(coordinator, key, token, cooldown)
                finished = True
                remaining = _remaining(deadline)
                if overflow or attempt == attempts - 1 or wait >= remaining:
                    raise OnchainRateLimited(f"{label} rate-limited the request", math.ceil(wait))
                # The next acquisition waits for the shared cooldown, which
                # another response may extend in the meantime.
            finally:
                if not finished:
                    try:
                        # Also remove a grant whose response was lost on cancel.
                        async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                            await _finish(coordinator, key, token, cooldown)
                    except (RedisError, TimeoutError):
                        pass  # A crashed/cancelled owner's lease expires shortly.
    except (RedisError, TimeoutError) as exc:
        if isinstance(exc, OnchainDeadlineExceeded):
            raise
        _remaining(deadline)
        raise RuntimeError("On-chain request coordination is unavailable") from None
    finally:
        try:
            async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                await coordinator.aclose()
        except (RedisError, TimeoutError):
            pass
