"""Synthetic-only owned-history collector/decoder checks; no chain or DB writes."""

import asyncio
import copy
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.providers import onchain, solana_history as history

OWNER = "owner-A"
TOKEN = onchain.SOLANA_TOKEN_PROGRAMS[0]
TOKEN_2022 = onchain.SOLANA_TOKEN_PROGRAMS[1]
STAMP = 1_800_000_000


def instruction(program, kind, **info):
    return {"programId": program, "parsed": {"type": kind, "info": info}}


def balance(index, amount, *, owner=OWNER, mint="mint-A", program=TOKEN, decimals=6):
    result = {"accountIndex": index, "mint": mint, "programId": program,
              "uiTokenAmount": {"amount": str(amount), "decimals": decimals}}
    if owner is not None:
        result["owner"] = owner
    return result


def transaction(instructions=(), *, keys=(OWNER, "recipient-A"), pre=(11_000_000_000, 0), post=(4_970_000_000, 6_000_000_000), fee=30_000_000, err=None, tokens_pre=(), tokens_post=()):
    return {"slot": 10, "blockTime": STAMP, "version": "legacy",
            "transaction": {"signatures": ["synthetic-signature"], "message": {"accountKeys": list(keys), "instructions": list(instructions)}},
            "meta": {"err": err, "fee": fee, "preBalances": list(pre), "postBalances": list(post),
                     "preTokenBalances": list(tokens_pre), "postTokenBalances": list(tokens_post), "innerInstructions": []}}


def decode(payload, confirmation="finalized"):
    return history.decode_solana_transaction("synthetic-signature", payload, payload_digest="synthetic-digest", owner=OWNER,
                                              confirmation_status=confirmation, anchor_slot=100)


def signature(reference, *, stamp=STAMP, status="finalized"):
    return {"signature": reference, "blockTime": stamp, "confirmationStatus": status, "slot": 10, "err": None}


def token_account(address, *, mint="mint-A", amount=0, program=TOKEN):
    return {"pubkey": address, "account": {"owner": program, "data": {"parsed": {"info": {
        "owner": OWNER, "mint": mint, "tokenAmount": {"amount": str(amount), "decimals": 6},
    }}}}}


class RPC:
    def __init__(self, *, accounts=None, pages=None, payloads=None, statuses=None):
        self.accounts = accounts or {}
        self.pages = pages or {}
        self.payloads = payloads or {}
        self.statuses = statuses or {}
        self.calls = []

    async def __call__(self, client, method, url, **kwargs):
        kwargs["budget"].consume()
        body = kwargs["json_body"]
        method, params = body["method"], body["params"]
        self.calls.append((method, copy.deepcopy(params)))
        if method == "getSlot":
            result = 100
        elif method == "getBlock":
            result = {"blockhash": "synthetic-anchor", "blockTime": STAMP + 100}
        elif method == "getTokenAccountsByOwner":
            result = {"context": {"slot": 100}, "value": self.accounts.get(params[1]["programId"], [])}
        elif method == "getSignaturesForAddress":
            result = self.pages.get((params[0], params[1].get("before")), [])
        elif method == "getTransaction":
            result = copy.deepcopy(self.payloads.get(params[0]))
            if isinstance(result, dict) and isinstance(result.get("transaction"), dict) and result["transaction"].get("signatures"):
                result["transaction"]["signatures"][0] = params[0]
        elif method == "getSignatureStatuses":
            result = {"value": [self.statuses.get(params[0][0], {"confirmationStatus": "finalized"})]}
        else:
            raise AssertionError(method)
        if isinstance(result, BaseException):
            raise result
        return {"jsonrpc": "2.0", "id": 1, "result": copy.deepcopy(result)}


def install(monkeypatch, rpc):
    monkeypatch.setattr(history, "request_json", rpc)
    monkeypatch.setattr(onchain, "rpc_url", lambda chain: "https://synthetic.invalid/private-key?secret=hidden")


async def collect(**kwargs):
    return await history.collect_solana_history(OWNER, source_identity="solana:synthetic-config", **kwargs)


