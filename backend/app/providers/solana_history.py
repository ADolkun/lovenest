"""Owned Solana evidence, independent of priced holdings and financial imports.

The archive is the decoder input: balance observations are not extra movements.
Ownership mappings describe particular observations, never inferred lifetimes.
RPC structures: https://solana.com/docs/rpc/json-structures
Token fees: https://solana.com/docs/tokens/extensions/transfer-fees
Jupiter route IDL: https://github.com/jup-ag/instruction-parser/blob/main/src/idl/jupiter.ts
"""

from __future__ import annotations

import asyncio
from asyncio import CancelledError
import copy
import hashlib
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.providers import onchain
from app.providers.onchain_reads import MAX_HISTORY_PAGES, interruption_code
from app.providers.onchain_transport import (
    OnchainDeadlineExceeded,
    OnchainRequestLimitExceeded,
    RequestBudget,
    request_json,
)

ARCHIVE_VERSION = "owned-history-1"
DECODER_VERSION = "solana-1"
MAX_ACCOUNTS = 128
MAX_INVENTORY_ENTRIES = 512
MAX_ARCHIVE_BYTES = 24 * 1024 * 1024
MAX_TOTAL_BYTES = 30 * 1024 * 1024
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_ATTEMPTS = 600
TIME_BUDGET_SECONDS = 45
SYSTEM_PROGRAM = "11111111111111111111111111111111"
WSOL_MINT = "So11111111111111111111111111111111111111112"
NATIVE_MINTS = {onchain.SOLANA_TOKEN_PROGRAMS[0]: WSOL_MINT,
                onchain.SOLANA_TOKEN_PROGRAMS[1]: "9pan9bMn5HatX4EJdBwg9VgCa7Uz5HL8N1m5D3NdXejP"}
JUPITER_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ROUTE_DISCRIMINATOR = hashlib.sha256(b"global:route").digest()[:8]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        result = int(value)
        return result if 0 <= result <= 2**64 - 1 else None
    except ValueError:
        return None


def _quantity(units: int | None, decimals: int | None) -> str | None:
    return format(Decimal(units).scaleb(-decimals), "f") if units is not None and decimals is not None else None


def _asset(mint: str | None = None, program: str | None = None) -> dict:
    return {"chain": "solana", "native": mint is None, "mint": mint, "token_program": program}


def _timestamp(value: Any) -> str | None:
    parsed = onchain._history_time(value)
    return parsed.isoformat() if parsed else None


def _instructions(message: dict, meta: dict) -> list[tuple[str, dict]]:
    result = []
    for index, instruction in enumerate(message.get("instructions") or []):
        result.append((f"instruction:{index}", instruction))
        for group in meta.get("innerInstructions") or []:
            if isinstance(group, dict) and group.get("index") == index:
                for inner, child in enumerate(group.get("instructions") or []):
                    result.append((f"instruction:{index}/inner:{inner}", child))
    return result


