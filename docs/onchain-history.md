# Owned historical evidence

Historical evidence connects the source crosswalk (#143), reviewed transfers
(#133), and Activity timeline (#147). The Solana archive (#144) also stores
Ethereum, Base, Polygon, Bitcoin, and explicit token-aware research (#155).
Collection and investigation do not apply financial transactions.

## Shared producer contract

The workspace connection/account mapping and the collecting user's ownership
assertion are separate from observed chain endpoints. Token-account ownership
is supported only at the recorded observation or reviewed scope. Current
ownership never establishes ownership throughout the account's history.

An archive identifies a transaction by network and source transaction reference.
Instruction paths, inner-instruction ordinals, balance observation identities,
and the transaction-level fee distinguish its facts. Asset identity includes
network, native marker or mint, and token program; symbols are display context.
EVM evidence uses contract addresses, receipt log indices and trace paths;
Bitcoin evidence uses input indices, scripts and outpoints. Equal symbols do
not equate assets across networks.

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
Collectors perform reads sequentially within the shared five-read transport
ceiling. Solana searches at most 128 account streams and retains
512 inventory entries. Further candidates remain named omissions; their original
discovery responses are retained. Re-observation is explicit; changed collection
scope requires a new collection.

The archive version is `owned-history-1`, with a network-specific decoder. Its stable
top-level fields are `chain`, `owner`, `source_identity`, `requested`, `anchor`,
`inventory` (address/script-keyed), `streams` (source/address-keyed), `payloads`
(digest-keyed), `transactions` (`chain:reference`-keyed), `coverage`, `limits`, and `gaps`.
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
and a pinned anchor. Explicit Continue revalidates that anchor; GET and export
remain offline reads. Provisional observations cannot supply settled quantity.
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
The equation includes only transactions inside those observed slot boundaries
with compatible account/asset ownership evidence. Earlier or later activity
under another owner does not change that equation. Missing ownership, balance
observations, or transaction ordering inside the interval leaves the known
subtotal visible but the expected closing quantity and discrepancy unresolved.

Unknown programs, unresolved keys/owners, unsupported token extensions, and
uncorroborated bridge destinations retain source evidence and named gaps.
Capabilities remain separate per network and source. The UI reads
`GET /api/onchain/chains`; unavailable source capabilities never become complete
empty history.

## Network qualification

| Network | Retained history | Settlement and quantity boundaries |
| --- | --- | --- |
| Solana | Owner and discovered/reviewed token-account streams, instructions, inner instructions, balances and supported conversions | Finalized pinned anchor; historical ownership and unknown programs remain explicit gaps |
| Ethereum / Polygon | Ordinary transactions, internal execution, ERC-20 logs, receipts and gas | Finalized block evidence; reverted descendants have no successful principal; unknown token economics remain observations |
| Base | EVM evidence plus execution, L1 and operator fee components | Finalized L2 must agree with configured rollup L1 context; missing context or fee components remain unresolved |
| Bitcoin | Confirmed/mempool streams, exact prevouts, outputs, scripts, transaction fee and outspends | Six active-chain confirmations; coinbase maturity requires 100 confirmations separately; mixed-input allocation and unlisted change ownership remain unknown |

EVM requests accept inclusive `start_block` and `end_block` as well as UTC
bounds. Discovery uses configured Etherscan or Blockscout, with bounded RPC log
ranges and retained cursors. Missing internal execution or receipts preserve
successful sibling evidence and identify unavailable coverage. Native opening
and closing balances are not manufactured. Token snapshots identify their
actual end-of-block bounds, independently of the requested UTC interval.

Standard ERC-20 quantities require reviewed non-proxy historical bytecode
semantics plus matching code and balance/log corroboration. Face-value logs
remain evidence when decimals, semantics, snapshots, or execution are unknown.
Rebasing, transfer-fee, proxy/upgraded and unsupported execution mechanics cannot
silently become standard token economics.

The server-only `EVM_HISTORY_POLICIES` JSON setting is keyed by `ethereum`,
`base`, or `polygon`. It defaults to `{}`. Each entry can contain:

- `rollup_rpc_url`: endpoint supporting `optimism_syncStatus` for Base finality.
- `operator_fee_forks`: sourced intervals with inclusive `from_timestamp`,
  exclusive optional `until_timestamp`, `formula` (`pre_isthmus`, `isthmus`,
  `jovian`) and `source`. An unknown or overlapping applicable interval leaves
  the fee formula unresolved.
- `standard_tokens`: contract-keyed reviews containing `from_block`, `to_block`,
  64-character lowercase SHA256 `code_hash` of decoded `eth_getCode` bytes,
  `semantics: standard_erc20`, `non_proxy: true`, and a review `source`.

These are operator-reviewed historical claims, not user-supplied API overrides.
No default deployment dates, token semantics or code hashes are inferred.
Source identity binds configuration without exporting endpoint credentials.
`backend/tests/test_evm_history.py` contains a complete synthetic policy example.

Bitcoin additional inventory accepts reviewed owned addresses or script hex,
without inferring an entire wallet or clustered ownership. The network fee is
input value minus output value and is already included in the owned input/output
net. It remains a non-additive fact; mixed inputs do not establish its owner.
Outpoint research follows exact outputs/spends without assigning a pooled
transaction's proceeds to the starting coin. Replaced/conflicted/reorganized
observations retain versions; a missing transaction does not prove replacement
or reversal. Known settled subtotals remain visible beside unknown full bounds.

## Token-aware investigation

Activity event details provide **Follow evidence**. Preview starts only on
submission and reads retained workspace evidence; it makes no provider calls.
Select an exact leg, direction, inclusive UTC bounds, up to six hops and five
branches, and exact minimum quantities keyed by canonical asset identity.
Supported swaps and wrapping preserve both asset sides and fee legs. Reviewed
owned transfers connect supplied acquisition lineage without electing a basis
method. External adjacency remains research with unknown ownership/allocation.

`POST /api/onchain/investigation/preview` accepts `event_id`, `leg_id`,
`direction` (`in`/`out`), optional `since`/`until`, `max_hops`, `max_branches`, and
`minimums`. Its response includes selected events/legs, steps, effective windows,
frontiers, boundaries, and a nullable retained `collection_id`/`revision`.
Inspecting a reached event reuses its full event/source details; asset filtering
never removes the transaction's other legs. Navigation can return to the trail.

**Collect selected continuation** is a separate writable-workspace action.
`POST /api/onchain/investigation/continue` adds `collection_id`,
`expected_revision` and the explicit `frontier_key` to that request. The server
rechecks the retained selection and ownership/source qualification before RPC.
Research shares the invocation deadline, attempts, page and byte limits and
retains unfinished frontier and named stop reasons inside the existing archive.
It does not create external owned accounts, financial records or a second ledger.
Opening/exporting the archive remains independent of short-lived trace checkpoints.

Bridge review is available from the same event. Candidates require independent
source/destination executions, protocol and message identity, exact asset mapping,
endpoints, quantities and fees. Imported leg derivation may supply
`bridge_protocol`, `bridge_message_id`, `bridge_role` (`send`/`receive`),
`bridge_source_chain`, `bridge_destination_chain`, `bridge_source_asset`, and
`bridge_destination_asset`; those labels alone cannot qualify a pair. Current
root execution must corroborate the source leg and fee. Ambiguous or conflicting
destination observations remain unresolved.

`GET /api/onchain/investigation/bridge-candidates?event_id=...` returns reviewed
facts and reasons. `POST /api/onchain/investigation/bridge` accepts the chosen
source/destination event, leg and source IDs, the collection/revision, and
`reviewed: true`. Confirmation stores references in the existing archive and
revalidates them on every preview/export. Source invalidation withdraws the
relationship's qualification. Review cannot supply missing chain proof or make
a tax/basis election. Unsupported bridges retain an explicit boundary.

## Primary decoding references

- [Solana transaction structures](https://solana.com/docs/rpc/json-structures)
- [Address signature pagination](https://solana.com/docs/rpc/http/getsignaturesforaddress)
- [Transaction retrieval and commitment](https://solana.com/docs/rpc/http/gettransaction)
- [Wrapped SOL and SyncNative](https://solana.com/docs/tokens/basics/sync-native)
- [Token-2022 transfer fees](https://solana.com/docs/tokens/extensions/transfer-fees)
- [Jupiter instruction definitions](https://github.com/jup-ag/instruction-parser/blob/main/src/idl/jupiter.ts)
