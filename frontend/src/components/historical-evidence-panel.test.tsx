import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { HistoricalEvidencePanel } from '@/components/historical-evidence-panel'
import { renderWithProviders } from '@/test/utils'
import type { HistoryCollection } from '@/types'

const api = vi.hoisted(() => ({ addresses: vi.fn(), histories: vi.fn(), collectHistory: vi.fn(), history: vi.fn(), exportHistory: vi.fn() }))
const workspace = vi.hoisted(() => ({ canWrite: true }))
vi.mock('@/lib/api', () => ({ onchain: api }))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: () => workspace }))

const result: HistoryCollection = {
  collection_id: 'collection-one', revision: 'revision-one', observations: [],
  request: { connection_id: 'connection-one', chain: 'solana', address: 'owner-A', ownership_confirmed: true },
  evidence: {
    version: 'owned-history-1', decoder_version: 'solana-history-1', chain: 'solana', owner: 'owner-A',
    requested: { since: null, until: null, commitment: 'finalized' }, anchor: { slot: 50, commitment: 'finalized' },
    coverage: { inventory: 'unknown', retrieval: 'partial', interpretation: 'partial', settlement: 'complete' },
    inventory: { 'token-A': { address: 'token-A', kind: 'token', discoveries: ['synthetic-current'], ownership: ['synthetic-owner'] } },
    streams: { 'token-A': { cursor: 'cursor-one', pages_examined: 2, exhausted: false, stop_reason: 'missing_payload', oldest_at: null, newest_at: null, unknown_timestamps: 1, payload_gaps: ['missing-one'] } },
    transactions: { 'solana:synthetic-tx': {
      signature: 'synthetic-tx', canonical_version: 'version-one', discovery_refs: ['owner-A', 'token-A'],
      versions: [{ version_id: 'version-one', payload_digest: 'digest-one', block_time: 1735689600, execution: 'succeeded', settlement: 'settled', gaps: [], legs: [
        { key: 'transfer-one', asset: { chain: 'solana', mint: 'mint-A', token_program: 'spl-token' }, role: 'principal', source: 'source-A', destination: 'token-A', quantity: '9007199254740993', raw_units: '9007199254740993', decimals: 0, settlement: 'settled' },
        { key: 'fee', asset: { chain: 'solana', native: true }, role: 'network_fee', source: 'sponsor-A', quantity: '0.03', raw_units: '30000000', decimals: 9, settlement: 'settled' },
      ] }],
    } },
    reconciliation: [{ account: 'token-A', asset: { chain: 'solana', mint: 'mint-A', token_program: 'spl-token' }, opening: null, settled_change: '9007199254740993', closing: '0', discrepancy: null, status: 'unresolved', reasons: ['opening_quantity_unknown'] }],
    limits: {}, gaps: ['historical_inventory_unknown'], resumable: true,
  },
}

beforeEach(() => {
  Object.values(api).forEach((mock) => mock.mockReset())
  localStorage.removeItem('privacyMode')
  workspace.canWrite = true
  api.addresses.mockResolvedValue([
    { connection_id: 'connection-one', connection_name: 'Owned wallet', chain: 'solana', address: 'owner-A', label: 'Wallet A' },
    { connection_id: 'connection-two', connection_name: 'EVM wallet', chain: 'ethereum', address: 'owner-E', label: 'Wallet E' },
  ])
  api.histories.mockResolvedValue([])
  api.collectHistory.mockResolvedValue(result)
  api.history.mockResolvedValue(result)
})

function panel(workspaceId = 'investment') {
  return <HistoricalEvidencePanel key={workspaceId} workspaceId={workspaceId} initial={{ selectedKey: 'solana:owner-A', since: '', until: '' }} />
}

async function collect(user: ReturnType<typeof renderWithProviders>['user']) {
  await screen.findByRole('combobox', { name: 'Evidence wallet' })
  await user.click(screen.getByRole('checkbox', { name: /I own the selected wallet/ }))
  await user.click(screen.getByRole('button', { name: 'Collect evidence' }))
  return screen.findByRole('region', { name: 'Historical evidence results' })
}

