"""Invented EVM evidence only; all transport is replaced before collection."""
import copy
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.providers import evm_history as history, onchain
from app.providers.onchain_transport import OnchainDeadlineExceeded, RequestBudget

OWNER, OTHER, TOKEN = ["0x" + digit * 40 for digit in "abc"]
TX, INTERNAL_TX = ["0x" + digit * 64 for digit in "12"]
STAMP, CODE = 1_800_000_000, "0x6001600055"
CODE_HASH = hashlib.sha256(bytes.fromhex(CODE[2:])).hexdigest()


def word(number):
    return "0x" + format(number, "064x")


def block(number):
    return {"number": hex(number), "hash": word(number + 1000), "timestamp": hex(STAMP + number), "transactions": [TX, INTERNAL_TX]}


def transfer(index, value, source=OWNER, destination=OTHER, reference=TX):
    return {"address": TOKEN, "topics": [history.TRANSFER_TOPIC, "0x" + source[2:].zfill(64), "0x" + destination[2:].zfill(64)],
            "data": word(value), "blockNumber": "0xa", "blockHash": block(10)["hash"], "transactionHash": reference,
            "transactionIndex": "0x0", "logIndex": hex(index), "removed": False}


def bundle(reference=TX, *, failed=False, logs=None, payer=OWNER, amount=6 * 10**18):
    tx = {"hash": reference, "blockNumber": "0xa", "blockHash": block(10)["hash"], "from": payer, "to": OTHER, "value": hex(amount), "type": "0x2"}
    receipt = {"transactionHash": reference, "blockNumber": "0xa", "blockHash": block(10)["hash"],
               "transactionIndex": "0x0" if reference == TX else "0x1", "from": payer, "status": "0x0" if failed else "0x1",
               "gasUsed": "0x5208", "effectiveGasPrice": hex(10**9), "l1Fee": "0x64", "operatorFeeScalar": "0xf4240",
               "operatorFeeConstant": "0x2", "operatorFee": hex(21002), "logs": logs if logs is not None else [transfer(0, 10**6), transfer(1, 2 * 10**6)]}
    trace = {"type": "CALL", "from": payer, "to": OTHER, "value": hex(amount), "calls": [{"type": "CALL", "from": OTHER, "to": OWNER, "value": "0x5"}]}
    if failed:
        trace["error"] = "execution reverted"
    return {"transaction": tx, "receipt": receipt, "trace": trace, "block": block(10), "tokens": {},
            "finality": {"finalized": block(20), "l1_corroborated": True}, "in_requested_window": True,
            "index_rows": [], "internal_rows": [], "discovered_logs": [], "source_refs": []}


def policy():
    return {"rollup_rpc_url": "https://rollup.synthetic.invalid/private-token",
            "operator_fee_forks": [{"from_timestamp": 0, "formula": "isthmus", "source": "synthetic-reviewed-fork"}],
            "standard_tokens": {TOKEN: {"from_block": 0, "to_block": 100, "code_hash": CODE_HASH,
                                         "semantics": "standard_erc20", "non_proxy": True, "source": "synthetic-reviewed-code"}}}


class RPC:
    def __init__(self, payloads=None):
        self.payloads = payloads if payloads is not None else {TX: bundle(), INTERNAL_TX: bundle(INTERNAL_TX, logs=[], payer=OTHER, amount=0)}
        self.calls, self.overrides, self.log_span = [], {}, None

    async def __call__(self, client, method, url, **kwargs):
        kwargs["budget"].consume()
        assert kwargs["deadline"] is not None and kwargs["concurrency"] <= 5
        body = kwargs.get("json_body")
        name = body["method"] if body else kwargs.get("params", {}).get("action") or url.rsplit("/", 1)[-1]
        params = body["params"] if body else kwargs["params"]
        self.calls.append((name, copy.deepcopy(params)))
        if name in self.overrides:
            value = self.overrides[name]
            value = value(params) if callable(value) else value
            if isinstance(value, BaseException):
                raise value
            return {"result": copy.deepcopy(value)} if body else copy.deepcopy(value)
        if name == "eth_getBlockByNumber":
            result = block(20 if params[0] in ("latest", "finalized") else int(params[0], 16))
            result["transactions"] = [reference for reference, payload in self.payloads.items()
                                      if payload["receipt"]["blockNumber"] == result["number"]]
        elif name == "optimism_syncStatus":
            result = {"finalized_l2": {**block(20), "l1origin": {"hash": word(500), "number": 100}}, "finalized_l1": {"hash": word(501), "number": 101}}
        elif name in ("transactions", "txlist", "internal-transactions", "txlistinternal"):
            internal = name in ("internal-transactions", "txlistinternal")
            rows = [{"transaction_hash": ref, "hash": ref, "blockNumber": "10", "timeStamp": str(STAMP + 10),
                     "traceId": "0", "from": OTHER, "to": OWNER, "type": "call", "value": "5", "isError": "0"}
                    for ref in self.payloads if internal or ref == TX]
            if not internal:
                rows = [{"hash": row["hash"], "blockNumber": "10", "timeStamp": str(STAMP + 10)} for row in rows]
            return {"status": "1", "result": rows} if name.startswith("txlist") else {"items": rows, "next_page_params": None}
        elif name in ("eth_getTransactionByHash", "eth_getTransactionReceipt", "debug_traceTransaction"):
            key = {"eth_getTransactionByHash": "transaction", "eth_getTransactionReceipt": "receipt", "debug_traceTransaction": "trace"}[name]
            result = self.payloads.get(params[0], {}).get(key)
        elif name == "eth_getLogs":
            spec = params[0]
            if "fromBlock" in spec and self.log_span is not None and int(spec["toBlock"], 16) - int(spec["fromBlock"], 16) > self.log_span:
                raise RuntimeError("synthetic limited range")
            result = []
            for payload in self.payloads.values():
                for log in payload["receipt"]["logs"]:
                    if spec.get("blockHash") and log["blockHash"] != spec["blockHash"]:
                        continue
                    if spec.get("address") and log["address"] != spec["address"]:
                        continue
                    if "fromBlock" in spec and not int(spec["fromBlock"], 16) <= int(log["blockNumber"], 16) <= int(spec["toBlock"], 16):
                        continue
                    if any(topic is not None and (len(log["topics"]) <= i or log["topics"][i] != topic) for i, topic in enumerate(spec["topics"])):
                        continue
                    result.append(log)
        elif name == "eth_getCode":
            result = CODE
        elif name == "eth_call":
            data = params[0]["data"]
            if data == "0x313ce567":
                result = word(6)
            else:
                account = "0x" + data[-40:]
                observed_block = int(params[1]["blockHash"], 16) - 1000
                delta = sum(int(log["data"], 16) * (int(log["topics"][2][-40:] == account[2:]) - int(log["topics"][1][-40:] == account[2:]))
                            for payload in self.payloads.values() for log in payload["receipt"]["logs"]
                            if history._log(log) and log["address"] == params[0]["to"] and int(log["blockNumber"], 16) <= observed_block)
                result = word(10**15 + delta)
        else:
            raise AssertionError(name)
        return {"result": copy.deepcopy(result)}