def test_native_principal_fee_gross_and_failed_third_party_fee_stay_separate():
    transfer = instruction(history.SYSTEM_PROGRAM, "transfer", source=OWNER, destination="recipient-A", lamports=6_000_000_000)
    payload = transaction([transfer])
    version = decode(payload)
    assert [(leg["role"], leg["quantity"]) for leg in version["legs"]] == [("network_fee", "0.030000000"), ("principal", "6.000000000")]
    assert version["observations"][0]["delta_raw_units"] == "-6030000000"
    assert version["observations"][1]["delta_raw_units"] == "6000000000"
    payload["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    payload["transaction"]["message"]["accountKeys"][0] = "fee-sponsor"
    failed = decode(payload)
    assert failed["execution"] == "failed"
    assert len(failed["legs"]) == 1
    assert failed["legs"][0]["source"] == "fee-sponsor"
    assert failed["legs"][0]["source_owner"] is None
    payload["meta"] = None
    unknown = decode(payload)
    assert unknown["legs"] == []
    assert "missing_execution_metadata" in unknown["gaps"]


async def test_token_only_closed_supplied_zero_unpriced_inventory_and_replay(monkeypatch):
    token_transfer = instruction(TOKEN, "transferChecked", source="token-source", destination="token-A", mint="mint-A",
                                 tokenAmount={"amount": "4000000", "decimals": 6})
    payload = transaction([token_transfer], keys=("sponsor", "token-source", "token-A", "closed-A"), pre=(100000, 0, 0, 0), post=(95000, 0, 0, 0), fee=5000,
                          tokens_pre=[balance(1, 10_000_000, owner="external"), balance(2, 0), balance(3, 0)],
                          tokens_post=[balance(1, 6_000_000, owner="external"), balance(2, 4_000_000)])
    supplied = [{"address": "supplied-closed", "owner": OWNER, "reviewed": True}]
    accounts = [token_account(f"spam-{i}", mint=f"mint-{i}") for i in range(30)] + [token_account("token-A")]
    rpc = RPC(accounts={TOKEN: accounts, TOKEN_2022: [token_account("token-2022", program=TOKEN_2022)]},
              pages={("token-A", None): [signature("receipt")], ("closed-A", None): [signature("receipt")]}, payloads={"receipt": payload})
    install(monkeypatch, rpc)
    archive = await collect(supplied_accounts=supplied)
    assert len(archive["transactions"]) == 1
    assert len([call for call in rpc.calls if call[0] == "getTransaction"]) == 1
    assert {"closed-A", "supplied-closed", "token-2022", "spam-29"} <= set(archive["streams"])
    assert archive["coverage"]["inventory"] == "unknown"
    assert archive["streams"][OWNER]["exhausted"] is True
    retained = archive["transactions"]["solana:receipt"]
    assert {ref["address"] for ref in retained["discovery_refs"]} == {"token-A", "closed-A"}
    assert retained["versions"][0]["legs"][1]["destination_owner"] == OWNER
    assert "private-key" not in json.dumps(archive)
    count = len(rpc.calls)
    replayed = await collect(state=json.loads(json.dumps(archive)), supplied_accounts=supplied)
    assert len(rpc.calls) == count
    assert replayed["transactions"] == archive["transactions"]
    assert all("getTokenAccountsByOwner" != call[0] or call[1][1]["programId"] in onchain.SOLANA_TOKEN_PROGRAMS for call in rpc.calls)


def test_two_same_mint_legs_inner_instructions_and_loaded_keys_keep_identity():
    legs = [instruction(TOKEN, "transferChecked", source="token-source", destination="token-A", mint="mint-A", tokenAmount={"amount": "2000000", "decimals": 6}) for _ in range(2)]
    payload = transaction([{"programId": "ComputeBudget111111111111111111111111111111"}], keys=("sponsor", "token-source"), pre=(100000, 0, 0), post=(95000, 0, 0), fee=5000,
                          tokens_pre=[balance(1, 10_000_000, owner="external"), balance(2, 0)], tokens_post=[balance(1, 6_000_000, owner="external"), balance(2, 4_000_000)])
    payload["version"] = 0
    payload["transaction"]["message"]["addressTableLookups"] = [{"accountKey": "table", "writableIndexes": [0]}]
    payload["meta"]["loadedAddresses"] = {"writable": ["token-A"], "readonly": []}
    payload["meta"]["innerInstructions"] = [{"index": 0, "instructions": legs}]
    version = decode(payload)
    assert len(version["legs"]) == 3
    principal = [leg for leg in version["legs"] if leg["role"] == "principal"]
    assert len({leg["key"] for leg in principal}) == 2
    assert all(leg["destination"] == "token-A" for leg in principal)
    payload["meta"].pop("loadedAddresses")
    assert "unresolved_loaded_keys" in decode(payload)["gaps"]
    assert len(decode(payload)["legs"]) == 1
    payload["version"] = 3
    assert "unsupported_transaction_version" in decode(payload)["gaps"]


def test_token_2022_fee_and_unknown_owner_or_extensions_are_qualified():
    transfer = instruction(TOKEN_2022, "transferCheckedWithFee", source="token-source", destination="token-A", mint="mint-A",
                           tokenAmount={"amount": "10000000", "decimals": 6}, feeAmount={"amount": "100000", "decimals": 6})
    payload = transaction([transfer], keys=(OWNER, "token-source", "token-A"), pre=(1, 0, 0), post=(1, 0, 0), fee=0,
                          tokens_pre=[balance(1, 10_000_000, program=TOKEN_2022), balance(2, 0, owner="external", program=TOKEN_2022)],
                          tokens_post=[balance(1, 0, program=TOKEN_2022), balance(2, 9_900_000, owner="external", program=TOKEN_2022)])
    version = decode(payload)
    principal, fee = version["legs"][1:]
    assert (principal["sender_debit_raw_units"], principal["receiver_credit_raw_units"], fee["raw_units"]) == ("10000000", "9900000", "100000")
    assert "token_balance_instruction_mismatch" not in version["gaps"]
    payload["meta"]["preTokenBalances"][0].pop("owner")
    payload["meta"]["postTokenBalances"][0].pop("owner")
    assert "unresolved_historical_owner" in decode(payload)["gaps"]
    payload["transaction"]["message"]["instructions"] = [instruction(TOKEN_2022, "confidentialTransfer", source="token-source", destination="token-A")]
    unknown = decode(payload)
    assert len(unknown["legs"]) == 1
    assert "unsupported_token_extension_or_instruction" in unknown["gaps"]


@pytest.mark.parametrize("program", [TOKEN, TOKEN_2022])
def test_wrap_unwrap_rent_and_bridge_keep_distinct_measures(program):
    mint = history.NATIVE_MINTS[program]
    funding = instruction(history.SYSTEM_PROGRAM, "createAccount", source=OWNER, newAccount="wrapped-A", lamports=6_002_000_000)
    sync = instruction(program, "syncNative", account="wrapped-A")
    payload = transaction([funding, sync], keys=(OWNER, "wrapped-A"), pre=(11_000_000_000, 0), post=(4_968_000_000, 6_002_000_000),
                          tokens_post=[balance(1, 6_000_000_000, mint=mint, program=program, decimals=9)])
    wrapped = decode(payload)
    roles = {leg["role"]: leg for leg in wrapped["legs"]}
    assert roles["wrapped_backing"]["non_additive"] is True
    assert roles["wrap_native"]["quantity"] == roles["wrap_token"]["quantity"] == "6.000000000"
    assert roles["rent_deposit"]["quantity"] == "0.002000000"
    assert wrapped["relationships"][0]["kind"] == "wrap"
    payload = transaction([instruction(program, "closeAccount", account="wrapped-A", destination=OWNER, owner=OWNER)], keys=(OWNER, "wrapped-A"),
                          pre=(4_968_000_000, 6_002_000_000), post=(10_940_000_000, 0),
                          tokens_pre=[balance(1, 6_000_000_000, mint=mint, program=program, decimals=9)])
    unwrapped = decode(payload)
    assert {leg["role"]: leg["quantity"] for leg in unwrapped["legs"]} == {
        "network_fee": "0.030000000", "unwrap_token": "6.000000000", "unwrap_native": "6.000000000", "rent_refund": "0.002000000"}
    bridge = transaction([instruction(history.SYSTEM_PROGRAM, "transfer", source=OWNER, destination="bridge-escrow", lamports=6_000_000_000),
                          {"programId": "synthetic-bridge", "data": "unknown"}], keys=(OWNER, "bridge-escrow"))
    unknown = decode(bridge)
    assert unknown["relationships"] == []
    assert "unsupported_instruction" in unknown["gaps"]
    assert len(unknown["legs"]) == 2


def test_pinned_jupiter_route_matches_actual_inner_transfers_not_quote():
    # Anchor discriminator plus a synthetic opaque route body/quote. The
    # decoder uses executed inner transfers, never amounts in the route body.
    raw = hashlib.sha256(b"global:route").digest()[:8] + b"synthetic-quote-999999"
    value = int.from_bytes(raw, "big")
    encoded = ""
    while value:
        value, remainder = divmod(value, 58)
        encoded = history._B58[remainder] + encoded
    outer = {"programId": history.JUPITER_PROGRAM, "data": encoded}
    payload = transaction([outer], keys=(OWNER, "owned-wsol", "pool-wsol", "pool-usdc", "owned-usdc"), pre=(100000, 0, 0, 0, 0), post=(95000, 0, 0, 0, 0), fee=5000,
                          tokens_pre=[balance(1, 4_000_000_000, mint=history.WSOL_MINT, decimals=9), balance(2, 0, owner="pool", mint=history.WSOL_MINT, decimals=9),
                                      balance(3, 500_000_000, owner="pool", mint="synthetic-usdc"), balance(4, 0, mint="synthetic-usdc")],
                          tokens_post=[balance(1, 0, mint=history.WSOL_MINT, decimals=9), balance(2, 4_000_000_000, owner="pool", mint=history.WSOL_MINT, decimals=9),
                                       balance(3, 100_000_000, owner="pool", mint="synthetic-usdc"), balance(4, 400_000_000, mint="synthetic-usdc")])
    payload["meta"]["innerInstructions"] = [{"index": 0, "instructions": [
        instruction(TOKEN, "transferChecked", source="owned-wsol", destination="pool-wsol", mint=history.WSOL_MINT, tokenAmount={"amount": "4000000000", "decimals": 9}),
        instruction(TOKEN, "transferChecked", source="pool-usdc", destination="owned-usdc", mint="synthetic-usdc", tokenAmount={"amount": "400000000", "decimals": 6}),
    ]}]
    version = decode(payload)
    assert version["relationships"][0]["kind"] == "swap"
    assert version["legs"][-1]["quantity"] == "400.000000"
    assert version["relationships"][0]["basis"] == "unknown"
    payload["transaction"]["message"]["instructions"][0]["programId"] = "lookalike-jupiter"
    assert decode(payload)["relationships"] == []


async def test_partial_null_payload_resume_and_repeated_cursor_retain_success(monkeypatch):
    good = transaction([])
    rpc = RPC(pages={(OWNER, None): [signature("good"), signature("missing")], (OWNER, "missing"): [signature("missing")]}, payloads={"good": good})
    install(monkeypatch, rpc)
    archive = await collect()
    assert archive["streams"][OWNER]["stop_reason"] == "payload_unavailable"
    assert archive["streams"][OWNER]["payload_gaps"] == ["missing"]
    assert len(archive["transactions"]["solana:good"]["versions"]) == 1
    rpc.payloads["missing"] = good
    resumed = await collect(state=archive)
    assert resumed["streams"][OWNER]["stop_reason"] == "repeated_cursor"
    assert len(resumed["transactions"]["solana:good"]["versions"]) == 1
    assert len(resumed["transactions"]["solana:missing"]["versions"]) == 1
    assert resumed["coverage"]["retrieval"] == "partial"


@pytest.mark.parametrize("bad_page", [None, {}, [{"unexpected": 1}]])
async def test_empty_exhaustion_is_distinct_from_malformed_page(monkeypatch, bad_page):
    rpc = RPC(pages={(OWNER, None): bad_page})
    install(monkeypatch, rpc)
    malformed = await collect()
    assert not malformed["streams"][OWNER]["exhausted"]
    assert malformed["streams"][OWNER]["stop_reason"] == "malformed_page"
    rpc.pages[(OWNER, None)] = []
    empty = await collect()
    assert empty["streams"][OWNER]["exhausted"]
    assert empty["coverage"]["retrieval"] == "complete"
    assert empty["coverage"]["inventory"] == "unknown"


async def test_deadline_request_and_account_caps_are_resumable(monkeypatch):
    rpc = RPC(pages={(OWNER, None): [signature("first"), signature("later")]}, payloads={"first": transaction([]), "later": history.OnchainDeadlineExceeded()})
    install(monkeypatch, rpc)
    partial = await collect()
    assert "deadline_exceeded" in partial["gaps"]
    assert len(partial["transactions"]["solana:first"]["versions"]) == 1
    rpc.payloads["later"] = transaction([])
    resumed = await collect(state=partial)
    assert len(resumed["transactions"]["solana:first"]["versions"]) == 1
    assert resumed["streams"][OWNER]["exhausted"]
    monkeypatch.setattr(history, "MAX_ATTEMPTS", 3)
    capped = await collect()
    assert "request_limit" in capped["gaps"]
    assert capped["limits"]["attempts_used"] == 3
    monkeypatch.setattr(history, "MAX_ATTEMPTS", 600)
    monkeypatch.setattr(history, "MAX_ACCOUNTS", 2)
    rpc.accounts[TOKEN] = [token_account("token-1"), token_account("token-2")]
    accounts = await collect()
    assert "token-2" in accounts["inventory"]
    assert accounts["unsearched_candidates"] == ["token-2"]


async def test_inclusive_ties_unknown_time_out_of_order_and_page_cap(monkeypatch):
    rpc = RPC(pages={(OWNER, None): [signature("older", stamp=STAMP - 1), signature("tie"), signature("unknown", stamp=None)],
                     (OWNER, "unknown"): [signature("lower", stamp=STAMP - 1)]},
              payloads={"tie": transaction([]), "unknown": transaction([])})
    install(monkeypatch, rpc)
    moment = datetime.fromtimestamp(STAMP, timezone.utc)
    result = await collect(since=moment, until=moment)
    assert set(result["transactions"]) == {"solana:tie", "solana:unknown"}
    assert result["streams"][OWNER]["unknown_timestamps"] == 1
    assert result["streams"][OWNER]["stop_reason"] == "requested_boundary"
    monkeypatch.setattr(history, "MAX_HISTORY_PAGES", 1)
    limited = await collect()
    assert limited["streams"][OWNER]["stop_reason"] == "page_limit"
    assert limited["streams"][OWNER]["pending"]


async def test_provisional_finalize_conflict_and_null_reobservation(monkeypatch):
    payload = transaction([])
    rpc = RPC(pages={(OWNER, None): [signature("tx", status="confirmed")]}, payloads={"tx": payload}, statuses={"tx": {"confirmationStatus": "confirmed"}})
    install(monkeypatch, rpc)
    provisional = await collect()
    assert provisional["transactions"]["solana:tx"]["versions"][0]["settlement"] == "provisional"
    rpc.statuses["tx"] = {"confirmationStatus": "finalized"}
    finalized = await collect(state=provisional, reobserve=True)
    tx = finalized["transactions"]["solana:tx"]
    assert len(tx["versions"]) == 2
    assert tx["canonical_version"] == tx["versions"][-1]["version_id"]
    assert tx["versions"][-1]["settlement"] == "settled"
    rpc.payloads["tx"] = None
    missing = await collect(state=finalized, reobserve=True)
    assert missing["transactions"]["solana:tx"]["canonical_version"] == tx["canonical_version"]
    assert missing["transactions"]["solana:tx"]["retrieval_gap"] == "payload_unavailable"
    assert missing["coverage"]["retrieval"] == "partial"
    rpc.payloads["tx"] = copy.deepcopy(payload)
    rpc.payloads["tx"]["meta"]["fee"] = 1
    conflict = await collect(state=missing, reobserve=True)
    assert conflict["transactions"]["solana:tx"]["canonical_version"] is None
    assert conflict["coverage"]["settlement"] == "partial"


async def test_zero_101_legs_and_raw_overflow_are_lossless_or_explicit(monkeypatch):
    instructions = [instruction(history.SYSTEM_PROGRAM, "transfer", source=OWNER, destination="recipient-A", lamports=1) for _ in range(101)]
    rpc = RPC(pages={(OWNER, None): [signature("many"), signature("zero")]}, payloads={"many": transaction(instructions), "zero": {"slot": 10, "blockTime": STAMP, "transaction": {"message": {"accountKeys": [], "instructions": []}}, "meta": None}})
    install(monkeypatch, rpc)
    archive = await collect()
    assert len(archive["transactions"]["solana:many"]["versions"][0]["legs"]) == 102
    assert archive["transactions"]["solana:zero"]["versions"][0]["legs"] == []
    for tx in archive["transactions"].values():
        version = tx["versions"][0]
        raw = archive["payloads"][version["payload_digest"]]["response"]["result"]
        replay = history.decode_solana_transaction(tx["signature"], raw, payload_digest=version["payload_digest"], owner=OWNER,
                                                    confirmation_status=version["confirmation_status"], anchor_slot=archive["anchor"]["slot"])
        assert {key: value for key, value in version.items() if key not in ("in_requested_window", "confirmation_source")} == replay
    monkeypatch.setattr(history, "MAX_PAYLOAD_BYTES", 1000)
    overflow = await collect()
    assert "payload_byte_limit" in overflow["gaps"]
    assert overflow["unavailable_payloads"][0]["digest"]
    assert overflow["resumable"]


async def test_resume_rejects_source_window_owner_and_inventory_changes(monkeypatch):
    rpc = RPC()
    install(monkeypatch, rpc)
    archive = await collect()
    count = len(rpc.calls)
    with pytest.raises(ValueError, match="changed"):
        await history.collect_solana_history(OWNER, source_identity="another-source", state=archive)
    with pytest.raises(ValueError, match="changed"):
        await collect(state=archive, since=datetime.fromtimestamp(STAMP, timezone.utc))
    with pytest.raises(ValueError, match="reviewed"):
        await collect(supplied_accounts=[{"address": "unreviewed", "owner": OWNER}])
    with pytest.raises(ValueError, match="inventory changed"):
        await collect(state=archive, supplied_accounts=[{"address": "new", "owner": OWNER, "reviewed": True}])
    assert len(rpc.calls) == count


async def test_timeout_cancels_inflight_without_background_work(monkeypatch):
    rpc = RPC()
    completed = []

    async def slow(*args, **kwargs):
        if kwargs["json_body"]["method"] == "getSignaturesForAddress":
            try:
                await asyncio.sleep(1)
            finally:
                completed.append("cancelled")
        return await rpc(*args, **kwargs)

    install(monkeypatch, slow)
    monkeypatch.setattr(history, "TIME_BUDGET_SECONDS", 0.01)
    result = await collect()
    assert result["gaps"] == ["deadline_exceeded"]
    assert completed == ["cancelled"]
    assert result["payloads"]


async def test_fake_monotonic_deadline_retains_successful_reads(monkeypatch):
    rpc = RPC()
    clock = SimpleNamespace(now=0)

    async def advance(*args, **kwargs):
        result = await rpc(*args, **kwargs)
        clock.now += 10
        return result

    install(monkeypatch, advance)
    monkeypatch.setattr(history, "time", SimpleNamespace(monotonic=lambda: clock.now))
    result = await collect()
    assert "deadline_exceeded" in result["gaps"] or result["streams"][OWNER]["exhausted"]
    assert len(result["payloads"]) <= 5


@pytest.mark.parametrize("status", [{"confirmationStatus": "finalized", "slot": 11, "err": None},
                                  {"confirmationStatus": "finalized", "slot": 10, "err": "failed"}])
async def test_contradictory_finalized_status_invalidates_canonical(monkeypatch, status):
    rpc = RPC(pages={(OWNER, None): [signature("tx")]}, payloads={"tx": transaction([])})
    install(monkeypatch, rpc)
    original = await collect()
    rpc.statuses["tx"] = status
    updated = await collect(state=original, reobserve=True)
    assert updated["transactions"]["solana:tx"]["canonical_version"] is None
    assert updated["coverage"]["settlement"] == "partial"
    assert len(updated["transactions"]["solana:tx"]["versions"]) == 2


async def test_null_status_preserves_settled_and_duplicate_payload_fits_archive_cap(monkeypatch):
    rpc = RPC(pages={(OWNER, None): [signature("tx")]}, payloads={"tx": transaction([])})
    install(monkeypatch, rpc)
    original = await collect()
    monkeypatch.setattr(history, "MAX_ARCHIVE_BYTES", original["bytes"])
    repeated = await collect(state=original, reobserve=True)
    assert "payload_byte_limit" not in repeated["gaps"]
    monkeypatch.setattr(history, "MAX_ARCHIVE_BYTES", 24 * 1024 * 1024)
    rpc.statuses["tx"] = None
    updated = await collect(state=repeated, reobserve=True)
    assert updated["transactions"]["solana:tx"]["canonical_version"] == original["transactions"]["solana:tx"]["canonical_version"]
    assert "signature_status_unavailable" in updated["gaps"]


def test_raw_u64_bound_and_wsol_transfer_balance_mismatch_are_unknown():
    payload = transaction([instruction(history.SYSTEM_PROGRAM, "transfer", source=OWNER, destination="recipient-A", lamports="999999999999999999999999999999")])
    assert len(decode(payload)["legs"]) == 1
    assert "malformed_native_instruction" in decode(payload)["gaps"]
    payload = transaction([instruction(TOKEN, "transferChecked", source="a", destination="b", mint=history.WSOL_MINT, tokenAmount={"amount": "600", "decimals": 9})],
                          keys=(OWNER, "a", "b"), pre=(100, 0, 0), post=(100, 0, 0), fee=0,
                          tokens_pre=[balance(1, 1000, mint=history.WSOL_MINT, decimals=9), balance(2, 0, owner="external", mint=history.WSOL_MINT, decimals=9)],
                          tokens_post=[balance(1, 300, mint=history.WSOL_MINT, decimals=9), balance(2, 600, owner="external", mint=history.WSOL_MINT, decimals=9)])
    decoded = decode(payload)
    assert "token_balance_instruction_mismatch" in decoded["gaps"]
    assert decoded["legs"][1]["interpretation"] == "unresolved"


def test_wrong_transaction_identity_and_malformed_fields_never_invent_legs():
    payload = transaction([])
    payload["transaction"]["signatures"] = ["wrong-signature"]
    decoded = decode(payload)
    assert decoded["legs"] == []
    assert "transaction_identity_mismatch" in decoded["gaps"]
    payload["transaction"]["signatures"] = []
    assert "missing_transaction_identity" in decode(payload)["gaps"]
    payload = transaction([])
    payload["transaction"]["message"]["accountKeys"] = None
    assert "malformed_payload_structure" in decode(payload)["gaps"]
    payload = transaction([], tokens_pre=[balance(0, 0)])
    payload["meta"]["preTokenBalances"][0]["uiTokenAmount"] = "not-an-object"
    assert "malformed_token_balance" in decode(payload)["gaps"]


async def test_source_clock_disagreement_is_not_requested_interval_evidence(monkeypatch):
    payload = transaction([])
    payload["blockTime"] = STAMP + 10
    rpc = RPC(pages={(OWNER, None): [signature("tx")]}, payloads={"tx": payload})
    install(monkeypatch, rpc)
    stamp = datetime.fromtimestamp(STAMP, timezone.utc)
    archive = await collect(since=stamp, until=stamp)
    version = archive["transactions"]["solana:tx"]["versions"][0]
    assert version["in_requested_window"] is None
    assert "source_timestamp_disagreement" in version["gaps"]


async def test_full_archive_cap_retains_raw_when_decode_would_overflow(monkeypatch):
    payload = transaction([instruction(history.SYSTEM_PROGRAM, "transfer", source=OWNER, destination="recipient-A", lamports=1) for _ in range(101)])
    rpc = RPC(pages={(OWNER, None): [signature("many")]}, payloads={"many": payload})
    install(monkeypatch, rpc)
    monkeypatch.setattr(history, "MAX_TOTAL_BYTES", 65000)
    archive = await collect()
    assert "decoded_projection_byte_limit" in archive["gaps"]
    assert archive["transactions"]["solana:many"]["versions"] == []
    assert any(record["method"] == "getTransaction" for record in archive["payloads"].values())
    assert len(json.dumps(archive).encode()) < 65000


async def test_owner_two_token_pages_deduplicate_two_principal_legs_and_fee(monkeypatch):
    transfer = instruction(TOKEN, "transferChecked", source="token-A", destination="token-B", mint="mint-A", tokenAmount={"amount": "100", "decimals": 6})
    payload = transaction([transfer, transfer], keys=(OWNER, "token-A", "token-B"), pre=(100000, 0, 0), post=(95000, 0, 0), fee=5000,
                          tokens_pre=[balance(1, 300), balance(2, 0)], tokens_post=[balance(1, 100), balance(2, 200)])
    rpc = RPC(accounts={TOKEN: [token_account("token-A"), token_account("token-B")]},
              pages={(address, None): [signature("shared")] for address in (OWNER, "token-A", "token-B")}, payloads={"shared": payload})
    install(monkeypatch, rpc)
    archive = await collect()
    repeated = await collect(state=archive)
    tx = repeated["transactions"]["solana:shared"]
    assert len(tx["versions"]) == 1
    assert len(tx["discovery_refs"]) == 3
    assert [leg["role"] for leg in tx["versions"][0]["legs"]] == ["network_fee", "principal", "principal"]
    assert len([call for call in rpc.calls if call[0] == "getTransaction"]) == 1


def test_changed_historical_owner_is_never_replaced_by_current_owner():
    payload = transaction([], keys=(OWNER, "token-A"), pre=(100, 0), post=(100, 0), fee=0,
                          tokens_pre=[balance(1, 100)], tokens_post=[balance(1, 100, owner="different-owner")])
    decoded = decode(payload)
    assert "changed_token_owner" in decoded["gaps"]
    token = next(observation for observation in decoded["observations"] if not observation["asset"]["native"])
    assert token["owner"] is None
    assert {mapping["owner"] for mapping in decoded["ownership"]} == {OWNER, "different-owner"}


@pytest.mark.parametrize("program", [TOKEN, TOKEN_2022])
def test_wrap_then_transfer_uses_full_funding_and_receive_then_close_uses_received_units(program):
    mint = history.NATIVE_MINTS[program]
    wrapped = transaction([
        instruction(history.SYSTEM_PROGRAM, "createAccount", source=OWNER, newAccount="wrapped-A", lamports=6_002_000_000),
        instruction(program, "initializeAccount3", account="wrapped-A", owner=OWNER, mint=mint),
        instruction(program, "syncNative", account="wrapped-A"),
        instruction(program, "transferChecked", source="wrapped-A", destination="pool-wsol", mint=mint, tokenAmount={"amount": "4000000000", "decimals": 9}),
    ], keys=(OWNER, "wrapped-A", "pool-wsol"), pre=(11_000_000_000, 0, 2_000_000), post=(4_968_000_000, 2_002_000_000, 4_002_000_000),
        tokens_pre=[balance(2, 0, owner="pool", mint=mint, program=program, decimals=9)],
        tokens_post=[balance(1, 2_000_000_000, mint=mint, program=program, decimals=9), balance(2, 4_000_000_000, owner="pool", mint=mint, program=program, decimals=9)])
    decoded = decode(wrapped)
    assert decoded["gaps"] == []
    roles = {leg["role"]: leg["quantity"] for leg in decoded["legs"]}
    assert roles["wrap_native"] == roles["wrap_token"] == "6.000000000"
    assert roles["rent_deposit"] == "0.002000000"
    unwrapped = transaction([
        instruction(program, "transferChecked", source="pool-wsol", destination="wrapped-A", mint=mint, tokenAmount={"amount": "4000000000", "decimals": 9}),
        instruction(program, "closeAccount", account="wrapped-A", destination=OWNER, owner=OWNER),
    ], keys=(OWNER, "wrapped-A", "pool-wsol"), pre=(11_000_000_000, 2_002_000_000, 4_002_000_000), post=(16_972_000_000, 0, 2_000_000),
        tokens_pre=[balance(1, 2_000_000_000, mint=mint, program=program, decimals=9), balance(2, 4_000_000_000, owner="pool", mint=mint, program=program, decimals=9)],
        tokens_post=[balance(2, 0, owner="pool", mint=mint, program=program, decimals=9)])
    decoded = decode(unwrapped)
    assert decoded["gaps"] == []
    roles = {leg["role"]: leg["quantity"] for leg in decoded["legs"]}
    assert roles["unwrap_native"] == roles["unwrap_token"] == "6.000000000"
    assert roles["rent_refund"] == "0.002000000"
    for payload, role in ((wrapped, "wrap_native"), (unwrapped, "unwrap_native")):
        invalid = copy.deepcopy(payload)
        for field in ("preTokenBalances", "postTokenBalances"):
            for observed in invalid["meta"][field]:
                observed["uiTokenAmount"]["decimals"] = 6
        unknown = decode(invalid)
        assert "invalid_native_token_decimals" in unknown["gaps"]
        assert not any(leg["role"] == role for leg in unknown["legs"])


def test_atomic_native_wrap_and_jupiter_swap_reconciles_each_representation():
    raw = history._ROUTE_DISCRIMINATOR + b"synthetic-route"
    number, encoded = int.from_bytes(raw, "big"), ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = history._B58[remainder] + encoded
    mint = history.WSOL_MINT
    payload = transaction([
        instruction(history.SYSTEM_PROGRAM, "createAccount", source=OWNER, newAccount="owned-wsol", lamports=6_002_000_000),
        instruction(TOKEN, "initializeAccount3", account="owned-wsol", owner=OWNER, mint=mint),
        instruction(TOKEN, "syncNative", account="owned-wsol"),
        {"programId": history.JUPITER_PROGRAM, "data": encoded},
    ], keys=(OWNER, "owned-wsol", "pool-wsol", "pool-usdc", "owned-usdc"),
        pre=(11_000_000_000, 0, 2_000_000, 2_000_000, 2_000_000),
        post=(4_968_000_000, 2_002_000_000, 4_002_000_000, 2_000_000, 2_000_000),
        tokens_pre=[balance(2, 0, owner="pool", mint=mint, decimals=9), balance(3, 500_000_000, owner="pool", mint="synthetic-usdc"), balance(4, 0, mint="synthetic-usdc")],
        tokens_post=[balance(1, 2_000_000_000, mint=mint, decimals=9), balance(2, 4_000_000_000, owner="pool", mint=mint, decimals=9),
                     balance(3, 100_000_000, owner="pool", mint="synthetic-usdc"), balance(4, 400_000_000, mint="synthetic-usdc")])
    payload["meta"]["innerInstructions"] = [{"index": 3, "instructions": [
        instruction(TOKEN, "transferChecked", source="owned-wsol", destination="pool-wsol", mint=mint, tokenAmount={"amount": "4000000000", "decimals": 9}),
        instruction(TOKEN, "transferChecked", source="pool-usdc", destination="owned-usdc", mint="synthetic-usdc", tokenAmount={"amount": "400000000", "decimals": 6}),
    ]}]
    decoded = decode(payload)
    assert decoded["gaps"] == []
    assert {relation["kind"] for relation in decoded["relationships"]} == {"wrap", "swap"}
    economic = [leg for leg in decoded["legs"] if not leg.get("non_additive")]
    owner_native_change = sum((int(leg["raw_units"]) if leg["destination"] == OWNER else 0) - (int(leg["raw_units"]) if leg["source"] == OWNER else 0)
                              for leg in economic if leg["asset"]["native"])
    assert owner_native_change == -6_032_000_000
    owned_wrapped_change = sum((int(leg["raw_units"]) if leg["destination"] == "owned-wsol" else 0) - (int(leg["raw_units"]) if leg["source"] == "owned-wsol" else 0)
                               for leg in economic if leg["asset"]["mint"] == mint)
    assert owned_wrapped_change == 2_000_000_000
    assert next(leg["quantity"] for leg in economic if leg["destination"] == "owned-usdc") == "400.000000"
    payload["transaction"]["message"]["instructions"].append(instruction(TOKEN, "syncNative", account="owned-wsol"))
    ambiguous = decode(payload)
    assert "unresolved_wrap" in ambiguous["gaps"]
    assert not any(leg["role"] == "rent_deposit" for leg in ambiguous["legs"])
