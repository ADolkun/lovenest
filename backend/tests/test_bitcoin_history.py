"""Synthetic Esplora responses only; no chain access or financial writes."""

import asyncio
import copy
import hashlib
import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast

import pytest

from app.providers import bitcoin_history as history
from app.providers.onchain_transport import RequestBudget

OWNER = "owned-A"
STAMP = 1_800_000_000


def output(value, address=OWNER, script="51"):
    item = {"value": value, "scriptpubkey": script, "scriptpubkey_type": "unknown"}
    if address is not None:
        item["scriptpubkey_address"] = address
    return item


def spend(previous="funding-A", index=0, value=1000, address=OWNER, script="51"):
    return {"txid": previous, "vout": index, "is_coinbase": False, "sequence": 4294967293,
            "scriptsig": "", "witness": ["aa"], "prevout": output(value, address, script)}


def transaction(reference="send-A", *, inputs=None, outputs=None, height=95, stamp=STAMP, confirmed=True):
    status = {"confirmed": confirmed}
    if confirmed:
        status.update(block_height=height, block_hash=f"block-{height}", block_time=stamp)
    return {"txid": reference, "vin": [spend()] if inputs is None else inputs,
            "vout": [output(990)] if outputs is None else outputs, "status": status,
            "fee": 10, "wtxid": reference + "-synthetic-witness"}


def decode(payload, *, supplied=(), height=100, active=True):
    return history.decode_bitcoin_transaction(payload["txid"], payload, payload_digest="synthetic-source", owner=OWNER,
        inventory=history._inventory(OWNER, list(supplied)), anchor={"height": height, "blockhash": "anchor-A", "active": True},
        block_status={"in_best_chain": active})


class Esplora:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []
        self.tip = 100
        self.orphaned = set()

    async def __call__(self, client, method, url, **kwargs):
        assert method == "GET"
        assert kwargs["deadline"] is not None
        assert kwargs["concurrency"] <= 5
        kwargs["budget"].consume()
        path = url.removeprefix(kwargs["endpoint"])
        self.calls.append(path)
        if path in self.routes:
            result = self.routes[path]
        elif path == "/blocks":
            result = [{"height": self.tip, "id": f"block-{self.tip}", "timestamp": STAMP + 100}]
        elif path.startswith("/block/"):
            blockhash = path.split("/")[2]
            if path.endswith("/status"):
                result = {"in_best_chain": blockhash not in self.orphaned}
            else:
                result = {"id": blockhash, "height": int(blockhash.removeprefix("block-")), "timestamp": STAMP}
        elif "/txs/" in path:
            result = []
        else:
            result = None
        if callable(result):
            result = cast(Callable[[], Any], result)()
            if asyncio.iscoroutine(result):
                result = await result
        if isinstance(result, BaseException):
            raise result
        return copy.deepcopy(result)


def install(monkeypatch, rpc):
    monkeypatch.setattr(history, "request_json", rpc)
    monkeypatch.setattr(history.onchain, "rpc_url", lambda chain: "https://synthetic.invalid/credential-not-for-export")


async def collect(**kwargs):
    return await history.collect_bitcoin_history(OWNER, source_identity="bitcoin:synthetic-config", **kwargs)


def current(archive, reference="send-A"):
    tx = archive["transactions"]["bitcoin:" + reference]
    return next(v for v in tx["versions"] if v["version_id"] == tx["canonical_version"])


def test_exact_owned_net_fee_and_script_inventory():
    reviewed = [{"address": "change-A", "owner": OWNER, "reviewed": True}]
    version = decode(transaction(outputs=[output(600, "external-A", "52"), output(390, "change-A", "53"), output(0, None, "6a")]), supplied=reviewed)
    assert version["fee"]["raw_units"] == "10"
    assert version["fee"]["payer_owner"] == OWNER
    assert version["owned_quantity"]["delta_raw_units"] == "-610"
    assert version["owned_quantity"]["fee_already_included"] is True
    assert [leg["raw_units"] for leg in version["legs"]] == ["1000", "600", "390", "0", "10"]
    assert version["legs"][-1]["non_additive"] is True
    assert version["outputs"][1]["change"] == "reviewed_owned"
    assert version["outputs"][0]["change"] == "unresolved"
    assert version["legs"][3]["destination"] == "scripthash:" + hashlib.sha256(b"\x6a").digest()[::-1].hex()
    assert version["outputs"][2]["maturity_eligible"] is False
    assert version["settlement"] == "settled"
    assert version["owned_quantity"]["spendable_quantity"] is None


def test_mixed_inputs_never_allocate_fee_or_external_outputs():
    version = decode(transaction(inputs=[spend(), spend("external-funding", value=2000, address="external-A", script="52")],
                                 outputs=[output(500), output(2490, "fresh-unknown", "53")]))
    assert version["fee"]["raw_units"] == "10"
    assert version["fee"]["payer_owner"] is None
    assert version["owned_quantity"]["delta_raw_units"] == "-500"
    assert version["ownership_allocation"] == "mixed_or_unresolved_inputs"
    assert version["legs"][1]["source_owner"] is None
    assert version["outputs"][1]["owned"] is False
    assert version["outputs"][1]["change"] == "unresolved"