def install(monkeypatch, rpc, *, key="", policies=None):
    monkeypatch.setattr(history, "request_json", rpc)
    monkeypatch.setattr(onchain, "rpc_url", lambda chain: "https://rpc.synthetic.invalid/private-token?apikey=hidden")
    monkeypatch.setattr(onchain, "get_settings", lambda: SimpleNamespace(etherscan_api_key=key, evm_history_policies=policies or {}))


async def collect(chain="ethereum", **kwargs):
    return await history.collect_evm_history(OWNER, chain=chain, source_identity="synthetic-source", **kwargs)


def current(archive, reference=TX):
    transaction = archive["transactions"][archive["chain"] + ":" + reference]
    return next(v for v in transaction["versions"] if v["version_id"] == transaction["canonical_version"])


@pytest.mark.parametrize("chain", ["ethereum", "base", "polygon"])
@pytest.mark.parametrize("indexed", [False, True])
async def test_networks_ordinary_internal_tokens_fees_replay_and_retention(monkeypatch, chain, indexed):
    rpc = RPC()
    install(monkeypatch, rpc, key="synthetic-index-key" if indexed else "", policies={chain: policy()})
    archive = await collect(chain)
    assert archive["coverage"] == {"inventory": "declared_address_only", "retrieval": "complete", "interpretation": "complete", "settlement": "complete"}
    version = current(archive)
    logs = [leg for leg in version["legs"] if not leg["asset"]["native"]]
    assert [leg["quantity"] for leg in logs] == ["1.000000", "2.000000"]
    assert len({leg["key"] for leg in logs}) == 2
    assert all(leg["asset"]["contract"] == TOKEN and leg["asset"]["chain"] == chain for leg in logs)
    assert [leg["raw_units"] for leg in version["legs"] if leg["role"] == "principal" and leg["asset"]["native"]] == [str(6 * 10**18), "5"]
    assert len([leg for leg in version["legs"] if leg["key"].endswith("fee:execution")]) == 1
    assert version["fee_status"] == "complete"
    internal = current(archive, INTERNAL_TX)
    assert any(leg["destination"] == OWNER and leg["raw_units"] == "5" for leg in internal["legs"])
    assert all(leg["source_owner"] is None for leg in internal["legs"] if leg["role"] == "network_fee")
    serialized = json.dumps(archive)
    assert "private-token" not in serialized and "synthetic-index-key" not in serialized
    assert json.loads(serialized) == archive
    first_count = len(rpc.calls)
    resumed = await collect(chain, state=archive)
    assert len(rpc.calls) == first_count + 1
    assert len(resumed["transactions"][chain + ":" + TX]["versions"]) == 1


@pytest.mark.parametrize("chain", ["ethereum", "base", "polygon"])
def test_failed_principal_and_reverted_descendants_keep_payer_fee(chain):
    payload = bundle(failed=True, payer=OTHER)
    result = history.decode_evm_transaction(TX, payload, chain=chain, owner=OWNER, payload_digest="synthetic", policy=policy())
    assert result["execution"] == "failed" and result["attempted"]
    assert all(leg["role"] == "network_fee" and leg["source"] == OTHER and leg["source_owner"] is None for leg in result["legs"])
    payload = bundle(logs=[])
    payload["trace"]["calls"] = [{"type": "CALL", "from": OTHER, "to": OWNER, "value": "0x3", "error": "reverted", "calls": [
        {"type": "CALL", "from": OWNER, "to": OTHER, "value": "0x2"}]}, {"type": "CALL", "from": OTHER, "to": OWNER, "value": "0x5"}]
    result = history.decode_evm_transaction(TX, payload, chain=chain, owner=OWNER, payload_digest="synthetic", policy=policy())
    assert [leg["raw_units"] for leg in result["legs"] if ":trace:" in leg["key"]] == ["5"]
    assert any(item.get("ancestor_reverted") for item in result["attempted"])


async def test_self_mint_burn_zero_approval_nft_and_unknown_decimals(monkeypatch):
    approval, nft = transfer(5, 7), transfer(6, 8)
    approval["topics"][0] = word(999)
    nft["topics"].append(word(8))
    logs = [transfer(0, 3, OWNER, OWNER), transfer(1, 4, history.ZERO, OWNER), transfer(2, 2, OWNER, history.ZERO), transfer(3, 0), approval, nft]
    install(monkeypatch, RPC({TX: bundle(logs=logs)}), policies={"ethereum": policy()})
    version = current(await collect())
    tokens = [leg for leg in version["legs"] if not leg["asset"]["native"]]
    assert [(leg["role"], leg["raw_units"]) for leg in tokens] == [("principal", "3"), ("mint", "4"), ("burn", "2"), ("principal", "0")]
    assert "unsupported_or_malformed_transfer_log" in version["gaps"]
    rpc = RPC({TX: bundle()})
    rpc.overrides["eth_call"] = None
    install(monkeypatch, rpc, policies={"ethereum": policy()})
    unknown = current(await collect())
    assert all(leg["quantity"] is None and leg["raw_units"] is not None for leg in unknown["legs"] if not leg["asset"]["native"])


