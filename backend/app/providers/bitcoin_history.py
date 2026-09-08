"""Bounded Esplora evidence for declared Bitcoin addresses and scripts.

Inputs debit their spent outputs; outputs credit their scripts. The fee is
already in that difference and never supplies another wallet debit.
Source contract: https://github.com/Blockstream/esplora/blob/master/API.md
Maturity: https://github.com/bitcoin/bitcoin/blob/v30.0/src/consensus/tx_verify.cpp
"""

from __future__ import annotations

import asyncio
from asyncio import CancelledError
import copy
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from app.providers import onchain
from app.providers.onchain_reads import MAX_HISTORY_PAGES, interruption_code
from app.providers.onchain_transport import (
    OnchainDeadlineExceeded,
    OnchainRequestLimitExceeded,
    RequestBudget,
    request_json,
)
from app.providers.solana_history import (
    ARCHIVE_VERSION,
    MAX_ACCOUNTS,
    MAX_ARCHIVE_BYTES,
    MAX_ATTEMPTS,
    MAX_PAYLOAD_BYTES,
    MAX_TOTAL_BYTES,
    TIME_BUDGET_SECONDS,
    _digest,
    _integer,
    _quantity,
    _timestamp,
)

DECODER_VERSION = "bitcoin-1"
MAX_SATOSHIS = 21_000_000 * 100_000_000
CONFIRMATIONS = 6
COINBASE_MATURITY = 100
MEMPOOL_LIMIT = 50
MAX_RESEARCH_HOPS = 6
MAX_RESEARCH_BRANCHES = 5
MAX_RESEARCH_NODES = 24
ASSET = {"chain": "bitcoin", "native": True, "mint": None, "token_program": None}


def _satoshis(value):
    value = _integer(value)
    return value if value is not None and value <= MAX_SATOSHIS else None


def _hex(value):
    if not isinstance(value, str) or len(value) % 2:
        return None
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return None
    return raw.hex() if len(raw) * 2 == len(value) else None


def _endpoint(output):
    address = output.get("scriptpubkey_address")
    if isinstance(address, str) and address:
        return address
    script = _hex(output.get("scriptpubkey"))
    return "scripthash:" + hashlib.sha256(bytes.fromhex(script)).digest()[::-1].hex() if script is not None else None


def _outpoint(txid, index):
    return f"{txid}:{index}"


def _inventory(owner, supplied):
    inventory = {owner: {"address": owner, "kind": "owner", "ownership": [{
        "owner": owner, "source": "workspace_connection_ownership_assertion", "period": "declared_scope",
    }]}}
    for entry in supplied:
        if entry.get("reviewed") is not True or entry.get("owner") != owner:
            raise ValueError("Bitcoin inventory requires a reviewed owner assertion")
        address, script = entry.get("address"), entry.get("scriptpubkey")
        if address is not None and (not isinstance(address, str) or not address):
            raise ValueError("Invalid reviewed Bitcoin address")
        if script is not None and _hex(script) is None:
            raise ValueError("Invalid reviewed Bitcoin script")
        if not address and script is None:
            raise ValueError("A reviewed Bitcoin address or script is required")
        key = address or "script:" + script.lower()
        inventory[key] = {"address": address, "scriptpubkey": script.lower() if script is not None else None,
                          "kind": "reviewed", "ownership": [{"owner": owner, "source": "user_review",
                          "period": "declared_scope", "provenance": entry.get("provenance")}]}
    return inventory


def _owned(output, inventory):
    address, script = output.get("scriptpubkey_address"), _hex(output.get("scriptpubkey"))
    return any((address is not None and address == entry.get("address")) or
               (script is not None and script == entry.get("scriptpubkey")) for entry in inventory.values())


def _compact_size(value):
    if value < 253:
        return bytes([value])
    size, marker = (2, 253) if value <= 65535 else (4, 254) if value <= 2**32 - 1 else (8, 255)
    return bytes([marker]) + value.to_bytes(size, "little")


def _transaction_hashes(payload):
    """Reconstruct Esplora wire fields; never call txid a witness hash."""
    def number(value, size, signed=False):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Missing wire integer")
        return value.to_bytes(size, "little", signed=signed)

    def blob(value):
        value = _hex(value)
        if value is None:
            raise ValueError("Missing wire bytes")
        raw = bytes.fromhex(value)
        return _compact_size(len(raw)) + raw

    try:
        inputs, outputs = payload["vin"], payload["vout"]
        if not isinstance(inputs, list) or not inputs or not isinstance(outputs, list) or not outputs:
            return None, None
        encoded_inputs, witnesses = [], []
        for item in inputs:
            previous = bytes.fromhex(item["txid"])
            if len(previous) != 32:
                return None, None
            encoded_inputs.append(previous[::-1] + number(item["vout"], 4) + blob(item["scriptsig"]) + number(item["sequence"], 4))
            witness = item.get("witness", [])
            if not isinstance(witness, list):
                return None, None
            witnesses.append(_compact_size(len(witness)) + b"".join(blob(part) for part in witness))
        encoded_outputs = [number(item["value"], 8) + blob(item["scriptpubkey"]) for item in outputs]
        head, tail = number(payload["version"], 4, signed=True), number(payload["locktime"], 4)
        body = (_compact_size(len(inputs)) + b"".join(encoded_inputs) +
                _compact_size(len(outputs)) + b"".join(encoded_outputs))
        wire = head + b"\x00\x01" + body + b"".join(witnesses) + tail if any(item.get("witness") for item in inputs) else head + body + tail
        def hashed(raw):
            return hashlib.sha256(hashlib.sha256(raw).digest()).digest()[::-1].hex()
        return hashed(head + body + tail), hashed(wire)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return None, None