def test_missing_prevout_and_malformed_values_are_unknown_not_zero():
    payload = transaction()
    payload["vin"][0]["prevout"] = None
    version = decode(payload)
    assert version["fee"]["raw_units"] is None
    assert version["owned_quantity"]["delta_raw_units"] is None
    assert {"missing_prevout_value", "missing_prevout_script", "transaction_fee_unresolved"} <= set(version["gaps"])
    payload = transaction(outputs=[output(True)])
    assert decode(payload)["outputs"][0]["raw_units"] is None
    assert decode(payload)["fee"]["raw_units"] is None


def test_coinbase_six_confirmations_are_settled_but_not_mature():
    payload = transaction(inputs=[{"is_coinbase": True, "txid": "0" * 64, "vout": 4294967295, "scriptsig": "aa"}], outputs=[output(5000)])
    six = decode(payload)
    assert six["settlement"] == "settled"
    assert six["coinbase_mature"] is False
    assert six["owned_quantity"]["delta_raw_units"] == "5000"
    assert six["owned_quantity"]["maturity_eligible_output_raw_units"] == "0"
    assert six["fee"]["status"] == "not_applicable_coinbase"
    assert [leg["role"] for leg in six["legs"]] == ["issuance"]
    assert decode(payload, height=193)["coinbase_mature"] is False
    mature = decode(payload, height=194)
    assert mature["coinbase_mature"] is True
    assert mature["owned_quantity"]["maturity_eligible_output_raw_units"] == "5000"


def test_six_active_confirmations_and_wire_witness_identity():
    assert decode(transaction(height=96))["settlement"] == "provisional"
    assert decode(transaction(), active=None)["settlement"] == "provisional"
    assert decode(transaction(), active=False)["confirmation_status"] == "reorged"
    assert decode(transaction(confirmed=False))["confirmation_status"] == "mempool"
    assert decode(transaction(confirmed=False))["owned_quantity"]["maturity_eligible_output_raw_units"] is None
    # Independently serialize one entirely invented witness transaction.
    head = b"\x02\x00\x00\x00"
    body = b"\x01" + bytes(32) + b"\x00\x00\x00\x00" + b"\x00" + b"\xff\xff\xff\xff" + b"\x01" + (990).to_bytes(8, "little") + b"\x01\x51"
    tail = bytes(4)
    def hashed(raw):
        return hashlib.sha256(hashlib.sha256(raw).digest()).digest()[::-1].hex()
    txid = hashed(head + body + tail)
    wtxid = hashed(head + b"\x00\x01" + body + b"\x01\x01\xaa" + tail)
    payload = transaction(txid, inputs=[spend("0" * 64)])
    payload.update(version=2, locktime=0)
    payload.pop("wtxid")
    payload["vin"][0]["sequence"] = 4294967295
    version = decode(payload)
    assert version["witness_hash"] == wtxid != txid
    assert version["settlement"] == "settled"
    payload["txid"] = "incorrect"
    assert decode(payload)["settlement"] == "provisional"
    assert "transaction_wire_identity_mismatch" in decode(payload)["gaps"]


async def test_paging_and_capped_mempool_are_separate_and_replay_is_local(monkeypatch):
    first = [transaction(f"confirmed-{i}", inputs=[spend(f"fund-{i}")]) for i in range(25)]
    second = [transaction("confirmed-tail", inputs=[spend("fund-tail")])]
    pending = [transaction(f"mempool-{i}", inputs=[spend(f"unconfirmed-fund-{i}")], confirmed=False) for i in range(50)]
    rpc = Esplora({f"/address/{OWNER}/txs/chain": first, f"/address/{OWNER}/txs/chain/confirmed-24": second,
                   f"/address/{OWNER}/txs/mempool": pending})
    install(monkeypatch, rpc)
    archive = await collect()
    assert len(archive["transactions"]) == 76
    assert archive["limits"]["attempts_used"] == len(rpc.calls) == 8
    assert archive["streams"][OWNER + ":confirmed"]["exhausted"] is True
    assert archive["streams"][OWNER + ":mempool"]["stop_reason"] == "mempool_cap_nonpageable"
    assert archive["coverage"]["retrieval"] == "partial"
    assert archive["coverage"]["inventory"] == "unknown"
    assert "credential-not-for-export" not in json.dumps(archive)
    assert len([path for path in rpc.calls if path.endswith("/txs/mempool")]) == 1
    rpc.routes[f"/address/{OWNER}/txs/mempool"] = pending[:1]
    complete = await collect(state=json.loads(json.dumps(archive)))
    assert complete["streams"][OWNER + ":mempool"]["exhausted"] is True
    before = list(rpc.calls)
    replayed = await collect(state=complete)
    assert rpc.calls == before
    assert replayed["transactions"] == complete["transactions"]


async def test_prevout_lookup_preserves_source_and_resume_does_not_duplicate(monkeypatch):
    payload = transaction()
    payload["vin"][0]["prevout"] = None
    rpc = Esplora({f"/address/{OWNER}/txs/chain": [payload]})
    install(monkeypatch, rpc)
    partial = await collect()
    assert current(partial)["fee"]["raw_units"] is None
    assert partial["resumable"] is True
    rpc.routes["/tx/funding-A"] = transaction("funding-A", outputs=[output(1000)])
    resumed = await collect(state=partial)
    assert current(resumed)["fee"]["raw_units"] == "10"
    assert current(resumed)["inputs"][0]["prevout_source"] in resumed["payloads"]
    assert len(resumed["transactions"]) == 1
    assert len(resumed["transactions"]["bitcoin:send-A"]["versions"]) == 2
    assert resumed["streams"][OWNER + ":confirmed"]["exhausted"] is True


