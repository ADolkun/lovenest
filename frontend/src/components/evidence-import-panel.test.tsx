import { act, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { EvidenceImportPanel } from './evidence-import-panel'
import { renderWithProviders } from '@/test/utils'
import type { EvidenceObservation, EvidencePreview, EvidenceRecord } from '@/types/investment-evidence'

const api = vi.hoisted(() => ({ evidence: vi.fn(), previewImport: vi.fn(), importEvidence: vi.fn(), confirmEvidence: vi.fn(), unlinkEvidence: vi.fn(), list: vi.fn(), workspace: vi.fn() }))
vi.mock('@/lib/api', () => ({ assets: api, assetGroups: { list: api.list }, assetErrorMessage: (error: { response?: { data?: { detail?: string } } }, fallback: string) => error.response?.data?.detail ?? fallback }))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: api.workspace }))

function fixture(): EvidencePreview {
  const observations = ['linked', 'candidate', 'conflicting', 'unmatched'].map((status, index): EvidenceObservation => ({
    reference: `observation-${index}`, source: 'csv', source_kind: 'primary_activity', provider: 'synthetic', source_account_id: 'source-account-A', source_local_id: `row-${status}`, source_locator: `synthetic.csv:${index + 2}`, observed_at: '2026-01-10T12:00:00Z', event_time_raw: '2026-01-02', event_date: '2026-01-02', event_at: null, timezone: null, time_precision: 'date', provider_status: 'completed', network_status: 'unconfirmed', settlement_status: 'unknown', order_ref: null, historical_workspace_label: 'Historical label', coverage: ['history_partial'],
    legs: [{ key: 'purchase', asset_symbol: `COIN${index}`, asset_id: null, chain: null, token_address: null, isin: null, direction: 'in', classification: 'buy', quantity: '12.123456789012345678', unit_price: '7', subtotal: '82', total: '84', fee: '3', fee_currency: 'USD', valuation_currency: 'USD', valuation_amount: '84', external_funding_amount: null, external_funding_currency: null, acquisition_basis: null, transaction_ref: null, leg_ref: null, execution_id: null }],
  }))
  const records = observations.map((observation, index): EvidenceRecord => ({
    observation_ref: observation.reference, leg_key: 'purchase', match_status: ['linked', 'candidate', 'conflicting', 'unmatched'][index] as EvidenceRecord['match_status'], application_status: ['already_applied', 'blocked', 'blocked', 'eligible'][index] as EvidenceRecord['application_status'],
    source_refs: [{ observation_ref: observation.reference, source: observation.source, source_local_id: observation.source_local_id, source_locator: observation.source_locator, leg_key: 'purchase' }], link_ids: index === 0 ? ['link-a'] : [],
    candidate_legs: index === 1 ? ['leg-a', 'leg-b'].map((leg_id) => ({ leg_id, event_id: 'event-a', asset_symbol: 'COIN1', direction: 'in', classification: 'buy', quantity: '6', event_date: '2026-01-02', source_refs: [{ observation_ref: `api-${leg_id}`, source: 'api', source_local_id: `api-${leg_id}`, source_locator: 'synthetic API', leg_key: 'buy' }] })) : [],
    reason_codes: index === 2 ? ['total_disagrees'] : [], conflicting_fields: index === 2 ? ['subtotal', 'fee', 'total'] : [], effects: { ledger_rows: index === 3 ? 1 : 0, units_delta: index === 3 ? '12.123456789012345678' : '0', basis_delta: null },
  }))
  return { revision: 'revision-1', target: { workspace_id: 'investment', workspace_name: 'Investment', group_id: 'wallet-a', group_name: 'Synthetic exchange', account_id: null }, observations, records, reconciliation: [{ asset_symbol: 'COIN3', opening_quantity: null, opening_assumption: 'unknown', opening_as_of: null, snapshot_quantity: '12.123456789012345678', snapshot_as_of: '2026-01-03', settled_movement_quantity: '12.123456789012345678', missing_coverage: ['history_partial'], unresolved_fee_semantics: true, unresolved_funding_semantics: true, basis_complete: false, history_complete: false }] }
}

beforeEach(() => {
  vi.clearAllMocks()
  api.workspace.mockReturnValue({ current: { id: 'investment', name: 'Investment' }, canWrite: true })
  api.list.mockResolvedValue([{ id: 'wallet-a', name: 'Synthetic exchange', source: 'manual', connection_id: null }, { id: 'wallet-b', name: 'Empty wallet', source: 'manual', connection_id: null }])
  api.evidence.mockImplementation(async (groupId: string) => groupId === 'wallet-a' ? fixture() : { ...fixture(), target: { ...fixture().target, group_id: groupId, group_name: 'Empty wallet' }, observations: [], records: [], reconciliation: [] })
  api.previewImport.mockResolvedValue({ orders: [], errors: [], skips: [], warnings: [], csv_columns: ['Asset', 'Amount'], parse_error: null, evidence: fixture() })
  for (const method of [api.importEvidence, api.confirmEvidence]) method.mockResolvedValue({ imported: 0, retained: 4, linked: 1, evidence: { ...fixture(), revision: 'revision-2' } })
  api.unlinkEvidence.mockResolvedValue({ ...fixture(), revision: 'revision-2' })
})