def decode_bitcoin_transaction(
    txid: str, payload: dict, *, payload_digest: str, owner: str,
    inventory: dict | None = None, anchor: dict | None = None,
    block_status: dict | None = None, prevouts: dict | None = None,
) -> dict:
    """Pure replay; raw outpoints and scripts stay inspectable without owners."""
    inventory = inventory if inventory is not None else _inventory(owner, [])
    anchor, prevouts = anchor or {}, prevouts or {}
    gaps = []
    status = payload.get("status")
    status = status if isinstance(status, dict) else {}
    height, tip = _integer(status.get("block_height")), _integer(anchor.get("height"))
    active = block_status.get("in_best_chain") if isinstance(block_status, dict) else None
    confirmations = tip - height + 1 if tip is not None and height is not None and height <= tip else None
    confirmed = status.get("confirmed") is True
    inclusion_valid = confirmed and isinstance(status.get("block_hash"), str) and bool(status["block_hash"])
    settled = inclusion_valid and active is True and anchor.get("active") is True and confirmations is not None and confirmations >= CONFIRMATIONS
    if active is False and confirmed:
        state = "reorged"
    elif confirmed:
        state = "confirmed"
    elif status.get("confirmed") is False:
        state = "mempool"
    else:
        state = "unknown"
        gaps.append("transaction_status_unavailable")
    if confirmed and (active is None or anchor.get("active") is not True):
        gaps.append("active_chain_unavailable")
    if confirmed and (height is None or tip is None or height > tip or not inclusion_valid):
        gaps.append("inclusion_anchor_unresolved")
    stamp = _timestamp(status.get("block_time"))
    if stamp is None:
        gaps.append("unknown_timestamp")
    result = {
        "version_id": _digest({"payload": payload, "prevouts": prevouts, "anchor": anchor, "block_status": block_status}),
        "payload_digest": payload_digest, "decoder_version": DECODER_VERSION,
        "evidence_fingerprint": _digest({key: value for key, value in payload.items() if key not in {"status", "fee"}} | {
            "vin": [{key: value for key, value in item.items() if key != "prevout"} if isinstance(item, dict) else item
                    for item in payload.get("vin", [])] if isinstance(payload.get("vin"), list) else payload.get("vin"),
        }),
        "block_height": height, "slot": height, "block_hash": status.get("block_hash"),
        "block_time": stamp, "original_timestamp": status.get("block_time"),
        "time_precision": "second" if stamp else "unknown", "confirmations": confirmations,
        "confirmation_status": state, "execution": "success" if state == "confirmed" else "unknown",
        "source_status": copy.deepcopy(status), "active_chain": active,
        "settlement": "settled" if settled else "provisional",
        "anchor": copy.deepcopy(anchor), "legs": [], "inputs": [], "outputs": [],
        "observations": [], "ownership": [], "relationships": [], "gaps": gaps,
        "drop_status": "unknown", "replacement_status": "unknown",
    }
    if payload.get("txid") != txid:
        gaps.append("transaction_identity_mismatch")
        result["settlement"] = "provisional"
        return result
    computed_txid, witness_hash = _transaction_hashes(payload)
    supplied_witness = payload.get("wtxid")
    result["witness_hash"] = witness_hash or (supplied_witness if isinstance(supplied_witness, str) else None)
    result["witness_hash_source"] = "reconstructed_wire" if witness_hash else "provider" if supplied_witness else "unavailable"
    if result["witness_hash"] is None:
        gaps.append("witness_hash_unavailable")
    if computed_txid is not None and computed_txid != txid:
        gaps.append("transaction_wire_identity_mismatch")
        result["settlement"] = "provisional"
    if supplied_witness and witness_hash and supplied_witness != witness_hash:
        gaps.append("witness_hash_conflict")
        result["settlement"] = "provisional"
    inputs, outputs = payload.get("vin"), payload.get("vout")
    if not isinstance(inputs, list) or not inputs or not isinstance(outputs, list) or not outputs:
        gaps.append("malformed_transaction_structure")
        result["settlement"] = "provisional"
        return result
    coinbase = len(inputs) == 1 and isinstance(inputs[0], dict) and inputs[0].get("is_coinbase") is True
    result["coinbase"] = coinbase
    sequences = [_integer(item.get("sequence")) if isinstance(item, dict) else None for item in inputs]
    result["replaceability_signal"] = "signaled" if any(value is not None and value < 4294967294 for value in sequences) else "not_signaled" if all(value is not None and value <= 2**32 - 1 for value in sequences) else "unknown"
    result["coinbase_mature"] = (confirmations >= COINBASE_MATURITY if active is True and confirmations is not None else None) if coinbase else None
    seen_outpoints = set()
    for index, original in enumerate(inputs):
        if not isinstance(original, dict):
            gaps.append("malformed_input")
            result["inputs"].append({"index": index, "raw_units": None, "owned": False})
            continue
        previous_id, output_index = original.get("txid"), _integer(original.get("vout"))
        outpoint = _outpoint(previous_id, output_index) if isinstance(previous_id, str) and previous_id and output_index is not None and output_index <= 2**32 - 1 else None
        resolved = original.get("prevout")
        resolved = resolved if isinstance(resolved, dict) else {}
        lookup = prevouts.get(outpoint, {})
        if lookup:
            candidate = lookup.get("output", {})
            for field in ("value", "scriptpubkey", "scriptpubkey_address"):
                if resolved.get(field) is not None and candidate.get(field) is not None and resolved[field] != candidate[field]:
                    gaps.append("prevout_conflict")
            resolved = {**candidate, **{key: value for key, value in resolved.items() if value is not None}}
        units = _satoshis(resolved.get("value"))
        owned = _owned(resolved, inventory)
        entry = {"index": index, "outpoint": outpoint, "previous_txid": previous_id, "vout": output_index,
                 "raw_units": str(units) if units is not None else None, "owned": owned,
                 "address": resolved.get("scriptpubkey_address"), "scriptpubkey": resolved.get("scriptpubkey"),
                 "scriptsig": original.get("scriptsig"), "witness": copy.deepcopy(original.get("witness")),
                 "sequence": original.get("sequence"), "is_coinbase": original.get("is_coinbase"),
                 "prevout_source": lookup.get("payload_digest", payload_digest)}
        result["inputs"].append(entry)
        if coinbase:
            continue
        if original.get("is_coinbase") is True:
            gaps.append("malformed_coinbase")
        if outpoint is None or outpoint in seen_outpoints:
            gaps.append("invalid_or_duplicate_outpoint")
        seen_outpoints.add(outpoint)
        if units is None:
            gaps.append("missing_prevout_value")
        if _hex(resolved.get("scriptpubkey")) is None:
            gaps.append("missing_prevout_script")
        result["legs"].append({
            "key": f"bitcoin:{txid}:input:{index}:{outpoint}", "role": "utxo_input", "asset": dict(ASSET),
            "source": _endpoint(resolved), "destination": None, "source_owner": owner if owned else None,
            "destination_owner": None, "raw_units": entry["raw_units"], "quantity": _quantity(units, 8),
            "decimals": 8, "outpoint": outpoint, "input_index": index, "scriptpubkey": resolved.get("scriptpubkey"),
            "settlement": result["settlement"], "derivation": {"payload_digest": payload_digest, "path": f"vin.{index}", "prevout_source": entry["prevout_source"], "spent_outpoint": outpoint},
        })
    for index, original in enumerate(outputs):
        original = original if isinstance(original, dict) else {}
        units, script = _satoshis(original.get("value")), _hex(original.get("scriptpubkey"))
        owned = _owned(original, inventory)
        if units is None:
            gaps.append("missing_output_value")
        if script is None:
            gaps.append("missing_output_script")
        unspendable = script is not None and script.startswith("6a")
        spendable = False if unspendable or (coinbase and result["coinbase_mature"] is False) else None if not settled or script is None or (coinbase and result["coinbase_mature"] is None) else True
        entry = {"index": index, "outpoint": _outpoint(txid, index), "raw_units": str(units) if units is not None else None,
                 "address": original.get("scriptpubkey_address"), "scriptpubkey": original.get("scriptpubkey"),
                 "scriptpubkey_type": original.get("scriptpubkey_type"), "scriptpubkey_asm": original.get("scriptpubkey_asm"),
                 "owned": owned, "maturity_eligible": spendable, "unspent": None,
                 "change": "reviewed_owned" if owned and any(item["owned"] for item in result["inputs"]) else "unresolved" if any(item["owned"] for item in result["inputs"]) else "unknown"}
        result["outputs"].append(entry)
        result["legs"].append({
            "key": f"bitcoin:{txid}:output:{index}", "role": "issuance" if coinbase else "utxo_output", "asset": dict(ASSET),
            "source": None, "destination": _endpoint(original), "source_owner": None, "destination_owner": owner if owned else None,
            "raw_units": entry["raw_units"], "quantity": _quantity(units, 8), "decimals": 8,
            "outpoint": entry["outpoint"], "output_index": index, "scriptpubkey": original.get("scriptpubkey"),
            "settlement": result["settlement"], "maturity_eligible": spendable,
            "derivation": {"payload_digest": payload_digest, "path": f"vout.{index}", "outpoint": entry["outpoint"]},
        })
    input_values = [int(item["raw_units"]) for item in result["inputs"] if item["raw_units"] is not None]
    output_values = [int(item["raw_units"]) for item in result["outputs"] if item["raw_units"] is not None]
    complete_values = len(input_values) == len(inputs) and len(output_values) == len(outputs)
    total_in, total_out = sum(input_values), sum(output_values)
    fee = total_in - total_out if not coinbase and complete_values and 0 <= total_out <= total_in <= MAX_SATOSHIS else None
    if not coinbase and complete_values and fee is None:
        gaps.append("invalid_input_output_totals")
    if "prevout_conflict" in gaps or "invalid_or_duplicate_outpoint" in gaps:
        fee = None
    reported_fee = _satoshis(payload.get("fee"))
    if not coinbase and reported_fee is not None and fee is not None and reported_fee != fee:
        gaps.append("provider_fee_disagreement")
    all_owned = not coinbase and all(item["owned"] for item in result["inputs"])
    any_owned = any(item["owned"] for item in result["inputs"])
    result["ownership_allocation"] = "known_owned_inputs" if all_owned else "mixed_or_unresolved_inputs" if any_owned else "external_or_unresolved"
    result["fee"] = {"raw_units": str(fee) if fee is not None else None, "quantity": _quantity(fee, 8),
                     "reported_raw_units": str(reported_fee) if reported_fee is not None else None,
                     "payer_owner": owner if all_owned else None, "non_additive": True,
                     "status": "not_applicable_coinbase" if coinbase else "known" if fee is not None else "unresolved"}
    if not coinbase:
        result["legs"].append({"key": f"bitcoin:{txid}:fee", "role": "network_fee", "asset": dict(ASSET),
            "source": None, "destination": None, "source_owner": owner if all_owned else None, "destination_owner": None,
            "raw_units": result["fee"]["raw_units"], "quantity": _quantity(fee, 8), "decimals": 8,
            "non_additive": True, "settlement": result["settlement"],
            "derivation": {"payload_digest": payload_digest, "path": "sum(vin.prevout.value)-sum(vout.value)"}})
        if fee is None:
            gaps.append("transaction_fee_unresolved")
    owned_inputs = [item for item in result["inputs"] if item["owned"] and not coinbase]
    owned_outputs = [item for item in result["outputs"] if item["owned"]]
    known_delta = sum(int(item["raw_units"]) for item in owned_outputs if item["raw_units"] is not None) - sum(int(item["raw_units"]) for item in owned_inputs if item["raw_units"] is not None)
    quantity_complete = not any(item["raw_units"] is None for item in owned_inputs + owned_outputs) and not any(gap in gaps for gap in ("missing_prevout_script", "missing_output_script", "invalid_or_duplicate_outpoint", "prevout_conflict", "malformed_input"))
    result["owned_quantity"] = {"known_delta_raw_units": str(known_delta),
        "delta_raw_units": str(known_delta) if quantity_complete else None,
        "status": "known_declared_inventory" if quantity_complete else "unresolved",
        "scope": "declared_addresses_and_scripts", "whole_wallet_inventory": "unknown", "fee_already_included": True,
        "maturity_eligible_output_raw_units": str(sum(int(item["raw_units"]) for item in owned_outputs if item["maturity_eligible"] is True and item["raw_units"] is not None)),
        "spendable_quantity": None}
    result["owned_quantity"]["known_maturity_eligible_output_raw_units"] = result["owned_quantity"]["maturity_eligible_output_raw_units"]
    if any(item["maturity_eligible"] is None or item["raw_units"] is None for item in owned_outputs):
        result["owned_quantity"]["maturity_eligible_output_raw_units"] = None
    invalid = {"transaction_wire_identity_mismatch", "witness_hash_conflict", "prevout_conflict",
               "invalid_or_duplicate_outpoint", "malformed_coinbase", "malformed_input", "invalid_input_output_totals"}
    if invalid.intersection(gaps):
        result["settlement"] = "provisional"
        result["owned_quantity"]["delta_raw_units"] = None
        result["owned_quantity"]["status"] = "unresolved"
        result["owned_quantity"]["maturity_eligible_output_raw_units"] = None
        result["owned_quantity"]["known_maturity_eligible_output_raw_units"] = "0"
        for leg in result["legs"]:
            leg["settlement"] = "provisional"
            leg["interpretation"] = "unresolved"
        for item in result["outputs"]:
            if item["maturity_eligible"] is True:
                item["maturity_eligible"] = None
    result["gaps"] = sorted(set(gaps))
    return result