@pytest.mark.parametrize("recovered", [False, True])
@pytest.mark.parametrize("field,changed", [("value", 2000), ("scriptpubkey", "52"), ("scriptpubkey_address", "external-A")])
async def test_known_prevout_conflicts_qualify_local_and_peer_archives(monkeypatch, recovered, field, changed):
    from app.services import onchain_history as shared

    payload = transaction()
    parent = transaction("funding-A", outputs=[output(1000)])
    if recovered:
        payload["vin"][0]["prevout"] = None
    rpc = Esplora({f"/address/{OWNER}/txs/chain": [payload], "/tx/funding-A": parent})
    install(monkeypatch, rpc)
    first = await collect()
    original = copy.deepcopy(first)
    if recovered:
        parent["vout"][0][field] = changed
    else:
        payload["vin"][0]["prevout"][field] = changed
    if field == "value":
        payload["fee"] = 1010
    rpc.routes["/tx/send-A"] = payload
    second = await collect()
    assert current(first)["evidence_fingerprint"] == current(second)["evidence_fingerprint"]
    assert current(first)["settlement"] == current(second)["settlement"] == "settled"
    if recovered:
        assert current(first)["prevout_refs"]["funding-A:0"]["output"] != current(second)["prevout_refs"]["funding-A:0"]["output"]
    resumed = await collect(state=first, reobserve=True)
    retained = resumed["transactions"]["bitcoin:send-A"]
    assert retained["canonical_version"] is None
    assert retained["revision_status"] == "conflicting_or_reorganized"
    assert retained["conflicting_prevout_fields"] == [f"bitcoin:prevout:funding-A:0:{field}"]
    assert len(retained["versions"]) == 2
    assert resumed["coverage"]["settlement"] == "partial"
    assert shared.project_observations(resumed, "local") == []
    assert ("bitcoin", OWNER, "bitcoin:send-A") in shared._transaction_conflicts([(1, first), (2, second)])
    for archive, peer in ((first, second), (second, first)):
        qualified = shared.qualify_archive(archive, [("peer", peer)])
        assert qualified["transactions"]["bitcoin:send-A"]["canonical_version"] is None
        assert shared.project_observations(qualified, "peer-qualified") == []
    # A nested research overlap has the same qualification as a root archive.
    nested = {"chain": "bitcoin", "owner": OWNER, "investigations": {"overlap": {"archive": second}}}
    assert shared.qualify_archive(first, [("nested", nested)])["transactions"]["bitcoin:send-A"]["canonical_version"] is None
    assert first == original


@pytest.mark.parametrize("field", ["value", "scriptpubkey", "scriptpubkey_address"])
async def test_missing_prevout_field_enriches_without_conflict_or_false_zero(monkeypatch, field):
    from app.services import onchain_history as shared

    complete = transaction(inputs=[spend(value=0)], outputs=[output(0)])
    complete["fee"] = 0
    missing = copy.deepcopy(complete)
    missing["vin"][0]["prevout"].pop(field)
    rpc = Esplora({f"/address/{OWNER}/txs/chain": [missing]})
    install(monkeypatch, rpc)
    first = await collect()
    if field == "value":
        assert current(first)["inputs"][0]["raw_units"] is None
        assert current(first)["fee"]["raw_units"] is None
    rpc.routes[f"/address/{OWNER}/txs/chain"] = [complete]
    rpc.routes["/tx/send-A"] = complete
    second = await collect()
    resumed = await collect(state=first, reobserve=True)
    assert current(resumed)["owned_quantity"]["delta_raw_units"] == "0"
    assert current(resumed)["fee"]["raw_units"] == "0"
    assert resumed["transactions"]["bitcoin:send-A"]["revision_status"] == "current"
    assert shared._transaction_conflicts([(1, first), (2, second)]) == {}


def test_prevout_fingerprint_field_identity_normalization_and_recovered_facts():
    version = {"inputs": [
        {"outpoint": "funding-A:0", "raw_units": "0", "scriptpubkey": "AA", "address": OWNER},
        {"outpoint": "funding-A:1", "raw_units": True, "scriptpubkey": "invalid", "address": None},
    ], "prevout_refs": {"funding-A:1": {"output": output(2000, "external-A", "")}}}
    facts = history.bitcoin_prevout_fingerprints(version)
    assert facts == {
        "bitcoin:prevout:funding-A:0:value": history._digest(0),
        "bitcoin:prevout:funding-A:0:scriptpubkey": history._digest("aa"),
        "bitcoin:prevout:funding-A:0:scriptpubkey_address": history._digest(OWNER),
        "bitcoin:prevout:funding-A:1:value": history._digest(2000),
        "bitcoin:prevout:funding-A:1:scriptpubkey": history._digest(""),
        "bitcoin:prevout:funding-A:1:scriptpubkey_address": history._digest("external-A"),
    }
    version.pop("prevout_refs")
    assert set(history.bitcoin_prevout_fingerprints(version)) == {key for key in facts if "funding-A:0:" in key}


