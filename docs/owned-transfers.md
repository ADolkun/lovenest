# Reviewed owned transfers

Assets Activity and investment source review use retained observations from
source reconciliation and owned history collection. Review identifies the
workspace, both holdings, explicit ownership, exact movement legs and selected
source lots. Collection and preview do not apply financial activity.

Confirmation compares network, native or token identity, token program,
transaction and leg references, endpoints, exact units and settlement. Symbol,
amount, date or hash alone is insufficient. Missing/conflicting facts remain
reviewable candidates; provider completion cannot override pending network status.

Select available acquisition fragments explicitly. Partial and chained transfers
retain original identity, supported cost and date. Unknown-basis quantities
remain separate from known subtotals; an unknown date is not short-term.
Performance cost retains its weighted-average meaning separately from original
lot costs. Neither is automatically a filing-ready tax result.

Principal and fees have separate identities and quantities. Failed principal
does not settle, a net balance delta is not another fee debit, and third-party
fees do not debit an owned holding. Fee tax treatment stays unresolved.

Repeated confirmation returns the retained decision. Reversal restores both
holdings atomically or identifies dependent activity to resolve first. Source
edits, historical inserts and import undo cannot silently change active
allocations. Original observations and reversal history survive; independently
reported provider quantities remain authoritative over incomplete replay.

An external outbound can retain acquisition lineage and a reported-scam
annotation without creating an owned recipient, sale gain, income or tax loss.
Later pooled movement amounts are not attributed wholly to the selected transfer.

The workspace-scoped API is under `/api/assets/evidence`: `/ownership` for
assertions, `/transfers` for list/preview/confirmation/reversal, `/movements`
for reviewed quantity-only application and `/incidents` for allegations.
`backend/app/schemas/owned_transfer.py` defines exact request/response fields.
Financial review amounts use decimal strings; null differs from explicit zero.
Writes require workspace write access and the current review revision. Source
retrieval, bank-funding completeness and tax elections remain separate.
