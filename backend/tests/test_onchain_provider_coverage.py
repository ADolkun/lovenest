"""Native history coverage: synthetic pages only, no network or account data."""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.providers import onchain
from tests.test_providers_onchain import (
    A, B, BASE, BTC, BTC_A, BTC_B, COIN, EVM, JAN23, SOL,
    _blockscout_item, _btc_tx, _settings, _sig, _tx,
)

pytestmark = pytest.mark.asyncio
DAY = 86400
AT = datetime.fromtimestamp(JAN23, timezone.utc)


async def _solana(pages, txs=None, **window):
    calls = []
    remaining = iter(pages)

    async def rpc(chain, method, params, **kwargs):
        calls.append((method, params, kwargs))
        if method == "getSignaturesForAddress":
            return next(remaining)
        assert method == "getTransaction"
        return (txs or {}).get(params[0])

    with patch.object(onchain, "_json_rpc", rpc), patch.object(onchain, "SOLANA_SIGNATURE_PAGE", 3):
        result = await onchain.transfers(SOL, A, limit=window.pop("limit", 25), **window)
    return result, calls


def _payloads(rows):
    return {row["signature"]: _tx(row["blockTime"], {A: -COIN, B: COIN}) for row in rows}


async def test_ceiling_only_reaches_the_second_page_before_examining_payloads():
    newer = [_sig(f"new-{i}", JAN23 + (6 - 2 * i) * DAY) for i in range(3)]
    wanted = [_sig("wanted", JAN23)]
    result, calls = await _solana([newer, wanted], _payloads(wanted), until=AT)
    coverage = result.coverage
    assert result.complete and [t.reference for t in result.items] == ["wanted"]
    assert calls[1][1][1]["before"] == "new-2"
    assert coverage.pages_read == 2 and coverage.signatures_read == coverage.rows_read == 4
    assert coverage.payloads_requested == coverage.payloads_read == 1
    assert coverage.since_reached is None and coverage.until_reached is True
    assert coverage.provider_exhausted is True and coverage.next_cursor is None
    assert coverage.observed_newest == AT + timedelta(days=6)
    assert coverage.examined_oldest == coverage.examined_newest == AT
    assert asdict(coverage)["omitted_transfers"] is None


async def test_inclusive_floor_and_ceiling_keep_equal_timestamps_across_pages():
    first = [_sig("new", JAN23 + 4 * DAY), _sig("newer", JAN23 + 2 * DAY), _sig("tie-a", JAN23)]
    second = [_sig("tie-b", JAN23), _sig("old", JAN23 - 2 * DAY)]
    result, calls = await _solana([first, second], _payloads(first + second), since=AT, until=AT)
    assert result.complete
    assert {t.reference for t in result.items} == {"tie-a", "tie-b"}
    assert sum(method == "getSignaturesForAddress" for method, _, _ in calls) == 2


async def test_crossing_floor_is_complete_without_claiming_provider_exhaustion():
    rows = [_sig("new", JAN23 + 4 * DAY), _sig("at", JAN23), _sig("old", JAN23 - 2 * DAY)]
    result, calls = await _solana([rows], _payloads(rows), since=AT)
    assert result.complete and len(result.items) == 2
    assert result.coverage.since_reached is True
    assert result.coverage.provider_exhausted is False
    assert result.coverage.next_cursor == "old"
    assert len(calls) == 3


async def test_open_window_requires_exhaustion_after_a_full_page():
    rows = [_sig(f"s{i}", JAN23 - i * 2 * DAY) for i in range(3)]
    result, calls = await _solana([rows, []], _payloads(rows))
    assert result.complete and result.coverage.pages_read == 2
    assert result.coverage.provider_exhausted is True
    assert calls[1][1][1]["before"] == "s2"


async def test_four_page_limit_keeps_cursor_and_unreached_ceiling():
    pages = [[_sig(f"s{p}-{i}", JAN23 + (30 - p * 6 - i * 2) * DAY) for i in range(3)] for p in range(4)]
    result, calls = await _solana(pages, until=AT)
    assert len(calls) == 4 and result.items == [] and not result.complete
    assert result.coverage.next_cursor == "s3-2"
    assert result.coverage.until_reached is False
    assert result.coverage.provider_exhausted is False
    assert set(result.coverage.stop_reasons) == {"provider_page_limit", "window_not_reached"}