async function openReview(mode: 'evidence' | 'opening_lots' = 'evidence') {
  const rendered = renderWithProviders(<EvidenceImportPanel mode={mode} />)
  await screen.findByRole('option', { name: 'Synthetic exchange' })
  await rendered.user.selectOptions(screen.getByLabelText('Destination wallet / account'), 'wallet-a')
  await screen.findByText('row-linked')
  return rendered
}

describe('investment evidence review', () => {
  it('loads retained observations without upload and filters all four match states', async () => {
    const { user } = await openReview()
    expect(api.evidence).toHaveBeenCalledWith('wallet-a', undefined)
    for (const status of ['linked', 'candidate', 'conflicting', 'unmatched']) {
      await user.selectOptions(screen.getByLabelText('Match status'), status)
      expect(screen.getByText(`row-${status}`)).toBeInTheDocument()
      expect(screen.queryByText(`row-${status === 'linked' ? 'candidate' : 'linked'}`)).not.toBeInTheDocument()
    }
    await user.type(screen.getByLabelText('Search source IDs, assets or reasons'), 'no-such-source')
    expect(screen.getByText('No observations match these filters.')).toBeInTheDocument()
    expect(screen.getByText(/Acquisition basis incomplete \/ unknown/)).toBeInTheDocument()
    expect(screen.getByText('Original funding unresolved')).toBeInTheDocument()
    expect(api.confirmEvidence).not.toHaveBeenCalled()
  })

  it('preserves exact values, unknown basis and separate provider/network status', async () => {
    const { user } = await openReview()
    await user.click(screen.getByText('row-conflicting'))
    const detail = screen.getByText('row-conflicting').closest('details')!
    expect(within(detail).getByText('12.123456789012345678')).toBeInTheDocument()
    expect(within(detail).getByText(/Provider status.*completed.*Network status.*unconfirmed/)).toBeInTheDocument()
    expect(within(detail).getByText(/Precision.*date.*Timezone.*Unknown/)).toBeInTheDocument()
    expect(within(detail).getByText('acquisition basis').nextElementSibling).toHaveTextContent('Unknown')
    expect(within(detail).getByText('total disagrees')).toBeInTheDocument()
    expect(within(detail).queryByRole('button', { name: 'Apply supported activity' })).not.toBeInTheDocument()
  })

  it('confirms reviewed one-to-many allocations without applying a financial row', async () => {
    const { user } = await openReview()
    await user.click(screen.getByText('row-candidate'))
    const detail = within(screen.getByText('row-candidate').closest('details')!)
    await user.click(detail.getByLabelText(/api-leg-a/))
    await user.click(detail.getByLabelText(/api-leg-b/))
    const quantities = detail.getAllByLabelText('Supported quantity (blank = full leg)')
    await user.type(quantities[0], '6.123456789012345678')
    await user.type(quantities[1], '6')
    await user.type(detail.getByLabelText('Evidence supporting this link'), 'Documented multi-fill order')
    await user.click(detail.getByRole('button', { name: 'Confirm source link' }))
    await waitFor(() => expect(api.confirmEvidence).toHaveBeenCalledWith({ group_id: 'wallet-a', expected_revision: 'revision-1', opening_boundary: undefined, allow_unpriced: false, decisions: [{ observation_ref: 'observation-1', leg_key: 'purchase', action: 'link', allocations: [{ leg_id: 'leg-a', quantity: '6.123456789012345678' }, { leg_id: 'leg-b', quantity: '6' }], reason: 'Documented multi-fill order' }] }))
    expect(api.importEvidence).not.toHaveBeenCalled()
  })

  it('requires reviewing the application effect and confirming unknown settlement', async () => {
    const { user } = await openReview()
    await user.click(screen.getByText('row-unmatched'))
    const detail = within(screen.getByText('row-unmatched').closest('details')!)
    const apply = detail.getByRole('button', { name: 'Apply supported activity' })
    expect(apply).toBeDisabled()
    await user.click(detail.getByLabelText('I reviewed the source, destination and financial effect above.'))
    expect(apply).toBeDisabled()
    await user.click(detail.getByLabelText('I verified this activity settled. The source did not report settlement status.'))
    await user.click(apply)
    await waitFor(() => expect(api.confirmEvidence).toHaveBeenCalledWith(expect.objectContaining({ decisions: [{ observation_ref: 'observation-3', leg_key: 'purchase', action: 'apply', settlement_confirmed: true }] })))
  })

  it('saves observations separately and requires an explicit backfill boundary', async () => {
    const { user } = await openReview('opening_lots')
    await user.upload(screen.getByLabelText('CSV source file'), new File(['Asset,Amount\nCOIN,12'], 'synthetic.csv', { type: 'text/csv' }))
    expect(screen.getByRole('button', { name: 'Preview source' })).toBeDisabled()
    await user.type(screen.getByLabelText('Opening history as of'), '2026-01-01')
    await user.type(screen.getByLabelText('Opening history assumption'), 'Inventory before supplied history')
    await user.click(screen.getByLabelText(/I reviewed the opening history and overlap/))
    await user.click(screen.getByRole('button', { name: 'Preview source' }))
    const save = await screen.findByRole('button', { name: 'Save observations' })
    await user.click(save)
    await waitFor(() => expect(api.importEvidence).toHaveBeenCalledWith(expect.objectContaining({ mode: 'opening_lots', decisions: [], expected_revision: 'revision-1', opening_boundary: { as_of: '2026-01-01', assumption: 'Inventory before supplied history', overlap_reviewed: true } })))
    expect(api.confirmEvidence).not.toHaveBeenCalled()
  })

  it('discards a late preview after choosing another wallet', async () => {
    let resolvePreview!: (value: unknown) => void
    api.previewImport.mockImplementation(() => new Promise((resolve) => { resolvePreview = resolve }))
    const { user } = await openReview()
    await user.upload(screen.getByLabelText('CSV source file'), new File(['Asset,Amount'], 'synthetic.csv', { type: 'text/csv' }))
    await user.click(screen.getByRole('button', { name: 'Preview source' }))
    await user.selectOptions(screen.getByLabelText('Destination wallet / account'), 'wallet-b')
    await screen.findByText('No source observations in this wallet. Preview an exchange or brokerage CSV to start.')
    await act(async () => { resolvePreview({ evidence: fixture(), csv_columns: [], errors: [] }) })
    expect(screen.queryByText('row-linked')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save observations' })).not.toBeInTheDocument()
    expect(api.importEvidence).not.toHaveBeenCalled()
  })

  it('refreshes a stale review and removes pending confirmations after 409', async () => {
    api.unlinkEvidence.mockRejectedValue({ response: { status: 409, data: { detail: 'Review changed. Refresh before confirming.' } } })
    const { user } = await openReview()
    api.evidence.mockResolvedValue({ ...fixture(), revision: 'revision-2' })
    await user.click(screen.getByText('row-linked'))
    await user.click(screen.getByRole('button', { name: 'Unlink source' }))
    await user.click(screen.getByRole('button', { name: 'Confirm unlink; retain activity' }))
    expect(await screen.findByText('Review changed. Refresh before confirming.')).toBeInTheDocument()
    await waitFor(() => expect(api.evidence).toHaveBeenCalledTimes(2))
    expect(screen.queryByRole('button', { name: 'Confirm unlink; retain activity' })).not.toBeInTheDocument()
    expect(api.unlinkEvidence).toHaveBeenCalledWith('link-a', 'revision-1', undefined)
  })

  it('shows fetch errors as retryable errors and keeps viewer actions disabled', async () => {
    api.workspace.mockReturnValue({ current: { id: 'investment', name: 'Investment' }, canWrite: false })
    api.evidence.mockRejectedValueOnce({ response: { data: { detail: 'Source temporarily unavailable' } } })
    const { user } = renderWithProviders(<EvidenceImportPanel />)
    await screen.findByRole('option', { name: 'Synthetic exchange' })
    await user.selectOptions(screen.getByLabelText('Destination wallet / account'), 'wallet-a')
    expect(await screen.findByText('Source temporarily unavailable')).toBeInTheDocument()
    expect(screen.queryByText(/No source observations/)).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await user.click(await screen.findByText('row-unmatched'))
    expect(screen.getByRole('button', { name: 'Apply supported activity' })).toBeDisabled()
    expect(screen.queryByLabelText('CSV source file')).not.toBeInTheDocument()
  })

  it('uses the same reviewed opening boundary when previewing and unlinking', async () => {
    const { user } = await openReview('opening_lots')
    await user.type(screen.getByLabelText('Opening history as of'), '2026-01-01')
    await user.type(screen.getByLabelText('Opening history assumption'), 'Reviewed opening inventory')
    await user.click(screen.getByLabelText(/I reviewed the opening history and overlap/))
    const boundary = { as_of: '2026-01-01', assumption: 'Reviewed opening inventory', overlap_reviewed: true }
    await waitFor(() => expect(api.evidence).toHaveBeenCalledWith('wallet-a', boundary))
    await user.click(screen.getByText('row-linked'))
    await user.click(screen.getByRole('button', { name: 'Unlink source' }))
    await user.click(screen.getByRole('button', { name: 'Confirm unlink; retain activity' }))
    await waitFor(() => expect(api.unlinkEvidence).toHaveBeenCalledWith('link-a', 'revision-1', boundary))
  })
})