def bitcoin_source_identity():
    return "bitcoin:" + _digest({"endpoint": onchain.rpc_url(onchain.CHAINS["bitcoin"]), "decoder": DECODER_VERSION})


def bitcoin_prevout_fingerprints(version):
    """Compare known spent-output fields, including retained lookup enrichment."""
    facts = {}
    for item in version.get("inputs", []):
        outpoint = item.get("outpoint")
        if not outpoint or item.get("is_coinbase"):
            continue
        lookup = version.get("prevout_refs", {}).get(outpoint, {}).get("output", {})
        address = next((value for value in (item.get("address"), lookup.get("scriptpubkey_address"))
                        if isinstance(value, str) and value), None)
        fields = {
            "value": (_satoshis(item.get("raw_units")), _satoshis(lookup.get("value"))),
            "scriptpubkey": (_hex(item.get("scriptpubkey")), _hex(lookup.get("scriptpubkey"))),
            "scriptpubkey_address": (address,),
        }
        for field, values in fields.items():
            value = next((value for value in values if value is not None), None)
            if value is not None:
                facts[f"bitcoin:prevout:{outpoint}:{field}"] = _digest(value)
    return facts


class _Stopped(Exception):
    pass


class _BitcoinCollection:
    """Invocation-local reads into the shared producer archive, no separate store."""

    def __init__(self, owner, *, source_identity, since, until, supplied_accounts, state, reobserve, budget, deadline, research=False, byte_limit=None):
        for bound in (since, until):
            if bound is not None and bound.utcoffset() is None:
                raise ValueError("History bounds require a timezone")
        self.since = since.astimezone(timezone.utc) if since else None
        self.until = until.astimezone(timezone.utc) if until else None
        if self.since and self.until and self.since > self.until:
            raise ValueError("End must be on or after start")
        self.owner, self.reobserve = owner, reobserve
        self.endpoint = onchain.rpc_url(onchain.CHAINS["bitcoin"]).rstrip("/")
        self.budget = budget if budget is not None else RequestBudget(max_attempts=MAX_ATTEMPTS)
        self.budget.max_attempts = min(self.budget.max_attempts, MAX_ATTEMPTS)
        self.start_attempts = self.budget.attempts
        if byte_limit is not None and (isinstance(byte_limit, bool) or not isinstance(byte_limit, int) or byte_limit < 0):
            raise ValueError("Invalid archive byte allowance")
        self.byte_limit = min(MAX_TOTAL_BYTES, byte_limit) if byte_limit is not None else MAX_TOTAL_BYTES
        self.deadline = min(deadline, time.monotonic() + TIME_BUDGET_SECONDS) if deadline is not None else time.monotonic() + TIME_BUDGET_SECONDS
        self.reads = {}
        self.block_checks = {}
        self.processed = set()
        supplied = sorted(supplied_accounts or [], key=lambda entry: json.dumps(entry, sort_keys=True))
        requested = {"since": self.since.isoformat() if self.since else None, "until": self.until.isoformat() if self.until else None,
                     "commitment": "six_active_chain_confirmations", "research": research}
        expected = {"version": ARCHIVE_VERSION, "decoder_version": DECODER_VERSION, "chain": "bitcoin", "owner": owner,
                    "source_identity": source_identity, "configuration_identity": bitcoin_source_identity(),
                    "requested": requested, "supplied_accounts": supplied}
        self.archive: dict[str, Any] = copy.deepcopy(state) if state is not None else {
            **expected, "anchor": None, "anchors": [], "inventory": {} if research else _inventory(owner, supplied),
            "streams": {}, "payloads": {}, "transactions": {}, "gaps": [], "bytes": 0,
            "snapshots": [], "unsearched_candidates": [],
        }
        if any(self.archive.get(key) != value for key, value in expected.items()):
            raise ValueError("Bitcoin history source, scope, inventory, decoder or window changed; restart collection")
        self.archive["limits"] = {"seconds": TIME_BUDGET_SECONDS, "attempts": MAX_ATTEMPTS, "concurrency": 1,
            "pages_per_address": MAX_HISTORY_PAGES, "accounts": MAX_ACCOUNTS, "mempool_rows": MEMPOOL_LIMIT,
            "archive_bytes": min(MAX_ARCHIVE_BYTES, self.byte_limit), "payload_bytes": MAX_PAYLOAD_BYTES, "total_bytes": self.byte_limit}
        self.archive["gaps"] = [gap for gap in self.archive["gaps"] if gap not in {
            "deadline_exceeded", "request_limit", "upstream_rate_limited", "anchor_unavailable", "anchor_status_unavailable",
        }]

    def retain(self, path, response):
        raw = {"method": "GET", "params": {"path": path}, "response": response}
        digest = _digest(raw)
        if digest in self.archive["payloads"]:
            return digest
        size = len(json.dumps(raw, separators=(",", ":"), allow_nan=False).encode())
        # ponytail: bounded full-archive size check; use incremental accounting
        # if profiling shows serialization dominates the collection deadline.
        total = len(json.dumps(self.archive, separators=(",", ":"), allow_nan=False).encode())
        if size > MAX_PAYLOAD_BYTES or self.archive["bytes"] + size > MAX_ARCHIVE_BYTES or total + size + 16384 > self.byte_limit:
            self.archive.setdefault("unavailable_payloads", []).append({"digest": digest, "method": "GET", "params": {"path": path}, "bytes": size, "reason": "payload_byte_limit"})
            raise _Stopped("payload_byte_limit")
        self.archive["payloads"][digest] = {**raw, "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source_identity": self.archive["source_identity"], "bytes": size}
        self.archive["bytes"] += size
        return digest

    async def read(self, client, path, *, fresh=False):
        if time.monotonic() >= self.deadline:
            raise OnchainDeadlineExceeded()
        if not fresh and path in self.reads:
            return self.reads[path]
        response = await request_json(client, "GET", self.endpoint + path, endpoint=self.endpoint,
            label="Bitcoin history index", deadline=self.deadline, attempts=onchain.RPC_RETRY_ATTEMPTS,
            backoff=onchain.RPC_RETRY_BACKOFF_SECONDS, timeout=onchain.ONCHAIN_HTTP_TIMEOUT,
            concurrency=onchain.TX_FETCH_CONCURRENCY, budget=self.budget)
        digest = self.retain(path, response)
        # Missing reads are retained as observations, never reusable absence.
        if response is not None:
            self.reads[path] = response, digest
        return response, digest

    async def optional(self, client, path, *, fresh=False):
        try:
            return await self.read(client, path, fresh=fresh)
        except (OnchainDeadlineExceeded, OnchainRequestLimitExceeded, _Stopped):
            raise
        except Exception:
            return None, None

    async def anchor(self, client):
        if self.archive["anchor"] is None or self.reobserve:
            blocks, ref = await self.optional(client, "/blocks")
            tip = blocks[0] if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict) else {}
            if _integer(tip.get("height")) is None or not isinstance(tip.get("id"), str) or not tip["id"]:
                self.archive["gaps"].append("anchor_unavailable")
                return
            anchor: dict[str, Any] = {"height": tip["height"], "blockhash": tip["id"], "block_time": _timestamp(tip.get("timestamp")),
                      "commitment": "six_active_chain_confirmations", "source_refs": [ref], "active": None}
            if self.archive["anchor"] and self.archive["anchor"] not in self.archive["anchors"]:
                self.archive["anchors"].append(copy.deepcopy(self.archive["anchor"]))
            self.archive["anchor"] = anchor
        anchor = self.archive["anchor"]
        status, ref = await self.optional(client, "/block/" + quote(anchor["blockhash"], safe="") + "/status")
        anchor["active"] = status.get("in_best_chain") if isinstance(status, dict) and isinstance(status.get("in_best_chain"), bool) else None
        if ref and ref not in anchor["source_refs"]:
            anchor["source_refs"].append(ref)
        if anchor["active"] is False:
            self.archive["gaps"].append("anchor_reorganized")
            for tx in self.archive["transactions"].values():
                tx["canonical_version"] = None
                tx["revision_status"] = "conflicting_or_reorganized"
        elif anchor["active"] is None:
            self.archive["gaps"].append("anchor_status_unavailable")

    async def block(self, client, status):
        blockhash = status.get("block_hash")
        if status.get("confirmed") is not True or not isinstance(blockhash, str) or not blockhash:
            return None, []
        key = blockhash, status.get("block_height")
        if key in self.block_checks:
            return self.block_checks[key]
        base = "/block/" + quote(blockhash, safe="")
        metadata, metadata_ref = await self.optional(client, base)
        observed, status_ref = await self.optional(client, base + "/status")
        observed = copy.deepcopy(observed) if isinstance(observed, dict) else {}
        if not isinstance(metadata, dict) or metadata.get("id") != blockhash or _integer(metadata.get("height")) != _integer(status.get("block_height")):
            observed = {"in_best_chain": None, "gap": "block_identity_unresolved"}
        elif metadata.get("timestamp") is not None and status.get("block_time") is not None and metadata["timestamp"] != status["block_time"]:
            observed = {"in_best_chain": None, "gap": "source_timestamp_disagreement"}
        result = observed, [ref for ref in (metadata_ref, status_ref) if ref]
        self.block_checks[key] = result
        return result

    async def transaction(self, client, txid, *, payload=None, digest=None, payload_path=None, discovery=None, force=False):
        transaction = self.archive["transactions"].setdefault("bitcoin:" + txid, {
            "signature": txid, "reference": txid, "transaction_ref": txid,
            "discovery_refs": [], "versions": [], "canonical_version": None,
        })
        if discovery and discovery not in transaction["discovery_refs"]:
            transaction["discovery_refs"].append(discovery)
        if txid in self.processed:
            return not transaction.get("retrieval_gap")
        if transaction["versions"] and not self.reobserve and not force and not transaction.get("retrieval_gap"):
            return True
        if payload is None and not self.reobserve and transaction.get("pending_payload"):
            retained = transaction["pending_payload"]
            digest, payload_path = retained["payload_digest"], retained["path"]
            payload = self.archive["payloads"][digest]["response"]
            if payload_path != "response":
                payload = payload[int(payload_path.split(".")[-1])]
        if payload is None:
            payload, digest = await self.optional(client, "/tx/" + quote(txid, safe=""))
            payload_path = "response"
        if not isinstance(payload, dict) or payload.get("txid") != txid or not isinstance(digest, str):
            transaction["retrieval_gap"] = "payload_unavailable" if payload is None else "transaction_identity_mismatch"
            return False
        previous_version = next((v for v in transaction["versions"] if v["version_id"] == transaction["canonical_version"]), None)
        # Retain the transaction/page before optional prevout/status reads so a
        # deadline can never throw away a successful sibling or page tail.
        transaction["pending_payload"] = {"payload_digest": digest, "path": payload_path}
        prevouts = {}
        for item in payload.get("vin", []) if isinstance(payload.get("vin"), list) else []:
            if not isinstance(item, dict) or item.get("is_coinbase") is True:
                continue
            previous, index = item.get("txid"), _integer(item.get("vout"))
            if not isinstance(previous, str) or not previous or index is None:
                continue
            embedded = item.get("prevout")
            if isinstance(embedded, dict) and _satoshis(embedded.get("value")) is not None and _hex(embedded.get("scriptpubkey")) is not None:
                continue
            parent_ref = self.archive.get("prevout_payloads", {}).get(previous)
            if parent_ref in self.archive["payloads"] and not self.reobserve:
                parent = self.archive["payloads"][parent_ref]["response"]
            else:
                parent, parent_ref = await self.optional(client, "/tx/" + quote(previous, safe=""))
            outputs = parent.get("vout") if isinstance(parent, dict) and parent.get("txid") == previous else None
            if isinstance(outputs, list) and index < len(outputs) and isinstance(outputs[index], dict):
                prevouts[_outpoint(previous, index)] = {"output": outputs[index], "payload_digest": parent_ref}
                if parent_ref and _satoshis(outputs[index].get("value")) is not None and _hex(outputs[index].get("scriptpubkey")) is not None:
                    self.archive.setdefault("prevout_payloads", {})[previous] = parent_ref
        block_status, block_refs = await self.block(client, payload.get("status") if isinstance(payload.get("status"), dict) else {})
        version = decode_bitcoin_transaction(txid, payload, payload_digest=digest, owner=self.owner,
            inventory=self.archive["inventory"], anchor=self.archive["anchor"], block_status=block_status, prevouts=prevouts)
        version["payload_path"] = payload_path or "response"
        version["prevout_refs"] = prevouts
        version["confirmation_sources"] = block_refs
        if block_status and block_status.get("gap"):
            version["gaps"].append(block_status["gap"])
        status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
        moment = onchain._history_time(status.get("block_time"))
        version["in_requested_window"] = None if moment is None else (self.since is None or moment >= self.since) and (self.until is None or moment <= self.until)
        if version["in_requested_window"] is not True:
            version["gaps"].append("requested_window_unresolved" if moment is None else "outside_requested_window")
        duplicate = any(item["version_id"] == version["version_id"] for item in transaction["versions"])
        if not duplicate:
            if len(json.dumps(self.archive, separators=(",", ":")).encode()) + len(json.dumps(version, separators=(",", ":")).encode()) + 16384 > self.byte_limit:
                raise _Stopped("decoded_projection_byte_limit")
            transaction["versions"].append(version)
        prevout_facts = {}
        for item in transaction["versions"]:
            for field, fingerprint in bitcoin_prevout_fingerprints(item).items():
                prevout_facts.setdefault(field, set()).add(fingerprint)
        transaction["conflicting_prevout_fields"] = sorted(field for field, values in prevout_facts.items() if len(values) > 1)
        if (len({item["evidence_fingerprint"] for item in transaction["versions"]}) > 1
                or transaction["conflicting_prevout_fields"]
                or any("prevout_conflict" in item["gaps"] for item in transaction["versions"])
                or "transaction_wire_identity_mismatch" in version["gaps"]):
            transaction["canonical_version"] = None
            transaction["revision_status"] = "conflicting_or_reorganized"
        elif version["confirmation_status"] == "reorged":
            transaction["canonical_version"] = None
            transaction["revision_status"] = "conflicting_or_reorganized"
        elif any(item["settlement"] == "settled" for item in transaction["versions"]) and version["settlement"] != "settled":
            if version["confirmation_status"] == "mempool" or (previous_version and version["block_hash"] and version["block_hash"] != previous_version["block_hash"]):
                transaction["canonical_version"] = None
                transaction["revision_status"] = "conflicting_or_reorganized"
            # Unavailable finality alone preserves the preceding verdict.
        else:
            transaction["canonical_version"] = version["version_id"]
            transaction["revision_status"] = "current"
        unresolved_prevout = any(gap in version["gaps"] for gap in ("missing_prevout_value", "missing_prevout_script"))
        unresolved_status = any(gap in version["gaps"] for gap in ("active_chain_unavailable", "block_identity_unresolved", "transaction_status_unavailable"))
        if unresolved_prevout or unresolved_status:
            transaction["retrieval_gap"] = "prevout_unavailable" if unresolved_prevout else "status_unavailable"
        else:
            transaction.pop("retrieval_gap", None)
        transaction.pop("pending_payload", None)
        self.processed.add(txid)
        return not (unresolved_prevout or unresolved_status)

    async def stream(self, client, key, entry, kind):
        stream_key = key + ":" + kind
        stream = self.archive["streams"].setdefault(stream_key, {"address": entry.get("address"), "scriptpubkey": entry.get("scriptpubkey"),
            "kind": kind, "cursor": None, "pages_examined": 0, "exhausted": False, "stop_reason": None,
            "pending": [], "page_refs": [], "seen_cursors": [], "oldest_at": None, "newest_at": None, "unknown_timestamps": 0})
        if kind == "mempool" and self.reobserve:
            stream.update(exhausted=False, stop_reason=None)
        if stream["exhausted"]:
            return
        stream["stop_reason"] = None
        script = entry.get("scriptpubkey")
        target = "/scripthash/" + hashlib.sha256(bytes.fromhex(script)).digest()[::-1].hex() if script is not None else "/address/" + quote(entry["address"], safe="")
        for _ in range(MAX_HISTORY_PAGES if kind == "confirmed" else 1):
            if stream["pending"]:
                await self.pending(client, stream)
                if stream["pending"]:
                    stream["stop_reason"] = "payload_unavailable"
                    return
            if stream.pop("end_after_pending", False):
                stream.update(exhausted=True, stop_reason="provider_exhausted")
                return
            path = target + ("/txs/chain" + ("/" + quote(stream["cursor"], safe="") if stream["cursor"] else "") if kind == "confirmed" else "/txs/mempool")
            page, digest = await self.read(client, path)
            stream["pages_examined"] += 1
            stream["page_refs"].append(digest)
            if not isinstance(page, list):
                stream["stop_reason"] = "malformed_page"
                return
            valid = [item for item in page if isinstance(item, dict) and isinstance(item.get("txid"), str) and item["txid"]]
            malformed = len(valid) != len(page) or len(page) > (25 if kind == "confirmed" else MEMPOOL_LIMIT)
            status_mismatch = any(not isinstance(item.get("status"), dict) or item["status"].get("confirmed") is not (kind == "confirmed") for item in valid)
            malformed = malformed or status_mismatch
            for index, item in enumerate(page):
                if item not in valid:
                    continue
                timestamp = _timestamp((item.get("status") or {}).get("block_time")) if isinstance(item.get("status"), dict) else None
                if timestamp is None:
                    stream["unknown_timestamps"] += 1
                else:
                    stream["oldest_at"] = min(stream["oldest_at"] or timestamp, timestamp)
                    stream["newest_at"] = max(stream["newest_at"] or timestamp, timestamp)
                # Retain every row. Block-time order is not a strict UTC order,
                # so an older page cannot justify skipping later page ties.
                stream["pending"].append({"txid": item["txid"], "payload_digest": digest, "index": index})
            repeated = bool(valid and (valid[-1]["txid"] == stream["cursor"] or valid[-1]["txid"] in stream["seen_cursors"]))
            if valid and not repeated and not malformed and kind == "confirmed":
                stream["cursor"] = valid[-1]["txid"]
                stream["seen_cursors"].append(stream["cursor"])
            stream["end_after_pending"] = not malformed and ((kind == "confirmed" and len(page) < 25) or (kind == "mempool" and len(page) < MEMPOOL_LIMIT))
            await self.pending(client, stream)
            if malformed or repeated or stream["pending"]:
                stream["stop_reason"] = "malformed_page" if malformed else "repeated_cursor" if repeated else "payload_unavailable"
                return
            if stream.pop("end_after_pending", False):
                stream.update(exhausted=True, stop_reason="provider_exhausted")
                return
            if kind == "mempool":
                stream["stop_reason"] = "mempool_cap_nonpageable"
                return
        stream["stop_reason"] = "page_limit"

    async def pending(self, client, stream):
        for item in list(stream["pending"]):
            source = self.archive["payloads"][item["payload_digest"]]["response"]
            if await self.transaction(client, item["txid"], payload=source[item["index"]], digest=item["payload_digest"],
                payload_path=f"response.{item['index']}", discovery={"stream": stream.get("kind"), "address": stream.get("address"), "scriptpubkey": stream.get("scriptpubkey"), "page_ref": item["payload_digest"]}):
                stream["pending"].remove(item)

    def conflicts(self):
        spends = {}
        for key, transaction in self.archive["transactions"].items():
            transaction["conflicts"] = []
            transaction["canonical_conflict"] = None
            for version in transaction["versions"]:
                for item in version["inputs"]:
                    if item.get("outpoint") and not item.get("is_coinbase"):
                        spends.setdefault(item["outpoint"], set()).add(key)
        for outpoint, keys in spends.items():
            if len(keys) < 2:
                continue
            settled = []
            for key in keys:
                transaction = self.archive["transactions"][key]
                current = next((v for v in transaction["versions"] if v["version_id"] == transaction["canonical_version"]), None)
                if current and current["settlement"] == "settled":
                    settled.append(key)
                transaction["conflicts"].append({"outpoint": outpoint, "transactions": sorted(keys - {key}), "relation": "competing_spend"})
            if settled:
                for key in keys:
                    if len(settled) == 1 and key == settled[0]:
                        continue
                    transaction = self.archive["transactions"][key]
                    transaction["canonical_version"] = None
                    transaction["revision_status"] = "conflicting_or_reorganized"
                    transaction["canonical_conflict"] = settled[0] if len(settled) == 1 else None

    def finish(self):
        self.conflicts()
        archive = self.archive
        archive["limits"]["attempts_used"] = self.budget.attempts - self.start_attempts
        archive["limits"]["aggregate_attempts_used"] = self.budget.attempts
        archive["gaps"] = sorted(set(archive["gaps"]))
        transactions = list(archive["transactions"].values())
        versions = [version for tx in transactions for version in tx["versions"] if version["version_id"] == tx["canonical_version"]]
        conflicts = any(tx["versions"] and tx["canonical_version"] is None for tx in transactions)
        archive["resumable"] = any(not stream["exhausted"] for stream in archive["streams"].values()) or any(tx.get("retrieval_gap") or tx.get("pending_payload") for tx in transactions) or bool(archive.get("research", {}).get("frontier")) or archive["anchor"] is None
        boundaries = archive.get("research", {}).get("boundaries", [])
        research_partial = any(item["reason"] not in {"mixed_input_allocation_unresolved", "outside_requested_window"} for item in boundaries)
        archive["coverage"] = {"inventory": "unknown", "retrieval": "partial" if archive["resumable"] or archive["gaps"] else "complete",
            "interpretation": "partial" if conflicts or boundaries or any(v["gaps"] for v in versions) else "complete",
            "settlement": "partial" if conflicts or any(v["settlement"] != "settled" for v in versions) else "complete"}
        if research_partial:
            archive["coverage"]["retrieval"] = "partial"
        subtotal = sum(int(v["owned_quantity"]["known_delta_raw_units"]) for v in versions
                       if v["settlement"] == "settled" and v.get("in_requested_window") is True and v.get("owned_quantity"))
        archive["reconciliation"] = [{"account": self.owner, "asset": dict(ASSET), "status": "unresolved",
            "scope": "declared_addresses_and_scripts", "requested_interval_status": "unresolved",
            "opening_quantity": None, "closing_quantity": None, "expected_closing_quantity": None, "discrepancy": None,
            "known_settled_change": _quantity(subtotal, 8), "known_settled_change_raw_units": str(subtotal),
            "reasons": ["opening_quantity_unknown", "closing_quantity_unknown", "requested_boundary_snapshots_unavailable", "historical_inventory_unresolved", "basis_unknown"]}]
        return archive

    async def research(self, client, outpoint, direction, limits):
        research = self.archive.setdefault("research", {"outpoint": outpoint, "direction": direction,
            "frontier": [{"outpoint": outpoint, "depth": 0, "allocation": "selected_evidence_only"}],
            "visited": [], "nodes": [], "edges": [], "boundaries": [], "outspends": [],
            "limits": limits})
        if (research["outpoint"], research["direction"]) != (outpoint, direction):
            raise ValueError("Bitcoin continuation selection changed; restart research")
        while research["frontier"]:
            selected = research["frontier"][0]
            current = selected["outpoint"]
            key = direction + ":" + current
            if key in research["visited"]:
                research["frontier"].pop(0)
                continue
            txid, index = current.rsplit(":", 1)
            if txid not in research["nodes"] and len(research["nodes"]) >= limits["nodes"]:
                research["boundaries"].append({"outpoint": current, "reason": "node_limit"})
                break
            await self.transaction(client, txid, discovery={"source": "selected_outpoint", "outpoint": current})
            tx = self.archive["transactions"]["bitcoin:" + txid]
            version = next((v for v in tx["versions"] if v["version_id"] == tx["canonical_version"]), None)
            if version is None:
                research["boundaries"].append({"outpoint": current, "reason": "transaction_unavailable_or_conflicting"})
                break
            if int(index) >= len(version["outputs"]):
                research["boundaries"].append({"outpoint": current, "reason": "outpoint_not_found"})
                research["frontier"].pop(0)
                research["visited"].append(key)
                continue
            if txid not in research["nodes"]:
                research["nodes"].append(txid)
            next_points = []
            allocation = selected["allocation"]
            if version["ownership_allocation"] == "mixed_or_unresolved_inputs":
                allocation = "unresolved_after_mixed_inputs"
                research["boundaries"].append({"outpoint": current, "reason": "mixed_input_allocation_unresolved"})
            if selected["depth"] >= limits["hops"]:
                research["boundaries"].append({"outpoint": current, "reason": "hop_limit"})
            elif version["in_requested_window"] is False:
                research["boundaries"].append({"outpoint": current, "reason": "outside_requested_window"})
            elif direction == "in":
                next_points = [item["outpoint"] for item in version["inputs"] if item.get("outpoint") and not item.get("is_coinbase")]
                for previous in next_points:
                    edge = {"source_outpoint": previous, "destination_txid": txid, "selected_output": current,
                            "direction": "in", "allocation": allocation, "payload_digest": version["payload_digest"]}
                    if edge not in research["edges"]:
                        research["edges"].append(edge)
            else:
                status, ref = await self.optional(client, "/tx/" + quote(txid, safe="") + "/outspend/" + index)
                if not isinstance(status, dict) or not isinstance(status.get("spent"), bool):
                    research["boundaries"].append({"outpoint": current, "reason": "outspend_unavailable"})
                    break
                observed = {"outpoint": current, "status": status, "payload_digest": ref}
                if observed not in research["outspends"]:
                    research["outspends"].append(observed)
                if status["spent"]:
                    spend, vin = status.get("txid"), _integer(status.get("vin"))
                    if not isinstance(spend, str) or not spend or vin is None:
                        research["boundaries"].append({"outpoint": current, "reason": "malformed_outspend"})
                        break
                    if spend not in research["nodes"] and len(research["nodes"]) >= limits["nodes"]:
                        research["boundaries"].append({"outpoint": current, "reason": "node_limit"})
                        break
                    await self.transaction(client, spend, discovery={"source": "outspend", "outpoint": current, "payload_digest": ref})
                    target = self.archive["transactions"]["bitcoin:" + spend]
                    target_version = next((v for v in target["versions"] if v["version_id"] == target["canonical_version"]), None)
                    if target_version is None or vin >= len(target_version["inputs"]) or target_version["inputs"][vin].get("outpoint") != current:
                        research["boundaries"].append({"outpoint": current, "reason": "outspend_identity_unresolved"})
                        break
                    if spend not in research["nodes"]:
                        research["nodes"].append(spend)
                    if target_version["ownership_allocation"] != "known_owned_inputs":
                        allocation = "unresolved_after_mixed_inputs"
                        research["boundaries"].append({"outpoint": current, "reason": "mixed_input_allocation_unresolved"})
                    edge = {"source_outpoint": current, "destination_txid": spend, "input_index": vin,
                            "direction": "out", "allocation": allocation, "payload_digest": ref}
                    if edge not in research["edges"]:
                        research["edges"].append(edge)
                    next_points = [item["outpoint"] for item in target_version["outputs"]]
            if len(next_points) > limits["branches"]:
                research["boundaries"].append({"outpoint": current, "reason": "branch_limit", "omitted": next_points[limits["branches"]:]})
            research["frontier"].extend({"outpoint": item, "depth": selected["depth"] + 1, "allocation": allocation} for item in next_points[:limits["branches"]])
            research["visited"].append(key)
            research["frontier"].pop(0)
        # Boundaries retain observations, never confer ownership on a branch.
        research["boundaries"] = list({json.dumps(item, sort_keys=True): item for item in research["boundaries"]}.values())

    async def run(self, research=None):
        timeout = asyncio.timeout_at(self.deadline)
        try:
            async with timeout, onchain.session() as client:
                # Completed replay makes no new calls; explicit re-observation
                # is required to advance the anchor or refresh mutable evidence.
                work = self.reobserve or (research is not None and (not self.archive.get("research") or self.archive["research"]["frontier"])) or (research is None and (not self.archive["streams"] or any(not stream["exhausted"] for stream in self.archive["streams"].values())))
                if work and (research is None or research["limits"]["nodes"] > 0):
                    await self.anchor(client)
                if self.reobserve:
                    for tx in list(self.archive["transactions"].values()):
                        await self.transaction(client, tx["signature"], force=True)
                if research is not None:
                    await self.research(client, **research)
                else:
                    inventory = self.archive["inventory"] or {self.owner: {"address": self.owner}}
                    for index, (key, entry) in enumerate(inventory.items()):
                        if index >= MAX_ACCOUNTS:
                            self.archive["unsearched_candidates"] = list(inventory)[index:]
                            self.archive["gaps"].append("account_limit")
                            break
                        for kind in ("confirmed", "mempool"):
                            try:
                                await self.stream(client, key, entry, kind)
                            except (OnchainDeadlineExceeded, OnchainRequestLimitExceeded, _Stopped):
                                raise
                            except Exception as exc:
                                self.archive["streams"][key + ":" + kind]["stop_reason"] = interruption_code(exc)
                if work and self.archive["anchor"]:
                    anchor = self.archive["anchor"]
                    status, ref = await self.optional(client, "/block/" + quote(anchor["blockhash"], safe="") + "/status", fresh=True)
                    if isinstance(status, dict) and status.get("in_best_chain") is False:
                        self.archive["gaps"].append("anchor_reorganized")
                        for tx in self.archive["transactions"].values():
                            tx["canonical_version"] = None
                            tx["revision_status"] = "conflicting_or_reorganized"
                        anchor["active"] = False
                    if ref and ref not in anchor["source_refs"]:
                        anchor["source_refs"].append(ref)
        except OnchainRequestLimitExceeded:
            self.archive["gaps"].append("request_limit")
        except CancelledError:
            self.archive["gaps"].append("cancelled")
        except (OnchainDeadlineExceeded, TimeoutError):
            self.archive["gaps"].append("deadline_exceeded")
        except _Stopped as exc:
            self.archive["gaps"].append(str(exc))
        except Exception as exc:
            self.archive["gaps"].append(interruption_code(exc))
        return self.finish()


