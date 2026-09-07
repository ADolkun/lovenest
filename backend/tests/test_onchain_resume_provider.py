"""Recovery uses only synthetic HTTP responses and counts real transport calls."""

import asyncio
import json
import time
from collections import Counter
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest

from app.providers import onchain, onchain_reads
from tests.test_providers_onchain import (
    A, B, BASE, BTC, BTC_A, BTC_B, COIN, EVM, JAN23, SOL,
    _blockscout_item, _btc_tx, _patched_client, _settings, _sig, _solana_handler, _tx,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("failure", ["throttle", "timeout", "null", "malformed"])
@pytest.mark.parametrize("movement", [True, False])
async def test_successful_sibling_survives_and_only_failed_payload_is_retried(failure, movement):
    state = {}
    recovered = False
    good_returned = asyncio.Event()
    calls = Counter()
    good = _tx(JAN23, {A: -COIN, B: COIN} if movement else {A: 0, B: 0})
    serve = _solana_handler(
        signatures={A: [_sig("synthetic-good", JAN23), _sig("synthetic-bad", JAN23)]},
        txs={"synthetic-good": good, "synthetic-bad": _tx(JAN23, {A: -2 * COIN, B: 2 * COIN})},
    )

    async def handler(request):
        body = json.loads(request.content)
        method, identity = body["method"], body["params"][0]
        calls[method, identity] += 1
        assert body["params"][1]["commitment"] == "finalized"
        if method == "getTransaction" and identity == "synthetic-good":
            good_returned.set()
        if method == "getTransaction" and identity == "synthetic-bad" and not recovered:
            await good_returned.wait()
            if failure == "throttle":
                return httpx.Response(429)
            if failure == "timeout":
                raise httpx.ReadTimeout("synthetic timeout")
            return httpx.Response(200, json={"result": None if failure == "null" else {"broken": True}})
        return serve(request)

    with _settings(), _patched_client(handler), patch.object(onchain, "RPC_RETRY_BACKOFF_SECONDS", 0):
        first = await onchain.transfers(SOL, A, limit=25, read_state=state)
        assert len(first.items) == int(movement)
        assert not first.complete and first.resumable
        assert first.coverage is not None
        assert first.coverage.failed_payloads == 1
        assert first.coverage.pending_payloads == 0
        assert first.coverage.pages_read == 1
        assert len(state["transactions"]) == 1
        retained = onchain.retained_transfers(SOL, A, limit=25, read_state=state)
        assert retained == first
        state = json.loads(json.dumps(state))
        recovered = True
        second = await onchain.transfers(SOL, A, limit=25, read_state=state)
    assert second.complete
    assert len(second.items) == 1 + int(movement)
    assert second.coverage is not None
    assert second.coverage.failed_payloads == second.coverage.pending_payloads == 0
    assert second.coverage.fetched_at == first.coverage.fetched_at
    assert calls["getSignaturesForAddress", A] == calls["getTransaction", "synthetic-good"] == 1
    assert calls["getTransaction", "synthetic-bad"] == (4 if failure == "throttle" else 2)


async def test_sender_recipient_share_payload_but_balances_remain_live():
    state = {}
    calls = Counter()
    serve = _solana_handler(
        signatures={address: [_sig("synthetic-shared", JAN23)] for address in (A, B)},
        txs={"synthetic-shared": _tx(JAN23, {A: -COIN, B: COIN})},
    )

    def handler(request):
        body = json.loads(request.content)
        calls[body["method"]] += 1
        return serve(request)

    with _settings(), _patched_client(handler):
        sender = await onchain.transfers(SOL, A, limit=25, read_state=state)
        recipient = await onchain.transfers(SOL, B, limit=25, read_state=state)
        await onchain.native_balance(SOL, A)
        await onchain.native_balance(SOL, A)
    assert sender.items == recipient.items
    assert calls == {"getSignaturesForAddress": 2, "getTransaction": 1, "getBalance": 2}


@pytest.mark.parametrize("external_cancel", [True, False])
async def test_cancellation_retains_completed_read_and_starts_no_later_batch(external_cancel):
    state = {}
    calls = []
    started = asyncio.Event()
    pending = 0

    async def handler(request):
        nonlocal pending
        body = json.loads(request.content)
        calls.append((body["method"], body["params"][0]))
        if body["method"] == "getSignaturesForAddress":
            return httpx.Response(200, json={"result": [_sig(f"synthetic-{i}", JAN23) for i in range(9)]})
        if body["params"][0] == "synthetic-0":
            return httpx.Response(200, json={"result": _tx(JAN23, {A: -COIN, B: COIN})})
        pending += 1
        if pending == 4:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            pending -= 1

    with _settings(), _patched_client(handler):
        async with onchain.session() as client:
            task = asyncio.create_task(onchain.transfers(
                SOL, A, limit=25, client=client, read_state=state,
                deadline=None if external_cancel else time.monotonic() + 0.05,
            ))
            await asyncio.wait_for(started.wait(), 1)
            if external_cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await asyncio.wait_for(task, 1)
                assert result.interruption == "deadline_exceeded"
            assert pending == 0
        count = len(calls)
        await asyncio.sleep(0)
        assert len(calls) == count == 6
    retained = onchain.retained_transfers(SOL, A, limit=25, read_state=state)
    assert retained is not None
    assert len(retained.items) == 1
    assert retained.coverage is not None
    assert retained.coverage.pending_payloads + retained.coverage.failed_payloads == 8
    if external_cancel:
        assert retained.coverage.pending_payloads == 8
    assert retained.interruption == "deadline_exceeded" and retained.resumable


async def test_exhausted_throttle_cancels_inflight_and_does_not_start_next_group():
    state = {}
    calls = Counter()
    started = asyncio.Event()
    blocked = 0

    async def handler(request):
        nonlocal blocked
        body = json.loads(request.content)
        key = body["params"][0]
        calls[body["method"], key] += 1
        if body["method"] == "getSignaturesForAddress":
            return httpx.Response(200, json={"result": [_sig(f"synthetic-{i}", JAN23) for i in range(9)]})
        if key == "synthetic-0":
            return httpx.Response(200, json={"result": _tx(JAN23, {A: -COIN, B: COIN})})
        if key == "synthetic-1":
            await started.wait()
            return httpx.Response(429)
        blocked += 1
        if blocked == 3:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            blocked -= 1

    with _settings(), _patched_client(handler), patch.object(onchain, "RPC_RETRY_BACKOFF_SECONDS", 0):
        result = await onchain.transfers(SOL, A, limit=25, read_state=state)
        before = dict(calls)
        await asyncio.sleep(0)
    assert dict(calls) == before and blocked == 0
    assert len(result.items) == 1 and result.interruption == "upstream_rate_limited"
    assert result.coverage is not None
    assert result.coverage.failed_payloads == 1 and result.coverage.pending_payloads == 7
    assert calls["getTransaction", "synthetic-1"] == 3
    assert all(calls["getTransaction", f"synthetic-{i}"] == 0 for i in range(5, 9))


async def test_continuation_pages_from_cursor_without_refreshing_the_head():
    state = {}
    calls = []
    signatures = [_sig(f"synthetic-new-{i}", JAN23 + (6 - i * 2) * 86400) for i in range(3)]
    signatures.append(_sig("synthetic-old", JAN23))
    serve = _solana_handler(signatures={A: signatures}, txs={"synthetic-old": _tx(JAN23, {A: -COIN, B: COIN})})

    def handler(request):
        body = json.loads(request.content)
        calls.append((body["method"], body["params"]))
        return serve(request)

    with _settings(), _patched_client(handler), patch.object(onchain, "SOLANA_SIGNATURE_PAGE", 3), patch.object(onchain, "SOLANA_HISTORY_MAX_PAGES", 1):
        bound = datetime.fromtimestamp(JAN23, timezone.utc)
        first = await onchain.transfers(SOL, A, limit=25, until=bound, read_state=state)
        assert not first.items and first.resumable
        second = await onchain.transfers(SOL, A, limit=25, until=bound, read_state=state)
    assert len(second.items) == 1 and second.complete
    pages = [params[1] for method, params in calls if method == "getSignaturesForAddress"]
    assert len(pages) == 2 and "before" not in pages[0] and pages[1]["before"] == "synthetic-new-2"
    assert [method for method, _ in calls].count("getTransaction") == 1


@pytest.mark.parametrize("source", ["etherscan", "blockscout", "bitcoin"])
async def test_inline_history_retains_completed_page_or_stream_on_later_failure(source):
    state = {}
    recovered = False
    calls = Counter()
    recipient = "0x" + "cd" * 20

    def handler(request):
        action = request.url.params.get("action")
        key = action or str(request.url)
        calls[key] += 1
        if source == "etherscan":
            if action == "txlist":
                return httpx.Response(200, json={"status": "1", "result": [{
                    "hash": "synthetic-evm", "timeStamp": str(JAN23), "value": str(10**18),
                    "from": EVM, "to": recipient, "isError": "0",
                }]})
            return httpx.Response(200, json={"status": "1", "result": []}) if recovered else httpx.Response(503)
        if source == "blockscout":
            if request.url.path.endswith("internal-transactions") or recovered and request.url.params:
                return httpx.Response(200, json={"items": [], "next_page_params": None})
            if request.url.params:
                return httpx.Response(503)
            return httpx.Response(200, json={
                "items": [_blockscout_item(sender=EVM, recipient=recipient, value=str(10**18), at=JAN23)],
                "next_page_params": {"index": 2},
            })
        if "/chain/" in request.url.path:
            return httpx.Response(200, json=[]) if recovered else httpx.Response(503)
        return httpx.Response(200, json=[_btc_tx("synthetic-bitcoin", JAN23, [(BTC_A, COIN)], [(BTC_B, COIN)])])

    with _settings(etherscan_api_key="synthetic-key" if source == "etherscan" else ""), _patched_client(handler), patch.object(onchain, "BITCOIN_HISTORY_PAGE", 1):
        chain, address = (BTC, BTC_A) if source == "bitcoin" else (BASE, EVM)
        first = await onchain.transfers(chain, address, limit=25, read_state=state)
        assert len(first.items) == 1 and first.resumable and not first.complete
        first_key = next(iter(calls))
        recovered = True
        second = await onchain.transfers(chain, address, limit=25, read_state=json.loads(json.dumps(state)))
    assert second.items == first.items and second.complete
    assert calls[first_key] == 1


@pytest.mark.parametrize("source", ["solana", "etherscan", "blockscout", "bitcoin"])
async def test_replay_keeps_useful_mixed_rows_and_their_explicit_gap(source):
    state = {}
    good_returned = asyncio.Event()
    calls = Counter()
    recipient = "0x" + "cd" * 20

    async def handler(request):
        if source == "solana":
            body = json.loads(request.content)
            method, identity = body["method"], body["params"][0]
            calls[method, identity] += 1
            if method == "getSignaturesForAddress":
                return httpx.Response(200, json={"result": [
                    _sig("synthetic-good", JAN23), _sig("synthetic-bad", JAN23), None,
                ]})
            if identity == "synthetic-good":
                good_returned.set()
                return httpx.Response(200, json={"result": _tx(JAN23, {A: -COIN, B: COIN})})
            await good_returned.wait()
            return httpx.Response(503)
        action = request.url.params.get("action")
        key = action or request.url.path
        calls[key] += 1
        if source == "etherscan":
            if action == "txlistinternal":
                return httpx.Response(503)
            return httpx.Response(200, json={"status": "1", "result": [{
                "hash": "synthetic-good", "timeStamp": str(JAN23), "value": str(10**18),
                "from": EVM, "to": recipient, "isError": "0",
            }, None]})
        if source == "blockscout":
            if request.url.path.endswith("internal-transactions"):
                return httpx.Response(503)
            return httpx.Response(200, json={
                "items": [_blockscout_item(sender=EVM, recipient=recipient, value=str(10**18), at=JAN23), None],
                "next_page_params": None,
            })
        if "/chain/" in request.url.path:
            return httpx.Response(503)
        return httpx.Response(200, json=[
            _btc_tx("synthetic-good", JAN23, [(BTC_A, COIN)], [(BTC_B, COIN)]), None,
        ])

    with _settings(etherscan_api_key="synthetic-key" if source == "etherscan" else ""), _patched_client(handler), patch.object(onchain, "BITCOIN_HISTORY_PAGE", 1):
        chain, address = (SOL, A) if source == "solana" else (BTC, BTC_A) if source == "bitcoin" else (BASE, EVM)
        first = await onchain.transfers(chain, address, limit=25, read_state=state)
        assert onchain.retained_transfers(chain, address, limit=25, read_state=json.loads(json.dumps(state))) == first
    assert len(first.items) == 1 and first.resumable and not first.complete
    assert first.coverage is not None and "invalid_row" in first.coverage.stop_reasons
    assert first.interruption == "provider_unavailable"
    assert all(count == 1 for count in calls.values())


@pytest.mark.parametrize("payload,accepted", [
    pytest.param({"status": "1", "result": []}, True, id="success-empty-list"),
    pytest.param({"status": 1, "result": []}, True, id="numeric-success-empty-list"),
    pytest.param({"status": "0", "message": "No transactions found", "result": []}, True, id="empty-list"),
    pytest.param({"status": "0", "message": "No transactions found", "result": None}, True, id="explicit-empty-null-result"),
    pytest.param({"status": "0", "message": "No transactions found"}, True, id="explicit-empty-missing-result"),
    pytest.param({"status": "0", "message": "No transactions found", "result": "No transactions found"}, True, id="empty-string"),
    pytest.param({"status": "0", "result": "No transactions found"}, True, id="result-only-marker"),
    pytest.param({"status": "0", "message": "NOTOK", "result": "No transactions found"}, True, id="result-marker-with-other-message"),
    pytest.param({"status": 0, "message": "  NO TRANSACTIONS FOUND  ", "result": []}, True, id="normalized-status-and-message"),
    pytest.param(None, False, id="raw-null"),
    pytest.param([], False, id="raw-list"),
    pytest.param({}, False, id="missing-fields"),
    pytest.param({"status": "1", "result": None}, False, id="success-null-result"),
    pytest.param({"status": "1", "result": {}}, False, id="success-malformed-result"),
    pytest.param({"status": "0", "result": []}, False, id="empty-list-without-marker"),
    pytest.param({"status": "0", "message": "NOTOK", "result": "Invalid API Key"}, False, id="provider-error"),
    pytest.param({"status": "0", "message": "NOTOK", "result": []}, False, id="error-empty-list"),
    pytest.param({"status": "0", "message": "No transactions found", "result": {}}, False, id="empty-marker-malformed-result"),
    pytest.param({"status": "0", "result": " no transactions found "}, False, id="noncanonical-result-string"),
    pytest.param({"message": "No transactions found", "result": []}, False, id="empty-marker-without-status"),
])
async def test_etherscan_empty_response_reuse_matches_decoder_across_serialized_continue(payload, accepted):
    state = {}
    calls = Counter()
    recovered = False

    def handler(request):
        action = request.url.params["action"]
        calls[action] += 1
        if action == "txlist" and not recovered:
            return httpx.Response(200, content=json.dumps(payload), headers={"Content-Type": "application/json"})
        if not recovered:
            return httpx.Response(503)
        return httpx.Response(200, json={"status": "1", "result": []})

    with _settings(etherscan_api_key="synthetic-key"), _patched_client(handler):
        if accepted:
            first = await onchain.transfers(BASE, EVM, limit=25, read_state=state)
            assert first.resumable and not first.complete and not first.items
            assert first.coverage is not None and first.coverage.pages_read == 1
        else:
            with pytest.raises(RuntimeError):
                await onchain.transfers(BASE, EVM, limit=25, read_state=state)
            assert all(not history["pages"] for history in state["histories"].values())
        recovered = True
        second = await onchain.transfers(BASE, EVM, limit=25, read_state=json.loads(json.dumps(state)))
    assert second.complete and not second.items
    assert second.coverage is not None and second.coverage.pages_read == 2
    assert calls == {"txlist": 1 if accepted else 2, "txlistinternal": 2 if accepted else 1}


async def test_invalid_version_source_and_bounded_storage_cannot_silently_reuse():
    state = {}
    serve = _solana_handler(signatures={A: [_sig("synthetic", JAN23)]}, txs={"synthetic": _tx(JAN23, {A: -COIN, B: COIN})})
    with _settings(), _patched_client(serve):
        await onchain.transfers(SOL, A, limit=25, read_state=state)
        changed = {**state, "version": "different-decoder"}
        with pytest.raises(ValueError, match="incompatible"):
            await onchain.transfers(SOL, A, limit=25, read_state=changed)
        with _settings(onchain_rpc_urls={"solana": "https://synthetic.invalid/other-network"}):
            with pytest.raises(ValueError, match="source changed"):
                await onchain.transfers(SOL, A, limit=25, read_state=state)
        with patch.object(onchain_reads, "MAX_READ_BYTES", 1):
            limited = await onchain.transfers(SOL, A, limit=25, read_state={})
    assert not limited.resumable and not limited.complete
    assert limited.coverage is not None
    assert "retention_limit" in limited.coverage.stop_reasons


@pytest.mark.parametrize("failed_execution", [True, False])
async def test_valid_finalized_failed_or_unsupported_transaction_is_reused_without_inventing_transfer(failed_execution):
    state = {}
    calls = Counter()
    # All fields are readable, but opposing deltas cannot be attributed.
    tx = _tx(JAN23, {A: -10 * COIN, B: COIN})
    if failed_execution:
        tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    serve = _solana_handler(signatures={A: [_sig("synthetic", JAN23)]}, txs={"synthetic": tx})

    def handler(request):
        body = json.loads(request.content)
        calls[body["method"]] += 1
        return serve(request)

    with _settings(), _patched_client(handler):
        first = await onchain.transfers(SOL, A, limit=25, read_state=state)
        second = await onchain.transfers(SOL, A, limit=25, read_state=json.loads(json.dumps(state)))
    assert first.items == second.items == []
    assert first.complete is failed_execution
    assert first.coverage is not None
    assert first.coverage.failed_payloads == 0
    assert first.coverage.unsupported_payloads == int(not failed_execution)
    assert calls == {"getSignaturesForAddress": 1, "getTransaction": 1}


async def test_total_page_and_window_caps_do_not_offer_ineffective_continuation():
    state = {}
    calls = Counter()
    serve = _solana_handler(signatures={A: [
        _sig(f"synthetic-{i}", JAN23 + i * 2 * 86400) for i in range(3)
    ]})

    def handler(request):
        body = json.loads(request.content)
        calls[body["method"]] += 1
        return serve(request)

    with (
        _settings(), _patched_client(handler),
        patch.object(onchain, "SOLANA_SIGNATURE_PAGE", 3),
        patch.object(onchain, "MAX_HISTORY_PAGES", 1),
        patch.object(onchain_reads, "MAX_HISTORY_PAGES", 1),
        patch.object(onchain_reads, "MAX_HISTORY_WINDOWS", 1),
    ):
        bound = datetime.fromtimestamp(JAN23 - 1, timezone.utc)
        first = await onchain.transfers(SOL, A, limit=25, until=bound, read_state=state)
        second = await onchain.transfers(SOL, A, limit=25, until=bound, read_state=state)
        assert not first.resumable and not second.resumable
        assert second.coverage is not None
        assert "retention_limit" in second.coverage.stop_reasons
        with pytest.raises(ValueError, match="window limit"):
            await onchain.transfers(SOL, B, limit=25, read_state=state)
    assert calls == {"getSignaturesForAddress": 1}
    assert state["limited"] is True