async def test_contradictory_embedded_and_recovered_prevout_never_qualifies(monkeypatch):
    from app.services import onchain_history as shared

    payload = transaction()
    payload["vin"][0]["prevout"].pop("scriptpubkey")
    rpc = Esplora({f"/address/{OWNER}/txs/chain": [payload],
                   "/tx/funding-A": transaction("funding-A", outputs=[output(2000)])})
    install(monkeypatch, rpc)
    archive = await collect()
    retained = archive["transactions"]["bitcoin:send-A"]
    assert retained["canonical_version"] is None
    assert "prevout_conflict" in retained["versions"][0]["gaps"]
    # Older archives could retain this invalid observation as canonical.
    retained["canonical_version"] = retained["versions"][0]["version_id"]
    retained["revision_status"] = "current"
    assert shared.qualify_archive(archive, [])["transactions"]["bitcoin:send-A"]["canonical_version"] is None


async def test_reviewed_scripts_use_scripthash_and_never_add_external_inventory(monkeypatch):
    supplied = [{"scriptpubkey": "52", "owner": OWNER, "reviewed": True}]
    script_hash = hashlib.sha256(bytes.fromhex("52")).digest()[::-1].hex()
    payload = transaction(outputs=[output(990, None, "52")])
    rpc = Esplora({f"/scripthash/{script_hash}/txs/chain": [payload]})
    install(monkeypatch, rpc)
    archive = await collect(supplied_accounts=supplied)
    assert set(archive["inventory"]) == {OWNER, "script:52"}
    assert current(archive)["owned_quantity"]["delta_raw_units"] == "-10"
    assert current(archive)["legs"][1]["destination_owner"] == OWNER
    with pytest.raises(ValueError, match="reviewed"):
        await collect(supplied_accounts=[{"scriptpubkey": "52", "owner": OWNER, "reviewed": False}])


async def test_rbf_competitors_finalization_reorg_and_null_do_not_invent_reversal(monkeypatch):
    a, b = transaction("rbf-A", confirmed=False), transaction("rbf-B", confirmed=False)
    rpc = Esplora({f"/address/{OWNER}/txs/mempool": [a, b]})
    install(monkeypatch, rpc)
    archive = await collect()
    assert current(archive, "rbf-A")["settlement"] == "provisional"
    assert archive["transactions"]["bitcoin:rbf-A"]["conflicts"][0]["transactions"] == ["bitcoin:rbf-B"]
    rpc.routes["/tx/rbf-A"] = a
    rpc.routes["/tx/rbf-B"] = transaction("rbf-B")
    rpc.routes[f"/address/{OWNER}/txs/mempool"] = []
    settled = await collect(state=archive, reobserve=True)
    assert current(settled, "rbf-B")["settlement"] == "settled"
    assert settled["transactions"]["bitcoin:rbf-A"]["canonical_version"] is None
    assert len(settled["transactions"]["bitcoin:rbf-B"]["versions"]) == 2
    rpc.routes["/tx/rbf-B"] = None
    null = await collect(state=settled, reobserve=True)
    assert current(null, "rbf-B")["settlement"] == "settled"
    assert null["transactions"]["bitcoin:rbf-B"]["retrieval_gap"] == "payload_unavailable"
    rpc.routes["/tx/rbf-B"] = transaction("rbf-B")
    rpc.orphaned.add("block-95")
    orphaned = await collect(state=null, reobserve=True)
    assert orphaned["transactions"]["bitcoin:rbf-B"]["canonical_version"] is None
    assert orphaned["transactions"]["bitcoin:rbf-B"]["versions"][-1]["confirmation_status"] == "reorged"


async def test_page_cap_request_budget_and_inclusive_time_bounds_keep_tail(monkeypatch):
    page = [transaction(f"row-{i}", inputs=[spend(f"fund-{i}")]) for i in range(25)]
    rpc = Esplora({f"/address/{OWNER}/txs/chain": page})
    install(monkeypatch, rpc)
    monkeypatch.setattr(history, "MAX_HISTORY_PAGES", 1)
    bound = datetime.fromtimestamp(STAMP, timezone.utc)
    archive = await collect(since=bound, until=bound)
    assert len(archive["transactions"]) == 25
    assert all(current(archive, f"row-{i}")["in_requested_window"] is True for i in range(25))
    assert archive["streams"][OWNER + ":confirmed"]["stop_reason"] == "page_limit"
    budget = RequestBudget(max_attempts=4)
    partial = await collect(budget=budget)
    assert budget.attempts == 4
    assert "request_limit" in partial["gaps"]
    assert len(partial["streams"][OWNER + ":confirmed"]["pending"]) == 25
    assert partial["payloads"]
    resumed = await collect(state=partial)
    assert len(resumed["transactions"]) == 25


@pytest.mark.parametrize("page,reason", [(None, "malformed_page"), ([None], "malformed_page"), (RuntimeError("private endpoint"), "provider_unavailable")])
async def test_unreadable_stream_is_not_empty_complete(monkeypatch, page, reason):
    rpc = Esplora({f"/address/{OWNER}/txs/chain": page})
    install(monkeypatch, rpc)
    archive = await collect()
    assert archive["streams"][OWNER + ":confirmed"]["exhausted"] is False
    assert archive["streams"][OWNER + ":confirmed"]["stop_reason"] == reason
    assert archive["streams"][OWNER + ":mempool"]["exhausted"] is True
    assert archive["coverage"]["retrieval"] == "partial"
    assert "private endpoint" not in json.dumps(archive)


async def test_zero_legs_and_more_than_100_legs_survive_raw_roundtrip(monkeypatch):
    malformed = transaction("zero-leg", outputs=[])
    large = transaction("large", outputs=[output(0) for _ in range(101)])
    rpc = Esplora({f"/address/{OWNER}/txs/chain": [malformed, large]})
    install(monkeypatch, rpc)
    archive = await collect()
    assert current(archive, "zero-leg")["legs"] == []
    assert len(current(archive, "large")["legs"]) == 103
    assert json.loads(json.dumps(archive))["transactions"] == archive["transactions"]