@pytest.mark.parametrize("mutation,reason", [("balance", "token_balance_log_mismatch"), ("code", "token_historical_semantics_unreviewed"), ("same_block", "token_block_transaction_ambiguity"), ("unreviewed", "token_historical_semantics_unreviewed")])
async def test_token_events_never_force_exact_economics(monkeypatch, mutation, reason):
    rpc = RPC({TX: bundle()})
    if mutation == "balance":
        rpc.overrides["eth_call"] = lambda params: word(6) if params[0]["data"] == "0x313ce567" else word(123)
    if mutation == "code":
        rpc.overrides["eth_getCode"] = "0x6002600055"
    if mutation == "same_block":
        rpc.payloads[INTERNAL_TX] = bundle(INTERNAL_TX, logs=[transfer(2, 5, reference=INTERNAL_TX)])
    install(monkeypatch, rpc, policies={"ethereum": policy()} if mutation != "unreviewed" else {})
    version = current(await collect())
    assert reason in version["gaps"]
    assert all(leg["interpretation"] == "unresolved" for leg in version["legs"] if not leg["asset"]["native"])


@pytest.mark.parametrize("mutation,complete", [("present", True), ("missing_l1", False), ("conflict", False), ("missing_operator", False), ("unknown_fork", False)])
def test_base_fee_components_unknowns_and_conflicts(mutation, complete):
    payload, config = bundle(logs=[]), policy()
    if mutation == "missing_l1":
        payload["receipt"].pop("l1Fee")
    elif mutation == "conflict":
        payload["index_rows"] = [{"l1Fee": "999"}]
    elif mutation == "missing_operator":
        payload["receipt"].pop("operatorFeeScalar")
    elif mutation == "unknown_fork":
        config = {}
    version = history.decode_evm_transaction(TX, payload, chain="base", owner=OWNER, payload_digest="synthetic", policy=config)
    assert (version["fee_status"] == "complete") == complete
    components = {item["component"]: item["raw_units"] for item in version["fee_components"]}
    assert components["execution"] == str(21000 * 10**9)
    assert components == {"execution": str(21000 * 10**9), "l1_data": "100", "operator": "21002"} if complete else None in components.values()


async def test_log_splits_empty_unavailable_and_repeated_streams_preserve_siblings(monkeypatch):
    rpc = RPC()
    rpc.log_span = 3
    rpc.overrides["internal-transactions"] = RuntimeError("synthetic unavailable")
    install(monkeypatch, rpc, policies={"ethereum": policy()})
    archive = await collect(start_block=0, end_block=20)
    assert archive["streams"]["logs_out"]["exhausted"]
    assert archive["streams"]["internal"]["stop_reason"] == "provider_unavailable"
    assert archive["coverage"]["retrieval"] == "partial"
    assert len([leg for leg in current(archive)["legs"] if not leg["asset"]["native"]]) == 2
    assert archive["limits"]["attempts_used"] == len(rpc.calls)
    rpc.overrides["internal-transactions"] = {"items": [], "next_page_params": None}
    assert (await collect(start_block=0, end_block=20, state=archive))["coverage"]["retrieval"] == "complete"
    empty = RPC({})
    install(monkeypatch, empty)
    result = await collect()
    assert result["transactions"] == {} and result["coverage"]["retrieval"] == "complete"
    empty.overrides["transactions"] = {"items": None, "next_page_params": None}
    assert (await collect())["coverage"]["retrieval"] == "partial"
    empty.overrides["transactions"] = lambda params: {"items": [{"hash": TX}], "next_page_params": {"page": 1}}
    assert (await collect())["streams"]["ordinary"]["stop_reason"] == "repeated_cursor"


async def test_attempt_deadline_byte_limits_retain_pending_raw(monkeypatch):
    rpc = RPC()
    install(monkeypatch, rpc)
    archive = await collect(budget=RequestBudget(5))
    assert "request_limit" in archive["gaps"] and len(rpc.calls) == 5
    assert archive["payloads"] and archive["resumable"]
    assert (await collect(state=archive))["transactions"]
    rpc.overrides["debug_traceTransaction"] = OnchainDeadlineExceeded()
    stopped = await collect()
    assert "deadline_exceeded" in stopped["gaps"]
    assert any(payload["method"] == "eth_getTransactionReceipt" for payload in stopped["payloads"].values())
    monkeypatch.setattr(history, "MAX_PAYLOAD_BYTES", 100)
    oversized = await collect()
    assert "payload_byte_limit" in oversized["gaps"] and oversized["unavailable_payloads"]


@pytest.mark.parametrize("chain", ["ethereum", "base", "polygon"])
async def test_finality_null_reorg_and_resume_binding(monkeypatch, chain):
    rpc = RPC({TX: bundle(logs=[])})
    rpc.overrides["eth_getBlockByNumber"] = lambda params: block(5 if params[0] == "finalized" else 20 if params[0] == "latest" else int(params[0], 16))
    install(monkeypatch, rpc, policies={chain: policy()})
    provisional = await collect(chain)
    assert current(provisional)["settlement"] == "provisional"
    rpc.overrides.pop("eth_getBlockByNumber")
    settled = await collect(chain, state=provisional, reobserve=True)
    assert current(settled)["settlement"] == "settled"
    assert len(settled["transactions"][chain + ":" + TX]["versions"]) == 2
    rpc.overrides["eth_getTransactionReceipt"] = None
    absent = await collect(chain, state=settled, reobserve=True)
    assert current(absent)["settlement"] == "settled" and absent["transactions"][chain + ":" + TX]["retrieval_gap"]
    rpc.overrides.pop("eth_getTransactionReceipt")
    rpc.payloads[TX]["receipt"]["blockHash"] = word(9999)
    rpc.payloads[TX]["transaction"]["blockHash"] = word(9999)
    reorg = await collect(chain, state=settled, reobserve=True)
    assert reorg["transactions"][chain + ":" + TX]["canonical_version"] is None
    for changes in ({"end_block": 15}, {"policy": {}}):
        with pytest.raises(ValueError, match="restart"):
            await collect(chain, state=settled, **changes)
    changed = copy.deepcopy(settled)
    changed["decoder_version"] = "unknown"
    with pytest.raises(ValueError, match="restart"):
        await collect(chain, state=changed)


