"""Watch-only on-chain wallets: native balances and transfer history.

Nothing here ever holds a private key. A connection's credential is a list of
public addresses, so the connection is watch-only by construction rather than
by permission — there is no key to scope down and nothing a leak of the
credential blob would let an attacker move.

Three chain families, and they are not symmetric. Solana's JSON-RPC answers
"what did this address do" directly (``getSignaturesForAddress``), so both
balances and history come from a public node with no key. Bitcoin has no
account at all — an address is a set of unspent outputs — so both balance and
history come from an Esplora indexer, also keyless. EVM JSON-RPC has neither:
an address's history only exists in an indexer, so EVM balances come from a
public node and EVM history from Blockscout, or from Etherscan when a key is
set. That asymmetry is why the EVM path has a source to choose and the others
do not.

Quantities come from the chain; the *value* of one does not. USD pricing is
Coinbase's public rate table (see ``get_holdings``), which is unauthenticated
but is still a second upstream this module depends on.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Iterable, Optional

import httpx

from app.core.config import get_settings
from app.providers.coinbase import usd_spot_prices
from app.providers.onchain_reads import MAX_HISTORY_PAGES, ReadPending, TraceReads, interruption_code
from app.providers.onchain_transport import (
    OnchainDeadlineExceeded as OnchainDeadlineExceeded,
    OnchainRateLimited as OnchainRateLimited,
    request_json,
)
from app.providers.base import (
    AccountData,
    BankProvider,
    ConnectionData,
    HoldingData,
    PartialHoldings,
    ProviderNotConfiguredError,
    ProviderRateLimited,
    ProviderUserActionRequired,
    TransactionData,
)

logger = logging.getLogger(__name__)

ONCHAIN_HTTP_TIMEOUT = 30.0
# Public nodes answer a burst and then 429 for a moment. A trace is dozens of
# requests, so treating the first 429 as fatal would fail nearly every trace
# run against a default endpoint. Retries are few and the backoff short: this
# rides out a shared node's throttle, it does not queue behind a real outage.
RPC_RETRY_ATTEMPTS = 3
RPC_RETRY_BACKOFF_SECONDS = 1.5
# Aggregate requests per endpoint across traces, holdings, API and Celery
# processes. Redis applies this budget; it is not a semaphore per trace.
TX_FETCH_CONCURRENCY = 5
MAX_WATCHED_ADDRESSES = 25
# Both history sources page newest-first. Asking for a big page is how an
# address's *rate* becomes visible, which is what tells a pooled address apart
# from a busy wallet: see `_saturation`.
SOLANA_SIGNATURE_PAGE = 1000
SOLANA_HISTORY_MAX_PAGES = 4
EVM_HISTORY_PAGE = 1000
# Blockscout is the keyless EVM index. Its page size is fixed, so depth costs
# requests rather than a bigger ask — and a deep page into a busy address is
# where it stops answering at all, which is why the page bound is low and the
# rate test below runs per page rather than after the paging.
BLOCKSCOUT_PAGE = 50
BLOCKSCOUT_HISTORY_MAX_PAGES = 2
# Esplora's page size is fixed at 25 and not negotiable, so depth comes from
# asking again rather than asking for more. The cap bounds a walk's request
# count; paging stops early once a page reaches past the window anyway.
BITCOIN_HISTORY_PAGE = 25
BITCOIN_HISTORY_MAX_PAGES = 8
# A full page spanning less than this triggers a high-activity stop. This
# heuristic does not establish that an address is pooled or identify its owner.
#
# The two chain families do not measure the same thing here, and the Solana
# side is the looser of the two: `getSignaturesForAddress` returns every
# signature an address *appears in*, as fee payer or program account included,
# not only the ones that moved its balance. A bot-run or DEX-heavy Solana
# wallet can clear the page in a day and be called pooled when it is not.
# Erring that way is deliberate — the trace stops and says it stopped, which
# is recoverable, where expanding a genuine exchange invents a trail.
POOLED_SPAN_SECONDS = 24 * 3600
SATURATED_POOLED = "pooled"
SATURATED_UNPAGEABLE = "unpageable"
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"

# SPL tokens live in accounts owned by one of two programs — the original and
# Token-2022 — and a wallet holding both kinds answers only half the question
# if just one is asked for.
SOLANA_TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)
# A token account names a mint, never a symbol, so identity comes from an
# index. Jupiter's is keyless, answers in one batched call, and — decisively —
# prices by mint rather than by symbol, so a token *claiming* to be USDC is
# quoted as the worthless thing it is instead of at a dollar.
JUPITER_TOKEN_URL = "https://lite-api.jup.ag/tokens/v2/search"
JUPITER_QUERY_BATCH = 50
# A spammed address holds thousands of airdropped tokens. The cap is applied
# after ranking, and the ranking puts vouched tokens ahead of unvouched ones
# before it looks at value at all. Ranking on value alone would hand the cap to
# an attacker: minting 25 tokens quoted at a million dollars each is cheap, and
# they would sort above every real position and push it out of the payload —
# which the sync layer reads as the position being gone.
MAX_TOKENS_PER_ADDRESS = 25

_SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Shape only. Both Bitcoin forms carry a checksum, and the checksum is what
# actually decides — see `_bitcoin_address_valid`.
_BITCOIN_LEGACY = re.compile(r"^[13][1-9A-HJ-NP-Za-km-z]{25,34}$")
_BITCOIN_BECH32 = re.compile(r"^(bc1|BC1)[023456789acdefghjklmnpqrstuvwxyzACDEFGHJKLMNPQRSTUVWXYZ]{11,71}$")

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _base58check_payload(address: str) -> Optional[bytes]:
    """Decode a base58check string, or None when its checksum does not hold."""
    number = 0
    for char in address:
        index = _B58_ALPHABET.find(char)
        if index < 0:
            return None
        number = number * 58 + index
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    raw = b"\x00" * (len(address) - len(address.lstrip("1"))) + raw
    if len(raw) < 5:
        return None
    body, checksum = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != checksum:
        return None
    return body


def _bech32_polymod(values: list[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for bit in range(5):
            checksum ^= generator[bit] if (top >> bit) & 1 else 0
    return checksum


def _witness_program(values: list[int]) -> Optional[bytes]:
    """Regroup the 5-bit data section into bytes, or None if it does not divide.

    Leftover bits are only legal as zero padding, and fewer than eight of them.
    Anything else is a re-encoding of a different program that happens to hit the
    same checksum, so it is rejected rather than truncated.
    """
    accumulator = bits = 0
    out = bytearray()
    for value in values:
        accumulator = (accumulator << 5) | value
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((accumulator >> bits) & 0xFF)
    if bits >= 5 or (accumulator << (8 - bits)) & 0xFF:
        return None
    return bytes(out)


def _bech32_valid(address: str) -> bool:
    """Validate a mainnet segwit address: checksum, witness version and length.

    Both checksum constants are needed: BIP-173 (witness v0, the ``bc1q``
    addresses) ends on 1, and BIP-350 (v1+, Taproot's ``bc1p``) on a different
    one after an earlier length-extension flaw. Accepting either constant for
    either version would wave through the exact substitution the split was made
    to stop.

    The program's length is checked here rather than left to the node. A
    checksum-valid address with a malformed program is one the index rejects,
    and `_native_holding` turns a rejected read into a failed sync — so letting
    one through would wedge the whole connection rather than the one address.
    """
    if address != address.lower() and address != address.upper():
        return False  # mixed case is unspecified, and a wallet never emits it
    hrp, separator, data = address.lower().rpartition("1")
    if separator != "1" or hrp != "bc" or len(data) < 7:
        return False
    try:
        values = [_BECH32_CHARSET.index(char) for char in data]
    except ValueError:
        return False
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    version = values[0]
    if version > 16:
        return False
    if _bech32_polymod(expanded + values) != (1 if version == 0 else 0x2BC830A3):
        return False
    program = _witness_program(values[1:-6])
    if program is None or not 2 <= len(program) <= 40:
        return False
    # v0 is only ever a 20-byte key hash or a 32-byte script hash.
    return version != 0 or len(program) in (20, 32)


def _bitcoin_address_valid(address: str) -> bool:
    if _BITCOIN_BECH32.match(address):
        return _bech32_valid(address)
    if _BITCOIN_LEGACY.match(address):
        payload = _base58check_payload(address)
        # 0x00 pay-to-pubkey-hash, 0x05 pay-to-script-hash. Any other version
        # byte is another network's address wearing a mainnet shape.
        return payload is not None and len(payload) == 21 and payload[0] in (0x00, 0x05)
    return False


@dataclass(frozen=True)
class Chain:
    """One supported network.

    ``symbol`` is the native coin's ticker, which is what a holding is
    denominated in. ``explorer_chain_id`` is Etherscan's V2 chain id, and every
    EVM chain here has one — Solana and Bitcoin never reach the explorer path.

    ``token_index_url`` is a Blockscout instance, and only the EVM chains have
    one: an ERC-20 balance is a mapping inside a contract, so listing what an
    address holds means asking an index rather than the chain. Solana carries
    its token accounts on-chain and reads them over its own RPC; Bitcoin has no
    tokens at all, so both leave it None for different reasons.
    """

    key: str
    kind: str  # "solana" | "evm" | "bitcoin"
    display_name: str
    symbol: str
    decimals: int
    default_rpc_url: str
    explorer_chain_id: Optional[int] = None
    token_index_url: Optional[str] = None


CHAINS: dict[str, Chain] = {
    "solana": Chain(
        key="solana",
        kind="solana",
        display_name="Solana",
        symbol="SOL",
        decimals=9,
        default_rpc_url="https://api.mainnet-beta.solana.com",
    ),
    "bitcoin": Chain(
        key="bitcoin",
        kind="bitcoin",
        display_name="Bitcoin",
        symbol="BTC",
        decimals=8,
        # Esplora's REST API, not a JSON-RPC node: a bitcoind RPC cannot answer
        # "what did this address do" either, so a watch-only wallet needs an
        # index whichever way it is reached. Blockstream runs the reference
        # instance keyless; mempool.space speaks the same routes, so
        # ONCHAIN_RPC_URLS can point at it or at a self-hosted Esplora.
        default_rpc_url="https://blockstream.info/api",
    ),
    "ethereum": Chain(
        key="ethereum",
        kind="evm",
        display_name="Ethereum",
        symbol="ETH",
        decimals=18,
        default_rpc_url="https://eth.llamarpc.com",
        explorer_chain_id=1,
        token_index_url="https://eth.blockscout.com",
    ),
    "base": Chain(
        key="base",
        kind="evm",
        display_name="Base",
        symbol="ETH",
        decimals=18,
        default_rpc_url="https://mainnet.base.org",
        explorer_chain_id=8453,
        token_index_url="https://base.blockscout.com",
    ),
    "polygon": Chain(
        key="polygon",
        kind="evm",
        display_name="Polygon",
        symbol="POL",
        decimals=18,
        default_rpc_url="https://polygon-rpc.com",
        explorer_chain_id=137,
        token_index_url="https://polygon.blockscout.com",
    ),
}

# Every watched address hangs off one account per connection, so the holdings
# land in a single Wallet with one tax character — the shape Coinbase uses.
# Accounts are keyed per connection, so a fixed id here cannot collide.
ACCOUNT_EXTERNAL_ID = "onchain:wallets"


def rpc_url(chain: Chain) -> str:
    return get_settings().onchain_rpc_urls.get(chain.key) or chain.default_rpc_url


def address_is_valid(chain: Chain, address: str) -> bool:
    if chain.kind == "bitcoin":
        return _bitcoin_address_valid((address or "").strip())
    pattern = _EVM_ADDRESS if chain.kind == "evm" else _SOLANA_ADDRESS
    return bool(pattern.match(address or ""))


def normalize_address(chain: Chain, address: str) -> str:
    """Case-insensitive forms are folded, so casing cannot mint a second wallet.

    EVM addresses are hex and case only carries an optional checksum. Bech32 is
    case-insensitive too, but only bech32 — a legacy base58 address means a
    different number in a different case, so folding one would corrupt it.
    """
    cleaned = (address or "").strip()
    if chain.kind == "evm":
        return cleaned.lower()
    if chain.kind == "bitcoin" and cleaned[:3].lower() == "bc1":
        return cleaned.lower()
    return cleaned


@dataclass(frozen=True)
class WatchedAddress:
    chain: Chain
    address: str

    @property
    def external_id(self) -> str:
        return f"{self.chain.key}:{self.address}"

    @property
    def short(self) -> str:
        return f"{self.address[:4]}…{self.address[-4:]}"


@dataclass(frozen=True)
class Transfer:
    """One native-coin movement, normalized across chains.

    ``amount`` is always positive and always in whole coins; ``sender`` and
    ``recipient`` say which way it went. Storing a signed delta instead would
    lose the counterparty, which is the only field a trace actually walks.
    """

    chain_key: str
    reference: str  # signature (Solana) or tx hash (EVM)
    occurred_at: datetime
    sender: str
    recipient: str
    amount: Decimal

    def counterparty(self, address: str) -> str:
        return self.recipient if self.sender == address else self.sender

    def direction(self, address: str) -> str:
        return "out" if self.sender == address else "in"


@dataclass
class TransferCoverage:
    """Measured endpoint-visible native history, not a lifetime ledger.

    Observed/examined extrema describe rows, not continuous readable intervals.
    A cursor resumes the signature list only; omitted payloads remain a gap.
    """

    requested_since: datetime | None = None
    requested_until: datetime | None = None
    fetched_at: datetime | None = field(default_factory=lambda: datetime.now(timezone.utc))
    observed_oldest: datetime | None = None
    observed_newest: datetime | None = None
    examined_oldest: datetime | None = None
    examined_newest: datetime | None = None
    since_reached: bool | None = None
    until_reached: bool | None = None
    provider_exhausted: bool | None = None
    pages_read: int = 0
    rows_read: int = 0
    signatures_read: int | None = None
    payloads_requested: int | None = None
    payloads_read: int | None = None
    missing_timestamps: int = 0
    missing_payloads: int = 0
    unsupported_payloads: int = 0
    failed_payloads: int = 0
    pending_payloads: int = 0
    omitted_signatures: int | None = None
    omitted_transfers: int | None = None
    next_cursor: str | None = None
    stop_reasons: list[str] = field(default_factory=list)

    def gap(self, reason: str) -> None:
        if reason not in self.stop_reasons:
            self.stop_reasons.append(reason)

    def observe(self, moment: datetime, *, examined: bool = False) -> None:
        if examined:
            self.examined_oldest = min(self.examined_oldest or moment, moment)
            self.examined_newest = max(self.examined_newest or moment, moment)
        else:
            self.observed_oldest = min(self.observed_oldest or moment, moment)
            self.observed_newest = max(self.observed_newest or moment, moment)

    def finish(self, exhausted: bool | None) -> None:
        self.provider_exhausted = exhausted
        # Malformed or reordered history cannot prove a continuous boundary
        # from its timestamp extrema; explicit source exhaustion is separate.
        ordered = not {"invalid_row", "invalid_page", "missing_timestamp"}.intersection(self.stop_reasons)
        if self.requested_since is not None:
            self.since_reached = exhausted is True or (
                ordered and self.observed_oldest is not None and self.observed_oldest < self.requested_since
            )
        if self.requested_until is not None:
            self.until_reached = exhausted is True or (
                ordered and self.observed_oldest is not None and self.observed_oldest <= self.requested_until
            )
        if self.since_reached is False or self.until_reached is False:
            self.gap("window_not_reached")

    @property
    def complete(self) -> bool:
        return (
            not self.stop_reasons
            and (self.since_reached is True or self.provider_exhausted is True)
            and self.until_reached is not False
        )


@dataclass(frozen=True)
class Transfers:
    """A page of transfers, plus every way that page falls short of the truth.

    A caller deciding "nothing moved" needs to know it saw everything, so each
    field below exists to stop that claim being made on partial evidence.

    ``saturated`` is None when the address's history was answerable at all.
    Otherwise it names which way it was not:

    * ``SATURATED_POOLED`` — a high-activity heuristic stopped the walk;
      this does not establish pooling, an exchange, or ownership.
    * ``SATURATED_UNPAGEABLE`` — so much newer history that a full page never
      reached back to the window asked about.

    ``trimmed`` means more transfers sat inside the window than ``limit``
    allowed, so ``items`` is a slice and the largest transfer may not be in it.
    ``unreadable`` counts transactions the node would not return. Both make
    "nothing moved out of here" unsayable.
    """

    items: list[Transfer]
    saturated: Optional[str] = None
    trimmed: bool = False
    unreadable: int = 0
    coverage: TransferCoverage | None = None
    interruption: str | None = None
    retry_after_seconds: int | None = None
    resumable: bool = False

    @property
    def complete(self) -> bool:
        return (
            self.saturated is None and not self.trimmed and not self.unreadable
            and (self.coverage is None or self.coverage.complete)
        )


@dataclass(frozen=True)
class TokenHolding:
    """One non-native token an address holds.

    ``usd_price`` is what the index quotes, and ``trusted`` is whether that
    quote may move a net worth. The two are kept apart on purpose: a token is
    listed on the strength of having a market at all, but only counted on the
    strength of the index vouching for it, because minting an airdrop with a
    manipulated price is cheap and a portfolio total is the thing it would be
    minted to attack.
    """

    chain_key: str
    contract: str  # SPL mint, or ERC-20 contract address
    symbol: str
    quantity: Decimal
    usd_price: Optional[Decimal]
    trusted: bool

    @property
    def quoted_value(self) -> Decimal:
        """What the index says it is worth, vouched for or not.

        Only meaningful next to ``trusted``. An unvouched quote is a number an
        attacker chose, so this may not order a vouched holding — see
        ``rank`` — and may not reach a total.
        """
        return self.quantity * self.usd_price if self.usd_price is not None else Decimal("0")

    @property
    def rank(self) -> tuple[bool, Decimal]:
        """Sort key for the cap: vouched first, then value.

        Value alone would let anyone who can mint a token decide which of a
        user's real holdings stay in the payload.
        """
        return self.trusted, self.quoted_value


def detect_chain(address: str) -> Optional[Chain]:
    """Guess the network from the address shape.

    A bare EVM address resolves to Ethereum because the same bytes are a valid
    address on every EVM chain and only the user knows which they meant. An
    unrecognized shape returns None so the caller can reject it by name rather
    than silently pick a chain.

    Bitcoin is tried before Solana because their base58 forms overlap: a legacy
    ``1``/``3`` address is 26–35 characters and so also matches the Solana
    pattern. Only Bitcoin's carries a checksum, so a base58check that verifies
    settles it — a Solana key passing that test by chance is a 1-in-2^32 event,
    and the ``solana:`` prefix overrides it anyway.
    """
    if _EVM_ADDRESS.match(address):
        return CHAINS["ethereum"]
    if _bitcoin_address_valid(address):
        return CHAINS["bitcoin"]
    if _SOLANA_ADDRESS.match(address):
        return CHAINS["solana"]
    return None


def parse_addresses(raw: str) -> list[WatchedAddress]:
    """Read the pasted credential blob into watched addresses.

    One address per line, optionally prefixed with ``chain:``. Duplicates are
    dropped rather than rejected — pasting the same address twice is a typo,
    not a reason to refuse the whole connection.
    """
    watched: list[WatchedAddress] = []
    seen: set[str] = set()
    for line_no, line in enumerate(raw.splitlines(), start=1):
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        chain_key, _, address = entry.rpartition(":")
        address = address.strip()
        if chain_key:
            chain = CHAINS.get(chain_key.strip().lower())
            if chain is None:
                raise ProviderUserActionRequired(
                    f"Line {line_no}: unknown chain {chain_key.strip()!r}. "
                    f"Supported: {', '.join(sorted(CHAINS))}.",
                    code="onchain_unknown_chain",
                )
        else:
            chain = detect_chain(address)
            if chain is None:
                raise ProviderUserActionRequired(
                    f"Line {line_no}: {entry!r} is not a Solana or EVM address. "
                    "Prefix it with a chain (e.g. base:0x…) if it is.",
                    code="onchain_bad_address",
                )
        if not address_is_valid(chain, address):
            raise ProviderUserActionRequired(
                f"Line {line_no}: {address!r} is not a valid {chain.display_name} address.",
                code="onchain_bad_address",
            )
        normalized = normalize_address(chain, address)
        entry_id = f"{chain.key}:{normalized}"
        if entry_id in seen:
            continue
        seen.add(entry_id)
        watched.append(WatchedAddress(chain=chain, address=normalized))
    if not watched:
        raise ProviderUserActionRequired(
            "Paste at least one wallet address, one per line.",
            code="onchain_no_addresses",
        )
    if len(watched) > MAX_WATCHED_ADDRESSES:
        raise ProviderUserActionRequired(
            f"One connection watches at most {MAX_WATCHED_ADDRESSES} addresses; "
            f"got {len(watched)}. Split them across connections.",
            code="onchain_too_many_addresses",
        )
    return watched


def _scale(raw: Any, decimals: int) -> Optional[Decimal]:
    """Base units to whole coins, or None when the node sent something else.

    ``scaleb`` rather than division: a wei-denominated balance has more
    significant digits than the default decimal context keeps, and dividing
    would round the tail off a number that is exact as an integer.
    """
    try:
        units = Decimal(int(raw, 16) if isinstance(raw, str) and raw.startswith("0x") else int(raw))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return units.scaleb(-decimals)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=ONCHAIN_HTTP_TIMEOUT,
        headers={"Accept": "application/json", "User-Agent": "Securo/0.1 (+https://usesecuro.com)"},
    )


@asynccontextmanager
async def session() -> AsyncIterator[httpx.AsyncClient]:
    """One client for a whole walk, so its hundreds of calls share connections.

    A client per request costs a TCP and TLS handshake per request, which
    against a shared public node is most of what the node sees us do.
    """
    async with _client() as client:
        yield client


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise OnchainDeadlineExceeded("On-chain read deadline exceeded")


async def _request_json(
    method: str,
    url: str,
    label: str,
    client: httpx.AsyncClient | None,
    *,
    endpoint: str,
    deadline: float | None = None,
    json_body: dict | None = None,
    params: dict | None = None,
    rpc: bool = False,
) -> Any:
    _check_deadline(deadline)
    if client is None:
        async with _client() as own:
            return await _request_json(
                method, url, label, own, endpoint=endpoint, deadline=deadline,
                json_body=json_body, params=params, rpc=rpc,
            )
    return await request_json(
        client, method, url, endpoint=endpoint, label=label, deadline=deadline,
        attempts=RPC_RETRY_ATTEMPTS, backoff=RPC_RETRY_BACKOFF_SECONDS,
        timeout=ONCHAIN_HTTP_TIMEOUT, concurrency=TX_FETCH_CONCURRENCY,
        json_body=json_body, params=params, rpc=rpc,
    )


async def _json_rpc(
    chain: Chain, method: str, params: list[Any], *, client: Optional[httpx.AsyncClient] = None,
    deadline: float | None = None,
) -> Any:
    """One JSON-RPC call, with a node's failure modes mapped to ours."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    endpoint = rpc_url(chain)
    payload = await _request_json(
        "POST", endpoint, f"{chain.display_name} RPC", client,
        endpoint=endpoint, deadline=deadline, json_body=body, rpc=True,
    )
    return payload.get("result") if isinstance(payload, dict) else None


async def native_balance(
    chain: Chain, address: str, *, client: Optional[httpx.AsyncClient] = None,
    deadline: float | None = None,
) -> Optional[Decimal]:
    """Native-coin balance in whole coins, or None when the node won't say."""
    _check_deadline(deadline)
    if chain.kind == "bitcoin":
        return await _bitcoin_balance(chain, address, client=client, deadline=deadline)
    if chain.kind == "solana":
        result = await _json_rpc(chain, "getBalance", [address], client=client, deadline=deadline)
        raw = result.get("value") if isinstance(result, dict) else None
    else:
        raw = await _json_rpc(
            chain, "eth_getBalance", [address, "latest"], client=client, deadline=deadline
        )
    return _scale(raw, chain.decimals) if raw is not None else None


def _within(timestamp: float, since: Optional[datetime], until: Optional[datetime]) -> bool:
    if since is not None and timestamp < since.timestamp():
        return False
    return until is None or timestamp <= until.timestamp()


def _history_time(raw: Any) -> datetime | None:
    """Unknown/invalid upstream timestamps never become the current instant."""
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _solana_error_is_valid(row: dict) -> bool:
    """The RPC error field is null, a named error, or an error object."""
    if "err" not in row:
        return False
    error = row["err"]
    return error is None or (isinstance(error, (dict, str)) and bool(error))


def _closest_to_horizon(rows: list, limit: int, since: Optional[datetime]) -> list:
    """Keep the ``limit`` rows nearest the window's anchor, newest-first order kept.

    Both history sources page newest-first, so a naive head slice keeps an
    address's most recent activity — the wrong end whenever a floor is set.
    Money leaves an address soon after it arrives, not months later, so the
    rows just *after* the floor are the ones carrying it. Slicing the head
    instead silently traces a different, later movement of the same wallet and
    reports it as this one's path, which is a wrong answer rather than a
    partial one.

    Tracing backwards sets a ceiling instead, and there the head slice is
    already correct: newest-first puts the rows nearest the ceiling first.
    """
    if len(rows) <= limit:
        return rows
    return rows[-limit:] if since is not None else rows[:limit]


def _saturation(
    timestamps: list[int], page_size: int, since: Optional[datetime]
) -> Optional[str]:
    """Why a page of history cannot answer, or None when it can.

    Only a page that came back full is suspect — a short page is the whole
    history and answers by definition. A full one is read two ways: it spans
    too little time to be a person's wallet, or it never reached back far
    enough to contain the window asked about.
    """
    if len(timestamps) < page_size or not timestamps:
        return None
    newest, oldest = max(timestamps), min(timestamps)
    if newest - oldest <= POOLED_SPAN_SECONDS:
        return SATURATED_POOLED
    if since is not None and oldest > since.timestamp():
        return SATURATED_UNPAGEABLE
    return None


async def transfers(
    chain: Chain,
    address: str,
    *,
    limit: int,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    client: Optional[httpx.AsyncClient] = None,
    deadline: float | None = None,
    read_state: dict[str, Any] | None = None,
) -> Transfers:
    """Native-coin transfers touching ``address``, newest first.

    ``since``/``until`` are applied before the expensive per-transaction fetch,
    not after, so narrowing the window narrows the request count too. A
    saturated address returns its verdict without its history: a caller that
    will not walk past one has no use for it, and fetching it anyway costs a
    request per transaction for an answer already known to be discarded.
    """
    since = since.replace(tzinfo=timezone.utc) if since is not None and since.tzinfo is None else since
    until = until.replace(tzinfo=timezone.utc) if until is not None and until.tzinfo is None else until
    since = since.astimezone(timezone.utc) if since is not None else None
    until = until.astimezone(timezone.utc) if until is not None else None
    if since is not None and until is not None and since > until:
        raise ValueError("since must be before or equal to until")
    address = normalize_address(chain, address)
    source, window = _read_identity(chain, address, limit, since, until)
    reads = TraceReads(read_state if read_state is not None else {}, chain.key, source, window)
    reader = {"solana": _solana_transfers, "bitcoin": _bitcoin_transfers}.get(chain.kind, _evm_transfers)
    arguments: dict[str, Any] = dict(
        limit=limit, since=since, until=until, client=client, deadline=deadline, reads=reads,
    )
    error: BaseException | None = None
    try:
        _check_deadline(deadline)
        page = await reader(chain, address, **arguments)
    except (Exception, asyncio.CancelledError) as exc:
        # The reader has drained its children. Re-run only the retained
        # observations so a timeout cannot strand already returned evidence.
        error = reads.error = exc
        reads.replay = True
        page = await reader(chain, address, **arguments)
    reasons = page.coverage.stop_reasons if page.coverage else []
    if reads.interruption and page.coverage:
        page.coverage.gap(reads.interruption)
    if reads.state.get("limited") and page.coverage:
        page.coverage.gap("retention_limit")
    page = Transfers(
        items=page.items, saturated=page.saturated, trimmed=page.trimmed,
        unreadable=page.unreadable, coverage=page.coverage,
        interruption=reads.interruption, retry_after_seconds=reads.retry_after_seconds,
        resumable=not reads.state.get("limited", False) and "retention_limit" not in reasons and bool(
            reads.interruption or {"provider_page_limit", "missing_payload"}.intersection(reasons)
            or (page.coverage and (page.coverage.failed_payloads or page.coverage.pending_payloads))
        ),
    )
    reads.history["snapshot"] = json.loads(json.dumps(asdict(page), default=_read_json))
    if isinstance(error, (asyncio.CancelledError, ProviderNotConfiguredError)):
        raise error
    # Preserve callers' empty-failure behavior while exposing recoverable state
    # to the service and returning every useful successful sibling.
    if error is not None and not page.items and read_state is None:
        raise error
    return page


def _read_json(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _read_identity(
    chain: Chain, address: str, limit: int, since: datetime | None, until: datetime | None,
) -> tuple[str, str]:
    source = rpc_url(chain)
    if chain.kind == "evm":
        key = get_settings().etherscan_api_key
        source = f"{ETHERSCAN_V2_URL}:{key}" if key else str(chain.token_index_url)
    source = chain.key + ":" + hashlib.sha256(source.encode()).hexdigest()
    bounds = [moment.replace(tzinfo=moment.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
              if moment is not None else None for moment in (since, until)]
    return source, json.dumps([source, normalize_address(chain, address), limit, *bounds])


def retained_transfers(
    chain: Chain, address: str, *, limit: int, read_state: dict[str, Any],
    since: datetime | None = None, until: datetime | None = None,
) -> Transfers | None:
    """Last partial page, including successes saved before deadline cancellation."""
    _, window = _read_identity(chain, address, limit, since, until)
    saved = read_state.get("histories", {}).get(window, {}).get("snapshot")
    if saved is None:
        return None
    payload = dict(saved)
    payload["items"] = [Transfer(
        chain_key=item["chain_key"], reference=item["reference"], sender=item["sender"],
        recipient=item["recipient"], amount=Decimal(item["amount"]),
        occurred_at=datetime.fromisoformat(item["occurred_at"]),
    ) for item in saved["items"]]
    if saved["coverage"] is not None:
        dates = {"requested_since", "requested_until", "fetched_at", "observed_oldest",
                 "observed_newest", "examined_oldest", "examined_newest"}
        coverage: dict[str, Any] = {
            key: datetime.fromisoformat(value) if key in dates and value is not None else value
            for key, value in saved["coverage"].items()
        }
        payload["coverage"] = TransferCoverage(**coverage)
    return Transfers(**payload)


async def _solana_transfers(
    chain: Chain,
    address: str,
    *,
    limit: int,
    since: Optional[datetime],
    until: Optional[datetime],
    client: Optional[httpx.AsyncClient],
    deadline: float | None = None,
    reads: TraceReads | None = None,
) -> Transfers:
    """Walk the address's signatures, then read each transaction's balance deltas.

    The amount of a Solana transfer is not in the signature list — only in the
    transaction — so this is unavoidably one request per transaction, which is
    why they go out concurrently under the shared endpoint budget.

    Amounts come from pre/post balance deltas rather than from parsed
    ``system.transfer`` instructions, because a drainer's sweep is often a
    CPI from a program and never appears as a top-level transfer.
    """
    coverage = TransferCoverage(
        since, until, signatures_read=0, payloads_requested=0, payloads_read=0,
        omitted_signatures=0,
    )
    if reads:
        coverage.fetched_at = datetime.fromisoformat(reads.history["fetched_at"])
    rows: list[dict] = []
    seen: set[str] = set()
    exhausted: bool | None = False
    saturated: str | None = None
    previous_time: int | None = None
    page_limit = reads.page_limit("solana", SOLANA_HISTORY_MAX_PAGES) if reads else SOLANA_HISTORY_MAX_PAGES
    for page_number in range(page_limit):
        params: dict[str, Any] = {"limit": SOLANA_SIGNATURE_PAGE, "commitment": "finalized"}
        if coverage.next_cursor is not None:
            params["before"] = coverage.next_cursor
        try:
            async def read_page():
                return await _json_rpc(
                    chain, "getSignaturesForAddress", [address, params],
                    client=client, deadline=deadline,
                )
            signatures = await reads.read(
                f"solana:{coverage.next_cursor or ''}", read_page,
                lambda value: isinstance(value, list) and all(
                    isinstance(row, dict) and isinstance(row.get("signature"), str)
                    and bool(row["signature"]) for row in value
                ),
            ) if reads else await read_page()
        except ReadPending:
            coverage.gap(reads.interruption if reads and reads.interruption else "provider_unavailable")
            break
        except (ProviderRateLimited, OnchainDeadlineExceeded):
            raise
        except Exception:
            if page_number == 0:
                raise
            coverage.gap("provider_unavailable")
            break
        if not isinstance(signatures, list):
            coverage.gap("invalid_page")
            exhausted = None
            break
        coverage.pages_read += 1
        coverage.rows_read += len(signatures)
        coverage.signatures_read = coverage.rows_read
        if len(signatures) > SOLANA_SIGNATURE_PAGE:
            coverage.gap("invalid_page")
        page_times: list[int] = []
        cursor: str | None = None
        invalid_row = False
        new_signatures = 0
        for row in signatures:
            if not isinstance(row, dict) or not isinstance(row.get("signature"), str) or not row["signature"]:
                coverage.gap("invalid_row")
                invalid_row = True
                continue
            cursor = row["signature"]
            moment = _history_time(row.get("blockTime"))
            if moment is not None:
                stamp = int(moment.timestamp())
                if previous_time is not None and stamp > previous_time:
                    coverage.gap("invalid_row")
                previous_time = stamp
                page_times.append(stamp)
            if cursor in seen:
                coverage.gap("nonadvancing_cursor")
                continue
            seen.add(cursor)
            new_signatures += 1
            if moment is None:
                coverage.missing_timestamps += 1
                coverage.gap("missing_timestamp")
                continue
            coverage.observe(moment)
            if not _solana_error_is_valid(row):
                coverage.gap("invalid_row")
                continue
            if row.get("err") is None:
                rows.append({**row, "blockTime": int(moment.timestamp())})
        if signatures and (cursor is None or cursor == coverage.next_cursor or not new_signatures):
            coverage.gap("nonadvancing_cursor")
            break
        coverage.next_cursor = cursor
        if len(signatures) < SOLANA_SIGNATURE_PAGE:
            exhausted = None if invalid_row else True
            coverage.next_cursor = None
            break
        if len(page_times) == len(signatures) and not invalid_row and not coverage.stop_reasons:
            if since is not None and max(page_times) < since.timestamp():
                break  # Activity wholly before the window cannot discard its evidence.
            if _saturation(page_times, SOLANA_SIGNATURE_PAGE, None) == SATURATED_POOLED:
                saturated = SATURATED_POOLED
                coverage.gap("high_activity")
                break
            if since is not None and min(page_times) < since.timestamp():
                break  # Strict crossing preserves ties at an inclusive floor.
    else:
        coverage.gap("provider_page_limit")
        if page_limit >= MAX_HISTORY_PAGES:
            coverage.gap("retention_limit")
    coverage.finish(exhausted)
    if saturated:
        return Transfers(items=[], saturated=saturated, coverage=coverage)
    in_window = sorted(
        (row for row in rows if _within(row["blockTime"], since, until)),
        key=lambda row: (-row["blockTime"], row["signature"]),
    )
    wanted = _closest_to_horizon(in_window, limit, since)
    if reads:
        # Continue fills the original payload allowance; it does not replace
        # successfully examined selections with a new set of transactions.
        selected = reads.history.setdefault("selected_signatures", [])
        selected.extend(row["signature"] for row in wanted if row["signature"] not in selected)
        del selected[limit:]
        wanted = [row for row in in_window if row["signature"] in selected]
    coverage.omitted_signatures = len(in_window) - len(wanted)
    if coverage.omitted_signatures:
        coverage.gap("payload_limit")

    async def fetch(signature: str) -> Any:
        async def read_transaction():
            return await _json_rpc(
                chain, "getTransaction", [signature, {
                    "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                    "commitment": "finalized",
                }], client=client, deadline=deadline,
            )
        try:
            tx = await reads.read(signature, read_transaction, _solana_payload_valid, transaction=True) if reads else await read_transaction()
        except ReadPending:
            return None
        except BaseException as exc:
            if reads:
                reads.history["payloads"][signature] = (
                    "pending_payload" if isinstance(exc, asyncio.CancelledError)
                    else interruption_code(exc)
                )
            raise
        if reads:
            reads.history["payloads"][signature] = (
                "missing_payload" if tx is None else "completed" if _solana_payload_valid(tx)
                else "unsupported_payload"
            )
        return tx

    if not reads or not reads.replay:
        _check_deadline(deadline)
    coverage.payloads_requested = len(wanted)
    fetched: list[Any] = []
    for start in range(0, len(wanted), TX_FETCH_CONCURRENCY):
        tasks = [asyncio.create_task(fetch(row["signature"])) for row in wanted[start:start + TX_FETCH_CONCURRENCY]]
        try:
            fetched.extend(await asyncio.gather(*tasks))
        finally:
            # Stop scheduling and drain before the shared client can close.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    found: list[Transfer] = []
    unreadable = 0
    for row, tx in zip(wanted, fetched):
        if tx is None:
            reason = reads.history["payloads"].get(row["signature"], "pending_payload") if reads else "missing_payload"
            if reason == "pending_payload":
                coverage.pending_payloads += 1
            else:
                coverage.failed_payloads += 1
                if reason == "missing_payload":
                    coverage.missing_payloads += 1
            coverage.gap(reason)
            unreadable += 1
            continue
        coverage.payloads_read = (coverage.payloads_read or 0) + 1
        moment = _history_time(tx.get("blockTime")) if isinstance(tx, dict) else None
        if moment is not None:
            coverage.observe(moment, examined=True)
        elif isinstance(tx, dict):
            coverage.missing_timestamps += 1
            coverage.gap("missing_timestamp")
        deltas = _solana_deltas(chain, address, row["signature"], tx)
        if deltas is None:
            # The node would not return the transaction, or returned one this
            # code cannot attribute. Either way the movement is unknown, and an
            # unknown must not read as an absence.
            unreadable += 1
            coverage.unsupported_payloads += 1
            if not _solana_payload_valid(tx):
                coverage.failed_payloads += 1
            coverage.gap("unsupported_payload")
            continue
        # Recheck the payload timestamp: a disagreement with the list must not
        # put an out-of-window transfer into the trace.
        if moment is None or moment.timestamp() != row["blockTime"]:
            coverage.gap("invalid_row")
        found.extend(t for t in deltas if _within(t.occurred_at.timestamp(), since, until))
    return Transfers(
        items=found,
        trimmed=len(wanted) < len(in_window),
        unreadable=unreadable,
        coverage=coverage,
    )


def _solana_payload_valid(tx: Any) -> bool:
    """A reusable transaction observation, independent of address attribution."""
    if not isinstance(tx, dict) or _history_time(tx.get("blockTime")) is None:
        return False
    meta, transaction = tx.get("meta"), tx.get("transaction")
    if not isinstance(meta, dict) or not _solana_error_is_valid(meta) or not isinstance(transaction, dict):
        return False
    message = transaction.get("message")
    if not isinstance(message, dict):
        return False
    keys, pre, post = message.get("accountKeys"), meta.get("preBalances"), meta.get("postBalances")
    return (
        all(isinstance(values, list) for values in (keys, pre, post))
        and bool(keys) and len(keys) == len(pre) == len(post)
        and all(isinstance(key, dict) and isinstance(key.get("pubkey"), str) and key["pubkey"] for key in keys)
        and len({key["pubkey"] for key in keys}) == len(keys)
        and all(type(value) is int and value >= 0 for value in pre + post)
    )


def _solana_deltas(
    chain: Chain, address: str, signature: str, tx: Any
) -> Optional[list[Transfer]]:
    """Attribute one transaction's lamport movement to a counterparty.

    Returns ``[]`` when the transaction genuinely did not move this address's
    balance, and ``None`` when it could not be read or attributed — the caller
    must not confuse the two.

    A transaction can move value between many accounts at once. Taking the
    largest account moving the other way would invent transfers: in one where
    A pays B a little while D pays C a lot, it names C as A's recipient and the
    trace then walks C's history as if it were A's money. So the counterparty
    has to be the account whose movement *matches* this one, within the fee
    that separates what a sender loses from what a recipient gains. No match
    means the transaction is a swap or a batch this code cannot decompose, and
    the honest answer is that it does not know.
    """
    if not isinstance(tx, dict):
        return None
    occurred_at = _history_time(tx.get("blockTime"))
    meta, transaction = tx.get("meta"), tx.get("transaction")
    if occurred_at is None or not isinstance(meta, dict) or not isinstance(transaction, dict):
        return None
    if not _solana_error_is_valid(meta):
        return None
    if meta["err"] is not None:
        return []  # A failed transaction can pay fees, but has no settled transfer.
    message = transaction.get("message")
    if not isinstance(message, dict):
        return None
    raw_keys = message.get("accountKeys")
    pre, post = meta.get("preBalances"), meta.get("postBalances")
    if not all(isinstance(values, list) for values in (raw_keys, pre, post)):
        return None
    keys = [k.get("pubkey") for k in raw_keys if isinstance(k, dict)]
    if (
        not keys or not (len(raw_keys) == len(keys) == len(pre) == len(post))
        or any(not isinstance(key, str) or not key for key in keys)
        or len(set(keys)) != len(keys)
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in pre + post)
    ):
        return None
    scale = Decimal(10) ** chain.decimals
    deltas = {key: (Decimal(b) - Decimal(a)) / scale for key, a, b in zip(keys, pre, post)}
    mine = deltas.get(address)
    if mine is None:
        return None
    if mine == 0:
        return []
    opposing = [
        (key, delta)
        for key, delta in deltas.items()
        if key != address and delta != 0 and (delta > 0) != (mine > 0)
    ]
    if not opposing:
        return None
    magnitude = abs(mine)
    counterparty, matched = min(opposing, key=lambda kd: abs(magnitude - abs(kd[1])))
    # A sender loses the amount plus fees; a recipient gains exactly it. The
    # gap between the two legs is therefore small and one-directional, and
    # anything wider is a different transaction shape wearing the same numbers.
    if abs(magnitude - abs(matched)) > max(magnitude / 100, Decimal("0.01")):
        return None
    return [
        Transfer(
            chain_key=chain.key,
            reference=signature,
            occurred_at=occurred_at,
            sender=address if mine < 0 else counterparty,
            recipient=counterparty if mine < 0 else address,
            amount=magnitude,
        )
    ]


async def _evm_transfers(
    chain: Chain,
    address: str,
    *,
    limit: int,
    since: Optional[datetime],
    until: Optional[datetime],
    client: Optional[httpx.AsyncClient],
    deadline: float | None = None,
    reads: TraceReads | None = None,
) -> Transfers:
    """Address history from an index, because EVM JSON-RPC does not have it.

    Two lists, not one. One covers transactions the address itself sent or
    received; the other covers native coin moved *by a contract*. Skipping the
    second would miss the dominant EVM drain: the victim signs a call whose own
    value is zero, and the sweep happens inside the contract. Reading only the
    first reports that drained wallet as untouched.
    """
    rows, saturated, trimmed, coverage = await _evm_history(
        chain, address, since, client, until=until, deadline=deadline, reads=reads
    )
    if saturated:
        return Transfers(items=[], saturated=saturated, coverage=coverage)

    found: list[Transfer] = []
    for row in rows:
        amount = _scale(row.get("value"), chain.decimals)
        occurred = _history_time(row.get("timeStamp"))
        recipient = str(row.get("to") or "").lower()
        sender = str(row.get("from") or "").lower()
        # A contract creation has no `to`. It is a real transaction but not a
        # transfer to anywhere a trace can follow.
        if occurred is None:
            continue
        coverage.observe(occurred, examined=True)
        if row.get("isError") == "1":
            continue
        if amount is not None and amount.is_finite() and amount == 0:
            continue
        if (
            amount is None or not amount.is_finite() or amount < 0
            or not recipient or not sender or not row.get("hash")
            or address.lower() not in (sender, recipient)
        ):
            coverage.unsupported_payloads += 1
            coverage.gap("unsupported_payload")
            continue
        if not _within(occurred.timestamp(), since, until):
            continue
        found.append(
            Transfer(
                chain_key=chain.key,
                reference=str(row.get("hash") or ""),
                occurred_at=occurred,
                sender=sender,
                recipient=recipient,
                amount=amount,
            )
        )
    found.sort(key=lambda transfer: transfer.occurred_at, reverse=True)
    kept = _closest_to_horizon(found, limit, since)
    coverage.omitted_transfers = len(found) - len(kept)
    if coverage.omitted_transfers:
        coverage.gap("transfer_limit")
    return Transfers(items=kept, trimmed=trimmed or len(kept) < len(found), coverage=coverage)


async def _evm_history(
    chain: Chain,
    address: str,
    since: Optional[datetime],
    client: Optional[httpx.AsyncClient],
    *,
    until: datetime | None = None,
    deadline: float | None = None,
    reads: TraceReads | None = None,
) -> tuple[list[dict], Optional[str], bool, TransferCoverage]:
    """Both native-history lists in Etherscan's row shape, and how they fall short.

    Etherscan is preferred when a key is set: one request reaches a thousand
    rows deep where Blockscout pages fifty at a time. Blockscout is the default
    because it is keyless — a tracer that needed a signup would be off on every
    deployment that never did one, which is what the missing-key error used to
    mean in practice.

    Returns the rows, the saturation verdict if either list has one, and
    whether paging stopped before the list did.
    """
    api_key = get_settings().etherscan_api_key
    rows: list[dict] = []
    saturated: Optional[str] = None
    streams: list[TransferCoverage] = []
    trimmed = False
    if api_key:
        for action in ("txlist", "txlistinternal"):
            stream = TransferCoverage(since, until)
            streams.append(stream)
            page_limit = reads.page_limit(action, 1) if reads else 1
            for page_number in range(1, page_limit + 1):
                # A later page can resolve a previous page-limit/window gap.
                stream.stop_reasons = [reason for reason in stream.stop_reasons if reason not in ("provider_page_limit", "window_not_reached")]
                try:
                    page = await _etherscan_page(
                        chain, address, action, api_key, client, deadline=deadline,
                        coverage=stream, reads=reads, page_number=page_number,
                    )
                except (ReadPending, RuntimeError) as exc:
                    # Invalid envelopes recur during replay; keep completed
                    # streams while leaving the failed page uncached/retryable.
                    if isinstance(exc, RuntimeError) and not (
                        reads and reads.replay and any(s.pages_read for s in streams)
                    ):
                        raise
                    stream.gap(reads.interruption if reads and reads.interruption else "provider_unavailable")
                    stream.finish(False)
                    break
                rows.extend(page)
                times = [int(t.timestamp()) for r in page if (t := _history_time(r.get("timeStamp"))) is not None]
                if len(times) == EVM_HISTORY_PAGE and _saturation(times, EVM_HISTORY_PAGE, None) == SATURATED_POOLED:
                    stream.gap("high_activity")
                    saturated = SATURATED_POOLED
                    break
                if stream.provider_exhausted or stream.since_reached:
                    break
            else:
                if page_limit >= MAX_HISTORY_PAGES:
                    stream.gap("retention_limit")
            if saturated:
                break
    elif not chain.token_index_url:
        raise ProviderNotConfiguredError(
            f"Tracing {chain.display_name} needs a transfer-history index, and this "
            "deployment has neither an ETHERSCAN_API_KEY nor a Blockscout instance "
            "for the chain."
        )
    else:
        index = chain.token_index_url.rstrip("/")
        for path in ("transactions", "internal-transactions"):
            stream = TransferCoverage(since, until)
            streams.append(stream)
            page, verdict, short = await _blockscout_history(
                chain, index, address, path, since, client, deadline=deadline, coverage=stream, reads=reads
            )
            rows.extend(page)
            saturated = saturated or verdict
            trimmed = trimmed or short
            if saturated == SATURATED_POOLED:
                break
    coverage = TransferCoverage(since, until)
    if reads:
        coverage.fetched_at = datetime.fromisoformat(reads.history["fetched_at"])
    for stream in streams:
        coverage.pages_read += stream.pages_read
        coverage.rows_read += stream.rows_read
        coverage.missing_timestamps += stream.missing_timestamps
        for reason in stream.stop_reasons:
            coverage.gap(reason)
        for moment in (stream.observed_oldest, stream.observed_newest):
            if moment is not None:
                coverage.observe(moment)
    coverage.provider_exhausted = (
        False if any(s.provider_exhausted is False for s in streams)
        else True if len(streams) == 2 and all(s.provider_exhausted is True for s in streams)
        else None
    )
    coverage.since_reached = (
        len(streams) == 2 and all(s.since_reached is True for s in streams) if since is not None else None
    )
    coverage.until_reached = (
        len(streams) == 2 and all(s.until_reached is True for s in streams) if until is not None else None
    )
    return rows, saturated, trimmed, coverage


async def _blockscout_history(
    chain: Chain,
    index: str,
    address: str,
    path: str,
    since: Optional[datetime],
    client: Optional[httpx.AsyncClient],
    *,
    deadline: float | None = None,
    coverage: TransferCoverage | None = None,
    reads: TraceReads | None = None,
) -> tuple[list[dict], Optional[str], bool]:
    """One Blockscout list, paged, in Etherscan's row shape.

    The instance arrives already resolved, because the caller is the one that
    knows whether the chain has one — `Chain.token_index_url` is optional and
    the chains without it never reach here.

    Speaking Etherscan's shape rather than its own keeps one decoder for both
    sources; a second would be a second place for "which field held the amount"
    to be answered differently.

    The rate test runs per page and stops the paging the moment it fires. That
    is not an optimisation: Blockscout returns an exchange hot wallet's first
    page in a second and then times out paging deeper into it, so judging only
    after the paging fails on exactly the addresses the test exists to catch.
    For the same reason a later page that will not load ends this list rather
    than failing the read — what did load is real, and returning it as a short
    list is what stops "nothing moved" being concluded from it.
    """
    url = f"{index}/api/v2/addresses/{address}/{path}"
    rows: list[dict] = []
    params: Optional[dict] = None
    verdict: Optional[str] = None
    coverage = coverage if coverage is not None else TransferCoverage(since)
    previous_time: int | None = None
    page_limit = reads.page_limit(path, BLOCKSCOUT_HISTORY_MAX_PAGES) if reads else BLOCKSCOUT_HISTORY_MAX_PAGES
    for page_number in range(page_limit):
        try:
            async def read_page():
                return await _get_json(
                    url, "Blockscout", client, params=params, endpoint=index, deadline=deadline
                )
            payload = await reads.read(
                f"{path}:{json.dumps(params, sort_keys=True)}", read_page,
                lambda value: isinstance(value, dict) and isinstance(value.get("items"), list)
                and "next_page_params" in value and (value["next_page_params"] is None or isinstance(value["next_page_params"], dict)),
            ) if reads else await read_page()
        except ReadPending:
            coverage.gap(reads.interruption if reads and reads.interruption else "provider_unavailable")
            coverage.finish(False)
            return rows, verdict, True
        except (ProviderRateLimited, OnchainDeadlineExceeded):
            raise
        except Exception:
            if page_number == 0:
                raise
            logger.warning("Blockscout stopped paging %s on %s", path, chain.key, exc_info=True)
            coverage.gap("provider_unavailable")
            coverage.finish(False)
            return rows, verdict, True
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            coverage.gap("invalid_page")
            coverage.finish(None)
            return rows, verdict, True
        coverage.pages_read += 1
        coverage.rows_read += len(items)
        page: list[dict] = []
        for item in items:
            row = _blockscout_row(item)
            if row is None:
                if isinstance(item, dict):
                    coverage.missing_timestamps += 1
                    coverage.gap("missing_timestamp")
                else:
                    coverage.gap("invalid_row")
                continue
            page.append(row)
            moment = _history_time(row["timeStamp"])
            if moment is not None:
                if previous_time is not None and row["timeStamp"] > previous_time:
                    coverage.gap("invalid_row")
                previous_time = row["timeStamp"]
                coverage.observe(moment)
        rows.extend(page)
        # Only pooled ends the paging. Unpageable says the window is further
        # back than this page reached, and paging is the remedy for that.
        verdict = (
            _saturation([row["timeStamp"] for row in page], BLOCKSCOUT_PAGE, None)
            if not coverage.stop_reasons else None
        )
        if verdict == SATURATED_POOLED:
            coverage.gap("high_activity")
            coverage.finish(False)
            return rows, verdict, False
        if "next_page_params" not in payload:
            coverage.gap("invalid_page")
            coverage.finish(None)
            return rows, verdict, True
        following = payload["next_page_params"]
        if following is None:
            coverage.finish(True)
            return rows, verdict, False
        if not isinstance(following, dict):
            coverage.gap("invalid_page")
            coverage.finish(None)
            return rows, verdict, True
        if (
            since is not None and page and not coverage.stop_reasons
            and min(r["timeStamp"] for r in page) < since.timestamp()
        ):
            coverage.finish(False)
            return rows, verdict, False
        # A null in the cursor is Blockscout saying there is no value for that
        # key, not a value to send back — httpx would serialise it as "None".
        next_params = {key: value for key, value in following.items() if value is not None}
        if not next_params or next_params == params:
            coverage.gap("nonadvancing_cursor")
            coverage.finish(False)
            return rows, verdict, True
        params = next_params
        coverage.next_cursor = json.dumps(params, sort_keys=True)
    coverage.gap("provider_page_limit")
    if page_limit >= MAX_HISTORY_PAGES:
        coverage.gap("retention_limit")
    coverage.finish(False)
    return rows, verdict, True


def _blockscout_row(item: Any) -> Optional[dict]:
    """One Blockscout entry as an Etherscan row, or None when it cannot be one.

    A transaction still in the mempool has no timestamp, and a trace can say
    nothing about money that has not moved yet.
    """
    if not isinstance(item, dict):
        return None
    stamp = item.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        occurred = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    occurred = occurred.replace(tzinfo=timezone.utc) if occurred.tzinfo is None else occurred
    parties: dict[str, Any] = {}
    for side in ("from", "to"):
        raw = item.get(side)
        parties[side] = raw.get("hash") if isinstance(raw, dict) else raw
    failed = (
        item.get("status") == "error" or item.get("success") is False or bool(item.get("error"))
    )
    return {
        "value": item.get("value"),
        "timeStamp": int(occurred.timestamp()),
        "from": parties["from"],
        "to": parties["to"],
        "hash": item.get("hash") or item.get("transaction_hash"),
        "isError": "1" if failed else "0",
    }


def _etherscan_empty(payload: Any) -> bool:
    """Recognize an explicit empty-history response for decoding and reuse."""
    result = payload.get("result") if isinstance(payload, dict) else None
    return (
        isinstance(payload, dict) and str(payload.get("status")) == "0"
        and any(str(payload.get(key, "")).strip().lower() == "no transactions found" for key in ("message", "result"))
        and (result is None or result == [] or result == "No transactions found")
    )


async def _etherscan_page(
    chain: Chain,
    address: str,
    action: str,
    api_key: str,
    client: Optional[httpx.AsyncClient],
    *,
    deadline: float | None = None,
    coverage: TransferCoverage | None = None,
    reads: TraceReads | None = None,
    page_number: int = 1,
) -> list[dict]:
    """One Etherscan list, with "no transactions found" read as an empty history.

    The API key shares a cooldown across both actions and all EVM chains.
    """
    params = {
        "chainid": chain.explorer_chain_id,
        "module": "account",
        "action": action,
        "address": address,
        "startblock": 0,
        "endblock": 99999999,
        "page": page_number,
        "offset": EVM_HISTORY_PAGE,
        "sort": "desc",
        "apikey": api_key,
    }
    async def read_page():
        return await _get_json(
            ETHERSCAN_V2_URL, "Etherscan", client, params=params,
            endpoint=f"{ETHERSCAN_V2_URL}\0{api_key}", deadline=deadline,
        )
    payload = await reads.read(
        f"{action}:{page_number}", read_page,
        lambda value: isinstance(value, dict) and (
            str(value.get("status")) == "1" and isinstance(value.get("result"), list)
            or _etherscan_empty(value)
        ),
    ) if reads else await read_page()
    result = payload.get("result") if isinstance(payload, dict) else None
    coverage = coverage if coverage is not None else TransferCoverage()
    if isinstance(payload, dict) and str(payload.get("status")) == "1" and isinstance(result, list):
        coverage.pages_read += 1
        coverage.rows_read += len(result)
        rows = []
        for row in result:
            if not isinstance(row, dict):
                coverage.gap("invalid_row")
                continue
            rows.append(row)
            moment = _history_time(row.get("timeStamp"))
            if moment is None:
                coverage.missing_timestamps += 1
                coverage.gap("missing_timestamp")
            else:
                coverage.observe(moment)
        coverage.finish(len(result) < EVM_HISTORY_PAGE)
        if not coverage.provider_exhausted and not coverage.since_reached:
            coverage.gap("provider_page_limit")
        return rows
    if _etherscan_empty(payload):
        coverage.pages_read += 1
        coverage.finish(True)
        return []
    raise RuntimeError(f"Etherscan returned an unexpected payload for {chain.key}")


async def _get_json(
    url: str,
    label: str,
    client: Optional[httpx.AsyncClient],
    *,
    params: Optional[dict] = None,
    endpoint: str | None = None,
    deadline: float | None = None,
) -> Any:
    """One GET against a public index, retried through a momentary throttle.

    The source base, not its per-address path/query, defines the shared limit.
    """
    return await _request_json(
        "GET", url, label, client, endpoint=endpoint or url, deadline=deadline, params=params
    )


async def _esplora(
    chain: Chain, path: str, client: Optional[httpx.AsyncClient], *, deadline: float | None = None
) -> Any:
    endpoint = rpc_url(chain).rstrip("/")
    return await _get_json(
        f"{endpoint}{path}", "Bitcoin indexer", client, endpoint=endpoint, deadline=deadline
    )


def _sats(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


async def _bitcoin_balance(
    chain: Chain, address: str, *, client: Optional[httpx.AsyncClient],
    deadline: float | None = None,
) -> Optional[Decimal]:
    """Sum the address's unspent outputs, confirmed and pending alike.

    Bitcoin has no balance field to read — an address is worth what its
    unspent outputs are worth — so this is funded minus spent. The mempool
    numbers are added because a wallet shows them too: a spend that has left
    but not confirmed is money already gone, and reporting it as still held
    would overstate net worth for as long as the block takes.
    """
    payload = await _esplora(chain, f"/address/{address}", client, deadline=deadline)
    if not isinstance(payload, dict):
        return None
    total = 0
    for key in ("chain_stats", "mempool_stats"):
        stats = payload.get(key)
        if isinstance(stats, dict):
            total += _sats(stats.get("funded_txo_sum")) - _sats(stats.get("spent_txo_sum"))
    return _scale(total, chain.decimals)


async def _bitcoin_history(
    chain: Chain, address: str, since: Optional[datetime], client: Optional[httpx.AsyncClient],
    *, deadline: float | None = None, coverage: TransferCoverage | None = None,
    reads: TraceReads | None = None,
) -> tuple[list[dict], bool]:
    """Recent transactions touching the address, and whether that was all of them.

    Esplora hands out 25 confirmed transactions at a time, keyed on the last
    txid seen, so reaching further back is a request per page. Paging stops as
    soon as a page reaches past the window asked about — everything older
    cannot be in the answer — and otherwise at a fixed cap, which is the
    second half of the return value: False means the history was cut off, and
    a cut-off history may not say "nothing moved".
    """
    rows: list[dict] = []
    cursor: Optional[str] = None
    coverage = coverage if coverage is not None else TransferCoverage(since)
    seen_cursors: set[str] = set()
    seen_transactions: set[str] = set()
    page_limit = reads.page_limit("bitcoin", BITCOIN_HISTORY_MAX_PAGES) if reads else BITCOIN_HISTORY_MAX_PAGES
    for _ in range(page_limit):
        path = f"/address/{address}/txs"
        if cursor:
            path = f"{path}/chain/{cursor}"
        async def read_page():
            return await _esplora(chain, path, client, deadline=deadline)
        try:
            page = await reads.read(
                f"bitcoin:{cursor or ''}", read_page,
                lambda value: isinstance(value, list) and all(isinstance(row, dict) for row in value),
            ) if reads else await read_page()
        except ReadPending:
            coverage.gap(reads.interruption if reads and reads.interruption else "provider_unavailable")
            coverage.finish(False)
            return rows, False
        if not isinstance(page, list):
            coverage.gap("invalid_page")
            coverage.finish(None)
            return rows, False
        coverage.pages_read += 1
        coverage.rows_read += len(page)
        valid = []
        for tx in page:
            if not isinstance(tx, dict):
                coverage.gap("invalid_row")
                continue
            moment = _bitcoin_time(tx)
            if moment is None:
                coverage.missing_timestamps += 1
                coverage.gap("missing_timestamp")
            else:
                coverage.observe(moment)
            if not isinstance(tx.get("txid"), str) or not tx["txid"]:
                coverage.gap("invalid_row")
            elif tx["txid"] in seen_transactions:
                coverage.gap("nonadvancing_cursor")
                continue
            else:
                seen_transactions.add(tx["txid"])
            valid.append(tx)
        if not page:
            coverage.finish(True)
            return rows, True
        rows.extend(valid)
        # The first page also carries unconfirmed transactions, so it can be
        # longer than a page; only the confirmed tail can be paged past.
        confirmed = [tx for tx in page if isinstance(tx, dict) and isinstance(tx.get("status"), dict) and tx["status"].get("confirmed") is True]
        if len(confirmed) < BITCOIN_HISTORY_PAGE:
            coverage.finish(True)
            return rows, True
        cursor = str(confirmed[-1].get("txid") or "")
        coverage.next_cursor = cursor
        if not cursor or cursor in seen_cursors:
            coverage.gap("nonadvancing_cursor")
            coverage.finish(False)
            return rows, False
        seen_cursors.add(cursor)
        oldest = _bitcoin_time(confirmed[-1])
        if since is not None and oldest is not None and oldest.timestamp() < since.timestamp():
            coverage.finish(False)
            return rows, True
    coverage.gap("provider_page_limit")
    if page_limit >= MAX_HISTORY_PAGES:
        coverage.gap("retention_limit")
    coverage.finish(False)
    return rows, False


def _bitcoin_time(tx: dict) -> Optional[datetime]:
    """The observed block time; an unconfirmed/unknown time stays unknown."""
    status = tx.get("status")
    if not isinstance(status, dict) or status.get("confirmed") is not True:
        return None
    block_time = status.get("block_time") if isinstance(status, dict) else None
    return _history_time(block_time)


def _bitcoin_deltas(chain: Chain, tx: dict, address: str) -> list[Transfer]:
    """Read one transaction as movement in or out of ``address``.

    A Bitcoin transaction has no sender and no recipient — it spends a set of
    outputs and creates another set — so both have to be inferred, and the two
    directions are not inferred the same way.

    Money arriving is unambiguous: the address gained ``received - spent``, and
    the largest input that is not its own names who sent it.

    Money leaving is not. Every output the transaction did not pay back to one
    of its own inputs is treated as a recipient, because that is the only
    signal available: change normally returns to an input address, and where a
    wallet sends change to a fresh address instead, nothing in the transaction
    distinguishes that from a payment. The trace therefore may follow a
    victim's — or a thief's — own change as if it were a transfer. That errs
    toward showing a hop that is real movement of the same coins, rather than
    toward dropping the hop that actually carried them.
    """
    occurred = _bitcoin_time(tx)
    reference = str(tx.get("txid") or "")
    if occurred is None or not reference:
        return []
    inputs: list[tuple[str, int]] = []
    for entry in tx.get("vin") or []:
        prevout = entry.get("prevout") if isinstance(entry, dict) else None
        if isinstance(prevout, dict):
            inputs.append((str(prevout.get("scriptpubkey_address") or ""), _sats(prevout.get("value"))))
    outputs: list[tuple[str, int]] = [
        (str(entry.get("scriptpubkey_address") or ""), _sats(entry.get("value")))
        for entry in tx.get("vout") or []
        if isinstance(entry, dict)
    ]
    spent = sum(value for owner, value in inputs if owner == address)
    received = sum(value for owner, value in outputs if owner == address)

    def _transfer(sender: str, recipient: str, sats: int) -> Optional[Transfer]:
        amount = _scale(sats, chain.decimals)
        if amount is None or amount <= 0:
            return None
        return Transfer(
            chain_key=chain.key,
            reference=reference,
            occurred_at=occurred,
            sender=sender,
            recipient=recipient,
            amount=amount,
        )

    if spent > received:
        senders = {owner for owner, _ in inputs if owner}
        moved = (_transfer(address, owner, value) for owner, value in outputs if owner and owner not in senders)
        return [transfer for transfer in moved if transfer is not None]
    if received > spent:
        external = [(owner, value) for owner, value in inputs if owner and owner != address]
        if not external:
            return []  # a coinbase reward or a self-consolidation: nobody paid it
        sender = max(external, key=lambda pair: pair[1])[0]
        transfer = _transfer(sender, address, received - spent)
        return [transfer] if transfer is not None else []
    return []


async def _bitcoin_transfers(
    chain: Chain,
    address: str,
    *,
    limit: int,
    since: Optional[datetime],
    until: Optional[datetime],
    client: Optional[httpx.AsyncClient],
    deadline: float | None = None,
    reads: TraceReads | None = None,
) -> Transfers:
    """Address history from Esplora, read as transfers rather than as UTXOs.

    Unlike Solana, the amounts are already in the list — a transaction carries
    its own inputs and outputs — so this costs a request per *page*, not per
    transaction.
    """
    coverage = TransferCoverage(since, until)
    if reads:
        coverage.fetched_at = datetime.fromisoformat(reads.history["fetched_at"])
    rows, exhausted = await _bitcoin_history(
        chain, address, since, client, deadline=deadline, coverage=coverage, reads=reads
    )
    timestamps = [int(moment.timestamp()) for moment in map(_bitcoin_time, rows) if moment]
    # Only a history that was cut short can be saturated: one that ran out is
    # the whole story, however fast it was written.
    saturated = (
        _saturation(timestamps, len(timestamps), None)
        if not exhausted and timestamps and len(timestamps) == len(rows)
        and "provider_page_limit" in coverage.stop_reasons else None
    )
    if saturated:
        coverage.gap("high_activity")
        return Transfers(items=[], saturated=saturated, coverage=coverage)

    found: list[Transfer] = []
    for row in rows:
        moment = _bitcoin_time(row)
        if moment is None:
            continue
        coverage.observe(moment, examined=True)
        if not row.get("txid") or not all(isinstance(row.get(key), list) for key in ("vin", "vout")):
            coverage.unsupported_payloads += 1
            coverage.gap("unsupported_payload")
            continue
        inputs, outputs = row["vin"], row["vout"]
        # A zero-valued OP_RETURN carries data, not a payment to an unknown
        # party. Keep the ordinary payments alongside this known output shape.
        values = [entry.get("prevout") for entry in inputs if isinstance(entry, dict) and not entry.get("is_coinbase")] + [
            entry for entry in outputs if not (
                isinstance(entry, dict) and type(entry.get("value")) is int and entry["value"] == 0
                and entry.get("scriptpubkey_type") == "op_return"
                and isinstance(entry.get("scriptpubkey"), str) and entry["scriptpubkey"].startswith("6a")
            )
        ]
        if (
            not inputs or not outputs or any(not isinstance(entry, dict) for entry in inputs)
            or any(
                not isinstance(entry, dict) or isinstance(entry.get("value"), bool)
                or not isinstance(entry.get("value"), int) or entry["value"] < 0
                or not isinstance(entry.get("scriptpubkey_address"), str) or not entry["scriptpubkey_address"]
                for entry in values
            )
        ):
            coverage.unsupported_payloads += 1
            coverage.gap("unsupported_payload")
            continue
        for transfer in _bitcoin_deltas(chain, row, address):
            if _within(transfer.occurred_at.timestamp(), since, until):
                found.append(transfer)
    found.sort(key=lambda transfer: transfer.occurred_at, reverse=True)
    trimmed = _closest_to_horizon(found, limit, since)
    coverage.omitted_transfers = len(found) - len(trimmed)
    if coverage.omitted_transfers:
        coverage.gap("transfer_limit")
    return Transfers(items=trimmed, trimmed=len(trimmed) < len(found), coverage=coverage)


async def token_holdings(
    chain: Chain, address: str, *, client: Optional[httpx.AsyncClient] = None
) -> list[TokenHolding]:
    """Non-native tokens the address holds, richest first.

    Only tokens the index can price are returned. That is the spam filter: an
    address that has been airdropped for years holds thousands of tokens with
    no market, and listing them would bury the handful that are positions. It
    is a filter on *having a price*, not on the price being large, so a
    memecoin worth cents survives it and a worthless airdrop does not.
    """
    if chain.kind == "solana":
        found = await _solana_token_holdings(chain, address, client)
    elif chain.kind == "evm":
        found = await _evm_token_holdings(chain, address, client)
    else:
        return []
    found.sort(key=lambda token: token.rank, reverse=True)
    return found[:MAX_TOKENS_PER_ADDRESS]


def _parsed_token_account(entry: Any) -> Optional[tuple[str, Decimal]]:
    """A token account's mint and balance, or None when the node sent a shape we don't know."""
    if not isinstance(entry, dict):
        return None
    account = entry.get("account")
    data = account.get("data") if isinstance(account, dict) else None
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    amount = info.get("tokenAmount") if isinstance(info, dict) else None
    if not isinstance(amount, dict) or not isinstance(info, dict):
        return None
    mint = info.get("mint")
    try:
        quantity = _scale(amount.get("amount"), int(amount.get("decimals")))
    except (TypeError, ValueError):
        return None
    if not isinstance(mint, str) or quantity is None:
        return None
    return mint, quantity


async def _solana_token_holdings(
    chain: Chain, address: str, client: Optional[httpx.AsyncClient]
) -> list[TokenHolding]:
    """SPL balances from the chain, then their identity from Jupiter.

    Balances are authoritative here — they come from the ledger — and only the
    naming and pricing depend on an index. A mint Jupiter does not know is
    dropped rather than shown as an unnamed number, which is what the wallet
    UIs do and is the only readable answer for a mint that has no market.
    """
    quantities: dict[str, Decimal] = {}
    for program in SOLANA_TOKEN_PROGRAMS:
        result = await _json_rpc(
            chain,
            "getTokenAccountsByOwner",
            [address, {"programId": program}, {"encoding": "jsonParsed"}],
            client=client,
        )
        entries = result.get("value") if isinstance(result, dict) else None
        for entry in entries or []:
            parsed = _parsed_token_account(entry)
            if parsed is None:
                continue
            mint, quantity = parsed
            if quantity > 0:
                # One wallet can hold several accounts for the same mint, and
                # the position is their sum, not whichever came back last.
                quantities[mint] = quantities.get(mint, Decimal("0")) + quantity
    if not quantities:
        return []
    known = await _jupiter_tokens(list(quantities), client)
    return [
        TokenHolding(
            chain_key=chain.key,
            contract=mint,
            symbol=meta["symbol"],
            quantity=quantity,
            usd_price=meta["price"],
            trusted=meta["trusted"],
        )
        for mint, quantity in quantities.items()
        if (meta := known.get(mint)) is not None and meta["price"] is not None
    ]


async def _jupiter_tokens(
    mints: list[str], client: Optional[httpx.AsyncClient]
) -> dict[str, dict]:
    """Symbol, USD price and a verification verdict per mint.

    Jupiter's `verified` tag is a curation decision, and it is what gates
    whether a price counts. An unverified token still gets listed with its
    quantity — the user asked to see their memecoins — but contributes nothing
    to a total, because a made-up price on a made-up token is the cheapest way
    to make this app report a number that is not true.
    """
    found: dict[str, dict] = {}
    for start in range(0, len(mints), JUPITER_QUERY_BATCH):
        batch = mints[start : start + JUPITER_QUERY_BATCH]
        payload = await _get_json(
            JUPITER_TOKEN_URL, "Jupiter", client, params={"query": ",".join(batch)},
            endpoint=JUPITER_TOKEN_URL,
        )
        for row in payload if isinstance(payload, list) else []:
            if not isinstance(row, dict):
                continue
            mint, symbol = row.get("id"), row.get("symbol")
            if not isinstance(mint, str) or not isinstance(symbol, str):
                continue
            tags = row.get("tags")
            found[mint] = {
                "symbol": symbol,
                "price": _decimal_or_none(row.get("usdPrice")),
                "trusted": bool(row.get("isVerified"))
                or (isinstance(tags, list) and "verified" in tags),
            }
    return found


async def _evm_token_holdings(
    chain: Chain, address: str, client: Optional[httpx.AsyncClient]
) -> list[TokenHolding]:
    """ERC-20 balances from Blockscout, which is keyless on every chain here.

    Deliberately not Etherscan: its token-balance endpoint is a paid tier,
    where Blockscout answers balance, symbol, decimals, price and a reputation
    verdict in one unauthenticated call. That also means token balances work on
    a deployment with no ETHERSCAN_API_KEY, unlike EVM *tracing*.

    The route is unpaginated and takes no type filter, so an address that has
    been airdropped into the thousands can time the request out. The caller
    treats that as "tokens unknown for this address" and keeps the native
    holding, which is the honest reading — it is a failure to look, not a
    finding that there is nothing there.
    """
    if not chain.token_index_url:
        return []
    payload = await _get_json(
        f"{chain.token_index_url.rstrip('/')}/api/v2/addresses/{address}/token-balances",
        "Blockscout",
        client,
        endpoint=chain.token_index_url.rstrip("/"),
    )
    found: list[TokenHolding] = []
    for row in payload if isinstance(payload, list) else []:
        token = row.get("token") if isinstance(row, dict) else None
        if not isinstance(token, dict) or token.get("type") != "ERC-20":
            continue
        contract, symbol = token.get("address_hash") or token.get("address"), token.get("symbol")
        if not isinstance(contract, str) or not isinstance(symbol, str):
            continue
        try:
            quantity = _scale(row.get("value"), int(token.get("decimals")))
        except (TypeError, ValueError):
            continue
        price = _decimal_or_none(token.get("exchange_rate"))
        if quantity is None or quantity <= 0 or price is None:
            continue
        found.append(
            TokenHolding(
                chain_key=chain.key,
                contract=contract.lower(),
                symbol=symbol,
                quantity=quantity,
                usd_price=price,
                trusted=token.get("reputation") == "ok",
            )
        )
    return found


def _decimal_or_none(raw: Any) -> Optional[Decimal]:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return value if value.is_finite() and value > 0 else None


class OnChainProvider(BankProvider):
    """Watch-only wallets addressed by their public keys.

    Reuses the paste-a-credential flow: the blob is a list of addresses rather
    than a token. Every watched address becomes one holding valued at the
    native coin's USD spot, and every connection reports a single investment
    account so the holdings land in one Wallet — the same shape Coinbase uses,
    for the same reason.
    """

    @property
    def name(self) -> str:
        return "onchain"

    @property
    def flow_type(self) -> str:
        return "token"

    async def get_oauth_url(self, *args, **kwargs) -> str:  # type: ignore[override]
        raise NotImplementedError("On-chain wallets are connected by pasting addresses")

    async def handle_oauth_callback(self, code: str) -> ConnectionData:
        watched = parse_addresses(code or "")
        credentials = {"addresses": [w.external_id for w in watched]}
        return ConnectionData(
            external_id=self._connection_external_id(watched),
            institution_name="On-chain wallets",
            credentials=credentials,
            accounts=await self.get_accounts(credentials),
        )

    async def refresh_credentials(self, credentials: dict) -> dict:
        self._watched(credentials)
        return credentials

    async def get_transactions(
        self,
        credentials: dict,
        account_external_id: str,
        since=None,
        payee_source: str = "auto",
    ) -> list[TransactionData]:
        # On-chain movement is a holding's ledger, not a cash-account
        # statement. It reaches the app through get_holdings, the same way
        # Coinbase's does.
        return []

    async def get_accounts(self, credentials: dict) -> list[AccountData]:
        # An address that cannot be read leaves this total short instead of
        # failing: the balance is derived and rewritten every sync, so it
        # self-corrects, while `get_holdings` still keeps the sweep off the
        # positions behind that address.
        holdings, _ = await self._read(credentials)
        total = sum((h.current_value for h in holdings), Decimal("0"))
        return [
            AccountData(
                external_id=ACCOUNT_EXTERNAL_ID,
                name="On-chain wallets",
                type="investment",
                balance=total,
                currency="USD",
                has_holdings=True,
            )
        ]

    async def get_holdings(self, credentials: dict) -> list[HoldingData]:
        """The native coin and every priced token, per watched address.

        The two are read independently — a wallet holding no SOL but 500 USDC is
        a real wallet, and neither read's result is inferred from the other's.

        Both raise rather than return short. Every holding this omits, the sync
        layer archives, so a partial answer here is indistinguishable from a
        liquidated wallet — and unlike a stale value, an archived asset does not
        come back on its own. An address that fails is named in
        ``PartialHoldings`` so the rest of the connection can still sync while
        that one's positions are left alone.
        """
        holdings, unreadable = await self._read(credentials)
        if unreadable:
            raise PartialHoldings(holdings, unreadable)
        return holdings

    async def _read(self, credentials: dict) -> tuple[list[HoldingData], list[str]]:
        """Every readable address's positions, plus the ids of those that failed.

        Failure is per address, not per read: a wallet whose native balance
        answers but whose token index does not is unread as a whole, because
        keeping the half that answered would archive the other half.
        """
        watched = self._watched(credentials)
        prices = await usd_spot_prices()
        holdings: list[HoldingData] = []
        unreadable: list[str] = []
        async with session() as client:
            for entry in watched:
                try:
                    native = await self._native_holding(entry, prices, client)
                    tokens = await self._token_holdings(entry, client)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Could not read %s; leaving its holdings untouched",
                        entry.external_id,
                        exc_info=True,
                    )
                    unreadable.append(entry.external_id)
                    continue
                if native is not None:
                    holdings.append(native)
                holdings.extend(tokens)
        return holdings, unreadable

    @staticmethod
    async def _native_holding(
        entry: WatchedAddress, prices: dict[str, Decimal], client: httpx.AsyncClient
    ) -> Optional[HoldingData]:
        """The address's native-coin position, valued at Coinbase's spot.

        The price comes from Coinbase's public rate table, which needs no key
        and works with Coinbase switched off — but does mean a Coinbase outage
        stops this sync. That is the intended failure: it raises, the sync
        layer logs and keeps the previous values, and the holdings go stale
        rather than being rewritten at a price nobody knows.

An address the node cannot answer for fails the whole sync rather
        than dropping out of the payload. The sync layer archives any holding
        it stops seeing, so a dropped holding is not a gap — it is a claim the
        position is gone. A raise is caught upstream and leaves every value
        stale, which is the recoverable answer; archiving is silent and sticks.
        """
        quantity = await native_balance(entry.chain, entry.address, client=client)
        if quantity is None:
            raise RuntimeError(f"No balance returned for {entry.external_id}")
        price = prices.get(entry.chain.symbol)
        if price is None and quantity != 0:
            raise RuntimeError(f"No USD price for {entry.chain.symbol}")
        return HoldingData(
            external_id=entry.external_id,
            name=f"{entry.chain.display_name} {entry.short}",
            currency="USD",
            ticker=entry.chain.symbol,
            quantity=quantity,
            unit_price=price,
            current_value=quantity * price if price is not None else Decimal("0"),
            account_external_id=ACCOUNT_EXTERNAL_ID,
            account_name="On-chain wallets",
            metadata={
                "chain": entry.chain.key,
                "address": entry.address,
                "watch_only": True,
            },
        )

    @staticmethod
    async def _token_holdings(
        entry: WatchedAddress, client: httpx.AsyncClient
    ) -> list[HoldingData]:
        """The address's token positions, valued only where the index vouches.

        An untrusted token is still reported, with its quantity and the quote
        that was refused, and carries no value. Showing it at the index's price
        would let anyone who can mint a token and a pool write a number into
        this user's net worth; hiding it would answer a question the user asked
        with silence. Naming it and valuing it at nothing does neither.

        An index that cannot be reached raises, for the reason
        `_native_holding` gives: an empty list here is read downstream as every
        token in this wallet having been disposed of.
        """
        tokens = await token_holdings(entry.chain, entry.address, client=client)
        holdings: list[HoldingData] = []
        for token in tokens:
            price = token.usd_price if token.trusted else None
            holdings.append(
                HoldingData(
                    external_id=f"{entry.external_id}:{token.contract}",
                    name=f"{token.symbol} · {entry.chain.display_name} {entry.short}",
                    currency="USD",
                    # Only a vouched symbol becomes a ticker. `ticker` is the
                    # key positions consolidate on and imports match against,
                    # so an attacker-chosen one joins a real position and takes
                    # its cost basis down with it — a token calling itself AAPL
                    # would null the gain on 500 shares of the real thing. It
                    # is also what `is_cash_equivalent_ticker` reads, so a junk
                    # "USDC" would be filed as cash. The name still shows the
                    # symbol; nothing keys on the name.
                    ticker=token.symbol if token.trusted else None,
                    quantity=token.quantity,
                    unit_price=price,
                    current_value=token.quoted_value if token.trusted else Decimal("0"),
                    account_external_id=ACCOUNT_EXTERNAL_ID,
                    account_name="On-chain wallets",
                    metadata={
                        "chain": entry.chain.key,
                        "address": entry.address,
                        "watch_only": True,
                        "token_contract": token.contract,
                        "token_symbol": token.symbol,
                        "token_trusted": token.trusted,
                        # Kept even when refused, so the reason a position shows
                        # no value is inspectable rather than mysterious.
                        "token_quoted_usd": str(token.usd_price) if token.usd_price else None,
                    },
                )
            )
        return holdings

    @staticmethod
    def _watched(credentials: dict) -> list[WatchedAddress]:
        entries: Iterable[Any] = (credentials or {}).get("addresses") or []
        return parse_addresses("\n".join(str(e) for e in entries))

    @staticmethod
    def _connection_external_id(watched: list[WatchedAddress]) -> str:
        """A stable id for this set of addresses, short enough for the column.

        ``bank_connections.external_id`` is VARCHAR(255) and the addresses
        themselves are ~50 characters each, so listing them inline overflows
        well before the 25 this provider accepts. A digest of the sorted set
        keeps the property that matters — reconnecting the same addresses
        identifies the same connection — without depending on how many there
        are.
        """
        joined = ",".join(sorted(entry.external_id for entry in watched))
        return f"onchain:{hashlib.sha256(joined.encode()).hexdigest()[:32]}"