@pytest.mark.parametrize("page", [None, {}, "bad", 3, [None], [{"blockTime": JAN23}]])
async def test_malformed_signature_pages_cannot_prove_empty_history(page):
    result, _ = await _solana([page])
    assert result.items == [] and not result.complete
    assert set(result.coverage.stop_reasons) & {"invalid_page", "invalid_row"}


@pytest.mark.parametrize("stamp", [None, True, "unknown", float("inf"), 10**30])
async def test_unknown_signature_time_survives_short_page_exhaustion(stamp):
    result, _ = await _solana([[_sig("unknown", stamp)]])
    assert not result.complete and result.coverage.provider_exhausted is True
    assert result.coverage.missing_timestamps == 1
    assert result.coverage.payloads_requested == 0
    assert "missing_timestamp" in result.coverage.stop_reasons


async def test_full_page_with_unknown_timestamp_keeps_gap_despite_exhaustion():
    rows = [_sig("a", JAN23 + 4 * DAY), _sig("b", JAN23 + 2 * DAY), _sig("unknown", None)]
    result, _ = await _solana([rows, []], until=AT)
    assert result.coverage.provider_exhausted is True
    assert result.coverage.missing_timestamps == 1 and not result.complete


@pytest.mark.parametrize("pages", [
    [[_sig("a", JAN23), _sig("b", JAN23 + DAY)]],
    [[_sig(f"s{i}", JAN23 - i * DAY) for i in range(4)]],
    [[_sig("a", JAN23 + 6 * DAY), _sig("b", JAN23 + 4 * DAY), _sig("c", JAN23 + 2 * DAY)],
     [_sig("d", JAN23 + 3 * DAY)]],
])
async def test_reordered_or_oversized_signature_data_retains_invalid_gap(pages):
    result, _ = await _solana(pages, until=AT)
    assert not result.complete
    assert set(result.coverage.stop_reasons) & {"invalid_row", "invalid_page"}


async def test_repeated_cursor_stops_and_does_not_duplicate_payload_requests():
    rows = [_sig(f"s{i}", JAN23 - i * 2 * DAY) for i in range(3)]
    result, calls = await _solana([rows, rows], _payloads(rows))
    assert not result.complete and len(result.items) == 3
    assert "nonadvancing_cursor" in result.coverage.stop_reasons
    assert sum(method == "getTransaction" for method, _, _ in calls) == 3


async def test_payload_limit_reports_omitted_signatures_and_preserves_nearest_floor():
    rows = [_sig(f"s{i}", JAN23 - i * 2 * DAY) for i in range(3)]
    result, _ = await _solana([rows, []], _payloads(rows), limit=1, since=AT - timedelta(days=5))
    assert [t.reference for t in result.items] == ["s2"]
    assert result.coverage.omitted_signatures == 2
    assert result.coverage.payloads_requested == result.coverage.payloads_read == 1
    assert "payload_limit" in result.coverage.stop_reasons and not result.complete


async def test_good_missing_and_unsupported_payloads_keep_distinct_counts():
    rows = [_sig(f"s{i}", JAN23 - i * 2 * DAY) for i in range(3)]
    txs = {"s0": _payloads(rows)["s0"], "s2": {"blockTime": JAN23 - 4 * DAY, "meta": ["bad"]}}
    result, _ = await _solana([rows, []], txs)
    assert [t.reference for t in result.items] == ["s0"] and not result.complete
    assert result.unreadable == 2
    assert result.coverage.payloads_requested == 3 and result.coverage.payloads_read == 2
    assert result.coverage.missing_payloads == result.coverage.unsupported_payloads == 1
    assert set(result.coverage.stop_reasons) == {"missing_payload", "unsupported_payload"}
    assert result.coverage.examined_oldest == AT - timedelta(days=4)


async def test_epoch_zero_and_naive_dates_are_utc_and_inclusive():
    rows = [_sig("epoch", 0)]
    epoch = datetime(1970, 1, 1)
    result, _ = await _solana([rows], _payloads(rows), since=epoch, until=epoch)
    assert result.complete and result.items[0].occurred_at == epoch.replace(tzinfo=timezone.utc)
    assert result.coverage.requested_since == result.coverage.requested_until == epoch.replace(tzinfo=timezone.utc)