async def test_base_context_and_external_research_ownership(monkeypatch):
    rpc = RPC({TX: bundle(logs=[])})
    install(monkeypatch, rpc)
    without = current(await collect("base"))
    assert without["settlement"] == "provisional" and "base_l1_finality_unavailable" in without["gaps"]
    external = await collect("base", policy=policy(), research=True)
    assert external["inventory"] == {} and external["research_endpoints"] == [OWNER]
    assert all(leg["source_owner"] is None and leg["destination_owner"] is None for leg in current(external)["legs"])


async def test_inclusive_ties_unknown_time_and_uint256(monkeypatch):
    rpc = RPC({TX: bundle(logs=[transfer(0, 2**255)])})
    rpc.overrides["eth_call"] = None
    install(monkeypatch, rpc)
    instant = datetime.fromtimestamp(STAMP + 10, timezone.utc)
    archive = await collect(since=instant, until=instant)
    assert current(archive)["in_requested_window"] is True
    assert any(leg["raw_units"] == str(2**255) for leg in current(archive)["legs"])
    assert json.loads(json.dumps(archive)) == archive
    quantity = history._quantity(2**256 - 1, 18)
    assert quantity is not None and quantity.replace(".", "") == str(2**256 - 1)
    rpc.overrides["eth_getBlockByNumber"] = lambda params: {**block(20 if params[0] in ("latest", "finalized") else int(params[0], 16)), "timestamp": None}
    unknown = current(await collect(since=instant, until=instant))
    assert unknown["in_requested_window"] is None and "unknown_timestamp" in unknown["gaps"]


def test_server_policy_setting_validation():
    assert Settings(_env_file=None, evm_history_policies="").evm_history_policies == {}
    assert Settings(_env_file=None, evm_history_policies=json.dumps({"base": policy()})).evm_history_policies["base"] == policy()
    for invalid in ({"other": {}}, {"base": {"rollup_rpc_url": "file:///secret"}}, {"ethereum": {"standard_tokens": {TOKEN: {"non_proxy": False}}}}):
        with pytest.raises(ValueError):
            Settings(_env_file=None, evm_history_policies=invalid)


async def test_cancellation_limited_bytes_and_zero_leg_payload(monkeypatch):
    import asyncio

    rpc = RPC({TX: bundle(logs=[])})
    rpc.overrides["debug_traceTransaction"] = asyncio.CancelledError()
    install(monkeypatch, rpc)
    cancelled = await collect()
    assert "cancelled" in cancelled["gaps"] and cancelled["resumable"]
    assert any(item["method"] == "eth_getTransactionReceipt" for item in cancelled["payloads"].values())
    count = len(rpc.calls)
    await asyncio.sleep(0)
    assert len(rpc.calls) == count
    rpc.overrides.clear()
    limited = await collect(byte_limit=20_000)
    assert "payload_byte_limit" in limited["gaps"] and limited["payloads"]
    assert len(json.dumps(limited).encode()) < 20_000
    payload = bundle(logs=[])
    payload["receipt"].pop("status")
    result = history.decode_evm_transaction(TX, payload, chain="ethereum", owner=OWNER, payload_digest="synthetic")
    assert result["legs"] == [] and result["attempted"] and result["execution"] == "unknown"


async def test_same_inclusion_conflicting_facts_and_log_disagreement_invalidate(monkeypatch):
    rpc = RPC({TX: bundle(logs=[])})
    install(monkeypatch, rpc)
    original = await collect()
    rpc.payloads[TX]["transaction"]["value"] = "0x2"
    rpc.payloads[TX]["trace"]["value"] = "0x2"
    conflict = await collect(state=original, reobserve=True)
    assert conflict["transactions"]["ethereum:" + TX]["canonical_version"] is None
    payload = bundle()
    payload["discovered_logs"] = [transfer(0, 999)]
    result = history.decode_evm_transaction(TX, payload, chain="ethereum", owner=OWNER, payload_digest="synthetic")
    assert result["settlement"] == "provisional" and "index_receipt_log_disagreement" in result["gaps"]
    payload = bundle(logs=[])
    payload["index_rows"] = [{"value": "2"}]
    result = history.decode_evm_transaction(TX, payload, chain="ethereum", owner=OWNER, payload_digest="synthetic")
    assert result["settlement"] == "provisional" and "index_receipt_transaction_disagreement" in result["gaps"]


async def test_101_logs_segment_losslessly_and_reconciliation_is_qualified(monkeypatch):
    import uuid
    from app.services.onchain_history import project_observations

    rpc = RPC({TX: bundle(logs=[transfer(i, 1) for i in range(101)])})
    install(monkeypatch, rpc, policies={"ethereum": policy()})
    archive = await collect()
    assert len([leg for leg in current(archive)["legs"] if not leg["asset"]["native"]]) == 101
    projected = project_observations(archive, uuid.uuid4())
    assert len(projected) == 2
    assert sum(len(item.legs) for item in projected) == 104
    token = next(row for row in archive["reconciliation"] if not row["asset"]["native"])
    native = next(row for row in archive["reconciliation"] if row["asset"]["native"])
    assert token["status"] == "matched" and token["discrepancy"] == "0.000000"
    assert token["requested_interval_status"] == "unresolved"
    assert native["status"] == "unknown" and native["opening"] is None and native["closing"] is None
    assert native["known_subtotal_raw_units"] == str(-6 * 10**18 - 21000 * 10**9 + 5)


