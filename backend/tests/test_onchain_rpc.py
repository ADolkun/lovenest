"""Synthetic RPC policy checks and real, isolated Redis process coordination."""

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
import pytest
import redis

from app.providers import onchain, onchain_transport as rpc
from tests.test_providers_onchain import A, JAN23, _settings, _sig, _tx


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.epoch = 1_800_000_000

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


class FakeCoordination:
    """Only time/cooldown for unit checks; the real Lua is tested below."""

    def __init__(self, clock):
        self.clock = clock
        self.cooldown_until = 0.0
        self.published = []

    async def eval(self, script, count, leases, cooldown, token, *args):
        if script == rpc._ACQUIRE_SCRIPT:
            remaining = max(0, self.cooldown_until - self.clock.now)
            return [0, round(remaining * 1000)] if remaining else [1, 0]
        self.published.append(int(args[0]) / 1000)
        self.cooldown_until = max(self.cooldown_until, self.clock.now + int(args[0]) / 1000)
        return round(max(0, self.cooldown_until - self.clock.now) * 1000)

    async def aclose(self):
        pass


@pytest.fixture
def clocked_rpc(monkeypatch):
    clock = FakeClock()
    coordination = FakeCoordination(clock)
    monkeypatch.setattr(rpc, "_redis_client", lambda: coordination)
    monkeypatch.setattr(rpc, "time", SimpleNamespace(
        monotonic=lambda: clock.now, time=lambda: clock.epoch + clock.now,
    ))
    monkeypatch.setattr(onchain, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(rpc, "asyncio", SimpleNamespace(
        timeout=asyncio.timeout, sleep=clock.sleep,
    ))
    monkeypatch.setattr(rpc.random, "uniform", lambda low, high: low)
    return clock, coordination


async def _balance(handler, *, deadline=45):
    with _settings(onchain_rpc_urls={"solana": "https://synthetic.invalid/rpc"}):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await onchain.native_balance(onchain.CHAINS["solana"], A,
                                                client=client, deadline=deadline)


@pytest.mark.asyncio
@pytest.mark.parametrize("header,delay", [
    ("10", 10), ("1", 1), ("date", 10), ("obsolete-date", 10),
    ("asctime-date", 10), ("bad", 1.5), (None, 1.5), ("", 1.5), ("-2", 1.5),
    ("1.5", 1.5), ("NaN", 1.5), ("0", 0), ("past", 0),
])
async def test_retry_after_delays_the_actual_next_attempt(clocked_rpc, header, delay):
    clock, _ = clocked_rpc
    if header in ("date", "obsolete-date", "asctime-date", "past"):
        stamp = clock.epoch + (10 if header != "past" else -10)
        if header == "obsolete-date":
            header = time.strftime("%A, %d-%b-%y %H:%M:%S GMT", time.gmtime(stamp))
        elif header == "asctime-date":
            header = time.asctime(time.gmtime(stamp))
        else:
            header = formatdate(stamp, usegmt=True)
    attempts = []

    def handler(request):
        attempts.append(clock.now)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": header} if header else {})
        return httpx.Response(200, json={"result": {"value": 10**9}})

    assert await _balance(handler) == 1
    assert attempts == [0, delay]


@pytest.mark.asyncio
@pytest.mark.parametrize("header,expected", [("120", 120), ("9" * 1000, 60)],
                         ids=["long-valid", "numeric-overflow"])
async def test_excessive_guidance_stops_this_call_and_publishes_a_finite_cooldown(
    clocked_rpc, header, expected,
):
    clock, coordinator = clocked_rpc
    attempts = []

    def handler(request):
        attempts.append(clock.now)
        return httpx.Response(429, headers={"Retry-After": header})

    with pytest.raises(onchain.OnchainRateLimited) as error:
        await _balance(handler)
    assert attempts == [0]
    assert coordinator.cooldown_until == expected
    assert error.value.retry_after_seconds == expected


@pytest.mark.asyncio
async def test_exhausted_throttle_publishes_the_last_cooldown(clocked_rpc):
    clock, coordinator = clocked_rpc
    attempts = []

    def handler(request):
        attempts.append(clock.now)
        return httpx.Response(429, headers={"Retry-After": "10"})

    with pytest.raises(onchain.OnchainRateLimited) as error:
        await _balance(handler)
    assert attempts == [0, 10, 20]
    assert coordinator.cooldown_until == 30
    assert error.value.retry_after_seconds == 10


@pytest.mark.asyncio
async def test_missing_retry_guidance_uses_jitter_and_caps_large_backoff(clocked_rpc, monkeypatch):
    clock, _ = clocked_rpc
    monkeypatch.setattr(rpc.random, "uniform", lambda low, high: high)
    attempts = []

    def handler(request):
        attempts.append(clock.now)
        if len(attempts) % 2:
            return httpx.Response(429)
        return httpx.Response(200, json={"result": {"value": 10**9}})

    assert await _balance(handler) == 1
    assert 1.5 < attempts[1] <= 10
    with patch.object(onchain, "RPC_RETRY_BACKOFF_SECONDS", 100):
        assert await _balance(handler) == 1
    assert attempts[3] - attempts[2] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("code,message,retryable", [
    (429, "rate limited", True),
    (-32007, "Per-second request limit reached", True),
    (-32008, "Per-minute request limit reached", True),
    (-32011, "Method rate limit reached", True),
    (-32007, "Slot was skipped", False),
    (-32011, "Network error", False),
    (-32005, "Node is unhealthy", False),
    (-32602, "Per-second request limit reached", False),
])
async def test_only_qualified_rpc_throttles_retry(clocked_rpc, code, message, retryable):
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(200, json={"error": {"code": code, "message": message}})
        return httpx.Response(200, json={"result": {"value": 10**9}})

    if retryable:
        assert await _balance(handler) == 1
        assert len(attempts) == 2
    else:
        with pytest.raises(RuntimeError):
            await _balance(handler)
        assert len(attempts) == 1


@pytest.mark.asyncio
async def test_expired_deadline_starts_no_request(clocked_rpc):
    attempts = []
    with pytest.raises(onchain.OnchainDeadlineExceeded):
        await _balance(lambda request: attempts.append(request), deadline=0)
    assert attempts == []


@pytest.mark.asyncio
async def test_collection_attempt_budget_counts_retries_across_reads(clocked_rpc):
    attempts = []
    budget = rpc.RequestBudget(max_attempts=3)

    def handler(request):
        attempts.append(1)
        if len(attempts) == 2:
            return httpx.Response(200, json={"result": []})
        return httpx.Response(429, headers={"Retry-After": "0"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async def read():
            return await rpc.request_json(
                client, "POST", "https://synthetic.invalid/rpc",
                endpoint="https://synthetic.invalid/rpc", label="Synthetic RPC",
                deadline=45, attempts=3, backoff=1.5, timeout=30,
                concurrency=5, rpc=True, budget=budget,
            )

        assert await read() == {"result": []}
        with pytest.raises(rpc.OnchainRequestLimitExceeded):
            await read()
        with pytest.raises(rpc.OnchainRequestLimitExceeded):
            await read()
    assert len(attempts) == budget.attempts == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["success", "http-error", "rpc-error", "bad-json", "network"])
async def test_signature_returned_past_deadline_starts_no_payload(clocked_rpc, response):
    clock, _ = clocked_rpc
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["method"])
        clock.now = 120
        if response == "http-error":
            return httpx.Response(500)
        if response == "rpc-error":
            return httpx.Response(200, json={"error": {"code": -32602, "message": "invalid"}})
        if response == "bad-json":
            return httpx.Response(200, text="not JSON")
        if response == "network":
            raise httpx.ConnectError("synthetic failure", request=request)
        return httpx.Response(200, json={"result": [_sig("synthetic-late", JAN23)]})

    with _settings():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(onchain.OnchainDeadlineExceeded):
                await onchain.transfers(onchain.CHAINS["solana"], A, limit=25,
                                        client=client, deadline=45)
    assert calls == ["getSignaturesForAddress"]


@pytest.mark.asyncio
async def test_coordination_outage_does_not_bypass_the_budget_or_expose_its_url(
    clocked_rpc, monkeypatch,
):
    _, coordinator = clocked_rpc
    calls = []

    async def unavailable(*args):
        raise redis.ConnectionError("https://synthetic.invalid/secret-sentinel")

    monkeypatch.setattr(coordinator, "eval", unavailable)
    with pytest.raises(RuntimeError) as error:
        await _balance(lambda request: calls.append(request))
    assert calls == []
    assert "sentinel" not in str(error.value)
    assert "unavailable" in str(error.value)


@pytest.mark.asyncio
async def test_waiting_for_a_full_endpoint_cannot_start_a_request_after_deadline(
    clocked_rpc, monkeypatch,
):
    clock, coordinator = clocked_rpc
    calls = []
    evaluate = coordinator.eval

    async def busy(script, *args):
        if script == rpc._ACQUIRE_SCRIPT:
            return [0, 0]
        return await evaluate(script, *args)

    monkeypatch.setattr(coordinator, "eval", busy)
    with pytest.raises(onchain.OnchainDeadlineExceeded):
        await _balance(lambda request: calls.append(request), deadline=0.1)
    assert clock.now == 0.1
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "rpc", "network"])
async def test_upstream_failures_do_not_leak_raw_bodies_or_exception_urls(clocked_rpc, failure):
    def handler(request):
        if failure == "network":
            raise httpx.ConnectError("https://synthetic.invalid/secret-sentinel", request=request)
        if failure == "http":
            return httpx.Response(403, text="raw-sentinel")
        return httpx.Response(200, json={"error": {"code": -32602, "message": "raw-sentinel"}})

    with pytest.raises(RuntimeError) as error:
        await _balance(handler)
    assert "sentinel" not in str(error.value)
    assert "synthetic.invalid" not in str(error.value)