async def test_inverted_provider_window_rejects_before_rpc():
    rpc = AsyncMock()
    with patch.object(onchain, "_json_rpc", rpc), pytest.raises(ValueError, match="since"):
        await onchain.transfers(SOL, A, limit=25, since=AT, until=AT - timedelta(seconds=1))
    rpc.assert_not_called()


async def test_signature_payload_timestamp_disagreement_does_not_escape_window():
    result, _ = await _solana([[_sig("x", JAN23)]], {"x": _tx(JAN23 + DAY, {A: -COIN, B: COIN})}, until=AT)
    assert result.items == [] and not result.complete
    assert "invalid_row" in result.coverage.stop_reasons


async def test_later_signature_page_error_keeps_earlier_evidence_and_gap():
    rows = [_sig(f"s{i}", JAN23 - i * 2 * DAY) for i in range(3)]
    result, _ = await _solana([rows], _payloads(rows))  # exhausted mock iterator is a failed second read
    assert len(result.items) == 3 and not result.complete
    assert result.coverage.pages_read == 1
    assert "provider_unavailable" in result.coverage.stop_reasons


async def test_etherscan_permanent_error_is_not_empty_history():
    with patch.object(onchain, "_get_json", AsyncMock(return_value={"status": "0", "message": "NOTOK", "result": "Invalid API Key"})):
        with pytest.raises(RuntimeError, match="unexpected payload"):
            await onchain._etherscan_page(BASE, EVM, "txlist", "synthetic", None)


async def test_etherscan_one_unfinished_stream_keeps_aggregate_partial():
    rows = [{"hash": f"synthetic-{i}", "timeStamp": str(JAN23 - i * 2 * DAY), "value": "0", "from": EVM, "to": EVM} for i in range(3)]
    with (
        _settings(etherscan_api_key="synthetic"),
        patch.object(onchain, "EVM_HISTORY_PAGE", 3),
        patch.object(onchain, "_get_json", AsyncMock(side_effect=[{"status": "1", "result": rows}, {"status": "1", "result": []}])),
    ):
        result = await onchain.transfers(BASE, EVM, limit=25)
    assert result.coverage is not None
    assert not result.complete and result.coverage.provider_exhausted is False
    assert result.coverage.pages_read == 2 and result.coverage.rows_read == 3
    assert result.coverage.signatures_read is result.coverage.payloads_requested is None
    assert result.coverage.next_cursor is None
    assert "provider_page_limit" in result.coverage.stop_reasons


@pytest.mark.parametrize("payload", [{}, {"items": None}, {"items": "bad"}, {"items": [], "next_page_params": 1}])
async def test_blockscout_malformed_page_is_not_complete(payload):
    with _settings(), patch.object(onchain, "_get_json", AsyncMock(return_value=payload)):
        result = await onchain.transfers(BASE, EVM, limit=25)
    assert result.coverage is not None
    assert not result.complete and "invalid_page" in result.coverage.stop_reasons
    assert result.coverage.provider_exhausted is None


async def test_blockscout_missing_timestamp_is_not_confirmed_absence():
    payload = {"items": [{"timestamp": None}], "next_page_params": None}
    with _settings(), patch.object(onchain, "_get_json", AsyncMock(return_value=payload)):
        result = await onchain.transfers(BASE, EVM, limit=25)
    assert result.coverage is not None
    assert result.coverage.provider_exhausted is True and not result.complete
    assert result.coverage.missing_timestamps == 2


async def test_blockscout_decoded_transfer_limit_has_transfer_units():
    rows = [_blockscout_item(sender=EVM, recipient="synthetic-other", value="100", at=JAN23 - i * DAY) for i in range(2)]
    with _settings(), patch.object(onchain, "_get_json", AsyncMock(side_effect=[{"items": rows, "next_page_params": None}, {"items": [], "next_page_params": None}])):
        result = await onchain.transfers(BASE, EVM, limit=1)
    assert result.coverage is not None
    assert result.coverage.omitted_transfers == 1 and result.coverage.omitted_signatures is None
    assert not result.complete and "transfer_limit" in result.coverage.stop_reasons


async def test_bitcoin_malformed_page_is_not_exhausted():
    with patch.object(onchain, "_esplora", AsyncMock(return_value=None)):
        result = await onchain.transfers(BTC, BTC_A, limit=25)
    assert result.coverage is not None
    assert not result.complete and result.coverage.provider_exhausted is None
    assert result.coverage.stop_reasons == ["invalid_page"]