@pytest.mark.parametrize("formula,expected", [("pre_isthmus", 0), ("isthmus", 21002), ("jovian", 21000 * 1_000_000 * 100 + 2)])
def test_operator_fork_formulas_and_blob_fees(formula, expected):
    payload, config = bundle(logs=[]), policy()
    config["operator_fee_forks"][0]["formula"] = formula
    if formula == "pre_isthmus":
        payload["receipt"]["operatorFeeScalar"] = payload["receipt"]["operatorFeeConstant"] = "0x0"
    payload["receipt"]["operatorFee"] = hex(expected)
    result = history.decode_evm_transaction(TX, payload, chain="base", owner=OWNER, payload_digest="synthetic", policy=config)
    assert result["fee_status"] == "complete" and result["fee_components"][-1]["raw_units"] == str(expected)
    payload["transaction"]["type"] = "0x3"
    payload["receipt"].update(blobGasUsed="0x2", blobGasPrice="0x3")
    result = history.decode_evm_transaction(TX, payload, chain="ethereum", owner=OWNER, payload_digest="synthetic")
    assert result["fee_components"][-1]["raw_units"] == "6"


async def test_log_page_limit_resumes_each_pending_range_and_pinned_anchor(monkeypatch):
    rpc = RPC({TX: bundle(logs=[])})
    rpc.log_span = 0
    install(monkeypatch, rpc)
    monkeypatch.setattr(history, "MAX_HISTORY_PAGES", 2)
    first = await collect(end_block=3)
    assert first["streams"]["logs_out"]["stop_reason"] == "page_limit"
    assert first["streams"]["logs_out"]["pending_ranges"]
    result = first
    for _ in range(5):
        result = await collect(end_block=3, state=result)
    assert result["streams"]["logs_out"]["exhausted"] and result["streams"]["logs_in"]["exhausted"]
    rpc.overrides["eth_getBlockByNumber"] = lambda params: {**block(20), "hash": word(999)}
    changed = await collect(end_block=3, state=result)
    assert "anchor_changed_reobserve_required" in changed["gaps"]


@pytest.mark.parametrize("chain", ["ethereum", "base", "polygon"])
async def test_collector_archive_api_activity_bidirectional_and_no_financial_writes(
    monkeypatch, client, auth_headers, session, test_workspace, test_user, chain,
):
    import uuid
    from decimal import Decimal
    from sqlalchemy import func, select
    from app.models.account import Account
    from app.models.asset_group import AssetGroup
    from app.models.asset_transaction import AssetTransaction
    from app.models.bank_connection import BankConnection
    from app.providers.onchain import ACCOUNT_EXTERNAL_ID
    from app.services.connection_service import _wallet_external_id

    rpc = RPC()
    failed_reference = word(333)
    rpc.payloads[failed_reference] = bundle(failed_reference, failed=True, payer=OTHER, logs=[])
    rpc.payloads[failed_reference]["receipt"]["transactionIndex"] = "0x2"
    install(monkeypatch, rpc, policies={chain: policy()})
    connection = BankConnection(id=uuid.uuid4(), workspace_id=test_workspace.id, user_id=test_user.id,
        provider="onchain", external_id="synthetic-evm-connection-" + chain, institution_name="Synthetic EVM",
        credentials={"addresses": [chain + ":" + OWNER]})
    session.add(connection)
    await session.flush()
    account = Account(id=uuid.uuid4(), workspace_id=test_workspace.id, user_id=test_user.id, connection_id=connection.id,
        external_id=ACCOUNT_EXTERNAL_ID, name="Synthetic EVM", type="investment", balance=Decimal("19.25"), currency="USD")
    group = AssetGroup(id=uuid.uuid4(), workspace_id=test_workspace.id, user_id=test_user.id, connection_id=connection.id,
        external_id=_wallet_external_id(connection.external_id, ACCOUNT_EXTERNAL_ID), source="onchain", name="Synthetic EVM")
    session.add_all([account, group])
    await session.commit()
    before = await session.scalar(select(func.count()).select_from(AssetTransaction))
    response = await client.post("/api/onchain/history", headers=auth_headers, json={"connection_id": str(connection.id),
        "chain": chain, "address": OWNER, "ownership_confirmed": True, "start_block": 0, "end_block": 20})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["evidence"]["chain"] == chain
    assert saved["evidence"]["coverage"]["interpretation"] == "complete"
    failed = current(saved["evidence"], failed_reference)
    assert failed["execution"] == "failed" and failed["fee_status"] == "complete"
    assert all(leg["role"] == "network_fee" and leg["source_owner"] is None for leg in failed["legs"])
    assert saved["observations"]
    count = len(rpc.calls)
    source = await client.get(f"/api/onchain/history/{saved['collection_id']}/export", headers=auth_headers)
    assert source.status_code == 200 and source.headers["cache-control"] == "no-store"
    assert json.loads(source.content)["evidence"] == saved["evidence"]
    requests = []
    for direction in ("in", "out"):
        response = await client.get("/api/assets/timeline", headers=auth_headers,
            params={"group_id": str(group.id), "direction": direction})
        assert response.status_code == 200, response.text
        events = response.json()["events"]
        assert events and all(any(leg["direction"] == direction for leg in event["legs"]) for event in events)
        event = events[0]
        leg = next(leg for leg in event["legs"] if leg["direction"] == direction and leg["quantity_role"] != "network_fee" and leg["token_address"] == "native")
        preview = await client.post("/api/onchain/investigation/preview", headers=auth_headers,
            json={"event_id": event["event_id"], "leg_id": leg["leg_id"], "direction": direction})
        assert preview.status_code == 200, preview.text
        assert preview.json()["history_complete"] is False
        requests.append({"event_id": event["event_id"], "leg_id": leg["leg_id"], "direction": direction})
    assert len(rpc.calls) == count  # Opening source, Activity and preview never auto-collects.
    token_event, token_leg = next((event, leg) for event in events for leg in event["legs"] if leg["token_address"] == TOKEN and leg["direction"] == "out")
    requests.append({"event_id": token_event["event_id"], "leg_id": token_leg["leg_id"], "direction": "out"})
    # Explicit continuation can discover earlier funding and later payments at
    # the selected external address, without adding that address to ownership.
    external = "0x" + "d" * 40
    for reference, number, sender, recipient in ((word(444), 8, external, OTHER), (word(555), 12, OTHER, external)):
        payload = bundle(reference, payer=sender, amount=7, logs=[])
        payload["transaction"].update(to=recipient, blockNumber=hex(number), blockHash=block(number)["hash"])
        payload["receipt"].update(blockNumber=hex(number), blockHash=block(number)["hash"], transactionIndex="0x0")
        payload["trace"].update(to=recipient, calls=[])
        log = transfer(0, 10**6, sender, recipient, reference)
        log.update(blockNumber=hex(number), blockHash=block(number)["hash"])
        payload["receipt"]["logs"] = [log]
        rpc.payloads[reference] = payload
    rpc.overrides["transactions"] = {"items": [{"hash": reference, "blockNumber": str(int(payload["receipt"]["blockNumber"], 16))}
                                               for reference, payload in rpc.payloads.items()], "next_page_params": None}
    rpc.overrides["internal-transactions"] = {"items": [{"hash": reference, "traceId": "0", "from": OTHER,
        "to": OWNER, "type": "call", "value": "5", "isError": "0"} for reference in (TX, INTERNAL_TX, failed_reference)], "next_page_params": None}
    for request in requests:
        preview = (await client.post("/api/onchain/investigation/preview", headers=auth_headers, json=request)).json()
        assert preview["frontier"], preview
        frontier = preview["frontier"][0]
        continued = await client.post("/api/onchain/investigation/continue", headers=auth_headers, json={
            **request, "collection_id": preview["collection_id"], "expected_revision": preview["revision"], "frontier_key": frontier["key"]})
        assert continued.status_code == 200, continued.text
        assert continued.json()["evidence"]["limits"]["attempts_used"] > 0
        exported = await client.get(f"/api/onchain/history/{saved['collection_id']}/export", headers=auth_headers)
        assert exported.status_code == 200
        retained = json.loads(exported.content)["evidence"]["investigations"][frontier["key"]]["archive"]
        assert retained["transactions"] and retained["payloads"] and retained["inventory"] == {}
        assert all(leg["source_owner"] is None and leg["destination_owner"] is None
                   for transaction in retained["transactions"].values() for version in transaction["versions"] for leg in version["legs"])
        source = next(source for event in continued.json()["events"] for source in event["sources"] if source["source_id"].startswith("research:"))
        detail = await client.get(source["detail_url"], headers=auth_headers)
        assert detail.status_code == 200, detail.text
    assert await session.scalar(select(func.count()).select_from(AssetTransaction)) == before
    await session.refresh(account)
    assert account.balance == Decimal("19.25")


