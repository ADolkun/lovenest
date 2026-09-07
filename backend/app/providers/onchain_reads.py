"""Bounded, JSON-only observations belonging to one explicit native trace."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from app.providers.base import ProviderRateLimited
from app.providers.onchain_transport import OnchainDeadlineExceeded, OnchainRateLimited

READ_STATE_VERSION = "native-trace-1:finalized:jsonParsed:0"
MAX_READ_BYTES = 2 * 1024 * 1024
MAX_HISTORY_WINDOWS = 144
MAX_HISTORY_PAGES = 16
MAX_TRANSACTION_READS = 600


class ReadPending(Exception):
    """Materializing retained evidence reached work that has not completed."""


def interruption_code(error: BaseException | None) -> str | None:
    if isinstance(error, (OnchainDeadlineExceeded, asyncio.CancelledError)):
        return "deadline_exceeded"
    if isinstance(error, ProviderRateLimited):
        return "upstream_rate_limited"
    return "provider_unavailable" if error else None


class TraceReads:
    """An invocation's reads; the supplied dictionary survives explicit Continue.

    Replaying these observations runs the same decoders without network access,
    including after cancellation. Mutable page observations remain frozen until
    the enclosing checkpoint expires; Restart creates a new dictionary.
    """

    def __init__(self, state: dict[str, Any], chain: str, source: str, window: str):
        if state and state.get("version") != READ_STATE_VERSION:
            raise ValueError("Trace read state is incompatible; restart the trace.")
        state.setdefault("version", READ_STATE_VERSION)
        sources = state.setdefault("sources", {})
        if chain in sources and sources[chain] != source:
            raise ValueError("Trace history source changed; restart the trace.")
        sources[chain] = source
        histories = state.setdefault("histories", {})
        if window not in histories and len(histories) >= MAX_HISTORY_WINDOWS:
            state["limited"] = True
            raise ValueError("Trace retained window limit reached; restart the trace.")
        self.history = histories.setdefault(window, {
            "fetched_at": datetime.now(timezone.utc).isoformat(), "pages": {}, "payloads": {},
        })
        self.state = state
        self.transactions = state.setdefault("transactions", {})
        self.source = source
        self.replay = False
        self.error: BaseException | None = None
        self.responses: dict[str, Any] = {}

    def page_limit(self, stream: str, allowance: int) -> int:
        completed = sum(key.startswith(stream + ":") for key in self.history["pages"])
        return min(MAX_HISTORY_PAGES, completed + allowance)

    async def read(
        self, key: str, fetch: Callable[[], Awaitable[Any]], valid: Callable[[Any], bool],
        *, transaction: bool = False,
    ) -> Any:
        cache = self.transactions if transaction else self.history["pages"]
        key = self.source + ":" + key if transaction else key
        if key in cache:
            return cache[key]["payload"]
        if key in self.responses:
            return self.responses[key]
        if self.replay or self.state.get("limited"):
            raise ReadPending()
        try:
            payload = await fetch()
        except BaseException as exc:
            self.error = exc
            raise
        self.responses[key] = payload
        if valid(payload):
            entry = {"payload": payload, "fetched_at": datetime.now(timezone.utc).isoformat()}
            size = len(json.dumps(entry, separators=(",", ":")).encode())
            used = self.state.get("bytes", 0)
            if used + size > MAX_READ_BYTES or (transaction and len(cache) >= MAX_TRANSACTION_READS):
                self.state["limited"] = True
            else:
                cache[key] = entry
                self.state["bytes"] = used + size
        return payload

    @property
    def interruption(self) -> str | None:
        return interruption_code(self.error)

    @property
    def retry_after_seconds(self) -> int | None:
        return self.error.retry_after_seconds if isinstance(self.error, OnchainRateLimited) else None
