"""Bounded EVM evidence; receipts/logs/traces are never a financial ledger.

References: ethereum.org/developers/docs/apis/json-rpc/ ; eips.ethereum.org/EIPS/eip-20
geth.ethereum.org/docs/developers/evm-tracing/built-in-tracers
specs.optimism.io/protocol/isthmus/exec-engine.html
specs.optimism.io/protocol/jovian/exec-engine.html
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from app.providers import onchain
from app.providers.onchain_reads import MAX_HISTORY_PAGES, interruption_code
from app.providers.onchain_transport import (
    OnchainDeadlineExceeded, OnchainRequestLimitExceeded, RequestBudget, request_json,
)
from app.providers.solana_history import (
    ARCHIVE_VERSION, MAX_ARCHIVE_BYTES, MAX_ATTEMPTS, MAX_PAYLOAD_BYTES,
    MAX_TOTAL_BYTES, TIME_BUDGET_SECONDS, _CollectionStopped, _digest,
)

DECODER_VERSION = "evm-1"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
MAX_TOKENS = 128
MAX_TOKEN_ACCOUNTS = 128
MAX_TRANSACTIONS = 4096
MAX_TRACE_FRAMES = 4096
_NETWORKS = {"ethereum": 1, "base": 8453, "polygon": 137}


def _uint(value: Any, bits: int = 256) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        number = int(value, 16 if value.startswith("0x") else 10) if isinstance(value, str) else value
        return number if 0 <= number < 2**bits else None
    except ValueError:
        return None


def _hex(value: Any, size: int) -> str | None:
    return value.lower() if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{" + str(size * 2) + "}", value) else None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _quantity(units: int | None, decimals: int | None) -> str | None:
    if units is None or decimals is None:
        return None
    # String placement is exact even for uint256 and all u8 decimals.
    digits = str(abs(units)).zfill(decimals + 1)
    return ("-" if units < 0 else "") + (digits[:-decimals] + "." + digits[-decimals:] if decimals else digits)


def _asset(chain: str, contract: str | None = None) -> dict:
    return {"chain": chain, "network_id": _NETWORKS[chain], "native": contract is None,
            "mint": contract, "contract": contract, "token_program": None,
            "symbol": onchain.CHAINS[chain].symbol if contract is None else None}


def _moment(value: Any) -> datetime | None:
    number = _uint(value, 64)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _index_time(row: dict) -> datetime | None:
    instant = _moment(row.get("timeStamp"))
    if instant is not None or not isinstance(row.get("timestamp"), str):
        return instant
    try:
        instant = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        return instant.replace(tzinfo=timezone.utc) if instant.tzinfo is None else instant.astimezone(timezone.utc)
    except ValueError:
        return None


def _log(log: Any) -> tuple[str, str, str, int, int] | None:
    if not isinstance(log, dict):
        return None
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) != 3 or _hex(topics[0], 32) != TRANSFER_TOPIC:
        return None
    source, destination = _hex(topics[1], 32), _hex(topics[2], 32)
    contract, data, index = _hex(log.get("address"), 20), _hex(log.get("data"), 32), _uint(log.get("logIndex"))
    if not source or not destination or not contract or not data or index is None:
        return None
    if source[2:26] != "0" * 24 or destination[2:26] != "0" * 24:
        return None
    return contract, "0x" + source[-40:], "0x" + destination[-40:], int(data, 16), index


def _fork(policy: dict, timestamp: Any) -> str | None:
    stamp = _uint(timestamp, 64)
    if stamp is None:
        return None
    matches = [item.get("formula") for item in policy.get("operator_fee_forks", [])
               if isinstance(item, dict) and item.get("source") and _uint(item.get("from_timestamp"), 64) is not None
               and item["from_timestamp"] <= stamp
               and (item.get("until_timestamp") is None or stamp < item["until_timestamp"])]
    return matches[0] if len(matches) == 1 and matches[0] in ("pre_isthmus", "isthmus", "jovian") else None


def _indexed_path(row: dict) -> tuple[int, ...] | None:
    path = row.get("traceAddress")
    if isinstance(path, list) and all(_uint(part, 32) is not None for part in path):
        return tuple(value for part in path if (value := _uint(part, 32)) is not None)
    if isinstance(row.get("traceId"), str) and re.fullmatch(r"\d+(?:_\d+)*", row["traceId"]):
        return tuple(int(part) for part in row["traceId"].split("_"))
    return None


def _indexed_fingerprints(rows: list[dict]) -> tuple[dict[str, str], bool]:
    """Compare known overlapping facts; missing frames/fields can be enriched."""
    facts: dict[str, set[str]] = {}
    for row in rows:
        path = _indexed_path(row)
        if path is None:
            continue
        known = {"from": _hex(_mapping(row.get("from")).get("hash", row.get("from")), 20),
                 "to": _hex(_mapping(row.get("to")).get("hash", row.get("to")), 20),
                 "value": _uint(row.get("value")), "type": row["type"].lower() if isinstance(row.get("type"), str) else None}
        if row.get("isError") == "1" or row.get("success") is False or row.get("error"):
            known["outcome"] = "failed"
        elif row.get("isError") == "0" or row.get("success") is True:
            known["outcome"] = "success"
        prefix = "trace:" + ("/".join(map(str, path)) if path else "root")
        for name, value in known.items():
            if value is not None:
                facts.setdefault(prefix + "/" + name, set()).add(_digest(value))
    return {key: _digest(sorted(values)) for key, values in facts.items()}, any(len(values) > 1 for values in facts.values())


def decode_evm_transaction(reference: str, payload: dict, *, chain: str, owner: str,
                           payload_digest: str, policy: dict | None = None, research: bool = False) -> dict:
    """Replay one retained evidence bundle; no I/O, price lookup or ownership inference."""
    policy = policy if policy is not None else _mapping(payload.get("interpretation_policy"))
    tx = _mapping(payload.get("transaction"))
    receipt = _mapping(payload.get("receipt"))
    block = _mapping(payload.get("block"))
    finality = _mapping(payload.get("finality"))
    gaps = list(payload.get("gaps", []))
    indexed_fingerprints, indexed_conflict = _indexed_fingerprints(payload.get("internal_rows", []))
    if indexed_conflict:
        gaps.append("internal_trace_identity_conflict")
    number, block_hash = _uint(receipt.get("blockNumber")), _hex(receipt.get("blockHash"), 32)
    status = _uint(receipt.get("status"), 8)
    execution = "success" if status == 1 else "failed" if status == 0 else "unknown"
    when = _moment(block.get("timestamp"))
    inclusion = bool(block_hash and _hex(block.get("hash"), 32) == block_hash
                     and _uint(block.get("number")) == number)
    identity = bool(_hex(tx.get("hash"), 32) == reference == _hex(receipt.get("transactionHash"), 32))
    if not identity:
        gaps.append("transaction_identity_mismatch")
        execution = "unknown"
    if _uint(tx.get("blockNumber")) != number or _hex(tx.get("blockHash"), 32) != block_hash:
        inclusion = False
        gaps.append("transaction_receipt_inclusion_conflict")
    index = _uint(receipt.get("transactionIndex"))
    block_transactions = block.get("transactions")
    if not isinstance(block_transactions, list) or index is None or index >= len(block_transactions):
        inclusion = False
        gaps.append("block_transaction_membership_unavailable")
    else:
        item = block_transactions[index]
        if _hex(item.get("hash") if isinstance(item, dict) else item, 32) != reference:
            inclusion = False
            gaps.append("block_transaction_membership_conflict")
    if not inclusion:
        gaps.append("canonical_inclusion_unresolved")
    if status not in (0, 1):
        gaps.append("missing_execution_metadata")
    if when is None:
        gaps.append("unknown_timestamp")
    for row in payload.get("index_rows", []):
        if not isinstance(row, dict):
            gaps.append("malformed_index_row")
            continue
        comparisons = [("value", _uint, tx.get("value")), ("blockNumber", _uint, receipt.get("blockNumber")),
                       ("blockHash", lambda value: _hex(value, 32), receipt.get("blockHash"))]
        for field, normalize, actual in comparisons:
            if field in row and normalize(row[field]) != normalize(actual):
                gaps.append("index_receipt_transaction_disagreement")
        for field in ("from", "to"):
            if field in row and _hex(_mapping(row[field]).get("hash", row[field]), 20) != _hex(tx.get(field), 20):
                gaps.append("index_receipt_transaction_disagreement")
        if _index_time(row) is not None and when is not None and _index_time(row) != when:
            gaps.append("source_timestamp_disagreement")
    finalized = finality.get("finalized") or {}
    finalized_number = _uint(finalized.get("number"))
    settled = bool(inclusion and identity and finalized_number is not None and number is not None
                   and number <= finalized_number and _hex(finalized.get("hash"), 32)
                   and (number != finalized_number or block_hash == _hex(finalized.get("hash"), 32)))
    if chain == "base":
        settled = settled and finality.get("l1_corroborated") is True
        if finality.get("l1_corroborated") is not True:
            gaps.append("base_l1_finality_unavailable")
    settlement = "settled" if settled and execution != "unknown" else "provisional"
    result: dict[str, Any] = {"payload_digest": payload_digest, "version_id": "", "decoder_version": DECODER_VERSION,
              "slot": number, "block_number": number, "block_hash": block_hash, "transaction_index": index,
              "block_time": when.isoformat() if when else None, "original_timestamp": block.get("timestamp"),
              "time_precision": "second" if when else "unknown", "execution": execution,
              "confirmation_status": "finalized" if settlement == "settled" else "provisional",
              "settlement": settlement, "in_requested_window": payload.get("in_requested_window"),
              "legs": [], "observations": [], "ownership": [], "relationships": [], "gaps": gaps,
              "fee_components": [], "fee_status": "unknown", "finality": finality,
              "source_refs": payload.get("source_refs", []), "attempted": []}

    def add(key, role, source, destination, units, contract=None, decimals=18, unresolved=False):
        identity = key if len(key) <= 128 else "trace-path-sha256:" + _digest(key)
        leg: dict[str, Any] = {"key": f"{chain}:{reference}:{identity}", "role": role, "source": source, "destination": destination,
               "source_owner": owner if source == owner and not research else None,
               "destination_owner": owner if destination == owner and not research else None,
               "asset": _asset(chain, contract), "raw_units": str(units) if units is not None else None,
               "decimals": decimals, "quantity": _quantity(units, decimals), "settlement": settlement,
               "derivation": {"payload_digest": payload_digest, "path": key, "block_hash": block_hash}}
        if unresolved or units is None:
            leg["interpretation"] = "unresolved"
        result["legs"].append(leg)
        return leg

    payer, receipt_payer = _hex(tx.get("from"), 20), _hex(receipt.get("from"), 20)
    tx_type = _uint(tx.get("type", "0x0"), 8)
    supported = tx_type in (0, 1, 2, 4) or (tx_type == 3 and chain == "ethereum")
    if not supported:
        gaps.append("unsupported_transaction_type")
    if payer is None or receipt_payer != payer:
        gaps.append("fee_payer_unresolved")
        payer = None
    gas, price = _uint(receipt.get("gasUsed"), 64), _uint(receipt.get("effectiveGasPrice"))
    components = {"execution": gas * price if gas is not None and price is not None else None}
    if chain == "ethereum" and tx_type == 3:
        used, rate = _uint(receipt.get("blobGasUsed"), 64), _uint(receipt.get("blobGasPrice"))
        components["blob"] = used * rate if used is not None and rate is not None else None
    if chain == "base":
        components["l1_data"] = _uint(receipt.get("l1Fee"))
        fork = _fork(policy, block.get("timestamp"))
        scalar, constant = _uint(receipt.get("operatorFeeScalar")), _uint(receipt.get("operatorFeeConstant"))
        operator = None
        if fork == "pre_isthmus":
            operator = 0 if scalar in (None, 0) and constant in (None, 0) else None
        elif fork and gas is not None and scalar is not None and constant is not None:
            operator = constant + (gas * scalar // 1_000_000 if fork == "isthmus" else gas * scalar * 100)
        if receipt.get("operatorFee") is not None and _uint(receipt["operatorFee"]) != operator:
            gaps.append("operator_fee_conflict")
            operator = None
        components["operator"] = operator
        result["operator_fee_policy"] = fork
        # Independent indexed values are corroboration, never another charge.
        for row in payload.get("index_rows", []):
            for field, component in (("l1Fee", "l1_data"), ("operatorFee", "operator")):
                if field in row and _uint(row[field]) != components[component]:
                    gaps.append(component + "_fee_conflict")
                    components[component] = None
    for component, amount in components.items():
        if amount is None or payer is None or not supported or not identity or execution == "unknown":
            amount = None
            gaps.append(component + "_fee_unavailable")
        result["fee_components"].append({"component": component, "raw_units": str(amount) if amount is not None else None,
                                         "payer": payer, "asset": _asset(chain)})
        if amount is not None:
            add("fee:" + component, "network_fee", payer, None, amount)
    result["fee_status"] = "complete" if all(item["raw_units"] is not None for item in result["fee_components"]) else "partial"
    if not supported or execution != "success":
        result["attempted"] = [{"path": "transaction", "raw_units": tx.get("value"), "source": tx.get("from"), "destination": tx.get("to")}]
    else:
        source, destination = _hex(tx.get("from"), 20), _hex(tx.get("to"), 20)
        if tx.get("to") is None:
            destination = _hex(receipt.get("contractAddress"), 20)
        amount = _uint(tx.get("value"))
        if source and destination and amount is not None:
            add("transaction", "principal", source, destination, amount)
        else:
            gaps.append("ordinary_transfer_unresolved")

    trace = payload.get("trace")
    if isinstance(trace, dict) and isinstance(trace.get("type"), str):
        frames = [("trace:root", trace, False)]
        seen = 0
        while frames and seen < MAX_TRACE_FRAMES:
            path, frame, ancestor_reverted = frames.pop()
            seen += 1
            if not isinstance(frame, dict):
                gaps.append("malformed_trace_frame")
                continue
            reverted = ancestor_reverted or bool(frame.get("error"))
            children = frame.get("calls", [])
            if isinstance(children, list):
                frames.extend((path + "/" + str(i), child, reverted) for i, child in reversed(list(enumerate(children))))
            else:
                gaps.append("malformed_trace_children")
            if path == "trace:root":
                if (_hex(frame.get("from"), 20) != _hex(tx.get("from"), 20)
                    or _uint(frame.get("value", "0x0")) != _uint(tx.get("value"))
                    or bool(frame.get("error")) != (execution == "failed")):
                    gaps.append("trace_receipt_conflict")
                continue  # Top-level value is represented by the transaction exactly once.
            source, destination, value = _hex(frame.get("from"), 20), _hex(frame.get("to"), 20), _uint(frame.get("value", "0x0"))
            if reverted or execution != "success" or not supported:
                result["attempted"].append({"path": path, "error": frame.get("error"), "ancestor_reverted": ancestor_reverted,
                                            "source": source, "destination": destination, "raw_units": str(value) if value is not None else None})
                continue
            kind = str(frame.get("type", "")).upper()
            if kind in ("CALL", "CREATE", "CREATE2", "SELFDESTRUCT") and source and destination and value is not None:
                add(path, "principal", source, destination, value)
            elif kind not in ("DELEGATECALL", "STATICCALL", "CALLCODE"):
                gaps.append("unsupported_internal_execution")
        if frames:
            gaps.append("trace_frame_limit")
    else:
        rows = payload.get("internal_rows", [])
        paths = {}
        for row in rows:
            path = _indexed_path(row)
            if path is None:
                gaps.append("internal_trace_identity_unavailable")
                continue
            paths[path] = {**paths.get(path, {}), **{key: value for key, value in row.items() if value is not None}}
        for path, row in paths.items():
            if not path:
                continue
            ancestors = [paths.get(path[:i]) for i in range(1, len(path))]
            explicit = all(parent is not None and (parent.get("isError") == "0" or parent.get("success") is True) for parent in ancestors)
            reverted = any(parent.get("isError") == "1" or parent.get("success") is False or parent.get("error")
                           for parent in [row, *[p for p in ancestors if p is not None]])
            if reverted or execution != "success" or not supported:
                result["attempted"].append({"path": "trace:" + "/".join(map(str, path)), "reverted": bool(reverted)})
                continue
            def party(side):
                value = row.get(side)
                return _hex(value.get("hash") if isinstance(value, dict) else value, 20)
            source, destination, value = party("from"), party("to"), _uint(row.get("value"))
            success = row.get("isError") == "0" or row.get("success") is True
            unresolved = not explicit or not success
            if source == _hex(tx.get("from"), 20) and destination == _hex(tx.get("to"), 20) and value == _uint(tx.get("value")):
                unresolved = True
                gaps.append("internal_root_overlap_unresolved")
            if source and destination and value is not None and row.get("type") in ("call", "create", "create2", "suicide", "selfdestruct"):
                leg = add("trace:" + "/".join(map(str, path)), "principal", source, destination, value, unresolved=unresolved)
                if unresolved:
                    leg["settlement"] = "provisional"
                    gaps.append("internal_trace_ancestry_unresolved")
            else:
                gaps.append("unsupported_internal_execution")
        gaps.append("call_trace_unavailable")

    if isinstance(trace, dict):
        executed = {(leg["source"], leg["destination"], leg["raw_units"]) for leg in result["legs"]
                    if leg["asset"]["native"] and leg["role"] == "principal"}
        attempted = {(item.get("source"), item.get("destination"), item.get("raw_units")) for item in result["attempted"]}
        for row in payload.get("internal_rows", []):
            source = _hex(_mapping(row.get("from")).get("hash", row.get("from")), 20)
            destination = _hex(_mapping(row.get("to")).get("hash", row.get("to")), 20)
            units = _uint(row.get("value"))
            item = (source, destination, str(units) if units is not None else None)
            reverted = row.get("isError") == "1" or row.get("success") is False or bool(row.get("error"))
            if item not in (attempted if reverted or execution == "failed" else executed):
                gaps.append("indexed_internal_trace_disagreement")

    logs = receipt.get("logs")
    if not isinstance(logs, list):
        logs = []
        gaps.append("receipt_logs_unavailable")
    retained = {}
    for log in logs:
        if (not isinstance(log, dict) or not isinstance(log.get("topics"), list)
            or not _hex(log.get("address"), 20) or any(not _hex(topic, 32) for topic in log["topics"])
            or not isinstance(log.get("data"), str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", log["data"])):
            gaps.append("malformed_receipt_log")
            continue
        parsed = _log(log)
        if parsed is None:
            if isinstance(log, dict) and isinstance(log.get("topics"), list) and log["topics"] and log["topics"][0] == TRANSFER_TOPIC:
                gaps.append("unsupported_or_malformed_transfer_log")
            continue
        contract, source, destination, value, ordinal = parsed
        if ordinal in retained:
            if retained[ordinal] != log:
                gaps.append("receipt_log_identity_conflict")
            continue
        retained[ordinal] = log
        if (_hex(log.get("transactionHash"), 32) != reference or _hex(log.get("blockHash"), 32) != block_hash
            or _uint(log.get("blockNumber")) != number or log.get("removed") is True):
            gaps.append("receipt_log_inclusion_conflict")
            continue
        if execution != "success" or not supported:
            continue
        evidence = payload.get("tokens", {}).get(contract, {})
        decimals = _uint(evidence.get("decimals"), 8)
        if decimals is None:
            gaps.append("token_decimals_unavailable")
        qualified = evidence.get("semantics") == "corroborated_standard_erc20"
        if not qualified:
            gaps.extend(evidence.get("gaps", []) or ["token_semantics_uncorroborated"])
        role = "mint" if source == ZERO else "burn" if destination == ZERO else "principal"
        leg = add("log:" + str(ordinal), role, source, destination, value, contract, decimals, not qualified)
        leg["source_log_index"] = ordinal
        leg["receipt_local_ordinal"] = next(i for i, item in enumerate(logs) if item is log)
    for observed in payload.get("discovered_logs", []):
        parsed = _log(observed)
        if parsed and retained.get(parsed[4]) != observed:
            # Providers may add harmless fields; compare only the chain event identity/content.
            canonical = retained.get(parsed[4], {})
            fields = ("address", "topics", "data", "blockHash", "blockNumber", "transactionHash", "logIndex", "removed")
            if any(canonical.get(key, False if key == "removed" else None) != observed.get(key, False if key == "removed" else None) for key in fields):
                gaps.append("index_receipt_log_disagreement")
    for contract, evidence in payload.get("tokens", {}).items():
        for observation in evidence.get("observations", []):
            result["observations"].append({**observation, "asset": _asset(chain, contract),
                                            "owner": owner if observation.get("account") == owner and not research else None})
    if any(gap in gaps for gap in ("trace_receipt_conflict", "receipt_log_identity_conflict", "receipt_log_inclusion_conflict", "index_receipt_log_disagreement",
                                   "index_receipt_transaction_disagreement", "source_timestamp_disagreement", "internal_trace_identity_conflict", "indexed_internal_trace_disagreement")):
        result["settlement"] = "provisional"
        result["confirmation_status"] = "conflicting"
        for leg in result["legs"]:
            leg["settlement"] = "provisional"
    result["gaps"] = sorted(set(gaps))
    result["evidence_fingerprint"] = _digest({"transaction": tx, "receipt": receipt})
    result["internal_evidence_fingerprint"] = _digest(trace) if isinstance(trace, dict) else None
    result["indexed_internal_fingerprints"] = indexed_fingerprints
    result["version_id"] = _digest({"payload": payload, "decoder": DECODER_VERSION, "policy": _policy_identity(policy)})
    return result


def _policy_identity(policy: dict) -> str:
    # URLs/configuration bind resume, but secrets never appear in the archive.
    return _digest(policy)


class _Reads:
    def __init__(self, archive: dict[str, Any], budget: RequestBudget, deadline: float, byte_limit: int, *, replay=False):
        self.archive, self.budget, self.deadline = archive, budget, deadline
        self.byte_limit = byte_limit
        self.cache = {}
        if replay:
            for digest, raw in archive["payloads"].items():
                method, params = raw["method"], raw["params"]
                value = _mapping(raw["response"]).get("result")
                immutable = method in ("eth_getTransactionByHash", "eth_getTransactionReceipt", "debug_traceTransaction")
                immutable |= method in ("eth_getCode", "eth_call") and isinstance(params, list) and any(isinstance(part, dict) and part.get("blockHash") for part in params)
                immutable |= method == "eth_getLogs" and isinstance(params, list) and bool(_mapping(params[0]).get("blockHash"))
                if immutable and value is not None and not _mapping(raw["response"]).get("error"):
                    self.cache[_digest({"source": raw["source"], "method": method, "params": params})] = (value, digest)

    def retain(self, method, params, response, source):
        raw = {"method": method, "params": copy.deepcopy(params), "response": copy.deepcopy(response)}
        digest = _digest(raw)
        size = len(json.dumps(raw, separators=(",", ":")).encode())
        archive = self.archive
        if digest not in archive["payloads"]:
            # ponytail: bounded archive-size serialization; increment sizes if this becomes hot.
            if size > MAX_PAYLOAD_BYTES or archive["bytes"] + size > min(MAX_ARCHIVE_BYTES, self.byte_limit) or len(json.dumps(archive).encode()) + size + 16384 > self.byte_limit:
                archive.setdefault("unavailable_payloads", []).append({"digest": digest, "method": method, "params": params,
                                                                       "bytes": size, "reason": "payload_byte_limit"})
                raise _CollectionStopped("payload_byte_limit")
            archive["payloads"][digest] = {**raw, "source_identity": archive["source_identity"], "source": source,
                                            "retrieved_at": datetime.now(timezone.utc).isoformat(), "bytes": size}
            archive["bytes"] += size
        return digest

    async def read(self, client, endpoint, method, params, *, rpc=True, source="rpc", url=None, private_params=None):
        if time.monotonic() >= self.deadline:
            raise OnchainDeadlineExceeded()
        key = _digest({"source": source, "method": method, "params": params})
        if key in self.cache:
            return self.cache[key]
        response = await request_json(client, "POST" if rpc else "GET", url or endpoint, endpoint=endpoint,
                                      label="EVM history", deadline=self.deadline,
                                      attempts=onchain.RPC_RETRY_ATTEMPTS, backoff=onchain.RPC_RETRY_BACKOFF_SECONDS,
                                      timeout=onchain.ONCHAIN_HTTP_TIMEOUT, concurrency=onchain.TX_FETCH_CONCURRENCY,
                                      rpc=rpc, budget=self.budget,
                                      json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params} if rpc else None,
                                      params={**params, **(private_params or {})} if not rpc else None)
        ref = self.retain(method, params, response, source)
        value = response.get("result") if rpc and isinstance(response, dict) and not response.get("error") else response if not rpc else None
        self.cache[key] = (value, ref)
        return value, ref


def evm_source_identity(chain: str, policy: dict | None = None) -> str:
    settings = onchain.get_settings()
    policy = policy if policy is not None else getattr(settings, "evm_history_policies", {}).get(chain, {})
    network = onchain.CHAINS[chain]
    return _digest({"chain": chain, "rpc": onchain.rpc_url(network), "index": network.token_index_url,
                    "etherscan": settings.etherscan_api_key, "policy": policy})


def reconcile_evm_history(archive: dict) -> list[dict]:
    """Qualify observed block equations; never invent requested-boundary snapshots."""
    owner = archive["owner"]
    rows: dict[str, Any] = {}
    for transaction in archive["transactions"].values():
        version = next((v for v in transaction["versions"] if v["version_id"] == transaction["canonical_version"]), None)
        conflict = version is None
        version = version or (transaction["versions"][-1] if transaction["versions"] else {})
        if version.get("in_requested_window") is False:
            continue
        for leg in version.get("legs", []):
            if owner not in (leg.get("source"), leg.get("destination")):
                continue
            identity = leg["asset"].get("contract") or "native"
            row = rows.setdefault(identity, {"account": owner, "asset": leg["asset"], "units": 0,
                                            "decimals": leg.get("decimals"), "samples": [], "gaps": set()})
            if leg.get("decimals") != row["decimals"]:
                row["gaps"].add("incompatible_token_decimals")
            if conflict or version.get("settlement") != "settled" or leg.get("settlement") != "settled":
                row["gaps"].add("settlement_unresolved")
            elif version.get("in_requested_window") is not True:
                row["gaps"].add("requested_window_unresolved")
            elif leg.get("interpretation") == "unresolved" or _uint(leg.get("raw_units")) is None:
                row["gaps"].add("account_change_unresolved")
            else:
                row["units"] += int(leg["raw_units"]) * (int(leg.get("destination") == owner) - int(leg.get("source") == owner))
            if leg["asset"]["native"] and version.get("fee_status") != "complete":
                row["gaps"].add("fee_components_unavailable")
            for observation in version.get("observations", []):
                if observation.get("account") == owner and observation.get("asset") == leg["asset"]:
                    sample = {**observation, "transaction": transaction["signature"], "time": version.get("block_time")}
                    if sample not in row["samples"]:
                        row["samples"].append(sample)
    result = []
    for row in rows.values():
        samples = sorted(row.pop("samples"), key=lambda value: (value["pre_block"], value["post_block"]))
        reasons = row.pop("gaps")
        if archive["coverage"]["retrieval"] != "complete":
            reasons.add("retrieval_incomplete")
        if archive.get("research_endpoints"):
            reasons.add("external_ownership_unasserted")
        opening = _uint(samples[0].get("pre_raw_units")) if samples else None
        closing = _uint(samples[-1].get("post_raw_units")) if samples else None
        if len({sample["post_block"] for sample in samples}) != len(samples):
            reasons.add("block_transaction_order_unresolved")
        if opening is None:
            reasons.add("opening_quantity_unknown")
        if closing is None:
            reasons.add("closing_quantity_unknown")
        if row["decimals"] is None:
            reasons.add("token_decimals_unavailable")
        units = row.pop("units")
        discrepancy = closing - opening - units if opening is not None and closing is not None and not reasons else None
        row.update(opening=_quantity(opening, row["decimals"]), closing=_quantity(closing, row["decimals"]),
                   settled_change=_quantity(units, row["decimals"]), known_subtotal_raw_units=str(units),
                   expected_closing=_quantity(opening + units, row["decimals"]) if opening is not None and discrepancy is not None else None,
                   discrepancy=_quantity(discrepancy, row["decimals"]),
                   status="unknown" if discrepancy is None else "matched" if discrepancy == 0 else "discrepancy",
                   scope="observed_block_boundaries", requested_interval_status="unresolved",
                   opening_snapshot={"block_number": samples[0]["pre_block"], "block_hash": samples[0]["pre_block_hash"], "position": "end_of_block"} if samples else None,
                   closing_snapshot={"block_number": samples[-1]["post_block"], "block_hash": samples[-1]["post_block_hash"], "position": "end_of_block"} if samples else None,
                   reasons=sorted(reasons | {"requested_boundary_snapshots_unavailable", "basis_unknown"}))
        result.append(row)
    return result


async def collect_evm_history(
    owner: str, *, chain: str, source_identity: str, since: datetime | None = None,
    until: datetime | None = None, start_block: int = 0, end_block: int | None = None,
    supplied_accounts: list[dict] | None = None, state: dict | None = None,
    reobserve: bool = False, budget: RequestBudget | None = None, deadline: float | None = None,
    research: bool = False, policy: dict | None = None,
    byte_limit: int | None = None,
) -> dict:
    """Read one declared address, retaining pending streams and canonical versions."""
    if chain not in _NETWORKS or _hex(owner, 20) is None:
        raise ValueError("Unsupported EVM network or address")
    owner = owner.lower()
    since = since.replace(tzinfo=timezone.utc) if since and since.tzinfo is None else since
    until = until.replace(tzinfo=timezone.utc) if until and until.tzinfo is None else until
    since, until = since.astimezone(timezone.utc) if since else None, until.astimezone(timezone.utc) if until else None
    if (since and until and since > until) or _uint(start_block, 64) is None or (end_block is not None and (_uint(end_block, 64) is None or start_block > end_block)):
        raise ValueError("Invalid inclusive history bounds")
    if supplied_accounts:
        raise ValueError("EVM history reads one declared address; additional owned inventory is unsupported")
    settings = onchain.get_settings()
    policy = copy.deepcopy(policy if policy is not None else getattr(settings, "evm_history_policies", {}).get(chain, {}))
    configuration = evm_source_identity(chain, policy)
    requested = {"since": since.isoformat() if since else None, "until": until.isoformat() if until else None,
                 "start_block": start_block, "end_block": end_block, "commitment": "finalized", "research": research}
    binding: dict[str, Any] = {"version": ARCHIVE_VERSION, "decoder_version": DECODER_VERSION, "chain": chain,
               "owner": owner, "source_identity": source_identity, "configuration": configuration, "requested": requested}
    archive: dict[str, Any] = copy.deepcopy(state) if state else {**binding, "anchor": None, "payloads": {}, "bytes": 0,
             "inventory": {} if research else {owner: {"address": owner, "kind": "owner", "discoveries": [{"source": "workspace_connection_ownership_assertion"}], "ownership": []}},
             "research_endpoints": [owner] if research else [], "streams": {}, "transactions": {}, "snapshots": [],
             "gaps": [], "unsearched_candidates": [], "supplied_accounts": []}
    if any(archive.get(key) != value for key, value in binding.items()):
        raise ValueError("History source, configuration, decoder, owner or bounds changed; restart collection")
    archive["gaps"] = []
    if reobserve:
        archive["streams"] = {}  # Re-read indexed execution facts as well as receipts.
    archive["interpretation_policy"] = {key: copy.deepcopy(policy.get(key, default))
                                        for key, default in (("operator_fee_forks", []), ("standard_tokens", {}))}
    archive["limits"] = {"seconds": TIME_BUDGET_SECONDS, "attempts": MAX_ATTEMPTS, "concurrency": 1,
                         "pages_per_address": MAX_HISTORY_PAGES, "accounts": 1, "tokens": MAX_TOKENS,
                         "token_accounts": MAX_TOKEN_ACCOUNTS, "transactions": MAX_TRANSACTIONS,
                         "archive_bytes": MAX_ARCHIVE_BYTES, "payload_bytes": MAX_PAYLOAD_BYTES, "total_bytes": MAX_TOTAL_BYTES}
    budget = budget if budget is not None else RequestBudget(max_attempts=MAX_ATTEMPTS)
    initial_attempts = budget.attempts
    budget.max_attempts = min(budget.max_attempts, initial_attempts + MAX_ATTEMPTS)
    deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + TIME_BUDGET_SECONDS)
    if byte_limit is not None and (type(byte_limit) is not int or byte_limit < 0):
        raise ValueError("Invalid history byte allowance")
    total_bytes = min(MAX_TOTAL_BYTES, byte_limit if byte_limit is not None else MAX_TOTAL_BYTES)
    if total_bytes < len(json.dumps(archive).encode()) + 16384:
        raise ValueError("History byte allowance cannot retain collection metadata")
    archive["limits"]["total_bytes"] = total_bytes
    reads = _Reads(archive, budget, deadline, total_bytes, replay=bool(state and not reobserve))
    network = onchain.CHAINS[chain]
    endpoint = onchain.rpc_url(network)
    api_key = settings.etherscan_api_key
    pending: list[str] = []
    processed: set[str] = set()

    def discover(reference, stream, ref, *, row=None, log=None):
        reference = _hex(reference, 32)
        if reference is None:
            stream["gaps"].append("malformed_transaction_reference")
            return
        if time.monotonic() >= deadline:
            raise OnchainDeadlineExceeded()
        # The full discovery page is retained before copying rows into pending
        # transaction metadata; its cursor stays put when this allowance stops.
        if len(json.dumps(archive).encode()) + len(json.dumps(row or log).encode()) + 18432 > total_bytes:
            raise _CollectionStopped("transaction_metadata_byte_limit")
        key = chain + ":" + reference
        if key not in archive["transactions"] and len(archive["transactions"]) >= MAX_TRANSACTIONS:
            raise _CollectionStopped("transaction_limit")
        transaction = archive["transactions"].setdefault(key, {"signature": reference, "transaction_ref": reference,
                       "discovery_refs": [], "versions": [], "canonical_version": None,
                       "index_rows": [], "internal_rows": [], "discovered_logs": []})
        if ref not in transaction["discovery_refs"]:
            transaction["discovery_refs"].append(ref)
        field = "internal_rows" if stream["kind"] == "internal" else "index_rows"
        if row is not None and row not in transaction[field]:
            transaction[field].append(row)
            if transaction["versions"]:
                transaction["retrieval_gap"] = "new_discovery_requires_replay"
        if log is not None and log not in transaction["discovered_logs"]:
            transaction["discovered_logs"].append(log)
            if transaction["versions"]:
                transaction["retrieval_gap"] = "new_discovery_requires_replay"
        if key not in pending:
            pending.append(key)

    async def rpc(client, method, params, **kwargs):
        return await reads.read(client, endpoint, method, params, **kwargs)

    async def optional(client, method, params, gaps, refs):
        try:
            value, ref = await rpc(client, method, params)
            refs.append(ref)
            return value
        except (OnchainDeadlineExceeded, OnchainRequestLimitExceeded, _CollectionStopped):
            raise
        except Exception:
            gaps.append(method + "_unavailable")
            return None

    async def tokens(client, receipt, block, gaps, refs):
        result: dict[str, Any] = {}
        logs = receipt.get("logs") if isinstance(receipt.get("logs"), list) else []
        parsed = [item for log in logs if (item := _log(log)) is not None]
        number = _uint(block.get("number"))
        if number is None or number == 0:
            return result
        previous = await optional(client, "eth_getBlockByNumber", [hex(number - 1), False], gaps, refs) if parsed else None
        previous_hash = _hex(previous.get("hash"), 32) if isinstance(previous, dict) and _uint(previous.get("number")) == number - 1 else None
        block_hash = _hex(block.get("hash"), 32)
        contracts = sorted({item[0] for item in parsed})
        if len(contracts) > MAX_TOKENS:
            gaps.append("token_inventory_limit")
        for contract in contracts[:MAX_TOKENS]:
            evidence: dict[str, Any] = {"decimals": None, "semantics": "unresolved", "observations": [], "gaps": []}
            result[contract] = evidence
            token_gaps = evidence["gaps"]
            if not previous_hash or not block_hash:
                token_gaps.append("token_snapshot_anchor_unavailable")
                continue
            tags = [{"blockHash": previous_hash, "requireCanonical": True}, {"blockHash": block_hash, "requireCanonical": True}]
            decimals = await optional(client, "eth_call", [{"to": contract, "data": "0x313ce567"}, tags[1]], token_gaps, refs)
            evidence["decimals"] = _uint(decimals, 8) if _hex(decimals, 32) else None
            code = [await optional(client, "eth_getCode", [contract, tag], token_gaps, refs) for tag in tags]
            code_hashes = [hashlib.sha256(bytes.fromhex(value[2:])).hexdigest() if isinstance(value, str) and re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", value) else None for value in code]
            reviewed = policy.get("standard_tokens", {}).get(contract, {})
            evidence["reviewed_policy"] = copy.deepcopy(reviewed)
            evidence["observed_code_hashes"] = code_hashes
            if not (reviewed.get("source") and reviewed.get("non_proxy") is True and reviewed.get("semantics") == "standard_erc20"
                    and _uint(reviewed.get("from_block")) is not None and _uint(reviewed.get("to_block")) is not None
                    and reviewed["from_block"] <= number - 1 <= number <= reviewed["to_block"]
                    and code_hashes[0] == code_hashes[1] == reviewed.get("code_hash") and code_hashes[0]):
                token_gaps.append("token_historical_semantics_unreviewed")
            block_logs = await optional(client, "eth_getLogs", [{"blockHash": block_hash, "address": contract, "topics": [TRANSFER_TOPIC]}], token_gaps, refs)
            block_events = [_log(log) for log in block_logs] if isinstance(block_logs, list) else []
            if not isinstance(block_logs, list) or any(event is None for event in block_events):
                token_gaps.append("token_block_logs_unavailable")
            applicable = [item for item in parsed if item[0] == contract]
            accounts = sorted({account for item in applicable for account in item[1:3] if account != ZERO})
            if len(accounts) > MAX_TOKEN_ACCOUNTS:
                token_gaps.append("token_account_limit")
            for account in accounts[:MAX_TOKEN_ACCOUNTS]:
                related = [log for log in block_logs or [] if (event := _log(log)) and account in event[1:3]] if isinstance(block_logs, list) else []
                if (any(_hex(log.get("blockHash"), 32) != block_hash or log.get("removed") is True for log in related)
                    or {log.get("transactionHash") for log in related} != {receipt.get("transactionHash")}):
                    token_gaps.append("token_block_transaction_ambiguity")
                expected_logs = [log for log in logs if (event := _log(log)) and event[0] == contract and account in event[1:3]]
                if {_digest(log) for log in related} != {_digest(log) for log in expected_logs}:
                    token_gaps.append("token_receipt_block_log_disagreement")
                values = [await optional(client, "eth_call", [{"to": contract, "data": "0x70a08231" + account[2:].zfill(64)}, tag], token_gaps, refs) for tag in tags]
                pre, post = [_uint(value) if _hex(value, 32) else None for value in values]
                delta = sum(item[3] * (int(item[2] == account) - int(item[1] == account)) for item in applicable)
                if pre is None or post is None or post - pre != delta:
                    token_gaps.append("token_balance_log_mismatch")
                evidence["observations"].append({"account": account, "scope": "block", "pre_block": number - 1, "post_block": number,
                                                 "pre_block_hash": previous_hash, "post_block_hash": block_hash,
                                                 "pre_raw_units": str(pre) if pre is not None else None, "post_raw_units": str(post) if post is not None else None,
                                                 "pre_quantity": _quantity(pre, evidence["decimals"]), "post_quantity": _quantity(post, evidence["decimals"]),
                                                 "delta_raw_units": str(post - pre) if pre is not None and post is not None else None,
                                                 "decimals": evidence["decimals"]})
            if not token_gaps:
                evidence["semantics"] = "corroborated_standard_erc20"
        return result

    async def transaction_read(client, key):
        transaction = archive["transactions"][key]
        if key in processed or (transaction["versions"] and not reobserve and not transaction.get("retrieval_gap")):
            return
        reference = transaction["signature"]
        gaps, refs = [], list(transaction["discovery_refs"])
        tx = await optional(client, "eth_getTransactionByHash", [reference], gaps, refs)
        receipt = await optional(client, "eth_getTransactionReceipt", [reference], gaps, refs)
        if not isinstance(tx, dict) or not isinstance(receipt, dict):
            transaction["retrieval_gap"] = "transaction_or_receipt_unavailable"
            return  # A null re-observation never reverses retained positive evidence.
        block_number = _uint(receipt.get("blockNumber"))
        block = await optional(client, "eth_getBlockByNumber", [hex(block_number), False], gaps, refs) if block_number is not None else None
        trace = await optional(client, "debug_traceTransaction", [reference, {"tracer": "callTracer"}], gaps, refs)
        token_evidence = await tokens(client, receipt, block or {}, gaps, refs)
        when = _moment((block or {}).get("timestamp"))
        in_window = None if when is None else (since is None or when >= since) and (until is None or when <= until)
        if block_number is None:
            in_window = None
        elif block_number < start_block or block_number > archive["scan_end_block"]:
            in_window = False
        if in_window is not True:
            gaps.append("requested_window_unresolved" if in_window is None else "outside_requested_window")
        bundle = {"transaction": tx, "receipt": receipt, "block": block, "trace": trace, "tokens": token_evidence,
                  "index_rows": transaction["index_rows"], "internal_rows": transaction["internal_rows"],
                  "discovered_logs": transaction["discovered_logs"], "finality": archive["anchor"],
                  "interpretation_policy": archive["interpretation_policy"],
                  "in_requested_window": in_window, "gaps": gaps, "source_refs": refs}
        digest = reads.retain("evm_history_transaction", [chain, reference], {"result": bundle}, "derived_bundle")
        version = decode_evm_transaction(reference, bundle, chain=chain, owner=owner, payload_digest=digest, policy=policy, research=research)
        if len(json.dumps(archive).encode()) + len(json.dumps(version).encode()) + 16384 > total_bytes:
            transaction["retrieval_gap"] = "decoded_projection_byte_limit"
            raise _CollectionStopped("decoded_projection_byte_limit")
        previous = next((v for v in transaction["versions"] if v["version_id"] == transaction["canonical_version"]), None)
        previous_indexed = previous.get("indexed_internal_fingerprints", {}) if previous else {}
        indexed_changed = any(previous_indexed[key] != value for key, value in version["indexed_internal_fingerprints"].items() if key in previous_indexed)
        if not any(item["version_id"] == version["version_id"] for item in transaction["versions"]):
            transaction["versions"].append(version)
        if previous and (indexed_changed or previous.get("evidence_fingerprint") != version["evidence_fingerprint"]
                         or (previous.get("internal_evidence_fingerprint") and version.get("internal_evidence_fingerprint")
                             and previous["internal_evidence_fingerprint"] != version["internal_evidence_fingerprint"])):
            transaction["revision_status"] = "conflicting_or_reorganized"
            transaction["canonical_version"] = None
        elif previous and previous["settlement"] == "settled" and version["settlement"] != "settled":
            transaction["revision_status"] = "conflicting_or_reorganized"
            transaction["canonical_version"] = None
        elif transaction.get("revision_status") == "conflicting_or_reorganized":
            transaction["canonical_version"] = None
        else:
            transaction["canonical_version"] = version["version_id"]
            transaction["revision_status"] = "current"
        transaction.pop("retrieval_gap", None)
        processed.add(key)

    timeout = asyncio.timeout(max(0, deadline - time.monotonic()))
    try:
        async with timeout, onchain.session() as client:
            head_gaps, head_refs = [], []
            if archive["anchor"] is None or reobserve:
                latest = await optional(client, "eth_getBlockByNumber", ["latest", False], head_gaps, head_refs)
                finalized = await optional(client, "eth_getBlockByNumber", ["finalized", False], head_gaps, head_refs)
                if not isinstance(latest, dict) or _uint(latest.get("number")) is None or not _hex(latest.get("hash"), 32):
                    raise _CollectionStopped("anchor_unavailable")
                latest_number = _uint(latest["number"])
                assert latest_number is not None
                finalized_number = _uint(_mapping(finalized).get("number"))
                if finalized_number is None or not _hex(_mapping(finalized).get("hash"), 32):
                    head_gaps.append("finalized_anchor_unavailable")
                    finalized = {}
                elif finalized_number > latest_number or (finalized_number == latest_number and finalized["hash"] != latest["hash"]):
                    head_gaps.append("finalized_anchor_conflict")
                    finalized = {}
                anchor: dict[str, Any] = {"number": latest_number, "hash": latest["hash"], "finalized": finalized if isinstance(finalized, dict) else {},
                          "commitment": "finalized", "source_refs": head_refs, "l1_corroborated": False}
                if chain == "base":
                    rollup = policy.get("rollup_rpc_url")
                    if rollup:
                        try:
                            status, ref = await reads.read(client, rollup, "optimism_syncStatus", [], source="rollup")
                            head_refs.append(ref)
                            if isinstance(status, dict):
                                anchor["l1_context"] = status
                                l2, l1 = status.get("finalized_l2") or {}, status.get("finalized_l1") or {}
                                origin = l2.get("l1origin") or {}
                                origin_number, l1_number = _uint(origin.get("number")), _uint(l1.get("number"))
                                anchor["l1_corroborated"] = bool(isinstance(finalized, dict) and _hex(l2.get("hash"), 32) == _hex(finalized.get("hash"), 32)
                                    and _uint(l2.get("number")) == _uint(finalized.get("number")) and _hex(l1.get("hash"), 32)
                                    and origin_number is not None and l1_number is not None
                                    and origin_number <= l1_number and _hex(origin.get("hash"), 32))
                        except (OnchainDeadlineExceeded, OnchainRequestLimitExceeded, _CollectionStopped):
                            raise
                        except Exception:
                            head_gaps.append("base_l1_finality_unavailable")
                if archive["anchor"] and archive["anchor"] != anchor:
                    archive.setdefault("anchor_versions", []).append(archive["anchor"])
                archive["anchor"] = anchor
                archive.setdefault("scan_end_block", min(end_block if end_block is not None else latest_number, latest_number))
            else:
                pinned, _ = await rpc(client, "eth_getBlockByNumber", [hex(archive["anchor"]["number"]), False])
                if not isinstance(pinned, dict) or _hex(pinned.get("hash"), 32) != _hex(archive["anchor"]["hash"], 32):
                    raise _CollectionStopped("anchor_changed_reobserve_required")
            archive["gaps"].extend(head_gaps)
            scan_end = archive["scan_end_block"]
            if end_block is not None and end_block > archive["anchor"]["number"]:
                archive["gaps"].append("requested_end_block_unreached")
            if start_block > archive["anchor"]["number"]:
                archive["gaps"].append("requested_start_block_unreached")
            for kind in ("ordinary", "internal", "logs_out", "logs_in"):
                archive["streams"].setdefault(kind, {"kind": kind, "cursor": 1 if api_key else {}, "pages_examined": 0,
                    "exhausted": False, "stop_reason": None, "gaps": [], "page_refs": [], "seen_cursors": [],
                    "pending_ranges": [[start_block, scan_end]] if kind.startswith("logs") and start_block <= scan_end else [],
                    "oldest_at": None, "newest_at": None, "unknown_timestamps": 0})
            for kind, stream in archive["streams"].items():
                if stream["exhausted"]:
                    continue
                stream["stop_reason"], stream["gaps"] = None, []
                try:
                    for _ in range(MAX_HISTORY_PAGES):
                        if kind.startswith("logs"):
                            if not stream["pending_ranges"]:
                                stream["exhausted"] = True
                                break
                            lower, upper = stream["pending_ranges"][0]
                            topic = "0x" + owner[2:].zfill(64)
                            topics = [TRANSFER_TOPIC, topic] if kind == "logs_out" else [TRANSFER_TOPIC, None, topic]
                            try:
                                page, ref = await rpc(client, "eth_getLogs", [{"fromBlock": hex(lower), "toBlock": hex(upper), "topics": topics}])
                            except (OnchainDeadlineExceeded, OnchainRequestLimitExceeded, _CollectionStopped):
                                raise
                            except Exception:
                                if lower < upper:
                                    middle = (lower + upper) // 2
                                    stream["pending_ranges"][:1] = [[lower, middle], [middle + 1, upper]]
                                    stream["pages_examined"] += 1
                                    continue
                                stream["stop_reason"] = "log_range_unavailable"
                                break
                            if not isinstance(page, list):
                                stream["stop_reason"] = "malformed_log_page"
                                break
                            stream["page_refs"].append(ref)
                            for log in page:
                                parsed = _log(log)
                                if parsed is None or _uint(log.get("blockNumber")) is None or not lower <= _uint(log["blockNumber"]) <= upper or owner not in parsed[1:3]:
                                    stream["gaps"].append("malformed_or_out_of_scope_log")
                                    continue
                                discover(log.get("transactionHash"), stream, ref, log=log)
                            stream["pending_ranges"].pop(0)
                            if not stream["pending_ranges"]:
                                stream["exhausted"] = True
                        else:
                            if api_key:
                                action = "txlist" if kind == "ordinary" else "txlistinternal"
                                params = {"chainid": network.explorer_chain_id, "module": "account", "action": action, "address": owner,
                                          "startblock": start_block, "endblock": scan_end, "page": stream["cursor"], "offset": onchain.EVM_HISTORY_PAGE, "sort": "asc"}
                                response, ref = await reads.read(client, onchain.ETHERSCAN_V2_URL + "\0" + api_key, action, params, rpc=False,
                                                                source="etherscan", url=onchain.ETHERSCAN_V2_URL, private_params={"apikey": api_key})
                                page = response.get("result") if isinstance(response, dict) and str(response.get("status")) == "1" else [] if onchain._etherscan_empty(response) else None
                                following = stream["cursor"] + 1 if isinstance(page, list) and len(page) >= onchain.EVM_HISTORY_PAGE else None
                            elif network.token_index_url:
                                path = "transactions" if kind == "ordinary" else "internal-transactions"
                                base = network.token_index_url.rstrip("/")
                                response, ref = await reads.read(client, base, f"addresses/{owner}/{path}", stream["cursor"], rpc=False, source="blockscout",
                                                                url=f"{base}/api/v2/addresses/{owner}/{path}")
                                page = response.get("items") if isinstance(response, dict) else None
                                following = response.get("next_page_params", "missing") if isinstance(response, dict) else "missing"
                            else:
                                stream["stop_reason"] = "address_index_unavailable"
                                break
                            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page) or (following is not None and not isinstance(following, (dict, int))):
                                stream["stop_reason"] = "malformed_index_page"
                                break
                            stream["page_refs"].append(ref)
                            for row in page:
                                row_number = _uint(row.get("blockNumber", row.get("block_number")))
                                if row_number is not None and not start_block <= row_number <= scan_end:
                                    continue
                                stamp = _index_time(row)
                                if stamp is None:
                                    stream["unknown_timestamps"] += 1
                                else:
                                    stream["oldest_at"] = min(stream["oldest_at"] or stamp.isoformat(), stamp.isoformat())
                                    stream["newest_at"] = max(stream["newest_at"] or stamp.isoformat(), stamp.isoformat())
                                discover(row.get("hash") or row.get("transaction_hash"), stream, ref, row=row)
                            if following is None:
                                stream["exhausted"] = True
                            else:
                                marker = _digest(following)
                                if not following or marker in stream["seen_cursors"] or following == stream["cursor"]:
                                    stream["stop_reason"] = "repeated_cursor"
                                    break
                                stream["seen_cursors"].append(marker)
                                stream["cursor"] = following
                        stream["pages_examined"] += 1
                        if stream["exhausted"]:
                            break
                    if stream["exhausted"]:
                        stream["stop_reason"] = "provider_exhausted"
                    elif stream["stop_reason"] is None:
                        stream["stop_reason"] = "page_limit"
                except (OnchainDeadlineExceeded, OnchainRequestLimitExceeded, _CollectionStopped):
                    raise
                except Exception as exc:
                    stream["stop_reason"] = interruption_code(exc)
            for key in archive["transactions"]:
                await transaction_read(client, key)
    except OnchainRequestLimitExceeded:
        archive["gaps"].append("request_limit")
    except OnchainDeadlineExceeded:
        archive["gaps"].append("deadline_exceeded")
    except asyncio.CancelledError:
        # Sequential reads leave no child tasks to drain; return retained progress.
        archive["gaps"].append("cancelled")
    except _CollectionStopped as exc:
        archive["gaps"].append(str(exc))
    except TimeoutError:
        if not timeout.expired():
            raise
        archive["gaps"].append("deadline_exceeded")
    except Exception as exc:
        archive["gaps"].append(interruption_code(exc))
    archive["limits"]["attempts_used"] = budget.attempts - initial_attempts
    versions = [version for transaction in archive["transactions"].values() for version in transaction["versions"]
                if version["version_id"] == transaction["canonical_version"]]
    unresolved = any(transaction["canonical_version"] is None for transaction in archive["transactions"].values())
    pending_payloads = any(not transaction["versions"] or transaction.get("retrieval_gap") for transaction in archive["transactions"].values())
    for stream in archive["streams"].values():
        page_refs = set(stream["page_refs"])
        stream["payload_gaps"] = [transaction["signature"] for transaction in archive["transactions"].values()
                                  if (not transaction["versions"] or transaction.get("retrieval_gap"))
                                  and page_refs.intersection(transaction["discovery_refs"])]
    archive["resumable"] = pending_payloads or not archive["streams"] or any(not stream["exhausted"] for stream in archive["streams"].values())
    archive["coverage"] = {"inventory": "declared_address_only", "retrieval": "partial" if archive["resumable"] or archive["gaps"] or any(stream["gaps"] for stream in archive["streams"].values()) else "complete",
                           "interpretation": "partial" if unresolved or any(version["gaps"] for version in versions) else "complete",
                           "settlement": "partial" if unresolved or any(version["settlement"] != "settled" for version in versions)
                           or not _mapping(archive.get("anchor")).get("finalized")
                           or (chain == "base" and not _mapping(archive.get("anchor")).get("l1_corroborated")) else "complete"}
    archive["gaps"] = sorted(set(archive["gaps"]))
    archive["capabilities"] = {"ordinary": "address_index", "internal": "indexed_paths_or_call_tracer", "erc20": "receipt_and_transfer_topics",
                               "token_economics": "reviewed_historical_code_and_balances", "nft": "unsupported", "snapshots": "token_block_boundaries"}
    archive["reconciliation"] = reconcile_evm_history(archive)
    if len(json.dumps(archive).encode()) > total_bytes:
        archive["reconciliation"] = []
        archive["gaps"].append("reconciliation_byte_limit")
        archive["coverage"]["interpretation"] = "partial"
    return archive