@pytest.mark.parametrize("chain", ["ethereum", "base", "polygon"])
async def test_failed_collector_retains_receipt_gas_and_no_attempted_principal(monkeypatch, chain):
    rpc = RPC({TX: bundle(failed=True, payer=OTHER)})
    rpc.overrides["internal-transactions"] = {"items": [], "next_page_params": None}
    install(monkeypatch, rpc, policies={chain: policy()})
    result = await collect(chain)
    version = current(result)
    assert version["execution"] == "failed" and version["fee_status"] == "complete"
    assert all(leg["role"] == "network_fee" and leg["source_owner"] is None for leg in version["legs"])
    raw = result["payloads"][version["payload_digest"]]["response"]["result"]
    assert raw["receipt"]["logs"] and raw["transaction"]["value"]
    replay = history.decode_evm_transaction(TX, raw, chain=chain, owner=OWNER, payload_digest=version["payload_digest"])
    assert replay["fee_components"] == version["fee_components"]


def test_indexed_trace_identity_reverted_parent_missing_parent_and_root_overlap():
    payload = bundle(logs=[])
    payload["trace"] = None
    payload["internal_rows"] = [
        {"traceAddress": [], "from": OWNER, "to": OTHER, "type": "call", "value": str(6 * 10**18), "isError": "0"},
        {"traceAddress": [0], "from": OTHER, "to": OWNER, "type": "call", "value": "5", "isError": "0"},
        {"traceAddress": [1], "from": OTHER, "to": OWNER, "type": "call", "value": "7", "isError": "1"},
        {"traceAddress": [1, 0], "from": OTHER, "to": OWNER, "type": "call", "value": "3", "isError": "0"},
        {"traceAddress": [2, 0], "from": OTHER, "to": OWNER, "type": "call", "value": "2", "isError": "0"},
    ]
    version = history.decode_evm_transaction(TX, payload, chain="ethereum", owner=OWNER, payload_digest="synthetic")
    internal = [leg for leg in version["legs"] if ":trace:" in leg["key"]]
    assert [leg["raw_units"] for leg in internal] == ["5", "2"]
    assert "interpretation" not in internal[0] and internal[1]["interpretation"] == "unresolved"
    assert len([leg for leg in version["legs"] if leg["raw_units"] == str(6 * 10**18)]) == 1
    payload["transaction"]["type"] = "0x7e"
    unsupported = history.decode_evm_transaction(TX, payload, chain="base", owner=OWNER, payload_digest="synthetic")
    assert unsupported["legs"] == [] and "unsupported_transaction_type" in unsupported["gaps"]


async def test_interrupted_payload_replay_reuses_success_and_missing_finality_is_not_complete(monkeypatch):
    rpc = RPC({TX: bundle(logs=[])})
    rpc.overrides["debug_traceTransaction"] = OnchainDeadlineExceeded()
    install(monkeypatch, rpc)
    stopped = await collect()
    receipts = sum(name == "eth_getTransactionReceipt" for name, _ in rpc.calls)
    rpc.overrides.clear()
    resumed = await collect(state=stopped)
    assert current(resumed)["settlement"] == "settled"
    assert sum(name == "eth_getTransactionReceipt" for name, _ in rpc.calls) == receipts
    empty = RPC({})
    empty.overrides["eth_getBlockByNumber"] = lambda params: None if params[0] == "finalized" else block(20)
    install(monkeypatch, empty)
    unknown = await collect()
    assert unknown["coverage"]["settlement"] == "partial" and "finalized_anchor_unavailable" in unknown["gaps"]
    rpc = RPC({TX: bundle(logs=[])})
    rpc.overrides["optimism_syncStatus"] = {"finalized_l2": {**block(20), "hash": word(999)}, "finalized_l1": block(200)}
    install(monkeypatch, rpc, policies={"base": policy()})
    assert current(await collect("base"))["settlement"] == "provisional"