@pytest.fixture
def isolated_redis():
    url = os.environ.get("REDIS_TEST_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("CI must supply REDIS_TEST_URL for real on-chain coordination tests")
        pytest.skip("Set REDIS_TEST_URL to a dedicated disposable local Redis")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}
    client = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
    client.ping()
    try:
        yield client
    finally:
        client.close()


def _coordination_worker():
    """Spawned with a real Redis client and entirely synthetic HTTP transport."""
    from app.core.config import Settings
    from app.services import onchain_trace

    Settings.model_config.update(env_file=None, secrets_dir=None)
    mode, endpoint = sys.argv[1:]
    settings = SimpleNamespace(redis_url=os.environ["REDIS_TEST_URL"],
                               onchain_rpc_urls={"solana": endpoint})
    throttled = False
    group = "other" if mode == "other" else "shared"

    def emit(event, **values):
        print(json.dumps({"event": event, "at": time.monotonic(),
                          "mode": mode, "group": group, **values}), flush=True)

    async def handler(request):
        nonlocal throttled
        method = json.loads(request.content)["method"]
        emit("start", method=method)
        try:
            await asyncio.sleep(0.04)
            if mode == "trace-throttle" and not throttled:
                throttled = True
                return httpx.Response(429, headers={"Retry-After": "1"})
            if method == "getSignaturesForAddress":
                return httpx.Response(200, json={"result": [
                    _sig(f"synthetic-{i}", JAN23) for i in range(6)
                ]})
            if method == "getTransaction":
                return httpx.Response(200, json={"result": _tx(JAN23, {A: 0})})
            return httpx.Response(200, json={"result": {"value": 0}})
        finally:
            emit("end", method=method)

    async def read():
        if mode.startswith("trace"):
            result = await onchain_trace.trace("solana", A, max_hops=1)
            assert result.interruption is None
        else:
            assert await onchain.native_balance(onchain.CHAINS["solana"], A) == 0

    with (
        patch.object(onchain, "get_settings", lambda: settings),
        patch.object(rpc, "get_settings", lambda: settings),
        patch.object(onchain, "TX_FETCH_CONCURRENCY", 2),
        patch.object(onchain, "RPC_RETRY_BACKOFF_SECONDS", 0),
        patch.object(onchain, "_client", lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler))),
    ):
        emit("ready")
        assert sys.stdin.readline().strip() == "start"
        # Celery reuses a process but creates a fresh loop for each sync.
        for _ in range(2):
            asyncio.run(read())
        emit("done")


