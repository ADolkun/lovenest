# Recovery evidence review

Recovery review links claim statements, historical platform ledgers, notices, receiving receipts, dispositions, proceeds, equity statements and tax-workpaper assertions. It preserves their different meanings. Retaining or reviewing these records creates no holdings, trades, cash credits, basis elections or losses.

Choose an existing destination wallet in the selected Investment workspace. Historical workspace and partner labels are source context, not permission to create an account. A retained source from another wallet in the same workspace can be attached without copying or relabeling it; its original source wallet remains visible. Cross-workspace references are rejected.

Manual evidence can optionally associate an existing active holding in the destination wallet. The source quantity and identity remain separate inputs; selecting a holding does not supply missing identity or apply money. Retained-source attachments preserve their original holding association. The server checks wallet and identity boundaries again, and reads/exports qualify unavailable, moved, archived or conflicting associations without exposing another workspace's holding facts.

## Sources, rounds and amounts

A source observation retains its provider/account identity, locator, original clock and precision, status, asset identity and exact reported amounts. Blank means unknown, and explicit zero remains zero. Observation/import time does not fill an unknown statement date. Claim face value, liquid withdrawals, recovery valuation, stock cost, reported sale proceeds and cash credited are separate measures with separate currencies.

Give related asset records an explicit case, round and round-asset key. A round with two assets counts as one round and two asset records. Repeated observations or notices do not add rounds or quantities. Missing identity and missing receiving activity stay visible.

The same source identity, leg, role and annotation facts replay idempotently even if a client supplies a fresh display key. A reused key with changed facts is rejected; changed source/annotation versions remain separate evidence and are qualified as conflicts. Sources are immutable.

Ordinary CSV order preview refuses `claim`, `distribution` and insolvency-distribution labels even when they have a price. Use the existing evidence CSV importer and attach those source rows, or enter source observations through recovery review. A bare distribution label remains unresolved; it does not establish dividend or insolvency treatment. Missing/unusable acquisition-value checks, ordinary purchases/rewards and explicitly independent opening-lot imports retain their existing behavior. Recovery claim/notice/equity/workpaper annotations cannot use opening-lots review to become acquisitions.

## Relationships and review assertions

Relationships have independent candidate, confirmed, conflict and missing states. A confirmed notice can still have a missing receipt. Notice-to-receipt confirmation requires compatible asset/quantity evidence, an explicit shared round, receipt settlement, source timing and documented account mapping. A quantity difference needs its exact sourced rounding or fee explanation. Similar amounts and times alone do not confirm a transfer or establish which recovery funded a sale.

A confirmed receipt-to-disposition chain uses a currently supported receipt application and canonical sale lot provenance. When accounts differ, the referenced owned transfer must contain the receipt's selected lots and appear in the consumed sale lineage. An unrelated transfer between the same accounts is insufficient. Unsupported chains remain candidate evidence; sales and reported proceeds can still be retained with unknown acquisition date, basis, fee treatment or cash credit.

Two separate documentary relationships preserve source associations before financial lineage is established:

- `documentary_equity_distribution` links a claim or recovery notice to an equity statement. Confirmation requires supported security identity, source dates, account/timing review, current supporting sources, and an explicit equity round/asset; a notice must use that same round/asset. Claim dollars and shares are never compared.
- `documentary_receipt_disposition` links a receipt or equity statement to a disposition. Confirmation requires compatible strong asset identity, dated current sources, supporting documents, account/timing review, and an exact positive `documentary_quantity` no greater than either source quantity. The amount describes the documented association, including a partial disposition; it does not allocate lots or establish cumulative inventory. Tickers, identical unverified holding selections and provider IDs from different namespaces cannot establish identity.

Documentary relationships reject owned-transfer references and fee/rounding adjustments. Their confirmation certifies only the recorded association, never applied inventory, acquisition basis, tax treatment or final claim settlement. Existing financial `receipt_disposition` and owned-transfer lineage checks remain independent. A documentary link does not clear a missing receipt or filing/model blocker, and confirmation is requalified when a source or its associated holding changes.

Reported stock cost, modeled allocation and valuation assertions retain separate labels, currencies and provenance. A tax workpaper remains modeled. Filing assertions remain unverified in this feature because there is no independently qualified filed-record contract; marking them supported is rejected, and allocation reliance stays blocked. Recording a supported assertion does not write basis or decide tax treatment.

Corrections record the named field/reference, proposed value, source, reason and status. New reviews may supersede a prior review of the same logical assertion; the original review and original source remain accessible. Changing a reference does not resolve unrelated missing valuations, mappings or filing assumptions.

Allocation previews declare required input entries and controlling assumption reviews. The server reports missing round inputs/identity, missing valuation/currency, unresolved source/account/lot mappings, superseded or unresolved assumptions, and explicit model defects. Readiness is recomputed from current evidence; it is not a client flag. There is no allocation-finalization or tax-election endpoint.

## Supported receipt application

A fully qualified on-chain receipt can be reviewed through the existing owned-movement flow. That flow owns the one canonical quantity application, workspace/ownership checks, locking, replay and reversal. A synthetic four-unit recovery described by a claim, notice, receipt and workpaper produces four observations and at most one reviewed four-unit incoming movement. An incoming movement without supported acquisition history preserves unknown acquisition date and basis, including across a later supported owned transfer.

Ordinary off-chain receipts remain evidence-only in this feature: the movement writer requires chain, token, transaction/leg, endpoint, raw-unit, settlement and fee evidence. The review exposes these blockers. It never fabricates chain information to make a source eligible, and it never treats a receipt-time valuation as acquisition basis.

## API and exports

All routes use the standard selected-workspace authentication and membership checks. Reads, previews and exports allow workspace readers; retention and reviews require write access.

| Route | Purpose |
|---|---|
| `GET /api/assets/recovery?group_id=...` | Recovery entries, related review context, counts and blockers. Optional case, round, role, state and text filters. |
| `POST /api/assets/recovery/preview` | Validate new observations or retained-source attachments without writes. |
| `POST /api/assets/recovery/retain` | Retain source annotations with an expected preview revision; no financial effects. |
| `POST /api/assets/recovery/reviews` | Append reviewed relationships, assertions, corrections or allocation previews with an expected revision. |
| `GET /api/assets/recovery/export?format=json\|csv&group_id=...` | Read-only export using the same filters and projection, optionally pinned to an expected revision. |

Filtered counts concern the selected rows; related context is labeled and included to explain their links and conflicts. Entries do not need a current holding, so sold, archived and unmapped evidence remains reviewable. JSON preserves exact decimal strings, nulls, source precision, original observations, reviewed states and blockers. CSV includes readable columns plus a quoted `payload_json` record for a lossless representation of null versus empty string and full source/review context. Source reattachment or re-retention does not execute financial decisions from an export.

The case summary uses the selected source rows and shows each associated holding once. Receipts, dispositions, statement cost, source valuation and stored holding quantities are not summed or subtracted. `associated_holding.stored_quantity` is an exact stored value, not verified current inventory; its quantity observation date remains unknown because the holding has no dedicated quantity clock. Quote dates and import times do not fill that gap. Source-reported cost retains its original currency/date and remains distinct from verified acquisition basis. The same qualified holding fields are included in existing JSON/CSV exports and the recovery revision.

A stale write or pinned export returns a conflict and requires refreshing the review. Repeated identical retention/review requests are idempotent. Every source, target, supporting observation, required assertion and owned-transfer identifier is checked inside the selected workspace.