async def test_byte_cap_and_cancel_retain_success_and_drain_reads(monkeypatch):
    rpc = Esplora({f"/address/{OWNER}/txs/chain": [transaction()]})
    install(monkeypatch, rpc)
    monkeypatch.setattr(history, "MAX_PAYLOAD_BYTES", 10)
    capped = await collect()
    assert "payload_byte_limit" in capped["gaps"]
    assert capped["unavailable_payloads"][0]["reason"] == "payload_byte_limit"
    monkeypatch.setattr(history, "MAX_PAYLOAD_BYTES", 8 * 1024 * 1024)
    drained = []
    active_timeout = []
    original_timeout = asyncio.timeout_at
    def capture_timeout(deadline):
        timeout = original_timeout(deadline)
        active_timeout.append(timeout)
        return timeout
    monkeypatch.setattr(history.asyncio, "timeout_at", capture_timeout)
    async def blocked():
        try:
            active_timeout[0].reschedule(asyncio.get_running_loop().time())
            await asyncio.sleep(10)
        finally:
            drained.append(True)
    rpc.routes[f"/address/{OWNER}/txs/mempool"] = blocked
    # The confirmed stream completes before the timeout cancels mempool.
    partial = await collect()
    assert drained == [True]
    assert current(partial)["fee"]["raw_units"] == "10"
    assert "deadline_exceeded" in partial["gaps"]


async def test_continuation_forward_backward_and_mixed_input_boundary(monkeypatch):
    root = transaction("root-A", outputs=[output(1000)])
    target = transaction("mixed-A", inputs=[spend("root-A"), spend("pool-A", value=5000, address="pool")], outputs=[output(5990, "external-B", "52")])
    rpc = Esplora({"/tx/root-A": root, "/tx/mixed-A": target, "/tx/root-A/outspend/0": {"spent": True, "txid": "mixed-A", "vin": 0},
                   "/tx/mixed-A/outspend/0": {"spent": False}, "/tx/funding-A": transaction("funding-A", inputs=[{"is_coinbase": True}], outputs=[output(1000)])})
    install(monkeypatch, rpc)
    forward = await history.continue_bitcoin_history(OWNER, outpoint="root-A:0", direction="out", source_identity="bitcoin:synthetic-config")
    assert {"root-A", "mixed-A"} == set(forward["research"]["nodes"])
    assert forward["research"]["edges"][0]["allocation"] == "unresolved_after_mixed_inputs"
    assert set(forward["inventory"]) == {OWNER}
    assert "external-B" not in forward["inventory"]
    backward = await history.continue_bitcoin_history(OWNER, outpoint="root-A:0", direction="in", source_identity="bitcoin:synthetic-config")
    assert backward["research"]["edges"][0]["source_outpoint"] == "funding-A:0"
    assert "funding-A" in backward["research"]["nodes"]
    before = len(rpc.calls)
    with pytest.raises(ValueError, match="selection changed"):
        await history.continue_bitcoin_history(OWNER, outpoint="root-A:0", direction="in", source_identity="bitcoin:synthetic-config", state=forward)
    assert len(rpc.calls) == before


async def test_changed_source_scope_and_inventory_require_restart(monkeypatch):
    rpc = Esplora()
    install(monkeypatch, rpc)
    archive = await collect()
    assert archive["coverage"]["retrieval"] == "complete"
    before = len(rpc.calls)
    with pytest.raises(ValueError, match="restart"):
        await history.collect_bitcoin_history(OWNER, source_identity="changed", state=archive)
    with pytest.raises(ValueError, match="restart"):
        await collect(state=archive, supplied_accounts=[{"address": "change", "owner": OWNER, "reviewed": True}])
    monkeypatch.setattr(history.onchain, "rpc_url", lambda chain: "https://changed.invalid")
    with pytest.raises(ValueError, match="restart"):
        await collect(state=archive)
    assert len(rpc.calls) == before


async def test_changed_block_reobservation_invalidates_old_settlement(monkeypatch):
    rpc = Esplora({f"/address/{OWNER}/txs/chain": [transaction()]})
    install(monkeypatch, rpc)
    archive = await collect()
    rpc.routes["/tx/send-A"] = transaction(height=100)
    changed = await collect(state=archive, reobserve=True)
    assert changed["transactions"]["bitcoin:send-A"]["canonical_version"] is None
    assert changed["transactions"]["bitcoin:send-A"]["versions"][-1]["settlement"] == "provisional"
    assert changed["reconciliation"][0]["known_settled_change_raw_units"] == "0"


async def test_finality_advances_only_on_explicit_reobserve(monkeypatch):
    payload = transaction(height=100)
    rpc = Esplora({f"/address/{OWNER}/txs/chain": [payload], "/tx/send-A": payload})
    install(monkeypatch, rpc)
    archive = await collect()
    assert current(archive)["settlement"] == "provisional"
    rpc.tip = 105
    resumed = await collect(state=archive)
    assert current(resumed)["settlement"] == "provisional"
    finalized = await collect(state=resumed, reobserve=True)
    assert current(finalized)["settlement"] == "settled"
    assert len(finalized["transactions"]["bitcoin:send-A"]["versions"]) == 2
    assert finalized["anchors"][0]["height"] == 100
    assert current(finalized)["original_timestamp"] == STAMP