async def test_later_discovery_cannot_mutate_retained_payload_digests(monkeypatch):
    rpc = RPC({TX: bundle(logs=[])})
    rpc.overrides["transactions"] = lambda params: {"items": [{"hash": TX, "provider_row_id": str(params)}],
                                                   "next_page_params": None if params else {"page": 1}}
    install(monkeypatch, rpc)
    monkeypatch.setattr(history, "MAX_HISTORY_PAGES", 1)
    first = await collect()
    saved_payloads = copy.deepcopy(first["payloads"])
    resumed = await collect(state=first)
    assert resumed["streams"]["ordinary"]["exhausted"]
    assert all(resumed["payloads"][digest] == raw for digest, raw in saved_payloads.items())
    assert all(history._digest({key: raw[key] for key in ("method", "params", "response")}) == digest
               for digest, raw in resumed["payloads"].items())
    version = current(resumed)
    assert len({leg["key"] for leg in version["legs"]}) == len(version["legs"])


async def test_unavailable_receipt_keeps_successful_sibling_and_only_retries_missing(monkeypatch):
    rpc = RPC()
    rpc.overrides["eth_getTransactionReceipt"] = lambda params: rpc.payloads[TX]["receipt"] if params[0] == TX else None
    install(monkeypatch, rpc, policies={"ethereum": policy()})
    first = await collect()
    assert current(first)["settlement"] == "settled"
    assert first["transactions"]["ethereum:" + INTERNAL_TX]["retrieval_gap"]
    assert INTERNAL_TX in first["streams"]["internal"]["payload_gaps"]
    before = len([params for name, params in rpc.calls if name == "eth_getTransactionReceipt" and params[0] == TX])
    rpc.overrides.clear()
    resumed = await collect(state=first)
    assert current(resumed, INTERNAL_TX)["settlement"] == "settled"
    assert len([params for name, params in rpc.calls if name == "eth_getTransactionReceipt" and params[0] == TX]) == before


def test_deep_call_paths_and_malformed_logs_remain_bounded_and_explicit():
    import uuid
    from app.services.onchain_history import project_observations

    payload = bundle(logs=[])
    parent = payload["trace"]
    for _ in range(100):
        child: dict = {"type": "CALL", "from": OTHER, "to": OWNER, "value": "0x0"}
        parent["calls"] = [child]
        parent = child
    result = history.decode_evm_transaction(TX, payload, chain="ethereum", owner=OWNER, payload_digest="synthetic")
    assert all(len(leg["key"]) <= 255 for leg in result["legs"])
    assert any(len(leg["derivation"]["path"]) > 128 for leg in result["legs"])
    archive = {"chain": "ethereum", "owner": OWNER, "transactions": {"ethereum:" + TX: {
        "signature": TX, "versions": [result], "canonical_version": result["version_id"]}}, "payloads": {}}
    assert sum(len(observation.legs) for observation in project_observations(archive, uuid.uuid4())) == len(result["legs"])
    payload = bundle(logs=[None])
    malformed = history.decode_evm_transaction(TX, payload, chain="ethereum", owner=OWNER, payload_digest="synthetic")
    assert "malformed_receipt_log" in malformed["gaps"]


async def test_unordered_index_rows_and_unknown_time_preserve_inclusive_boundaries(monkeypatch):
    rpc = RPC()
    for part in ("transaction", "receipt"):
        rpc.payloads[INTERNAL_TX][part].update(blockNumber="0x9", blockHash=block(9)["hash"])
    rpc.payloads[INTERNAL_TX]["receipt"]["transactionIndex"] = "0x0"
    rpc.overrides["eth_getBlockByNumber"] = lambda params: {
        **block(20 if params[0] in ("latest", "finalized") else int(params[0], 16)),
        "transactions": [INTERNAL_TX] if params[0] == "0x9" else [TX],
    }
    rpc.overrides["transactions"] = {"items": [
        {"hash": TX, "blockNumber": "10", "timeStamp": str(STAMP + 10)},
        {"hash": INTERNAL_TX, "blockNumber": "9", "timeStamp": str(STAMP + 9)},
        {"hash": TX, "blockNumber": "10", "timeStamp": str(STAMP + 10)},
        {"hash": INTERNAL_TX, "blockNumber": "9"},
    ], "next_page_params": None}
    rpc.overrides["internal-transactions"] = {"items": [
        {"hash": reference, "blockNumber": str(number), "timeStamp": str(STAMP + number),
         "traceId": "0", "from": OTHER, "to": OWNER, "type": "call", "value": "5", "isError": "0"}
        for reference, number in ((TX, 10), (INTERNAL_TX, 9))], "next_page_params": None}
    install(monkeypatch, rpc, policies={"ethereum": policy()})
    result = await collect(since=datetime.fromtimestamp(STAMP + 9, timezone.utc), until=datetime.fromtimestamp(STAMP + 10, timezone.utc))
    assert set(result["transactions"]) == {"ethereum:" + TX, "ethereum:" + INTERNAL_TX}
    assert current(result)["in_requested_window"] is True and current(result, INTERNAL_TX)["in_requested_window"] is True
    assert result["streams"]["ordinary"]["unknown_timestamps"] == 1
    assert result["coverage"]["retrieval"] == "complete"