it('collects only after an owned-wallet assertion and sends inclusive UTC bounds without financial actions', async () => {
  const { user } = renderWithProviders(panel())
  await screen.findByRole('combobox', { name: 'Evidence wallet' })
  expect(api.collectHistory).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Collect evidence' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Evidence from (UTC)'), { target: { value: '2025-01-01T00:00:00.001' } })
  fireEvent.change(screen.getByLabelText('Evidence through (UTC)'), { target: { value: '2025-01-02T23:59:59.999' } })
  await collect(user)
  expect(api.collectHistory).toHaveBeenCalledWith({ connection_id: 'connection-one', chain: 'solana', address: 'owner-A', ownership_confirmed: true, since: '2025-01-01T00:00:00.001Z', until: '2025-01-02T23:59:59.999Z' }, 'investment')
  expect(screen.queryByRole('button', { name: /Apply|Confirm source link/ })).not.toBeInTheDocument()
})

it('keeps coverage dimensions, unknown snapshots, exact units and all legs under an asset filter; resumes once', async () => {
  const { user } = renderWithProviders(panel())
  const region = await collect(user)
  expect(within(region).getByText('Inventory').nextElementSibling).toHaveTextContent('unknown')
  expect(within(region).getByText('Retrieval').nextElementSibling).toHaveTextContent('partial')
  expect(within(region).getByText('Unknown + (9007199254740993) = 0')).toBeInTheDocument()
  await user.click(within(region).getByText(/Account inventory and retrieval/))
  expect(region).toHaveTextContent('Payload gaps: 1')
  expect(region).toHaveTextContent('Resume cursor: cursor-one')
  await user.selectOptions(screen.getByLabelText('Evidence asset'), 'solana:spl-token:mint-A')
  await user.click(screen.getByText('synthetic-tx'))
  expect(region).toHaveTextContent('solana:native · network fee · 0.03')
  expect(region).toHaveTextContent('Atomic units: 9007199254740993')
  await user.selectOptions(screen.getByLabelText('Settlement status'), 'provisional')
  expect(screen.getByText(/No transactions match these filters/)).toBeInTheDocument()
  api.collectHistory.mockRejectedValueOnce({ response: { data: { detail: { code: 'history_revision_conflict' } } } })
  await user.click(screen.getByRole('button', { name: 'Continue collection' }))
  await screen.findByText(/Another collection updated this archive/)
  expect(api.collectHistory).toHaveBeenLastCalledWith({ ...result.request, collection_id: 'collection-one', expected_revision: 'revision-one', reobserve: false }, 'investment')
  expect(region).toBeInTheDocument()
  expect(api.collectHistory).toHaveBeenCalledTimes(2)
})

it('explains a busy workspace and retries only when requested, retaining saved evidence', async () => {
  const { user } = renderWithProviders(panel())
  const region = await collect(user)
  api.collectHistory.mockRejectedValueOnce({ response: { data: { detail: { code: 'history_busy' } } } })
  await user.click(screen.getByRole('button', { name: 'Continue collection' }))
  await screen.findByText(/Another operation is using this workspace/)
  expect(region).toBeInTheDocument()
  expect(api.collectHistory).toHaveBeenCalledTimes(2)
  await user.click(screen.getByRole('button', { name: 'Retry' }))
  await waitFor(() => expect(api.collectHistory).toHaveBeenCalledTimes(3))
  expect(screen.queryByText(/Another operation is using this workspace/)).not.toBeInTheDocument()
})

it('opens durable saved evidence for viewers without collecting and downloads original bytes', async () => {
  workspace.canWrite = false
  api.histories.mockResolvedValue([{ collection_id: result.collection_id, revision: result.revision, request: result.request, updated_at: '2025-01-01T00:00:00Z', coverage: result.evidence.coverage, transaction_count: 1 }])
  const blob = new Blob(['{"raw":9007199254740993,"quantity":"9007199254740993"}'], { type: 'application/json' })
  api.exportHistory.mockResolvedValue(blob)
  const create = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:synthetic')
  const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  const { user } = renderWithProviders(panel())
  await screen.findByRole('option', { name: /1 Transactions/ })
  await user.selectOptions(screen.getByLabelText('Saved collections'), 'collection-one')
  await user.click(screen.getByRole('button', { name: 'Open saved evidence' }))
  await screen.findByRole('region', { name: 'Historical evidence results' })
  expect(api.collectHistory).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Continue collection' })).toBeDisabled()
  await user.click(screen.getByRole('button', { name: 'Export private evidence' }))
  await waitFor(() => expect(create).toHaveBeenCalledWith(blob))
  expect(click).toHaveBeenCalledOnce()
  create.mockRestore(); revoke.mockRestore(); click.mockRestore()
})