async def collect_bitcoin_history(
    owner: str, *, source_identity: str, since: datetime | None = None, until: datetime | None = None,
    supplied_accounts: list[dict] | None = None, state: dict | None = None, reobserve: bool = False,
    budget: RequestBudget | None = None, deadline: float | None = None, research: bool = False, byte_limit: int | None = None,
) -> dict:
    collector = _BitcoinCollection(owner, source_identity=source_identity, since=since, until=until,
        supplied_accounts=supplied_accounts, state=state, reobserve=reobserve, budget=budget, deadline=deadline, research=research, byte_limit=byte_limit)
    return await collector.run()


async def continue_bitcoin_history(
    owner: str, *, outpoint: str, direction: str, source_identity: str, state: dict | None = None,
    since: datetime | None = None, until: datetime | None = None, supplied_accounts: list[dict] | None = None,
    budget: RequestBudget | None = None, deadline: float | None = None, byte_limit: int | None = None,
    max_hops: int = 6, max_branches: int = 5, max_nodes: int = 24,
) -> dict:
    if direction not in {"in", "out"} or not isinstance(outpoint, str) or ":" not in outpoint:
        raise ValueError("Bitcoin continuation requires an outpoint and in/out direction")
    txid, index = outpoint.rsplit(":", 1)
    if not txid or not index.isascii() or not index.isdigit() or int(index) > 2**32 - 1:
        raise ValueError("Invalid Bitcoin outpoint")
    outpoint = _outpoint(txid, int(index))
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (max_hops, max_branches, max_nodes)) or max_branches == 0:
        raise ValueError("Invalid Bitcoin research limits")
    limits = {"hops": min(max_hops, MAX_RESEARCH_HOPS), "branches": min(max_branches, MAX_RESEARCH_BRANCHES), "nodes": min(max_nodes, MAX_RESEARCH_NODES)}
    if state and state.get("research") and (state["research"]["outpoint"], state["research"]["direction"]) != (outpoint, direction):
        raise ValueError("Bitcoin continuation selection changed; restart research")
    if state and state.get("research") and state["research"].get("limits") != limits:
        raise ValueError("Bitcoin continuation limits changed; restart research")
    collector = _BitcoinCollection(owner, source_identity=source_identity, since=since, until=until,
        supplied_accounts=supplied_accounts, state=state, reobserve=False, budget=budget, deadline=deadline,
        research=bool(state and state.get("requested", {}).get("research")), byte_limit=byte_limit)
    return await collector.run({"outpoint": outpoint, "direction": direction, "limits": limits})
