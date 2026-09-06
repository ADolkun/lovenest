"""Watch-only on-chain wallets: native balances and transfer history.

Nothing here ever holds a private key. A connection's credential is a list of
public addresses, so the connection is watch-only by construction rather than
by permission — there is no key to scope down and nothing a leak of the
credential blob would let an attacker move.

Two chain families, and they are not symmetric. Solana's JSON-RPC answers
"what did this address do" directly (``getSignaturesForAddress``), so both
balances and history come from a public node with no key. EVM JSON-RPC has no
such call — an address's history only exists in an indexer — so EVM balances
come from a public node and EVM history needs an Etherscan key. That asymmetry
is the reason ``transfers`` can raise for one chain and not another.

Quantities come from the chain; the *value* of one does not. USD pricing is
Coinbase's public rate table (see ``get_holdings``), which is unauthenticated
but is still a second upstream this module depends on.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Iterable, Optional

import httpx

from app.core.config import get_settings
from app.providers.coinbase import usd_spot_prices
from app.providers.base import (
    AccountData,
    BankProvider,
    ConnectionData,
    HoldingData,
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
# How many transaction reads may be in flight at once. Solana gives a
# transfer's amount only inside the transaction, so a page of history is a
# request per row; serialising them makes a walk take minutes, and firing all
# of them at once gets a shared node to 429 the whole deployment.
TX_FETCH_CONCURRENCY = 5
MAX_WATCHED_ADDRESSES = 25
# Both history sources page newest-first. Asking for a big page is how an
# address's *rate* becomes visible, which is what tells a pooled address apart
# from a busy wallet: see `_saturation`.
SOLANA_SIGNATURE_PAGE = 1000
EVM_HISTORY_PAGE = 1000
# A full page spanning less than this is a pooled address, not a person. An
# exchange hot wallet fills a thousand transactions in seconds; the busiest
# personal wallet or scam aggregator takes weeks.
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

_SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class Chain:
    """One supported network.

    ``symbol`` is the native coin's ticker, which is what a holding is
    denominated in. ``explorer_chain_id`` is Etherscan's V2 chain id, and every
    EVM chain here has one — Solana never reaches the explorer path.
    """

    key: str
    kind: str  # "solana" | "evm"
    display_name: str
    symbol: str
    decimals: int
    default_rpc_url: str
    explorer_chain_id: Optional[int] = None


CHAINS: dict[str, Chain] = {
    "solana": Chain(
        key="solana",
        kind="solana",
        display_name="Solana",
        symbol="SOL",
        decimals=9,
        default_rpc_url="https://api.mainnet-beta.solana.com",
    ),
    "ethereum": Chain(
        key="ethereum",
        kind="evm",
        display_name="Ethereum",
        symbol="ETH",
        decimals=18,
        default_rpc_url="https://eth.llamarpc.com",
        explorer_chain_id=1,
    ),
    "base": Chain(
        key="base",
        kind="evm",
        display_name="Base",
        symbol="ETH",
        decimals=18,
        default_rpc_url="https://mainnet.base.org",
        explorer_chain_id=8453,
    ),
    "polygon": Chain(
        key="polygon",
        kind="evm",
        display_name="Polygon",
        symbol="POL",
        decimals=18,
        default_rpc_url="https://polygon-rpc.com",
        explorer_chain_id=137,
    ),
}

# Every watched address hangs off one account per connection, so the holdings
# land in a single Wallet with one tax character — the shape Coinbase uses.
# Accounts are keyed per connection, so a fixed id here cannot collide.
ACCOUNT_EXTERNAL_ID = "onchain:wallets"


def rpc_url(chain: Chain) -> str:
    return get_settings().onchain_rpc_urls.get(chain.key) or chain.default_rpc_url


def address_is_valid(chain: Chain, address: str) -> bool:
    pattern = _EVM_ADDRESS if chain.kind == "evm" else _SOLANA_ADDRESS
    return bool(pattern.match(address or ""))


def normalize_address(chain: Chain, address: str) -> str:
    """EVM addresses are case-insensitive, so casing must not mint a second wallet."""
    cleaned = (address or "").strip()
    return cleaned.lower() if chain.kind == "evm" else cleaned


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


@dataclass(frozen=True)
class Transfers:
    """A page of transfers, plus every way that page falls short of the truth.

    A caller deciding "nothing moved" needs to know it saw everything, so each
    field below exists to stop that claim being made on partial evidence.

    ``saturated`` is None when the address's history was answerable at all.
    Otherwise it names which way it was not:

    * ``SATURATED_POOLED`` — the address transacts at a rate no person does,
      so it is an exchange, bridge or service. Past one, funds are pooled and
      the next hop is a custodian's internal accounting rather than a payment.
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

    @property
    def complete(self) -> bool:
        return self.saturated is None and not self.trimmed and not self.unreadable


def detect_chain(address: str) -> Optional[Chain]:
    """Guess the network from the address shape.

    A bare EVM address resolves to Ethereum because the same bytes are a valid
    address on every EVM chain and only the user knows which they meant. An
    unrecognized shape returns None so the caller can reject it by name rather
    than silently pick a chain.
    """
    if _EVM_ADDRESS.match(address):
        return CHAINS["ethereum"]
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