async def test_bitcoin_floor_crossing_is_not_provider_exhaustion():
    rows = [_btc_tx(f"s{i}", JAN23 - i * 2 * DAY, [(BTC_A, COIN)], [(BTC_B, COIN)]) for i in range(3)]
    with patch.object(onchain, "BITCOIN_HISTORY_PAGE", 3), patch.object(onchain, "_esplora", AsyncMock(return_value=rows)):
        result = await onchain.transfers(BTC, BTC_A, limit=25, since=AT - timedelta(days=3))
    assert result.coverage is not None
    assert result.complete and result.coverage.provider_exhausted is False
    assert result.coverage.since_reached is True and result.coverage.pages_read == 1


async def test_bitcoin_pending_timestamp_stays_unknown_in_history():
    rows = [_btc_tx("pending", JAN23, [(BTC_A, COIN)], [(BTC_B, COIN)], confirmed=False)]
    with patch.object(onchain, "_esplora", AsyncMock(return_value=rows)):
        result = await onchain.transfers(BTC, BTC_A, limit=25)
    assert result.items == [] and not result.complete
    assert result.coverage is not None
    assert result.coverage.missing_timestamps == 1
    assert result.coverage.observed_oldest is result.coverage.examined_oldest is None


async def test_bitcoin_nonpooled_page_budget_is_not_lost():
    rows = [_btc_tx(f"s{i}", JAN23 - i * 2 * DAY, [(BTC_A, COIN)], [(BTC_B, COIN)]) for i in range(3)]
    with patch.object(onchain, "BITCOIN_HISTORY_PAGE", 3), patch.object(onchain, "BITCOIN_HISTORY_MAX_PAGES", 1), patch.object(onchain, "_esplora", AsyncMock(return_value=rows)):
        result = await onchain.transfers(BTC, BTC_A, limit=25)
    assert len(result.items) == 3 and not result.complete
    assert result.saturated is None
    assert result.coverage is not None
    assert result.coverage.stop_reasons == ["provider_page_limit"]


@pytest.mark.parametrize("value", [None, "unknown", -1, True])
async def test_bitcoin_malformed_inline_values_are_unknown_not_zero(value):
    row = _btc_tx("bad-value", JAN23, [(BTC_A, COIN)], [(BTC_B, value)])
    with patch.object(onchain, "_esplora", AsyncMock(return_value=[row])):
        result = await onchain.transfers(BTC, BTC_A, limit=25)
    assert not result.complete and result.items == []
    assert result.coverage is not None
    assert result.coverage.unsupported_payloads == 1


async def test_bitcoin_repeated_page_does_not_duplicate_transfers():
    rows = [_btc_tx(f"s{i}", JAN23 - i * 2 * DAY, [(BTC_A, COIN)], [(BTC_B, COIN)]) for i in range(3)]
    with patch.object(onchain, "BITCOIN_HISTORY_PAGE", 3), patch.object(onchain, "_esplora", AsyncMock(return_value=rows)):
        result = await onchain.transfers(BTC, BTC_A, limit=25)
    assert not result.complete and len(result.items) == 3
    assert result.coverage is not None
    assert result.coverage.pages_read == 2
    assert "nonadvancing_cursor" in result.coverage.stop_reasons


@pytest.mark.parametrize("error", [False, True, 0, [], {}, ""])
async def test_invalid_signature_error_is_a_gap_with_usable_siblings(error):
    rows = [_sig("good", JAN23), _sig("bad", JAN23 - DAY, err=error)]
    result, _ = await _solana([rows], _payloads(rows))
    assert not result.complete and result.coverage.stop_reasons == ["invalid_row"]
    assert [t.reference for t in result.items] == ["good"]


@pytest.mark.parametrize("error", [False, True, 0, [], {}, ""])
async def test_invalid_payload_error_is_unreadable_with_usable_siblings(error):
    rows = [_sig("good", JAN23), _sig("bad", JAN23 - DAY)]
    txs = _payloads(rows)
    txs["bad"]["meta"]["err"] = error
    result, _ = await _solana([rows], txs)
    assert not result.complete and result.coverage.unsupported_payloads == 1
    assert [t.reference for t in result.items] == ["good"]


