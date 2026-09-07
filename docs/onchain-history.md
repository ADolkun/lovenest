# Owned historical evidence

Issue #144 supplies historical Solana evidence for the source crosswalk (#143),
reviewed transfers (#133), activity presentation (#147), and later collectors
and token-aware continuation (#155). It does not apply financial transactions.

## Producer contract agreed for #155 Stage A

The workspace connection/account mapping and the collecting user's ownership
assertion are separate from observed chain endpoints. Token-account ownership
is supported only at the recorded observation or reviewed scope. Current
ownership never establishes ownership throughout the account's history.

An archive identifies a transaction by network and source transaction reference.
Instruction paths, inner-instruction ordinals, balance observation identities,
and the transaction-level fee distinguish its facts. Asset identity includes
network, native marker or mint, and token program; symbols are display context.
Later EVM collectors can use receipt log indices and trace paths; Bitcoin can
use input indices and outpoints without changing this boundary.

Raw RPC responses retain their logical source, nonsecret request parameters,
retrieval time, digest, and derivation references. Endpoint credentials and
headers are excluded. Quantities and atomic units in normalized evidence are
strings. Downloads preserve server-produced JSON bytes so JavaScript cannot
round large integers in raw payloads. Decoding can be reproduced from the
retained responses without another RPC request.

Zero-leg payloads remain evidence. Transactions with more than 100 legs retain
every leg in the archive and use bounded segments for the existing source
observation contract. Segmentation does not create another transaction or fee.
Durable archives are workspace-authorized and independent of native-trace
checkpoint expiry. Opening saved evidence does not collect more history.

Continuation binds the requested inclusive UTC bounds, owned target, source
configuration, decoder version, and anchor. Each invocation is bounded to
45 seconds, five shared endpoint reads, 600 upstream attempts including
retries, and 16 pages per address. Inventory and retained-byte ceilings are
also reported. An unfinished walk retains its cursor and stop reason.
The first Solana collector performs those reads sequentially within the shared
five-read transport ceiling. It searches at most 128 account streams and retains
512 inventory entries. Further candidates remain named omissions; their original
discovery responses are retained. Re-observation is explicit; changed collection
scope requires a new collection.

The current archive version is `owned-history-1`, decoder `solana-1`. Its stable
top-level fields are `chain`, `owner`, `source_identity`, `requested`, `anchor`,
`inventory` (address-keyed), `streams` (address-keyed), `payloads` (digest-keyed),
`transactions` (`solana:<signature>`-keyed), `coverage`, `limits`, and `gaps`.
Each transaction retains `discovery_refs`, `versions`, and a nullable
`canonical_version`. Versions identify their `payload_digest`, exact `legs`,
separate balance `observations`, supported `relationships`, and qualification.

`POST /api/onchain/history` requires a writable workspace, owned connection and
account mapping, and `ownership_confirmed: true`. It returns `collection_id`,
`revision`, `request`, `evidence`, and existing-contract `observations`.
Continue using the saved request with `collection_id` and `expected_revision`;
use `reobserve: true` explicitly to refresh retained transactions. A stale
revision returns 409. Contended workspace, connection or wallet locks return
`history_busy` (409) immediately, before any RPC read, so waiting for another
collection cannot extend the invocation deadline. `GET /api/onchain/history` lists bounded summaries;
`GET /api/onchain/history/{id}` reopens evidence and `/{id}/export` returns an
attachment. All reads are workspace-authorized and make no upstream calls.

Raw admission is limited to 8 MiB per payload and 24 MiB per archive. Full
collector serialization is limited to 30 MiB; durable storage allows 32 MiB per
collection, 128 MiB and 100 collections per workspace. Overflow retains a named
gap and digest/reference; it never silently drops decoded tail legs. Admission
reserves collection capacity under a workspace row lock before fetching.
Archives have no checkpoint TTL and remain until workspace deletion. Export
does not itself delete retained evidence or reclaim its storage.

## Interpretation and accounting

Native and token pre/post balances are observations. Decoded principal, network
fees, token fees, wrapping backing, and rent effects retain their own identities
and roles. A balance delta already includes its effects: it is not added to
instruction quantities, and its fee is not subtracted again. The network fee
is charged once to its observed payer. Failed attempted transfers have no
successful principal, even when their fee settles.

Execution status and settlement are separate. Solana uses a finalized policy
and a pinned anchor. Provisional observations cannot supply settled quantity.
Later observations retain prior versions and qualify the current interpretation;
conflicting evidence cannot silently preserve a settled verdict. A missing
response is a retrieval gap, not proof of reversal.

Qualification also compares overlapping archives for the same workspace,
connection and owner. Conflicting raw transaction bodies or a previously
recorded reorganization disqualify every current projection of that transaction;
replaying an older collection cannot restore its settled verdict. Read/export
responses show this current cross-collection qualification while retaining all
original local versions. The revision token binds saved continuation state.

Supported current legs are retained through the existing #143 observation/event
service, with no financial application. Superseded source payloads remain
immutable. Their `is_current` qualification makes preview return unknown
settlement and a conflict reason, preventing stale links or application while
keeping prior reviewed references inspectable. Zero-leg source payloads remain
in the producer archive without fabricated import legs.

The crosswalk's 128-digit arithmetic range is narrower than a token's valid u8
decimals. Out-of-range quantities remain exact in raw/decoded archive evidence;
their crosswalk quantities become unknown with `crosswalk_quantity_out_of_range`.
A connected account without an asset-group mapping still retains observations
with a nullable group and `crosswalk_account_mapping_unavailable`; collection
does not create a financial mapping. Both adapter limitations appear in the
response's overall gaps. Summary listing hashes retained transaction bodies
once across collections and does not reconstruct their full reconciliations.

Inventory, retrieval, interpretation, and settlement are separate coverage
axes. Exhausting the owner's signatures does not prove that all historical
token accounts have been found. Unpriced, zero-balance, and closed accounts
are evidence inventory, independent of holdings display limits.

Reconciliation compares compatible account/asset boundaries using exact
quantities: opening plus signed settled changes equals closing. Missing or
incompatible snapshots remain unresolved. Transaction-boundary observations
must identify their actual bounds; they cannot manufacture an opening at an
arbitrary requested UTC endpoint. Quantity agreement establishes neither
history completeness nor acquisition basis or tax treatment.

The initial reconciliation reports `scope: observed_transaction_boundaries`
with explicit before/after transaction slots and references, alongside
`requested_interval_status: unresolved` when independent UTC-boundary snapshots
are unavailable. Unknown window membership contributes no settled change;
interpretation gaps remain visible even when a known subtotal happens to match.

Unknown programs, unresolved keys/owners, unsupported token extensions, and
uncorroborated bridge destinations retain source evidence and named gaps.
EVM and Bitcoin historical collection are explicitly unsupported in this
release; their existing holdings and native research remain separate capabilities.

## Primary decoding references

- [Solana transaction structures](https://solana.com/docs/rpc/json-structures)
- [Address signature pagination](https://solana.com/docs/rpc/http/getsignaturesforaddress)
- [Transaction retrieval and commitment](https://solana.com/docs/rpc/http/gettransaction)
- [Wrapped SOL and SyncNative](https://solana.com/docs/tokens/basics/sync-native)
- [Token-2022 transfer fees](https://solana.com/docs/tokens/extensions/transfer-fees)
- [Jupiter instruction definitions](https://github.com/jup-ag/instruction-parser/blob/main/src/idl/jupiter.ts)