it('suppresses late collections and downloads after a workspace switch', async () => {
  let resolve!: (value: HistoryCollection) => void
  api.collectHistory.mockReturnValue(new Promise((done) => { resolve = done }))
  const { user, rerender } = renderWithProviders(panel())
  await screen.findByRole('combobox', { name: 'Evidence wallet' })
  await user.click(screen.getByRole('checkbox', { name: /I own the selected wallet/ }))
  await user.click(screen.getByRole('button', { name: 'Collect evidence' }))
  api.addresses.mockResolvedValue([])
  rerender(panel('household'))
  await act(async () => resolve(result))
  expect(screen.queryByRole('region', { name: 'Historical evidence results' })).not.toBeInTheDocument()
  expect(screen.queryByText('synthetic-tx')).not.toBeInTheDocument()
  expect(api.exportHistory).not.toHaveBeenCalled()
})

it('requires review for supplied historical accounts and keeps EVM collection unsupported', async () => {
  const { user } = renderWithProviders(panel())
  await screen.findByRole('combobox', { name: 'Evidence wallet' })
  await user.click(screen.getByRole('checkbox', { name: /I own the selected wallet/ }))
  await user.click(screen.getByText('Reviewed historical token accounts'))
  await user.type(screen.getByLabelText(/Historical token-account addresses/), 'closed-A, closed-A closed-B')
  expect(screen.getByRole('button', { name: 'Collect evidence' })).toBeDisabled()
  await user.click(screen.getByRole('checkbox', { name: /I reviewed these historical/ }))
  await user.click(screen.getByRole('button', { name: 'Collect evidence' }))
  await screen.findByRole('region', { name: 'Historical evidence results' })
  expect(api.collectHistory.mock.calls[0][0].supplied_accounts).toEqual([{ address: 'closed-A', owner: 'owner-A', reviewed: true }, { address: 'closed-B', owner: 'owner-A', reviewed: true }])
  await user.selectOptions(screen.getByLabelText('Evidence wallet'), 'connection-two:ethereum:owner-E')
  expect(screen.getByText(/EVM and Bitcoin history collection is unsupported/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Collect evidence' })).toBeDisabled()
  expect(screen.queryByRole('region', { name: 'Historical evidence results' })).not.toBeInTheDocument()
})

it('masks addresses, quantities and transaction references in privacy mode', async () => {
  localStorage.setItem('privacyMode', 'true')
  const { user } = renderWithProviders(panel())
  const region = await collect(user)
  for (const summary of region.querySelectorAll('summary')) await user.click(summary)
  for (const secret of ['owner-A', 'token-A', 'source-A', 'sponsor-A', 'synthetic-tx', '9007199254740993', '0.03', 'cursor-one', 'digest-one']) expect(region).not.toHaveTextContent(secret)
  expect(region).toHaveTextContent('•••••')
})

it('does not download a completed export after its workspace unmounts', async () => {
  let resolve!: (value: Blob) => void
  api.exportHistory.mockReturnValue(new Promise((done) => { resolve = done }))
  const create = vi.spyOn(URL, 'createObjectURL')
  const { user, rerender } = renderWithProviders(panel())
  await collect(user)
  await user.click(screen.getByRole('button', { name: 'Export private evidence' }))
  api.addresses.mockResolvedValue([])
  rerender(panel('household'))
  await act(async () => resolve(new Blob(['synthetic archive'])))
  expect(create).not.toHaveBeenCalled()
  create.mockRestore()
})

it('opens archived evidence after the watched connection is removed, while new collection stays disabled', async () => {
  api.addresses.mockResolvedValue([])
  api.histories.mockResolvedValue([{ collection_id: result.collection_id, revision: result.revision, request: result.request, updated_at: '2025-01-01T00:00:00Z', coverage: result.evidence.coverage, transaction_count: 1 }])
  const { user } = renderWithProviders(panel())
  await screen.findByRole('option', { name: /1 Transactions/ })
  expect(api.histories).toHaveBeenCalledWith('investment', undefined, undefined, expect.any(AbortSignal))
  await user.selectOptions(screen.getByLabelText('Saved collections'), 'collection-one')
  await user.click(screen.getByRole('button', { name: 'Open saved evidence' }))
  await screen.findByRole('region', { name: 'Historical evidence results' })
  expect(screen.getByRole('button', { name: 'Export private evidence' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Continue collection' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Re-observe settlement' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Start new collection' })).toBeDisabled()
  expect(api.collectHistory).not.toHaveBeenCalled()
})

it('keeps archived evidence within the Assets wallet filter without requiring a live target', async () => {
  api.addresses.mockResolvedValue([])
  api.histories.mockResolvedValue([{ collection_id: result.collection_id, revision: result.revision, request: result.request, updated_at: '2025-01-01T00:00:00Z', coverage: result.evidence.coverage, transaction_count: 1 }])
  renderWithProviders(<HistoricalEvidencePanel workspaceId="investment" connectionIds={['another-connection']} initial={{ selectedKey: '', since: '', until: '' }} />)
  await screen.findByText('No saved evidence matches this workspace and wallet filter yet.')
  expect(screen.queryByRole('option', { name: /1 Transactions/ })).not.toBeInTheDocument()
})

it('paginates retained transactions and shows raw-only payloads without invented legs', async () => {
  const transaction = result.evidence.transactions['solana:synthetic-tx']
  const transactions = Object.fromEntries(Array.from({ length: 21 }, (_, index) => [`solana:tx-${index}`, { ...transaction, signature: `synthetic-tx-${index}`, versions: [{ ...transaction.versions[0], legs: [] }] }]))
  api.collectHistory.mockResolvedValue({ ...result, evidence: { ...result.evidence, transactions } })
  const { user } = renderWithProviders(panel())
  await collect(user)
  expect(screen.queryByText('synthetic-tx-20')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Next' }))
  await user.click(screen.getByText('synthetic-tx-20'))
  expect(screen.getByText(/source payload is retained without decoded movements/)).toBeInTheDocument()
  expect(screen.queryByText('synthetic-tx-0')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Previous' }))
  expect(screen.getByText('synthetic-tx-0')).toBeInTheDocument()
})

it('distinguishes a missing payload and retrieval gap from unresolved or non-additive retained legs', async () => {
  const transaction = result.evidence.transactions['solana:synthetic-tx']
  api.collectHistory.mockResolvedValue({ ...result, evidence: { ...result.evidence, transactions: {
    missing: { signature: 'synthetic-missing', canonical_version: null, discovery_refs: ['owner-A'], versions: [], retrieval_gap: 'payload_unavailable' },
    unresolved: { ...transaction, canonical_version: null, retrieval_gap: 'confirmation_unavailable', versions: [{ ...transaction.versions[0], legs: [{ ...transaction.versions[0].legs[0], interpretation: 'unresolved', non_additive: true }] }] },
  } } })
  const { user } = renderWithProviders(panel())
  await collect(user)
  await user.click(screen.getByText('synthetic-missing'))
  expect(screen.getByText(/transaction payload is unavailable/)).toBeInTheDocument()
  expect(screen.queryByText(/source payload is retained without decoded movements/)).not.toBeInTheDocument()
  await user.click(screen.getByText('synthetic-tx'))
  expect(screen.getByText('Transaction retrieval gap: confirmation unavailable')).toBeInTheDocument()
  expect(screen.getByText(/transfer mechanics remain unresolved/)).toBeInTheDocument()
  expect(screen.getByText(/Backing observation, excluded from quantity totals/)).toBeInTheDocument()
  expect(screen.getByText('Atomic units: 9007199254740993 · unresolved')).toBeInTheDocument()
})

it.each(['complete', 'partial'])('distinguishes %s empty retrieval from a filtered empty result', async (retrieval) => {
  api.collectHistory.mockResolvedValue({ ...result, evidence: { ...result.evidence, transactions: {}, coverage: { ...result.evidence.coverage, retrieval } } })
  const { user } = renderWithProviders(panel())
  await collect(user)
  expect(screen.getByText(retrieval === 'complete' ? /No transactions were returned for the declared address streams/ : /No transactions have been retained yet/)).toBeInTheDocument()
})