async def test_missing_signature_and_payload_error_fields_are_gaps():
    rows = [_sig("bad-signature", JAN23), _sig("bad-payload", JAN23 - DAY)]
    del rows[0]["err"]
    txs = _payloads(rows)
    del txs["bad-payload"]["meta"]["err"]
    result, _ = await _solana([rows], txs)
    assert not result.complete and result.items == []
    assert set(result.coverage.stop_reasons) == {"invalid_row", "unsupported_payload"}


@pytest.mark.parametrize("envelope", [
    {"status": "0", "message": "NOTOK", "result": []},
    {"result": []},
    {"status": "0", "result": []},
])
async def test_empty_etherscan_list_without_success_envelope_is_unavailable(envelope):
    with _settings(etherscan_api_key="synthetic"), patch.object(onchain, "_get_json", AsyncMock(return_value=envelope)):
        with pytest.raises(RuntimeError, match="unexpected payload"):
            await onchain.transfers(BASE, EVM, limit=25)


async def test_etherscan_no_transactions_message_cannot_hide_nonempty_bad_data():
    payload = {"status": "0", "message": "No transactions found", "result": [{"unexpected": True}]}
    with _settings(etherscan_api_key="synthetic"), patch.object(onchain, "_get_json", AsyncMock(return_value=payload)):
        with pytest.raises(RuntimeError, match="unexpected payload"):
            await onchain.transfers(BASE, EVM, limit=25)


async def test_inline_evm_failed_payload_still_has_an_examined_timestamp():
    row = _blockscout_item(sender=EVM, recipient="synthetic-other", value="100", at=JAN23, status="error")
    with _settings(), patch.object(onchain, "_get_json", AsyncMock(side_effect=[{"items": [row], "next_page_params": None}, {"items": [], "next_page_params": None}])):
        result = await onchain.transfers(BASE, EVM, limit=25)
    assert result.complete and result.items == []
    assert result.coverage is not None
    assert result.coverage.examined_oldest == result.coverage.examined_newest == AT


async def test_unrelated_evm_row_cannot_invent_a_transfer_to_the_requested_address():
    row = _blockscout_item(sender="synthetic-sender", recipient="synthetic-recipient", value="100", at=JAN23)
    with _settings(), patch.object(onchain, "_get_json", AsyncMock(side_effect=[{"items": [row], "next_page_params": None}, {"items": [], "next_page_params": None}])):
        result = await onchain.transfers(BASE, EVM, limit=25)
    assert not result.complete and result.items == []
    assert result.coverage is not None
    assert result.coverage.unsupported_payloads == 1


@pytest.mark.parametrize("party", [None, "", {}, []])
async def test_bitcoin_unsupported_party_cannot_prove_no_movement(party):
    bad = _btc_tx("bad-party", JAN23, [(BTC_A, COIN)], [(party, COIN)])
    good = _btc_tx("good", JAN23 - DAY, [(BTC_A, COIN)], [(BTC_B, COIN)])
    with patch.object(onchain, "_esplora", AsyncMock(return_value=[bad, good])):
        result = await onchain.transfers(BTC, BTC_A, limit=25)
    assert result.coverage is not None
    assert not result.complete and result.coverage.unsupported_payloads == 1
    assert [t.reference for t in result.items] == ["good"]


@pytest.mark.parametrize("value,complete", [(0, True), (1, False), (None, False), ("0", False), (False, False)])
async def test_bitcoin_payment_with_zero_value_op_return_keeps_its_native_transfer(value, complete):
    row = _btc_tx("payment-with-data", JAN23, [(BTC_A, COIN)], [(BTC_B, COIN)])
    row["vout"].append({
        "scriptpubkey": "6a", "scriptpubkey_asm": "OP_RETURN",
        "scriptpubkey_type": "op_return", "value": value,
    })
    with patch.object(onchain, "_esplora", AsyncMock(return_value=[row])):
        result = await onchain.transfers(BTC, BTC_A, limit=25)
    assert result.coverage is not None
    assert result.complete is complete
    if complete:
        assert [(t.reference, t.recipient, t.amount) for t in result.items] == [("payment-with-data", BTC_B, 1)]
        assert result.coverage.unsupported_payloads == 0 and result.coverage.stop_reasons == []
    else:
        assert result.items == [] and result.coverage.unsupported_payloads == 1
        assert result.coverage.stop_reasons == ["unsupported_payload"]