def _route(instruction: dict) -> bool:
    encoded = instruction.get("data")
    if not isinstance(encoded, str) or not encoded or len(encoded) > 20000:
        return False
    number = 0
    for character in encoded:
        if character not in _B58:
            return False
        number = number * 58 + _B58.index(character)
    raw = b"\0" * (len(encoded) - len(encoded.lstrip("1"))) + number.to_bytes((number.bit_length() + 7) // 8, "big")
    return len(raw) >= 8 and raw[:8] == _ROUTE_DISCRIMINATOR


def decode_solana_transaction(
    signature: str, payload: dict, *, payload_digest: str, owner: str,
    confirmation_status: str | None, anchor_slot: int | None,
) -> dict:
    """Pure replay of one retained getTransaction result, with no RPC or prices."""
    gaps: list[str] = []
    meta = payload.get("meta")
    transaction = payload.get("transaction")
    message = transaction.get("message", {}) if isinstance(transaction, dict) else {}
    if not isinstance(message, dict):
        gaps.append("malformed_transaction_message")
        message = {}
    execution = "unknown" if not isinstance(meta, dict) or "err" not in meta else "success" if meta["err"] is None else "failed"
    meta = dict(meta) if isinstance(meta, dict) else {}
    message = dict(message)
    for mapping, fields in ((message, ("accountKeys", "instructions")),
                            (meta, ("preBalances", "postBalances", "preTokenBalances", "postTokenBalances", "innerInstructions"))):
        for field in fields:
            if field in mapping and not isinstance(mapping[field], list):
                gaps.append("malformed_payload_structure")
                mapping[field] = []
    slot = _integer(payload.get("slot"))
    settled = confirmation_status == "finalized" and slot is not None and anchor_slot is not None and slot <= anchor_slot
    settlement = "settled" if settled and execution != "unknown" else "provisional"
    keys = [entry.get("pubkey") if isinstance(entry, dict) else entry for entry in message.get("accountKeys", [])]
    # jsonParsed includes lookup-table keys in accountKeys; raw JSON appends them.
    if not any(isinstance(entry, dict) and entry.get("source") == "lookupTable" for entry in message.get("accountKeys", [])):
        loaded = meta.get("loadedAddresses") or {}
        if isinstance(loaded, dict):
            for field in ("writable", "readonly"):
                if isinstance(loaded.get(field), list):
                    keys.extend(loaded[field])
                elif loaded.get(field) is not None:
                    gaps.append("unresolved_loaded_keys")
    if execution == "unknown":
        gaps.append("missing_execution_metadata")
    if isinstance(payload.get("version"), bool) or payload.get("version", "legacy") not in ("legacy", 0):
        gaps.append("unsupported_transaction_version")
    if slot is not None and anchor_slot is not None and slot > anchor_slot:
        gaps.append("outside_pinned_anchor")
    if message.get("addressTableLookups") and not meta.get("loadedAddresses") and not any(
        isinstance(entry, dict) and entry.get("source") == "lookupTable" for entry in message.get("accountKeys", [])
    ):
        gaps.append("unresolved_loaded_keys")
    block_time = _timestamp(payload.get("blockTime"))
    if block_time is None:
        gaps.append("unknown_timestamp")
    result: dict[str, Any] = {
        "payload_digest": payload_digest,
        "version_id": _digest({"payload": payload, "confirmation_status": confirmation_status}),
        "slot": slot, "block_time": block_time, "original_timestamp": payload.get("blockTime"),
        "time_precision": "second" if block_time else "unknown", "execution": execution,
        "confirmation_status": confirmation_status, "settlement": settlement,
        "legs": [], "observations": [], "ownership": [], "gaps": gaps, "relationships": [],
        "decoder_version": DECODER_VERSION,
    }
    identities = transaction.get("signatures") if isinstance(transaction, dict) else None
    if not isinstance(identities, list) or not identities or identities[0] != signature:
        gaps.append("transaction_identity_mismatch" if identities else "missing_transaction_identity")
        result["settlement"] = "provisional"
        return result
    tokens: dict[str, dict] = {}
    for side, field in (("pre", "preTokenBalances"), ("post", "postTokenBalances")):
        for index, balance in enumerate(meta.get(field) or []):
            if not isinstance(balance, dict):
                gaps.append("malformed_token_balance")
                continue
            account_index = _integer(balance.get("accountIndex"))
            if account_index is None or account_index >= len(keys) or not isinstance(keys[account_index], str):
                gaps.append("unresolved_token_account_index")
                continue
            account = keys[account_index]
            amount = balance.get("uiTokenAmount") or {}
            if not isinstance(amount, dict):
                gaps.append("malformed_token_balance")
                continue
            program = balance.get("programId")
            token = tokens.setdefault(account, {"account": account, "owners": set()})
            for key, value in (("mint", balance.get("mint")), ("program", program), ("decimals", _integer(amount.get("decimals")))):
                if key in token and token[key] != value:
                    token["conflict"] = True
                token[key] = value
            token[side] = _integer(amount.get("amount"))
            if isinstance(balance.get("owner"), str) and balance["owner"]:
                token["owners"].add(balance["owner"])
            result["ownership"].append({
                "address": account, "owner": balance.get("owner"), "slot": slot, "side": side,
                "mint": balance.get("mint"), "token_program": program,
                "source": "token_balance", "path": f"meta.{field}.{index}", "payload_digest": payload_digest,
                "period": "observation_only",
            })
    for account, token in tokens.items():
        token["owner"] = next(iter(token["owners"])) if len(token["owners"]) == 1 else None
        if token["owner"] is None:
            gaps.append("unresolved_historical_owner")
        if len(token["owners"]) > 1:
            token["conflict"] = True
            gaps.append("changed_token_owner")
        if token.get("program") not in onchain.SOLANA_TOKEN_PROGRAMS or not token.get("mint"):
            token["conflict"] = True
            gaps.append("unresolved_token_identity")
        if token.get("mint") == NATIVE_MINTS.get(token.get("program")) and token.get("decimals") != 9:
            token["conflict"] = True
            gaps.append("invalid_native_token_decimals")

    def observe(account: str, asset: dict, pre: int | None, post: int | None, decimals: int | None, observed_owner: str | None) -> None:
        result["observations"].append({
            "account": account, "owner": observed_owner, "asset": asset,
            "pre_raw_units": str(pre) if pre is not None else None,
            "post_raw_units": str(post) if post is not None else None,
            "pre_quantity": _quantity(pre, decimals), "post_quantity": _quantity(post, decimals),
            "delta_raw_units": str(post - pre) if pre is not None and post is not None else None,
            "decimals": decimals, "settlement": settlement, "payload_digest": payload_digest,
            "measure": "balance_delta_not_additive_to_legs",
        })

    pre_balances, post_balances = meta.get("preBalances") or [], meta.get("postBalances") or []
    if len(pre_balances) != len(keys) or len(post_balances) != len(keys):
        gaps.append("missing_native_balances")
    for index, account in enumerate(keys):
        if not isinstance(account, str):
            gaps.append("unresolved_account_key")
            continue
        pre = _integer(pre_balances[index]) if index < len(pre_balances) else None
        post = _integer(post_balances[index]) if index < len(post_balances) else None
        observe(account, _asset(), pre, post, 9, owner if account == owner else None)
    for account, token in tokens.items():
        decimals = token.get("decimals")
        if decimals is not None and decimals > 255:
            decimals = None
            gaps.append("invalid_token_decimals")
        observe(account, _asset(token.get("mint"), token.get("program")), token.get("pre"), token.get("post"), decimals, token["owner"])

    def add_leg(path: str, role: str, source: str | None, destination: str | None, units: int, asset: dict, decimals: int, **extra: Any) -> dict:
        native = asset["native"]
        leg = {
            "key": f"solana:{signature}:{path}:{role}", "asset": asset, "role": role,
            "source": source, "destination": destination,
            "source_owner": (owner if source == owner else None) if native else tokens.get(source, {}).get("owner"),
            "destination_owner": (owner if destination == owner else None) if native else tokens.get(destination, {}).get("owner"),
            "raw_units": str(units), "quantity": _quantity(units, decimals), "decimals": decimals,
            "settlement": settlement, "derivation": {"payload_digest": payload_digest, "path": path},
            **extra,
        }
        result["legs"].append(leg)
        return leg

    fee = _integer(meta.get("fee"))
    if execution != "unknown" and fee is not None and keys and isinstance(keys[0], str):
        add_leg("meta.fee", "network_fee", keys[0], None, fee, _asset(), 9)
    elif execution != "unknown":
        gaps.append("missing_network_fee")
    if execution != "success" or "unsupported_transaction_version" in gaps or "unresolved_loaded_keys" in gaps:
        result["gaps"] = sorted(set(gaps))
        return result

    instructions = _instructions(message, meta)
    routes: list[str] = []
    closes: list[tuple[str, dict]] = []
    syncs: list[tuple[str, dict]] = []
    for path, instruction in instructions:
        if not isinstance(instruction, dict):
            gaps.append("malformed_instruction")
            continue
        program = instruction.get("programId")
        if program is None:
            index = _integer(instruction.get("programIdIndex"))
            program = keys[index] if index is not None and index < len(keys) else None
        if program == JUPITER_PROGRAM and _route(instruction):
            routes.append(path)
            continue
        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict) or not isinstance(parsed.get("info"), dict):
            # These programs carry no balance effects themselves.
            if program not in ("ComputeBudget111111111111111111111111111111", "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"):
                gaps.append("unsupported_instruction")
            continue
        kind, info = parsed.get("type"), parsed["info"]
        if program == SYSTEM_PROGRAM and kind in ("transfer", "transferWithSeed", "createAccount", "createAccountWithSeed"):
            source, destination = info.get("source"), info.get("destination", info.get("newAccount"))
            units = _integer(info.get("lamports"))
            if not isinstance(source, str) or not isinstance(destination, str) or source not in keys or destination not in keys or units is None:
                gaps.append("malformed_native_instruction")
                continue
            role = "account_funding" if kind.startswith("createAccount") else "principal"
            add_leg(path, role, source, destination, units, _asset(), 9)
        elif program in onchain.SOLANA_TOKEN_PROGRAMS:
            if kind in ("transfer", "transferChecked", "transferCheckedWithFee", "mintTo", "mintToChecked", "burn", "burnChecked"):
                source = info.get("source", info.get("account") if kind.startswith("burn") else None)
                destination = info.get("destination", info.get("account") if kind.startswith("mintTo") else None)
                if any(endpoint is not None and not isinstance(endpoint, str) for endpoint in (source, destination)):
                    gaps.append("unresolved_token_instruction")
                    continue
                related = [tokens[address] for address in (source, destination) if address in tokens]
                amount = info.get("tokenAmount") or {}
                if not isinstance(amount, dict):
                    gaps.append("unresolved_token_instruction")
                    continue
                units = _integer(amount.get("amount", info.get("amount")))
                mint = info.get("mint") or (related[0].get("mint") if related else None)
                decimals = _integer(amount.get("decimals"))
                if decimals is None and related:
                    decimals = related[0].get("decimals")
                if (units is None or decimals is None or decimals > 255 or not mint or not related
                    or any(token.get("conflict") or token.get("mint") != mint or token.get("program") != program or token.get("decimals") != decimals for token in related)
                    or (source is not None and source not in tokens) or (destination is not None and destination not in tokens)):
                    gaps.append("unresolved_token_instruction")
                    continue
                role = "mint" if kind.startswith("mintTo") else "burn" if kind.startswith("burn") else "principal"
                fee_amount = info.get("feeAmount")
                withheld = _integer(fee_amount.get("amount")) if kind == "transferCheckedWithFee" and isinstance(fee_amount, dict) else None if kind == "transferCheckedWithFee" else 0
                if withheld is None or withheld > units:
                    gaps.append("unresolved_token_transfer_fee")
                    continue
                leg = add_leg(path, role, source, destination, units - withheld, _asset(mint, program), decimals,
                              sender_debit_raw_units=str(units), receiver_credit_raw_units=str(units - withheld), withheld_fee_raw_units=str(withheld))
                if withheld:
                    add_leg(path, "token_transfer_fee", source, None, withheld, _asset(mint, program), decimals,
                            withheld_at=destination, related_leg=leg["key"])
            elif kind in ("initializeAccount", "initializeAccount2", "initializeAccount3"):
                if info.get("account") and info.get("owner"):
                    result["ownership"].append({"address": info["account"], "owner": info["owner"], "mint": info.get("mint"),
                                                "token_program": program, "slot": slot, "side": "created", "source": "instruction",
                                                "path": path, "payload_digest": payload_digest, "period": "observation_only"})
            elif kind == "closeAccount":
                closes.append((path, info))
            elif kind == "syncNative":
                syncs.append((path, info))
            elif kind in ("approve", "approveChecked", "revoke", "initializeImmutableOwner"):
                pass
            elif kind == "setAuthority":
                gaps.append("changed_token_owner")
            else:
                gaps.append("unsupported_token_extension_or_instruction")
        else:
            gaps.append("unsupported_instruction")

    instruction_order = {path: index for index, (path, _) in enumerate(instructions)}
    instruction_legs = list(result["legs"])

    def signed_units(legs: list[dict], account: str) -> int:
        return sum((int(leg["raw_units"]) if leg["destination"] == account else 0)
                   - (int(leg["raw_units"]) if leg["source"] == account else 0) for leg in legs)

    # Backing lamports remain native observations, never additional economic
    # SOL on top of the represented WSOL token units.
    for path, info in syncs:
        account = info.get("account")
        if not isinstance(account, str):
            gaps.append("unresolved_wrap")
            continue
        token = tokens.get(account, {})
        funding = [leg for leg in instruction_legs if leg["asset"]["native"] and leg["destination"] == account]
        token_legs = [leg for leg in instruction_legs if not leg["asset"]["native"] and account in (leg["source"], leg["destination"])]
        if (token.get("conflict") or token.get("mint") != NATIVE_MINTS.get(token.get("program")) or token.get("post") is None or not funding
            or sum(item.get("account") == account for _, item in syncs) != 1
            or any(instruction_order.get(leg["derivation"]["path"], -1) >= instruction_order[path] for leg in funding)):
            gaps.append("unresolved_wrap")
            continue
        pre = token.get("pre")
        created = any(leg["role"] == "account_funding" for leg in funding)
        if pre is None and not created:
            gaps.append("unresolved_wrap")
            continue
        # Final token balance includes transfers executed around SyncNative.
        # Remove those changes to isolate the newly represented backing SOL.
        token_change = signed_units(token_legs, account)
        wrapped = token["post"] - (pre or 0) - token_change
        total = sum(int(leg["raw_units"]) for leg in funding)
        index = keys.index(account)
        pre_lamports = _integer(pre_balances[index]) if index < len(pre_balances) else None
        post_lamports = _integer(post_balances[index]) if index < len(post_balances) else None
        rent = total - wrapped
        if (wrapped < 0 or wrapped > total or pre_lamports is None or post_lamports is None or post_lamports < token["post"]
            or pre_lamports + total + token_change != post_lamports
            or rent != (post_lamports - token["post"] if created else 0)
            or len({leg["source"] for leg in funding}) != 1):
            gaps.append("unresolved_wrap")
            continue
        for leg in funding:
            leg["role"] = "wrapped_backing"
            leg["non_additive"] = True
        source = funding[0]["source"]
        principal = add_leg(path, "wrap_native", source, None, wrapped, _asset(), 9)
        wrapped_leg = add_leg(path, "wrap_token", None, account, wrapped, _asset(token["mint"], token["program"]), 9)
        if rent:
            add_leg(path, "rent_deposit", source, account, rent, _asset(), 9)
        result["relationships"].append({"kind": "wrap", "legs": [principal["key"], wrapped_leg["key"]], "tax_treatment": "unknown"})

    for path, info in closes:
        account, destination = info.get("account"), info.get("destination")
        token = tokens.get(account, {})
        if not isinstance(account, str) or account not in keys or not isinstance(destination, str):
            gaps.append("unresolved_close_account")
            continue
        index = keys.index(account)
        pre = _integer(pre_balances[index]) if index < len(pre_balances) else None
        post = _integer(post_balances[index]) if index < len(post_balances) else None
        before_close = [leg for leg in instruction_legs if instruction_order.get(leg["derivation"]["path"], -1) < instruction_order[path]]
        native_legs = [leg for leg in before_close if leg["asset"]["native"]]
        token_legs = [leg for leg in before_close if not leg["asset"]["native"]]
        if (pre is None or post != 0 or sum(item.get("account") == account for _, item in closes) != 1
            or any(account in (leg["source"], leg["destination"]) and instruction_order.get(leg["derivation"]["path"], -1) >= instruction_order[path] for leg in instruction_legs)):
            gaps.append("unresolved_close_account")
            continue
        result["ownership"].append({"address": account, "owner": token.get("owner"), "slot": slot, "side": "closed", "source": "instruction",
                                    "mint": token.get("mint"), "token_program": token.get("program"), "path": path,
                                    "payload_digest": payload_digest, "period": "observation_only"})
        is_wrapped = token.get("mint") == NATIVE_MINTS.get(token.get("program"))
        if token.get("conflict"):
            gaps.append("unresolved_unwrap" if is_wrapped else "unresolved_close_account")
            continue
        starting_tokens = token.get("pre")
        token_change = signed_units(token_legs, account)
        wrap_legs = [leg for leg in result["legs"] if leg["role"] == "wrap_token" and leg["destination"] == account]
        if any(item.get("account") == account for _, item in syncs) and not wrap_legs:
            gaps.append("unresolved_unwrap")
            continue
        closing_tokens = starting_tokens + token_change + signed_units(wrap_legs, account) if starting_tokens is not None else None
        wrapped = closing_tokens if is_wrapped else 0
        closing_lamports = pre + signed_units(native_legs, account) + (token_change if is_wrapped else 0)
        if not is_wrapped and closing_tokens not in (0, None):
            gaps.append("unresolved_close_account")
            continue
        if wrapped is None or wrapped < 0 or wrapped > closing_lamports:
            gaps.append("unresolved_unwrap")
            continue
        if wrapped:
            token_leg = add_leg(path, "unwrap_token", account, None, wrapped, _asset(token["mint"], token["program"]), 9)
            native_leg = add_leg(path, "unwrap_native", None, destination, wrapped, _asset(), 9)
            result["relationships"].append({"kind": "unwrap", "legs": [token_leg["key"], native_leg["key"]], "tax_treatment": "unknown"})
        add_leg(path, "rent_refund", account, destination, closing_lamports - wrapped, _asset(), 9)

    # Reconcile token legs after wrap/unwrap derivation, including native mints.
    # Missing snapshots stay missing; lifecycle-derived zeros are used only
    # for this instruction equation, not emitted as independent observations.
    for account, token in tokens.items():
        applicable = [leg for leg in result["legs"] if not leg["asset"]["native"] and account in (leg["source"], leg["destination"])]
        pre, post = token.get("pre"), token.get("post")
        if pre is None and any(leg["role"] == "wrap_token" for leg in applicable):
            pre = 0
        if post is None and any(leg["role"] == "unwrap_token" for leg in applicable):
            post = 0
        if pre is not None and post is not None and post - pre != signed_units(applicable, account):
            gaps.append("token_balance_instruction_mismatch")
            for leg in applicable:
                leg["interpretation"] = "unresolved"

    for route in routes:
        legs = [leg for leg in result["legs"] if leg["derivation"]["path"].startswith(route + "/inner:") and leg["role"] == "principal"]
        owned = [leg for leg in legs if owner in (leg["source_owner"], leg["destination_owner"])]
        outgoing = [leg for leg in owned if leg["source_owner"] == owner and leg["destination_owner"] != owner]
        incoming = [leg for leg in owned if leg["destination_owner"] == owner and leg["source_owner"] != owner]
        if (len(outgoing) == len(incoming) == 1 and outgoing[0]["asset"] != incoming[0]["asset"]
            and all(int(leg["raw_units"]) > 0 and not leg.get("interpretation") for leg in owned)
            and not any(gap in gaps for gap in ("unsupported_instruction", "unsupported_token_extension_or_instruction", "unresolved_token_instruction"))):
            result["relationships"].append({"kind": "swap", "program": JUPITER_PROGRAM, "instruction": "route", "path": route,
                                            "legs": [outgoing[0]["key"], incoming[0]["key"]], "amount_source": "executed_inner_transfers",
                                            "tax_treatment": "unknown", "basis": "unknown"})
        else:
            gaps.append("unresolved_conversion")
    if any(gap in gaps for gap in ("unsupported_instruction", "unsupported_token_extension_or_instruction")):
        for leg in result["legs"]:
            if leg["asset"].get("token_program") == onchain.SOLANA_TOKEN_PROGRAMS[1]:
                leg["interpretation"] = "unresolved"
    result["gaps"] = sorted(set(gaps))
    return result