@pytest.mark.parametrize("throttle", [False, True])
def test_traces_and_balances_share_redis_across_processes_and_event_loops(isolated_redis, throttle):
    endpoint = f"https://{uuid.uuid4().hex}.invalid/rpc"
    other_endpoint = f"https://{uuid.uuid4().hex}.invalid/rpc"
    modes = ["trace-throttle" if throttle else "trace", "trace", "balance", "other"]
    processes = []
    events = []
    expected_release = None
    try:
        for mode in modes:
            process = subprocess.Popen(
                [sys.executable, "-c",
                 "from tests.test_onchain_rpc import _coordination_worker; _coordination_worker()",
                 mode, other_endpoint if mode == "other" else endpoint],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            processes.append(process)
        for process in processes:
            assert process.stdout is not None
            assert json.loads(process.stdout.readline())["event"] == "ready"
        assert processes[0].stdin is not None
        processes[0].stdin.write("start\n")
        processes[0].stdin.flush()
        if throttle:
            cutoff = time.monotonic() + 3
            while time.monotonic() < cutoff:
                ttl = isolated_redis.pttl(rpc._endpoint_key(endpoint) + ":cooldown")
                if ttl > 0:
                    expected_release = time.monotonic() + ttl / 1000
                    break
                time.sleep(0.01)
            assert expected_release is not None, "first worker did not publish its throttle"
        for process in processes[1:]:
            assert process.stdin is not None
            process.stdin.write("start\n")
            process.stdin.flush()
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
            events.extend(json.loads(line) for line in stdout.splitlines())
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
        for source in (endpoint, other_endpoint):
            key = rpc._endpoint_key(source)
            isolated_redis.delete(key + ":leases", key + ":cooldown")
    active = peak = 0
    for event in sorted(events, key=lambda entry: entry["at"]):
        if event["group"] == "shared" and event["event"] in {"start", "end"}:
            active += 1 if event["event"] == "start" else -1
            peak = max(peak, active)
            assert 0 <= active <= 2
    assert active == 0
    assert peak == 2
    assert sum(event["event"] == "done" for event in events) == 4
    if throttle:
        assert expected_release is not None
        shared_starts = [event["at"] for event in events if event["event"] == "start"
                         and event["group"] == "shared" and event["mode"] != "trace-throttle"]
        other_starts = [event["at"] for event in events if event["event"] == "start"
                        and event["group"] == "other"]
        assert min(shared_starts) >= expected_release - 0.03
        assert min(other_starts) < expected_release


def test_real_redis_lease_expiry_cooldown_maximum_and_owner_release(isolated_redis):
    key = rpc._endpoint_key(f"https://{uuid.uuid4().hex}.invalid/rpc")
    keys = (key + ":leases", key + ":cooldown")
    try:
        assert isolated_redis.eval(rpc._ACQUIRE_SCRIPT, 2, *keys, "old", 1, 60) == [1, 0]
        assert isolated_redis.eval(rpc._ACQUIRE_SCRIPT, 2, *keys, "new", 1, 500)[0] == 0
        time.sleep(0.08)
        assert isolated_redis.eval(rpc._ACQUIRE_SCRIPT, 2, *keys, "new", 1, 500) == [1, 0]
        isolated_redis.eval(rpc._FINISH_SCRIPT, 2, *keys, "old", 0)
        assert isolated_redis.zscore(keys[0], "new") is not None
        isolated_redis.eval(rpc._FINISH_SCRIPT, 2, *keys, "new", 1000)
        isolated_redis.eval(rpc._FINISH_SCRIPT, 2, *keys, "other", 50)
        assert isolated_redis.pttl(keys[1]) > 800
        assert isolated_redis.eval(rpc._ACQUIRE_SCRIPT, 2, *keys, "blocked", 1, 100)[0] == 0
    finally:
        isolated_redis.delete(*keys)


@pytest.mark.asyncio
async def test_cancelled_grant_acknowledgement_releases_the_real_redis_permit(
    isolated_redis, monkeypatch,
):
    from redis import asyncio as aioredis

    endpoint = f"https://{uuid.uuid4().hex}.invalid/rpc"
    key = rpc._endpoint_key(endpoint)
    acquired = asyncio.Event()
    real = aioredis.Redis.from_url(os.environ["REDIS_TEST_URL"])

    class LostReply:
        async def eval(self, script, *args):
            result = await real.eval(script, *args)
            if script == rpc._ACQUIRE_SCRIPT:
                acquired.set()
                await asyncio.Event().wait()
            return result

        async def aclose(self):
            await real.aclose()

    monkeypatch.setattr(rpc, "_redis_client", LostReply)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"result": {"value": 0}})

    try:
        with _settings(onchain_rpc_urls={"solana": endpoint}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                task = asyncio.create_task(onchain.native_balance(
                    onchain.CHAINS["solana"], A, client=client, deadline=time.monotonic() + 2,
                ))
                try:
                    await asyncio.wait_for(acquired.wait(), 1)
                finally:
                    task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert calls == []
        assert isolated_redis.zcard(key + ":leases") == 0
    finally:
        await real.aclose()
        isolated_redis.delete(key + ":leases", key + ":cooldown")