def test_duplicate_inputs_cannot_supply_settled_double_debit():
    version = decode(transaction(inputs=[spend(), spend()], outputs=[output(1990)]))
    assert version["fee"]["raw_units"] is None
    assert version["owned_quantity"]["delta_raw_units"] is None
    assert version["settlement"] == "provisional"
    assert all(leg["settlement"] == "provisional" for leg in version["legs"])


async def test_continuation_branch_hop_bounds_and_replay(monkeypatch):
    target = transaction("many", inputs=[spend("root")], outputs=[output(1, f"external-{i}", "52") for i in range(6)])
    rpc = Esplora({"/tx/root": transaction("root"), "/tx/root/outspend/0": {"spent": True, "txid": "many", "vin": 0}, "/tx/many": target})
    install(monkeypatch, rpc)
    monkeypatch.setattr(history, "MAX_RESEARCH_HOPS", 1)
    result = await history.continue_bitcoin_history(OWNER, outpoint="root:0", direction="out", source_identity="bitcoin:synthetic-config")
    assert len(result["research"]["edges"]) == 1
    assert {"hop_limit", "branch_limit"} <= {item["reason"] for item in result["research"]["boundaries"]}
    assert not any(path.startswith("/tx/many/outspend") for path in rpc.calls)
    assert result["coverage"]["retrieval"] == "partial"
    before = list(rpc.calls)
    replay = await history.continue_bitcoin_history(OWNER, outpoint="root:0", direction="out", source_identity="bitcoin:synthetic-config", state=result)
    assert rpc.calls == before
    assert replay["research"] == result["research"]


async def test_outspend_must_reference_the_selected_input(monkeypatch):
    rpc = Esplora({"/tx/root": transaction("root"), "/tx/root/outspend/0": {"spent": True, "txid": "wrong", "vin": 0}, "/tx/wrong": transaction("wrong")})
    install(monkeypatch, rpc)
    result = await history.continue_bitcoin_history(OWNER, outpoint="root:0", direction="out", source_identity="bitcoin:synthetic-config")
    assert result["research"]["edges"] == []
    assert result["research"]["frontier"]
    assert result["research"]["boundaries"][-1]["reason"] == "outspend_identity_unresolved"


async def test_shared_byte_allowance_stops_before_reads_and_preserves_metadata(monkeypatch):
    rpc = Esplora({f"/address/{OWNER}/txs/chain": [transaction()]})
    install(monkeypatch, rpc)
    result = await collect(byte_limit=18000)
    assert len(json.dumps(result).encode()) < 18000
    assert "payload_byte_limit" in result["gaps"] or "decoded_projection_byte_limit" in result["gaps"]
    assert result["limits"]["total_bytes"] == 18000
    assert result["reconciliation"][0]["opening_quantity"] is None
    assert result["reconciliation"][0]["closing_quantity"] is None


async def test_repeated_page_retains_its_rows_and_unavailable_anchor_is_provisional(monkeypatch):
    page = [transaction(f"tx-{i}", inputs=[spend(f"parent-{i}")]) for i in range(25)]
    rpc = Esplora({f"/address/{OWNER}/txs/chain": page, f"/address/{OWNER}/txs/chain/tx-24": page})
    install(monkeypatch, rpc)
    repeated = await collect()
    assert len(repeated["transactions"]) == 25
    assert repeated["streams"][OWNER + ":confirmed"]["stop_reason"] == "repeated_cursor"
    rpc.routes = {f"/address/{OWNER}/txs/chain": [transaction()], "/blocks": None}
    unavailable = await collect()
    assert current(unavailable)["settlement"] == "provisional"
    assert "anchor_unavailable" in unavailable["gaps"]
    assert unavailable["resumable"] is True


async def test_generic_external_research_never_asserts_owner(monkeypatch):
    rpc = Esplora({f"/address/{OWNER}/txs/chain": [transaction()]})
    install(monkeypatch, rpc)
    result = await collect(research=True)
    assert result["inventory"] == {}
    assert all(leg["source_owner"] is None and leg["destination_owner"] is None for leg in current(result)["legs"])
    assert current(result)["owned_quantity"]["known_delta_raw_units"] == "0"


async def test_successful_prevout_reads_survive_later_budget_stop(monkeypatch):
    payload = transaction()
    payload["vin"][0]["prevout"] = None
    rpc = Esplora({f"/address/{OWNER}/txs/chain": [payload], "/tx/funding-A": transaction("funding-A", outputs=[output(1000)])})
    install(monkeypatch, rpc)
    partial = await collect(budget=RequestBudget(max_attempts=4))
    assert rpc.calls.count("/tx/funding-A") == 1
    assert partial["transactions"]["bitcoin:send-A"]["pending_payload"]
    resumed = await collect(state=partial)
    assert current(resumed)["fee"]["raw_units"] == "10"
    assert rpc.calls.count("/tx/funding-A") == 1