def _raise_for_status(resp: httpx.Response, label: str) -> None:
    """Fail without quoting the request.

    ``httpx``'s own status error stringifies the full URL, and these URLs carry
    credentials: Etherscan takes its key as a query parameter, and paid Solana
    RPC providers put one in the path or query. That string would reach an API
    response and a log line, so it is never allowed to form. The chain and the
    status code are all a caller can act on anyway.
    """
    if not resp.is_success:
        raise RuntimeError(f"{label} returned HTTP {resp.status_code}")


async def _json_rpc(
    chain: Chain, method: str, params: list[Any], *, client: Optional[httpx.AsyncClient] = None
) -> Any:
    """One JSON-RPC call, with a node's failure modes mapped to ours."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(RPC_RETRY_ATTEMPTS):
        if client is not None:
            resp = await client.post(rpc_url(chain), json=body)
        else:
            async with _client() as own:
                resp = await own.post(rpc_url(chain), json=body)
        if resp.status_code != 429:
            break
        if attempt == RPC_RETRY_ATTEMPTS - 1:
            raise ProviderRateLimited(f"{chain.display_name} RPC rate-limited the request")
        await asyncio.sleep(RPC_RETRY_BACKOFF_SECONDS * (attempt + 1))
    _raise_for_status(resp, f"{chain.display_name} RPC")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"{chain.display_name} RPC returned a non-JSON response") from exc
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(f"{chain.display_name} RPC error on {method}")
    return payload.get("result") if isinstance(payload, dict) else None


async def native_balance(
    chain: Chain, address: str, *, client: Optional[httpx.AsyncClient] = None
) -> Optional[Decimal]:
    """Native-coin balance in whole coins, or None when the node won't say."""
    if chain.kind == "solana":
        result = await _json_rpc(chain, "getBalance", [address], client=client)
        raw = result.get("value") if isinstance(result, dict) else None
    else:
        raw = await _json_rpc(chain, "eth_getBalance", [address, "latest"], client=client)
    return _scale(raw, chain.decimals) if raw is not None else None


def _within(timestamp: float, since: Optional[datetime], until: Optional[datetime]) -> bool:
    if since is not None and timestamp < since.timestamp():
        return False
    return until is None or timestamp <= until.timestamp()


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
) -> Transfers:
    """Native-coin transfers touching ``address``, newest first.

    ``since``/``until`` are applied before the expensive per-transaction fetch,
    not after, so narrowing the window narrows the request count too. A
    saturated address returns its verdict without its history: a caller that
    will not walk past one has no use for it, and fetching it anyway costs a
    request per transaction for an answer already known to be discarded.
    """
    if chain.kind == "solana":
        return await _solana_transfers(
            chain, address, limit=limit, since=since, until=until, client=client
        )
    return await _evm_transfers(
        chain, address, limit=limit, since=since, until=until, client=client
    )