def test_unknown_indexed_ancestor_blocks_descendants_but_known_enrichment_is_compatible():
    payload = bundle(logs=[])
    payload["trace"] = None
    parent = {"traceAddress": [0], "from": OTHER, "to": OWNER, "value": "3", "type": "call"}
    child = {"traceAddress": [0, 0], "from": OWNER, "to": OTHER, "value": "2", "type": "call", "isError": "0"}
    payload["internal_rows"] = [parent, child]
    version = history.decode_evm_transaction(TX, payload, chain="ethereum", owner=OWNER, payload_digest="synthetic")
    internal = [leg for leg in version["legs"] if ":trace:" in leg["key"]]
    assert len(internal) == 2
    assert all(leg["interpretation"] == "unresolved" and leg["settlement"] == "provisional" for leg in internal)
    parent["isError"] = "0"
    known = history.decode_evm_transaction(TX, payload, chain="ethereum", owner=OWNER, payload_digest="synthetic")
    assert all("interpretation" not in leg and leg["settlement"] == "settled" for leg in known["legs"] if ":trace:" in leg["key"])
    before, after = version["indexed_internal_fingerprints"], known["indexed_internal_fingerprints"]
    assert all(after[key] == value for key, value in before.items())


async def test_indexed_internal_conflicts_invalidate_overlapping_collections_and_reobserve(monkeypatch):
    from app.services.onchain_history import _transaction_conflicts, qualify_archive

    rpc = RPC({TX: bundle(logs=[])})
    rpc.overrides["debug_traceTransaction"] = None
    rpc.overrides["internal-transactions"] = {"items": [{"transaction_hash": TX, "traceAddress": [0],
        "from": OTHER, "to": OWNER, "value": "5", "type": "call", "isError": "0"}], "next_page_params": None}
    install(monkeypatch, rpc)
    first = await collect()
    rpc.overrides["internal-transactions"]["items"][0]["value"] = "999"
    second = await collect()
    assert _transaction_conflicts([(1, first), (2, second)])
    assert qualify_archive(first, [(2, second)])["transactions"]["ethereum:" + TX]["canonical_version"] is None
    reobserved = await collect(state=first, reobserve=True)
    assert reobserved["transactions"]["ethereum:" + TX]["canonical_version"] is None
    rpc.overrides["internal-transactions"]["items"][0]["value"] = "5"
    rpc.overrides["internal-transactions"]["items"].append({"transaction_hash": TX, "traceAddress": [1],
        "from": OTHER, "to": OWNER, "value": "8", "type": "call", "isError": "0"})
    enriched = await collect()
    assert not _transaction_conflicts([(1, first), (3, enriched)])


@pytest.mark.parametrize("chain", ["ethereum", "base", "polygon"])
@pytest.mark.parametrize("keep_unaffected", [False, True])
async def test_cross_archive_conflict_recomputes_supported_subtotals(monkeypatch, chain, keep_unaffected):
    from decimal import Decimal
    from app.services.onchain_history import qualify_archive

    payloads = {TX: bundle()}
    if keep_unaffected:
        payloads[INTERNAL_TX] = bundle(INTERNAL_TX, logs=[], payer=OTHER, amount=0)
    rpc = RPC(payloads)
    rpc.overrides["eth_getBalance"] = None
    install(monkeypatch, rpc, policies={chain: policy()})
    first = await collect(chain)
    original = copy.deepcopy(first)
    rpc.payloads[TX] = bundle(amount=7 * 10**18)
    second = await collect(chain)
    qualified = qualify_archive(first, [("peer", second)])
    assert qualified["transactions"][chain + ":" + TX]["canonical_version"] is None
    native = next(row for row in qualified["reconciliation"] if row["asset"]["native"])
    assert native["known_subtotal_raw_units"] == ("5" if keep_unaffected else "0")
    assert Decimal(native["settled_change"]) == (Decimal("0.000000000000000005") if keep_unaffected else 0)
    assert native["opening"] is native["closing"] is None
    for row in qualified["reconciliation"]:
        assert row["expected_closing"] is row["discrepancy"] is None
        assert row["status"] == "unknown"
        assert "cross_collection_conflict" in row["reasons"]
        if not row["asset"]["native"]:
            assert row["known_subtotal_raw_units"] == "0"
    assert first == original
    assert qualified["payloads"] == original["payloads"]


@pytest.mark.parametrize("chain", ["ethereum", "base", "polygon"])
async def test_owned_transfer_accepts_qualified_chain_archive_and_rejects_conflict(
    monkeypatch, client, auth_headers, session, test_workspace, test_user, chain,
):
    from decimal import Decimal
    from app.services import owned_transfer_service as owned

    await test_collector_archive_api_activity_bidirectional_and_no_financial_writes(
        monkeypatch, client, auth_headers, session, test_workspace, test_user, chain)
    state = await owned.prepare_replay(session, test_workspace.id)
    selected = [identifier for identifier, row in state["legs"].items()
                if state["observations"][row.observation_id].payload.get("source") == "onchain_history"
                and row.payload.get("quantity_role") == "principal" and Decimal(row.payload.get("quantity") or "0") > 0]
    assert selected and all("archive_principal_unqualified" not in owned._movement_reasons(state, identifier) for identifier in selected)
    import uuid
    root = next(iter(state["archives"].values()))
    peer_payload = copy.deepcopy(root.payload)
    peer_payload.update(owner=OTHER, investigations={})
    for transaction in peer_payload["transactions"].values():
        for version in transaction["versions"]:
            version["evidence_fingerprint"] = history._digest([version["evidence_fingerprint"], "synthetic-conflicting-peer"])
    peer_id = uuid.uuid4()
    state["archives"][peer_id] = SimpleNamespace(id=peer_id, connection_id=uuid.uuid4(),
        request={"chain": chain, "address": OTHER}, payload=peer_payload)
    state["archive_qualification"].clear()
    state["reason_cache"].clear()
    assert all("archive_principal_unqualified" in owned._movement_reasons(state, identifier) for identifier in selected)
    del state["archives"][peer_id]
    # Qualification is read-only and must still honor a retained reorg verdict.
    state["archive_qualification"].clear()
    state["reason_cache"].clear()
    for archive in state["archives"].values():
        archive.payload = copy.deepcopy(archive.payload)
        for transaction in archive.payload["transactions"].values():
            transaction["revision_status"] = "conflicting_or_reorganized"
            transaction["canonical_version"] = None
    assert all("archive_principal_unqualified" in owned._movement_reasons(state, identifier) for identifier in selected)
    await session.rollback()  # The synthetic negative state is not persisted.