async def test_projection_keeps_long_addressless_scripts_and_does_not_charge_fee_twice(monkeypatch):
    from app.services.onchain_history import project_observations
    script = "51" * 300
    supplied = [{"scriptpubkey": script, "owner": OWNER, "reviewed": True}]
    rpc = Esplora({f"/address/{OWNER}/txs/chain": [transaction(outputs=[output(600, "external", "52"), output(390, None, script)])]})
    install(monkeypatch, rpc)
    archive = await collect(supplied_accounts=supplied)
    observations = project_observations(archive, "synthetic-collection")
    legs = [leg for observation in observations for leg in observation.legs]
    assert len(legs) == 2
    assert all(leg.classification != "fee" for leg in legs)
    assert {leg.direction for leg in legs} == {"in", "out"}
    assert sum(leg.quantity if leg.direction == "in" else -leg.quantity for leg in legs) == Decimal("-0.00000610")
    assert current(archive)["outputs"][1]["scriptpubkey"] == script
    assert current(archive)["legs"][2]["derivation"]["outpoint"] == "send-A:1"


async def test_two_claimed_settled_competitors_never_both_count(monkeypatch):
    rpc = Esplora({f"/address/{OWNER}/txs/chain": [transaction("competitor-A"), transaction("competitor-B")]})
    install(monkeypatch, rpc)
    archive = await collect()
    assert all(tx["canonical_version"] is None for tx in archive["transactions"].values())
    assert archive["coverage"]["settlement"] == "partial"
    assert archive["reconciliation"][0]["known_settled_change_raw_units"] == "0"


async def test_missing_mempool_transaction_is_not_replacement_or_drop(monkeypatch):
    rpc = Esplora({f"/address/{OWNER}/txs/mempool": [transaction(confirmed=False)]})
    install(monkeypatch, rpc)
    archive = await collect()
    rpc.routes[f"/address/{OWNER}/txs/mempool"] = []
    missing = await collect(state=archive, reobserve=True)
    observed = current(missing)
    assert observed["confirmation_status"] == "mempool"
    assert observed["replacement_status"] == observed["drop_status"] == "unknown"
    assert observed["replaceability_signal"] == "signaled"
    assert missing["transactions"]["bitcoin:send-A"]["retrieval_gap"] == "payload_unavailable"


async def test_expired_shared_deadline_starts_no_read(monkeypatch):
    rpc = Esplora()
    install(monkeypatch, rpc)
    archive = await collect(deadline=time.monotonic() - 1)
    assert rpc.calls == []
    assert "deadline_exceeded" in archive["gaps"]
    assert archive["resumable"] is True