async def _solana_transfers(
    chain: Chain,
    address: str,
    *,
    limit: int,
    since: Optional[datetime],
    until: Optional[datetime],
    client: Optional[httpx.AsyncClient],
) -> Transfers:
    """Walk the address's signatures, then read each transaction's balance deltas.

    The amount of a Solana transfer is not in the signature list — only in the
    transaction — so this is unavoidably one request per transaction, which is
    why they go out concurrently under a small semaphore.

    Amounts come from pre/post balance deltas rather than from parsed
    ``system.transfer`` instructions, because a drainer's sweep is often a
    CPI from a program and never appears as a top-level transfer.
    """
    signatures = await _json_rpc(
        chain,
        "getSignaturesForAddress",
        [address, {"limit": SOLANA_SIGNATURE_PAGE}],
        client=client,
    )
    rows = [row for row in signatures or [] if isinstance(row, dict)]
    saturated = _saturation(
        [int(row["blockTime"]) for row in rows if row.get("blockTime")],
        SOLANA_SIGNATURE_PAGE,
        since,
    )
    if saturated:
        return Transfers(items=[], saturated=saturated)

    in_window = [
        row
        for row in rows
        if not row.get("err") and row.get("blockTime") and _within(row["blockTime"], since, until)
    ]
    wanted = _closest_to_horizon(in_window, limit, since)

    gate = asyncio.Semaphore(TX_FETCH_CONCURRENCY)

    async def fetch(signature: str) -> Any:
        async with gate:
            return await _json_rpc(
                chain,
                "getTransaction",
                [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                client=client,
            )

    fetched = await asyncio.gather(*(fetch(row["signature"]) for row in wanted))

    found: list[Transfer] = []
    unreadable = 0
    for row, tx in zip(wanted, fetched):
        deltas = _solana_deltas(chain, address, row["signature"], tx)
        if deltas is None:
            # The node would not return the transaction, or returned one this
            # code cannot attribute. Either way the movement is unknown, and an
            # unknown must not read as an absence.
            unreadable += 1
            continue
        found.extend(deltas)
    return Transfers(
        items=found,
        trimmed=len(wanted) < len(in_window),
        unreadable=unreadable,
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
    if not isinstance(tx, dict) or not tx.get("blockTime"):
        return None
    meta = tx.get("meta") or {}
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = [k.get("pubkey") for k in message.get("accountKeys") or [] if isinstance(k, dict)]
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if not (len(keys) == len(pre) == len(post)) or not keys:
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
    occurred_at = datetime.fromtimestamp(int(tx["blockTime"]), tz=timezone.utc)
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
) -> Transfers:
    """Address history from Etherscan, because EVM JSON-RPC does not have it.

    Two lists, not one. ``txlist`` covers transactions the address itself sent
    or received; ``txlistinternal`` covers native coin moved *by a contract*.
    Skipping the second would miss the dominant EVM drain: the victim signs a
    call whose own value is zero, and the sweep happens inside the contract.
    Reading only ``txlist`` reports that drained wallet as untouched.

    Raising when the key is missing is deliberate. Returning an empty list
    would be indistinguishable from an address that has never transacted, and
    a trace that silently ends on a missing key is worse than one that stops
    and says why.
    """
    settings = get_settings()
    if not settings.etherscan_api_key:
        raise ProviderNotConfiguredError(
            f"Tracing {chain.display_name} needs an Etherscan API key. Set "
            "ETHERSCAN_API_KEY (one key covers every EVM chain here). Balances "
            "work without it."
        )
    rows: list[dict] = []
    saturated: Optional[str] = None
    for action in ("txlist", "txlistinternal"):
        page = await _etherscan_page(chain, address, action, settings.etherscan_api_key, client)
        rows.extend(page)
        saturated = saturated or _saturation(
            [int(r["timeStamp"]) for r in page if r.get("timeStamp")], EVM_HISTORY_PAGE, since
        )
    if saturated:
        return Transfers(items=[], saturated=saturated)

    found: list[Transfer] = []
    for row in rows:
        if row.get("isError") == "1":
            continue
        amount = _scale(row.get("value"), chain.decimals)
        timestamp = row.get("timeStamp")
        recipient = str(row.get("to") or "").lower()
        sender = str(row.get("from") or "").lower()
        # A contract creation has no `to`. It is a real transaction but not a
        # transfer to anywhere a trace can follow.
        if amount is None or amount == 0 or timestamp is None or not recipient or not sender:
            continue
        occurred = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
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
    trimmed = _closest_to_horizon(found, limit, since)
    return Transfers(items=trimmed, trimmed=len(trimmed) < len(found))


async def _etherscan_page(
    chain: Chain,
    address: str,
    action: str,
    api_key: str,
    client: Optional[httpx.AsyncClient],
) -> list[dict]:
    """One Etherscan list, with "no transactions found" read as an empty history.

    Etherscan's own 429 is not retried the way a public JSON-RPC node's is:
    its limit is a per-key quota rather than a momentary burst, so waiting a
    second and asking again spends the same quota to be refused again.
    """
    params = {
        "chainid": chain.explorer_chain_id,
        "module": "account",
        "action": action,
        "address": address,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": EVM_HISTORY_PAGE,
        "sort": "desc",
        "apikey": api_key,
    }
    if client is not None:
        resp = await client.get(ETHERSCAN_V2_URL, params=params)
    else:
        async with _client() as own:
            resp = await own.get(ETHERSCAN_V2_URL, params=params)
    if resp.status_code == 429:
        raise ProviderRateLimited("Etherscan rate-limited the request")
    _raise_for_status(resp, "Etherscan")
    try:
        payload = resp.json() if resp.content else {}
    except ValueError as exc:
        raise RuntimeError("Etherscan returned a non-JSON response") from exc
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    if isinstance(payload, dict) and str(payload.get("status")) == "0":
        return []
    raise RuntimeError(f"Etherscan returned an unexpected payload for {chain.key}")


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
        holdings = await self.get_holdings(credentials)
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
        """One holding per watched address, valued at the native coin's spot.

        The price comes from Coinbase's public rate table, which needs no key
        and works with Coinbase switched off — but does mean a Coinbase outage
        stops this sync. That is the intended failure: it raises, the sync
        layer logs and keeps the previous values, and the holdings go stale
        rather than being rewritten at a price nobody knows.

        An address the node cannot answer for is skipped for the same reason:
        a request that timed out is not a wallet that emptied, and writing the
        zero would archive a live position.
        """
        watched = self._watched(credentials)
        prices = await usd_spot_prices()
        holdings: list[HoldingData] = []
        async with session() as client:
            for entry in watched:
                try:
                    quantity = await native_balance(entry.chain, entry.address, client=client)
                except Exception:
                    logger.exception("Could not read %s balance", entry.external_id)
                    continue
                if quantity is None:
                    continue
                price = prices.get(entry.chain.symbol)
                if price is None and quantity != 0:
                    logger.warning("No USD price for %s; skipping holding", entry.chain.symbol)
                    continue
                holdings.append(
                    HoldingData(
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