class _CollectionStopped(Exception):
    pass


async def collect_solana_history(
    owner: str, *, source_identity: str, since: datetime | None = None,
    until: datetime | None = None, supplied_accounts: list[dict] | None = None,
    state: dict | None = None, reobserve: bool = False,
    budget: RequestBudget | None = None, deadline: float | None = None, research: bool = False,
    research_address: str | None = None, byte_limit: int | None = None,
) -> dict:
    """Resume only this declared owned inventory; never walk counterparties."""
    requested = {"since": since.isoformat() if since else None, "until": until.isoformat() if until else None, "commitment": "finalized"}
    if since and until and since > until:
        raise ValueError("End must be on or after start")
    archive: dict[str, Any] = copy.deepcopy(state) if state else {
        "version": ARCHIVE_VERSION, "decoder_version": DECODER_VERSION, "chain": "solana", "owner": owner,
        "source_identity": source_identity, "requested": requested, "anchor": None,
        "inventory": {}, "streams": {}, "payloads": {}, "transactions": {}, "gaps": [],
        "snapshots": [], "inventory_programs": [], "unsearched_candidates": [], "bytes": 0,
    }
    if state and archive.get("research", False) != research:
        raise ValueError("Owned inventory and external research cannot share continuation")
    archive["research"] = research
    if research_address is not None and not research:
        raise ValueError("Research endpoint requires an external research scope")
    if state and archive.get("research_address") != research_address:
        raise ValueError("Research endpoint changed; restart collection")
    archive["research_address"] = research_address
    total_limit = min(MAX_TOTAL_BYTES, byte_limit) if byte_limit is not None else MAX_TOTAL_BYTES
    if any(archive.get(key) != value for key, value in (("version", ARCHIVE_VERSION), ("decoder_version", DECODER_VERSION),
                                                      ("owner", owner), ("source_identity", source_identity), ("requested", requested))):
        raise ValueError("History source, owner, decoder or window changed; restart collection")
    supplied_accounts = supplied_accounts or []
    if any(not item.get("reviewed") or item.get("owner") != owner or not isinstance(item.get("address"), str) for item in supplied_accounts):
        raise ValueError("Historical token accounts require a reviewed owner assertion")
    supplied = sorted(supplied_accounts, key=lambda item: item["address"])
    if state and archive.get("supplied_accounts", []) != supplied:
        raise ValueError("Historical account inventory changed; restart collection")
    archive["supplied_accounts"] = supplied
    archive["gaps"] = [gap for gap in archive["gaps"] if gap not in ("deadline_exceeded", "request_limit", "upstream_rate_limited")]
    archive["limits"] = {"seconds": TIME_BUDGET_SECONDS, "attempts": MAX_ATTEMPTS, "concurrency": 1,
        "pages_per_address": MAX_HISTORY_PAGES, "accounts": MAX_ACCOUNTS,
                         "inventory_entries": MAX_INVENTORY_ENTRIES, "archive_bytes": MAX_ARCHIVE_BYTES,
                         "total_bytes": total_limit, "payload_bytes": MAX_PAYLOAD_BYTES}
    budget = budget if budget is not None else RequestBudget(max_attempts=MAX_ATTEMPTS)
    deadline = min(deadline, time.monotonic() + TIME_BUDGET_SECONDS) if deadline is not None else time.monotonic() + TIME_BUDGET_SECONDS
    chain = onchain.CHAINS["solana"]
    endpoint = onchain.rpc_url(chain)
    visited_reads: dict[str, tuple[Any, str]] = {}

    def inventory(address: str, kind: str, discovery: dict, mapping: dict | None = None) -> None:
        if address not in archive["inventory"] and len(archive["inventory"]) >= MAX_INVENTORY_ENTRIES:
            archive["omitted_inventory_count"] = archive.get("omitted_inventory_count", 0) + 1
            archive["gaps"].append("inventory_entry_limit")
            return  # Full enumeration remains in its retained discovery payload.
        account = archive["inventory"].setdefault(address, {"address": address, "kind": kind, "discoveries": [], "ownership": []})
        if discovery not in account["discoveries"]:
            account["discoveries"].append(discovery)
        if mapping and mapping not in account["ownership"]:
            account["ownership"].append(mapping)
        if address not in archive["streams"]:
            if len(archive["streams"]) >= MAX_ACCOUNTS:
                if address not in archive["unsearched_candidates"]:
                    archive["unsearched_candidates"].append(address)
                return
            archive["streams"][address] = {"cursor": None, "pages_examined": 0, "exhausted": False, "stop_reason": None,
                                            "oldest_at": None, "newest_at": None, "unknown_timestamps": 0,
                                            "payload_gaps": [], "pending": [], "seen_cursors": [], "page_refs": []}

    inventory(research_address or owner, "research_endpoint" if research else "owner", {"source": "explicit_selected_external_leg" if research else "workspace_connection_ownership_assertion"})
    for item in supplied:
        inventory(item["address"], "supplied_token", {"source": "reviewed_supplied_account"},
                  {"owner": owner, "period": "reviewed_assertion_unspecified_interval", "source": "user_review"})

    async def read(client: Any, method: str, params: list) -> tuple[Any, str]:
        key = _digest({"method": method, "params": params})
        if key in visited_reads:
            return visited_reads[key]
        if time.monotonic() >= deadline:
            raise OnchainDeadlineExceeded()
        response = await request_json(client, "POST", endpoint, endpoint=endpoint, label="Solana history RPC", deadline=deadline,
                                      attempts=onchain.RPC_RETRY_ATTEMPTS, backoff=onchain.RPC_RETRY_BACKOFF_SECONDS,
                                      timeout=onchain.ONCHAIN_HTTP_TIMEOUT, concurrency=onchain.TX_FETCH_CONCURRENCY,
                                      json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, rpc=True, budget=budget)
        raw = {"method": method, "params": params, "response": response}
        digest = _digest(raw)
        size = len(json.dumps(raw, separators=(",", ":")).encode())
        # ponytail: bounded O(reads * archive bytes) admission check; maintain
        # an incremental encoded-size counter if large archives become slow.
        total_size = len(json.dumps(archive, separators=(",", ":")).encode())
        if digest not in archive["payloads"] and (size > MAX_PAYLOAD_BYTES or archive["bytes"] + size > MAX_ARCHIVE_BYTES or total_size + size + 16384 > total_limit):
            archive["gaps"].append("payload_byte_limit")
            archive.setdefault("unavailable_payloads", []).append({"digest": digest, "method": method, "params": params, "bytes": size, "reason": "payload_byte_limit"})
            raise _CollectionStopped("payload_byte_limit")
        if digest not in archive["payloads"]:
            archive["payloads"][digest] = {**raw, "retrieved_at": datetime.now(timezone.utc).isoformat(), "source_identity": source_identity, "bytes": size}
            archive["bytes"] += size
        result = response.get("result") if isinstance(response, dict) and "result" in response and not response.get("error") else None
        visited_reads[key] = (result, digest)
        return result, digest

    async def read_transaction(client: Any, signature: dict, address: str, page_ref: str | None) -> bool:
        reference = signature["signature"]
        transaction = archive["transactions"].setdefault("solana:" + reference, {
            "signature": reference, "discovery_refs": [], "versions": [], "canonical_version": None,
        })
        discovery = {"address": address, "page_ref": page_ref}
        if discovery not in transaction["discovery_refs"]:
            transaction["discovery_refs"].append(discovery)
        if transaction["versions"] and not reobserve:
            return True
        payload, digest = await read(client, "getTransaction", [reference, {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
        if not isinstance(payload, dict) or not isinstance(payload.get("transaction"), dict):
            transaction["retrieval_gap"] = "payload_unavailable"
            return False
        transaction.pop("retrieval_gap", None)
        confirmation = signature.get("confirmationStatus")
        previous = next((version for version in transaction["versions"] if version["version_id"] == transaction["canonical_version"]), None)
        if confirmation is None and previous and archive["payloads"][previous["payload_digest"]]["response"].get("result") == payload:
            confirmation = previous["confirmation_status"]
        status_ref = None
        try:
            status, status_ref = await read(client, "getSignatureStatuses", [[reference], {"searchTransactionHistory": True}])
        except (OnchainDeadlineExceeded, OnchainRequestLimitExceeded, _CollectionStopped):
            raise
        except Exception:
            status = None
        values = status.get("value") if isinstance(status, dict) else None
        status_gaps = []
        if isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict):
            confirmation = values[0].get("confirmationStatus")
            status_slot = _integer(values[0].get("slot"))
            if status_slot is not None and status_slot != _integer(payload.get("slot")):
                status_gaps.append("confirmation_inclusion_conflict")
            metadata = payload.get("meta")
            if "err" in values[0] and isinstance(metadata, dict) and "err" in metadata and values[0]["err"] != metadata["err"]:
                status_gaps.append("confirmation_execution_conflict")
        else:
            # The finalized getTransaction response is itself positive finality
            # evidence; a null status lookup never reverses a retained version.
            archive["gaps"].append("signature_status_unavailable")
        version = decode_solana_transaction(reference, payload, payload_digest=digest, owner=owner,
                                             confirmation_status=confirmation, anchor_slot=(archive.get("anchor") or {}).get("slot"))
        observed_time = onchain._history_time(payload.get("blockTime"))
        page_time = onchain._history_time(signature.get("blockTime"))
        version["in_requested_window"] = (None if observed_time is None else
                                           (since is None or observed_time >= since) and (until is None or observed_time <= until))
        if page_time is not None and observed_time is not None and page_time != observed_time:
            version["gaps"].append("source_timestamp_disagreement")
            version["in_requested_window"] = None
        if version["in_requested_window"] is not True:
            version["gaps"].append("requested_window_unresolved" if version["in_requested_window"] is None else "outside_requested_window")
            version["version_id"] = _digest({"decoded_version": version["version_id"], "in_requested_window": version["in_requested_window"], "page_time": signature.get("blockTime")})
        version["confirmation_source"] = status_ref
        if status_gaps:
            version["gaps"] = sorted(set(version["gaps"] + status_gaps))
            version["settlement"] = "provisional"
            for leg in version["legs"]:
                leg["settlement"] = "provisional"
            version["version_id"] = _digest({"decoded_version": version["version_id"], "status": status})
            version["confirmation_source"] = status_ref
        duplicate = any(item["version_id"] == version["version_id"] for item in transaction["versions"])
        proposed_bytes = 0 if duplicate else len(json.dumps(version, separators=(",", ":")).encode())
        if len(json.dumps(archive, separators=(",", ":")).encode()) + proposed_bytes + 16384 > total_limit:
            transaction["retrieval_gap"] = "decoded_projection_byte_limit"
            raise _CollectionStopped("decoded_projection_byte_limit")
        prior = transaction["versions"]
        if not any(item["version_id"] == version["version_id"] for item in prior):
            prior.append(version)
        same_payload = all(archive["payloads"][item["payload_digest"]]["response"].get("result") == payload for item in prior)
        prior_settled = any(item["settlement"] == "settled" for item in prior)
        if status_gaps or not same_payload or (prior_settled and version["settlement"] != "settled"):
            transaction["canonical_version"] = None
            transaction["revision_status"] = "conflicting_or_reorganized"
        else:
            transaction["canonical_version"] = version["version_id"]
            transaction["revision_status"] = "current"
        for mapping in ([] if research else version["ownership"]):
            if mapping["owner"] == owner:
                inventory(mapping["address"], "discovered_token", {"source": mapping["source"], "payload_digest": digest, "path": mapping["path"]}, mapping)
            elif not mapping.get("owner"):
                candidate = mapping["address"]
                if candidate not in archive["unsearched_candidates"]:
                    archive["unsearched_candidates"].append(candidate)
        return True

    timeout = asyncio.timeout(max(0, deadline - time.monotonic()))
    try:
        async with timeout, onchain.session() as client:
            if state and (archive.get("anchor") or {}).get("blockhash"):
                anchor = archive["anchor"]
                block, _ = await read(client, "getBlock", [anchor["slot"], {"commitment": "finalized", "transactionDetails": "none", "rewards": False}])
                if not isinstance(block, dict) or not block.get("blockhash"):
                    archive["gaps"].append("anchor_revalidation_unavailable")
                    raise _CollectionStopped("anchor_revalidation_unavailable")
                if block["blockhash"] != anchor["blockhash"]:
                    archive["gaps"].append("anchor_changed_restart_required")
                    for transaction in archive["transactions"].values():
                        transaction["canonical_version"] = None
                        transaction["revision_status"] = "conflicting_or_reorganized"
                    raise _CollectionStopped("anchor_changed_restart_required")
            if archive["anchor"] is None:
                slot, slot_ref = await read(client, "getSlot", [{"commitment": "finalized"}])
                if _integer(slot) is None:
                    archive["gaps"].append("anchor_unavailable")
                else:
                    block, block_ref = await read(client, "getBlock", [slot, {"commitment": "finalized", "transactionDetails": "none", "rewards": False}])
                    archive["anchor"] = {"slot": slot, "blockhash": block.get("blockhash") if isinstance(block, dict) else None,
                                         "commitment": "finalized", "source_refs": [slot_ref, block_ref]}
                    if not archive["anchor"]["blockhash"]:
                        archive["gaps"].append("anchor_blockhash_unavailable")
            for program in (() if research else onchain.SOLANA_TOKEN_PROGRAMS):
                if program in archive["inventory_programs"]:
                    continue
                try:
                    response, ref = await read(client, "getTokenAccountsByOwner", [owner, {"programId": program}, {"encoding": "jsonParsed", "commitment": "finalized"}])
                except (OnchainDeadlineExceeded, OnchainRequestLimitExceeded, _CollectionStopped):
                    raise
                except Exception:
                    archive["gaps"].append("inventory_provider_unavailable")
                    continue
                entries = response.get("value") if isinstance(response, dict) else None
                if not isinstance(entries, list):
                    archive["gaps"].append("inventory_unreadable")
                    continue
                context_slot = (response.get("context") or {}).get("slot")
                for entry in entries:
                    try:
                        address = entry["pubkey"]
                        info = entry["account"]["data"]["parsed"]["info"]
                        if not isinstance(address, str) or info.get("owner") != owner:
                            raise ValueError()
                        mapping = {"owner": owner, "slot": context_slot, "mint": info.get("mint"), "token_program": program,
                                   "source": "current_token_account", "payload_digest": ref, "period": "observation_only"}
                        inventory(address, "current_token", {"source": "current_token_account", "payload_digest": ref}, mapping)
                    except (KeyError, TypeError, ValueError):
                        archive["gaps"].append("unresolved_current_account_owner")
                archive["inventory_programs"].append(program)
            if reobserve:
                for transaction in list(archive["transactions"].values()):
                    await read_transaction(client, {"signature": transaction["signature"]}, owner, None)
            processed: set[str] = set()
            while addresses := [address for address in archive["streams"] if address not in processed]:
                address = addresses[0]
                processed.add(address)
                stream = archive["streams"][address]
                if stream["exhausted"] or stream["stop_reason"] == "requested_boundary":
                    continue
                stream["stop_reason"] = None
                try:
                    # A page cursor advances only after its pending payloads
                    # have been retained, so cancellation cannot skip its tail.
                    for page_number in range(MAX_HISTORY_PAGES):
                        if stream["pending"]:
                            pending = list(stream["pending"])
                            stream["payload_gaps"] = []
                            for item in pending:
                                if await read_transaction(client, item, address, stream["page_refs"][-1] if stream["page_refs"] else None):
                                    stream["pending"].remove(item)
                                else:
                                    stream["payload_gaps"].append(item["signature"])
                            if stream["pending"]:
                                stream["stop_reason"] = "payload_unavailable"
                                break
                        if stream.pop("boundary_pending", False):
                            stream["stop_reason"] = "requested_boundary"
                            break
                        params = {"limit": onchain.SOLANA_SIGNATURE_PAGE, "commitment": "finalized"}
                        if stream["cursor"]:
                            params["before"] = stream["cursor"]
                        page, ref = await read(client, "getSignaturesForAddress", [address, params])
                        stream["pages_examined"] += 1
                        stream["page_refs"].append(ref)
                        if not isinstance(page, list) or any(not isinstance(item, dict) or not isinstance(item.get("signature"), str) for item in page):
                            stream["stop_reason"] = "malformed_page"
                            break
                        if not page:
                            stream["exhausted"] = True
                            stream["stop_reason"] = "provider_exhausted"
                            break
                        next_cursor = page[-1]["signature"]
                        if next_cursor == stream["cursor"] or next_cursor in stream["seen_cursors"]:
                            stream["stop_reason"] = "repeated_cursor"
                            break
                        stream["seen_cursors"].append(next_cursor)
                        stream["cursor"] = next_cursor
                        times = []
                        for item in page:
                            timestamp = _timestamp(item.get("blockTime"))
                            if timestamp is None:
                                stream["unknown_timestamps"] += 1
                            else:
                                times.append(timestamp)
                                stream["oldest_at"] = min(stream["oldest_at"] or timestamp, timestamp)
                                stream["newest_at"] = max(stream["newest_at"] or timestamp, timestamp)
                            moment = onchain._history_time(item.get("blockTime"))
                            if moment is None or ((since is None or moment >= since) and (until is None or moment <= until)):
                                stream["pending"].append(item)
                        # A whole page strictly below the inclusive lower bound
                        # avoids dropping boundary ties or out-of-order rows.
                        stream["boundary_pending"] = bool(since and len(times) == len(page) and max(times) < since.isoformat())
                    else:
                        stream["stop_reason"] = "page_limit"
                except (OnchainDeadlineExceeded, OnchainRequestLimitExceeded, _CollectionStopped):
                    raise
                except Exception as exc:
                    stream["stop_reason"] = interruption_code(exc)
    except OnchainRequestLimitExceeded:
        archive["gaps"].append("request_limit")
    except CancelledError:
        # Reads are sequential and awaited: leaving the client context drains
        # the active read before retained siblings can be persisted by the caller.
        archive["gaps"].append("cancelled")
    except _CollectionStopped as exc:
        archive["gaps"].append(str(exc))
    except OnchainDeadlineExceeded:
        archive["gaps"].append("deadline_exceeded")
    except TimeoutError:
        if not timeout.expired():
            raise
        archive["gaps"].append("deadline_exceeded")
    except Exception as exc:
        archive["gaps"].append(interruption_code(exc))
    archive["limits"]["attempts_used"] = budget.attempts
    archive["gaps"] = sorted(set(archive["gaps"]))
    for stream in archive["streams"].values():
        if not stream["exhausted"] and stream["stop_reason"] is None and archive["gaps"]:
            stream["stop_reason"] = archive["gaps"][-1]
    archive["resumable"] = any(not stream["exhausted"] and stream["stop_reason"] != "requested_boundary" for stream in archive["streams"].values())
    versions = [version for transaction in archive["transactions"].values() for version in transaction["versions"] if version["version_id"] == transaction["canonical_version"]]
    conflicts = any(transaction["versions"] and transaction["canonical_version"] is None for transaction in archive["transactions"].values())
    archive["coverage"] = {
        "inventory": "unknown",  # Current and discovered accounts cannot prove all historical closed accounts.
        "retrieval": "partial" if archive["resumable"] or archive["gaps"] or archive["unsearched_candidates"] or any(transaction.get("retrieval_gap") for transaction in archive["transactions"].values()) else "complete",
        "interpretation": "partial" if conflicts or any(version["gaps"] for version in versions) else "complete",
        "settlement": "partial" if conflicts or any(version["settlement"] != "settled" for version in versions) else "complete",
    }
    return archive