async def test_bitcoin_http_archive_export_replay_and_unchanged_financial_rows(
    client, auth_headers, viewer_auth_headers, session, test_workspace, test_user, monkeypatch,
):
    from sqlalchemy import func, select

    from app.models.asset import Asset
    from app.models.asset_transaction import AssetTransaction
    from app.models.investment_evidence import InvestmentObservation
    from tests.test_onchain_history_api import connected_context

    # Generate a checksum-valid address from invented bytes, never a chain record.
    payload = b"\x00" + hashlib.sha256(b"synthetic-bitcoin-history-owner").digest()[:20]
    number = int.from_bytes(payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4], "big")
    alphabet, encoded = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded
    address = "1" + encoded
    connection, account, group = await connected_context(session, test_workspace.id, test_user.id)
    connection.credentials = {"addresses": ["bitcoin:" + address]}
    await session.commit()
    payload = transaction(inputs=[spend(address=address)], outputs=[output(600, "external", "52"), output(390, None, "53")])
    rpc = Esplora({f"/address/{address}/txs/chain": [payload]})
    install(monkeypatch, rpc)
    request = {"connection_id": str(connection.id), "chain": "bitcoin", "address": address, "ownership_confirmed": True,
               "supplied_accounts": [{"scriptpubkey": "53", "owner": address, "reviewed": True}]}
    denied = await client.post("/api/onchain/history", headers=viewer_auth_headers, json=request)
    assert denied.status_code == 403
    assert rpc.calls == []
    response = await client.post("/api/onchain/history", headers=auth_headers, json=request)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert current(saved["evidence"])["owned_quantity"]["delta_raw_units"] == "-610"
    assert len(saved["observations"]) == 1
    assert all(leg["classification"] != "fee" for leg in saved["observations"][0]["legs"])
    prior_calls = list(rpc.calls)
    assert len(prior_calls) == 9  # Anchor, two declared streams, one block check, final anchor check.
    detail = await client.get(f"/api/onchain/history/{saved['collection_id']}", headers=auth_headers)
    exported = await client.get(f"/api/onchain/history/{saved['collection_id']}/export", headers=auth_headers)
    assert detail.status_code == exported.status_code == 200
    assert exported.json()["evidence"] == saved["evidence"]
    assert rpc.calls == prior_calls
    timeline = await client.get("/api/assets/timeline", headers=auth_headers,
                                params={"group_id": str(group.id)})
    assert timeline.status_code == 200, timeline.text
    events = timeline.json()["events"]
    assert len(events) == 1
    event = events[0]
    assert {leg["key"] for leg in event["legs"]} == {
        "bitcoin:send-A:input:0:funding-A:0", "bitcoin:send-A:output:0", "bitcoin:send-A:output:1", "bitcoin:send-A:fee",
    }
    assert [leg["raw_units"] for leg in event["legs"] if leg["key"].endswith(":fee")] == ["10"]
    source = next(item for item in event["sources"] if item["payload_digest"])
    source_detail = await client.get(source["detail_url"], headers=auth_headers,
                                    params={"group_id": str(group.id), "event_id": event["event_id"]})
    assert source_detail.status_code == 200, source_detail.text
    retained_payload = json.loads(source_detail.json()["raw_payload"]["json"])
    assert retained_payload["response"][0]["txid"] == "send-A"
    retained_transaction = json.loads(source_detail.json()["transaction"]["json"])
    assert retained_transaction["versions"][0]["inputs"][0]["outpoint"] == "funding-A:0"
    assert rpc.calls == prior_calls
    replayed = await client.post("/api/onchain/history", headers=auth_headers, json={
        **saved["request"], "collection_id": saved["collection_id"], "expected_revision": saved["revision"],
    })
    assert replayed.status_code == 200, replayed.text
    assert rpc.calls == prior_calls
    rpc.routes.update({
        "/tx/send-A": payload,
        "/tx/send-A/outspend/0": {"spent": True, "txid": "external-spend", "vin": 0},
        "/tx/external-spend": transaction("external-spend", inputs=[spend("send-A", value=600, address="external", script="52")], outputs=[output(590, "external-recipient", "54")]),
        "/tx/external-spend/outspend/0": {"spent": False},
    })
    outgoing = next(leg for leg in event["legs"] if leg["key"] == "bitcoin:send-A:output:0")
    selection = {"event_id": event["event_id"], "leg_id": outgoing["leg_id"], "direction": "out", "max_hops": 3}
    preview = await client.post("/api/onchain/investigation/preview", headers=auth_headers, json=selection)
    assert preview.status_code == 200, preview.text
    assert preview.json()["frontier"], preview.text
    assert rpc.calls == prior_calls
    investigation = await client.post("/api/onchain/investigation/continue", headers=auth_headers, json={
        **selection, "collection_id": saved["collection_id"], "expected_revision": preview.json()["revision"],
        "frontier_key": preview.json()["frontier"][0]["key"],
    })
    assert investigation.status_code == 200, investigation.text
    assert any(leg["transaction_ref"] == "external-spend" for found in investigation.json()["events"] for leg in found["legs"])
    rpc.routes["/tx/funding-A"] = transaction("funding-A", inputs=[spend("earlier-unknown", value=1010, address="external-origin")], outputs=[output(1000, address)])
    incoming = next(leg for leg in event["legs"] if leg["key"].startswith("bitcoin:send-A:input:"))
    selection = {"event_id": event["event_id"], "leg_id": incoming["leg_id"], "direction": "in", "max_hops": 3}
    before_preview = list(rpc.calls)
    backward = await client.post("/api/onchain/investigation/preview", headers=auth_headers, json=selection)
    assert backward.status_code == 200, backward.text
    assert backward.json()["frontier"], backward.text
    assert rpc.calls == before_preview
    frontier = next(item for item in backward.json()["frontier"] if item["leg_id"] == incoming["leg_id"])
    continued = await client.post("/api/onchain/investigation/continue", headers=auth_headers, json={
        **selection, "collection_id": saved["collection_id"], "expected_revision": backward.json()["revision"], "frontier_key": frontier["key"],
    })
    assert continued.status_code == 200, continued.text
    assert any(leg["transaction_ref"] == "funding-A" for found in continued.json()["events"] for leg in found["legs"])
    assert await session.scalar(select(func.count()).select_from(InvestmentObservation)) == 1
    assert await session.scalar(select(func.count()).select_from(AssetTransaction)) == 0
    assert await session.scalar(select(func.count()).select_from(Asset)) == 0
    await session.refresh(account)
    assert account.balance == Decimal("19.25")


async def test_nested_research_remaining_allowance_is_enforced_and_bound(monkeypatch):
    rpc = Esplora({"/tx/root": transaction("root")})
    install(monkeypatch, rpc)
    result = await history.continue_bitcoin_history(OWNER, outpoint="root:0", direction="out", source_identity="bitcoin:synthetic-config", max_hops=0, max_nodes=1)
    assert result["research"]["nodes"] == ["root"]
    assert result["research"]["edges"] == []
    assert "/tx/root/outspend/0" not in rpc.calls
    with pytest.raises(ValueError, match="limits changed"):
        await history.continue_bitcoin_history(OWNER, outpoint="root:0", direction="out", source_identity="bitcoin:synthetic-config", state=result, max_hops=1, max_nodes=1)
    rpc.calls.clear()
    empty = await history.continue_bitcoin_history(OWNER, outpoint="root:0", direction="out", source_identity="bitcoin:synthetic-config", max_nodes=0)
    assert rpc.calls == []
    assert empty["research"]["boundaries"][-1]["reason"] == "node_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize("research", [False, True])
async def test_external_cancellation_returns_retained_siblings_and_drains(monkeypatch, research):
    entered, drained = asyncio.Event(), asyncio.Event()
    async def blocked():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            drained.set()
    rpc = Esplora({f"/address/{OWNER}/txs/chain": [transaction()], f"/address/{OWNER}/txs/mempool": blocked,
                   "/tx/root": transaction("root"), "/tx/root/outspend/0": blocked})
    install(monkeypatch, rpc)
    existing = asyncio.all_tasks()
    pending = (history.continue_bitcoin_history(OWNER, source_identity="bitcoin:synthetic-config", outpoint="root:0", direction="out")
               if research else collect())
    task = asyncio.create_task(pending)
    await entered.wait()
    task.cancel()
    archive = await task
    assert drained.is_set()
    assert "cancelled" in archive["gaps"]
    assert archive["payloads"] and archive["transactions"]
    assert current(archive, "root" if research else "send-A")["settlement"] == "settled"
    assert archive["resumable"] is True
    assert not asyncio.all_tasks() - existing
