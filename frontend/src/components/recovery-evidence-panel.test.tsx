import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { RecoveryEvidencePanel } from './recovery-evidence-panel'
import { renderWithProviders } from '@/test/utils'
import type { EvidenceObservation } from '@/types/investment-evidence'
import type { RecoveryDetails, RecoveryEntryRead, RecoveryPackage, RecoveryReviewRead, RecoveryRole } from '@/types/recovery-evidence'

const api = vi.hoisted(() => ({ wallets: vi.fn(), list: vi.fn(), sources: vi.fn(), preview: vi.fn(), retain: vi.fn(), review: vi.fn(), export: vi.fn(), workspace: vi.fn(), transfers: vi.fn() }))
vi.mock('@/lib/recovery-api', () => ({ recovery: api }))
vi.mock('@/lib/api', () => ({ ownedTransfers: { index: api.transfers }, assetErrorMessage: (error: { message?: string }, fallback: string) => error?.message ?? fallback }))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: api.workspace }))
vi.mock('@/hooks/use-privacy-mode', () => ({ usePrivacyMode: () => ({ mask: (value: string) => value, privacyMode: false }) }))

const emptyDetails: RecoveryDetails = { account_bucket: null, boundary_kind: null, claim_amount: null, claim_currency: null, proceeds: null, proceeds_currency: null, cash_credited: null, cash_currency: null, acquisition_date: null, statement_date: null, reported_cost: null, reported_cost_currency: null, provisional_allocation: null, allocation_currency: null }
function observation(id: string): EvidenceObservation {
  return { reference: id, source: 'synthetic', source_kind: 'recovery_notice', provider: 'Synthetic provider', source_local_id: id, source_locator: `${id}.csv:2`, source_account_id: 'Earn source', historical_workspace_label: 'Old workspace', observed_at: null, event_at: null, event_date: null, event_time_raw: null, time_precision: 'unknown', timezone: null, provider_status: 'completed', network_status: null, settlement_status: 'unknown', order_ref: null, coverage: [], legs: [{ key: 'asset-1', asset_symbol: 'COIN', asset_id: null, chain: null, token_address: null, isin: null, direction: 'in', classification: 'transfer', quantity: '4', unit_price: null, subtotal: null, total: null, fee: null, fee_currency: null, valuation_amount: null, valuation_currency: null, acquisition_basis: null, external_funding_amount: null, external_funding_currency: null, transaction_ref: null, leg_ref: null, execution_id: null }] }
}
function entry(role: RecoveryRole, index: number): RecoveryEntryRead {
  return { id: `entry-${index}`, key: `key-${index}`, observation_id: `source-${index}`, observation: observation(`source-${index}`), source_group_id: 'wallet-a', source_group_name: 'Recovery wallet', leg_key: 'asset-1', case_key: 'Synthetic recovery', round_key: index < 3 ? 'Round one' : 'Round two', round_asset_key: index === 2 ? 'asset-b' : 'asset-a', role, reported_state: index === 0 ? 'confirmed' : index === 2 ? 'conflict' : 'candidate', details: { ...emptyDetails }, missing_evidence: index === 0 ? ['receipt_missing'] : [], reason_codes: [], application: { leg_id: null, application_id: null, status: 'unsupported', reason_codes: ['recovery_evidence_only'] } }
}
function fixture(): RecoveryPackage {
  return { workspace_id: 'workspace-a', group_id: 'wallet-a', revision: 'revision-1', entries: ['allowed_claim', 'receiving_receipt', 'tax_workpaper', 'equity_statement'].map((role, index) => entry(role as RecoveryRole, index)), reviews: [], round_count: 2, asset_record_count: 3, missing_evidence: ['basis_unknown'], allocation_blockers: ['shifted_recovery_reference', 'later_round_valuation_missing'], coverage: ['source_history_partial'] }
}
function reviewFixture(kind: RecoveryReviewRead['kind'] = 'allocation'): RecoveryReviewRead {
  return { id: 'review-1', key: 'review-key', entry_id: 'entry-2', target_entry_id: null, supersedes_id: null, kind, relation_kind: null, relation_state: null, assertion_kind: null, assertion_status: 'modeled', value: '110', currency: 'USD', field: 'Model allocation', proposed_value: null, source_locator: 'synthetic-model:row-8', reason: 'Modeled allocation pending review', supporting_observation_ids: ['source-2'], required_entry_ids: ['entry-3'], required_review_ids: [], missing_evidence: ['later_round_valuation_missing'], conflicting_fields: ['shifted_recovery_reference'], owned_transfer_id: null, account_mapping_evidence: null, timing_evidence: null, quantity_adjustment: null, adjustment_evidence: null, created_at: '2026-01-01T00:00:00Z', created_by: null, is_current: true, blockers: ['later_round_valuation_missing', 'filing_assumption_unverified'], ready_for_review: false }
}
beforeEach(() => {
  vi.clearAllMocks()
  api.workspace.mockReturnValue({ current: { id: 'workspace-a', name: 'Investment' }, canWrite: true })
  api.wallets.mockResolvedValue([{ id: 'wallet-a', name: 'Recovery wallet' }, { id: 'wallet-b', name: 'Receiving platform' }])
  api.list.mockImplementation(async (_workspace, filters) => ({ ...fixture(), group_id: filters.group_id }))
  api.sources.mockImplementation(async (_workspace, group) => ({ target: { workspace_id: 'workspace-a', group_id: group }, observations: [observation(group === 'wallet-b' ? 'external-source' : 'retained-source')] }))
  api.preview.mockImplementation(async (_workspace, _group, entries) => ({ ...fixture(), entries: entries.map((item: RecoveryEntryRead) => ({ ...item, id: null, observation: item.observation ?? observation('external-source'), application: { status: 'unsupported', leg_id: null, application_id: null, reason_codes: [] }, reason_codes: [] })) }))
  api.retain.mockResolvedValue({ ...fixture(), revision: 'revision-2' })
  api.review.mockResolvedValue({ ...fixture(), revision: 'revision-2' })
  api.transfers.mockResolvedValue({ workspace_id: 'workspace-a', transfers: [], movements: [] })
  api.export.mockResolvedValue(new Blob(['synthetic exact export']))
})
async function open() {
  const result = renderWithProviders(<RecoveryEvidencePanel />, { route: '/import?tab=investments&mode=recovery&wallet=wallet-a' })
  await screen.findByText('2 rounds · 3 asset records · 4 evidence entries')
  return result
}
async function manual(role: RecoveryRole = 'recovery_notice') {
  const result = await open()
  await result.user.click(screen.getByText('Add evidence or attach a retained source'))
  await result.user.selectOptions(screen.getByLabelText('Evidence role'), role)
  for (const [label, value] of [['Source provider', 'synthetic'], ['Source reference', 'notice-1'], ['Source document / row locator', 'notice.csv:2'], ['Recovery case', 'Case A']]) await result.user.type(screen.getByLabelText(label), value)
  return result
}
async function openReview(role = 'allowed claim') {
  const result = await open()
  const heading = screen.getByText(role, { selector: 'strong' })
  await result.user.click(heading)
  await result.user.click(within(heading.closest('details')!).getByRole('button', { name: 'Add relationship, correction or model review' }))
  return result
}

describe('recovery evidence user controls', () => {
  it('distinguishes two rounds and three asset records from four observations and keeps unknown facts visible', async () => {
    const { user } = await open()
    await user.click(screen.getByText('allowed claim', { selector: 'strong' }))
    expect(screen.getByText('receipt missing')).toBeInTheDocument()
    expect(screen.getAllByText('Unknown').length).toBeGreaterThan(3)
    expect(screen.getAllByText('Earn source')).toHaveLength(4)
    expect(screen.getAllByText('Old workspace')).toHaveLength(4)
    expect(screen.getByText('shifted recovery reference')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Apply supported activity$/ })).not.toBeInTheDocument()
  })

  it('labels source authenticity separately from quantity application in every row summary', async () => {
    await open()
    const claim = within(screen.getByRole('row', { name: 'allowed claim: source-0' }).querySelector('summary')!)
    expect(claim.getByText('Source state: confirmed')).toBeInTheDocument()
    expect(claim.getByText('Quantity application: Evidence only')).toBeInTheDocument()
    expect(claim.queryByText(/unsupported/)).not.toBeInTheDocument()
    const receipt = within(screen.getByRole('row', { name: 'receiving receipt: source-1' }).querySelector('summary')!)
    expect(receipt.getByText('Source state: candidate')).toBeInTheDocument()
    expect(receipt.getByText('Quantity application: unsupported')).toBeInTheDocument()
  })

  it('separates coverage limitations from actual missing evidence and model blockers', async () => {
    await open()
    const coverage = within(screen.getByRole('region', { name: 'Coverage and scope' }))
    const gaps = within(screen.getByRole('region', { name: 'Evidence gaps and model blockers' }))
    expect(coverage.getByText('source history partial')).toBeInTheDocument()
    expect(coverage.queryByText('basis unknown')).not.toBeInTheDocument()
    expect(gaps.getByText('basis unknown')).toBeInTheDocument()
    expect(gaps.getByText('shifted recovery reference')).toBeInTheDocument()
    expect(gaps.queryByText('source history partial')).not.toBeInTheDocument()
  })

  it('previews a multi-asset round, preserves decimal strings, zero and nulls, and saves only after preview', async () => {
    const { user } = await manual()
    await user.type(screen.getByLabelText('Distribution round reference'), 'Round A')
    await user.type(screen.getByLabelText('Reported quantity'), '4.123456789012345678901')
    await user.type(screen.getByLabelText('Reported valuation'), '0')
    await user.type(screen.getByLabelText('Asset reference within the round'), 'coin-a')
    await user.click(screen.getByRole('button', { name: 'Add asset row' }))
    await user.type(screen.getAllByLabelText('Reported asset / currency')[1], 'COINB')
    await user.type(screen.getAllByLabelText('Asset reference within the round')[1], 'coin-b')
    await user.click(screen.getByRole('button', { name: 'Preview evidence' }))
    await screen.findByText('Preview only: saving these observations adds no financial quantities or basis.')
    const entries = api.preview.mock.calls[0][2]
    expect(entries).toHaveLength(2)
    expect(entries.map((item: RecoveryEntryRead) => item.round_key)).toEqual(['Round A', 'Round A'])
    expect(entries[0].observation.legs[0]).toMatchObject({ quantity: '4.123456789012345678901', valuation_amount: '0', acquisition_basis: null })
    expect(entries[0].observation.legs[1].quantity).toBeNull()
    expect(entries[0].observation).toMatchObject({ event_at: null, event_date: null, observed_at: null, time_precision: 'unknown' })
    expect(api.retain).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Save evidence' }))
    await waitFor(() => expect(api.retain).toHaveBeenCalledWith('workspace-a', 'wallet-a', entries, 'revision-1'))
  })

  it.each([
    ['allowed_claim', 'Claim face amount', '900', 'claim_amount'],
    ['platform_ledger', 'Ledger boundary', 'after withdrawal', 'boundary_kind'],
    ['receiving_receipt', 'Cash credited', '0', 'cash_credited'],
    ['disposition', 'Reported sale proceeds', '50', 'proceeds'],
    ['cash_proceeds', 'Cash credited', '40', 'cash_credited'],
    ['equity_statement', 'Displayed stock cost', '80', 'reported_cost'],
    ['tax_workpaper', 'Modeled allocation', '110', 'provisional_allocation'],
  ] as const)('retains role-specific %s details without choosing basis or treatment', async (role, field, value, key) => {
    const { user } = await manual(role)
    await user.type(screen.getByLabelText(field), value)
    await user.click(screen.getByRole('button', { name: 'Preview evidence' }))
    await waitFor(() => expect(api.preview).toHaveBeenCalled())
    expect(api.preview.mock.calls[0][2][0].details[key]).toBe(value)
    expect(api.preview.mock.calls[0][2][0].observation.event_date).toBeNull()
    expect(api.preview.mock.calls[0][2][0].observation.legs[0].acquisition_basis).toBeNull()
  })

  it('attaches another same-workspace wallet source without copying or relabeling its facts', async () => {
    const { user } = await open()
    await user.click(screen.getByText('Add evidence or attach a retained source'))
    await user.selectOptions(screen.getByLabelText('Source wallet for retained evidence'), 'wallet-b')
    await screen.findByRole('option', { name: /external-source/ })
    await user.selectOptions(screen.getByLabelText('Source to use'), 'external-source')
    await user.type(screen.getByLabelText('Recovery case'), 'Case A')
    await user.click(screen.getByRole('button', { name: 'Preview evidence' }))
    await waitFor(() => expect(api.preview).toHaveBeenCalled())
    expect(api.preview.mock.calls[0][1]).toBe('wallet-a')
    expect(api.preview.mock.calls[0][2][0]).toMatchObject({ observation_id: 'external-source', observation: null, leg_key: 'asset-1' })
    expect(api.sources).toHaveBeenCalledWith('workspace-a', 'wallet-b', expect.any(AbortSignal))
  })

  it('records a candidate relationship with independent source/account/timing evidence and missing transfer', async () => {
    const { user } = await openReview()
    await user.selectOptions(screen.getByLabelText('Related evidence'), 'entry-1')
    await user.type(screen.getByLabelText('Documented source / account compatibility'), 'Source mapping candidate only')
    await user.type(screen.getByLabelText('Documented time / precision compatibility'), 'Date-only source')
    await user.type(screen.getByLabelText('Missing evidence / model defects (separate with semicolons)'), 'intervening transfer; original basis')
    await user.type(screen.getByLabelText('Review source / document reference'), 'synthetic.csv:3')
    await user.type(screen.getByLabelText('Evidence and reason for this review'), 'Equal quantities need supporting identity')
    await user.click(screen.getByRole('button', { name: 'Save separate review' }))
    await waitFor(() => expect(api.review).toHaveBeenCalled())
    expect(api.review.mock.calls[0][2][0]).toMatchObject({ kind: 'relation', relation_state: 'candidate', target_entry_id: 'entry-1', missing_evidence: ['intervening transfer', 'original basis'], owned_transfer_id: null, assertion_status: null })
  })

  it('records a correction separately while original cost, modeled allocation and filing blockers stay visible', async () => {
    const data = fixture(); data.entries[3].details.reported_cost = '80'; data.entries[3].observation.legs[0].quantity = '8'; data.reviews = [reviewFixture()]
    api.list.mockResolvedValue(data)
    const { user } = await openReview('tax workpaper')
    expect(screen.getByText('110')).toBeInTheDocument()
    expect(screen.getByText('filing assumption unverified')).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Review type'), 'correction')
    await user.type(screen.getByLabelText('Source field / reference being corrected'), 'recovery reference')
    await user.type(screen.getByLabelText('Proposed correction (original retained)'), 'Round two')
    await user.type(screen.getByLabelText('Review source / document reference'), 'workpaper:row-3')
    await user.type(screen.getByLabelText('Evidence and reason for this review'), 'Reference shifted')
    await user.click(screen.getByRole('button', { name: 'Save separate review' }))
    await waitFor(() => expect(api.review).toHaveBeenCalled())
    expect(api.review.mock.calls[0][2][0]).toMatchObject({ kind: 'correction', proposed_value: 'Round two', assertion_status: 'unverified' })
    await screen.findByText('Evidence review saved. Holdings, basis and tax treatment are unchanged.')
    expect(screen.getAllByText('later round valuation missing').length).toBeGreaterThan(0)
  })

  it('records a modeled allocation with explicit required inputs and controlling review references', async () => {
    const data = fixture(); data.reviews = [reviewFixture('assertion')]; data.reviews[0].assertion_kind = 'accounting_assumption'
    api.list.mockResolvedValue(data)
    const { user } = await openReview()
    await user.selectOptions(screen.getByLabelText('Review type'), 'allocation')
    await user.type(screen.getByLabelText('Model / allocation reference'), 'Allocation model v2')
    await user.type(screen.getByLabelText('Modeled amount (blank means unknown)'), '110')
    await user.type(screen.getByLabelText('Review source / document reference'), 'model:row-8')
    await user.type(screen.getByLabelText('Evidence and reason for this review'), 'Pending controlling assumption')
    const required = screen.getByRole('group', { name: 'Required source inputs' })
    await user.click(within(required).getAllByRole('checkbox')[3])
    await user.click(within(screen.getByRole('group', { name: 'Required valuation / controlling assumption reviews' })).getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: 'Save separate review' }))
    await waitFor(() => expect(api.review).toHaveBeenCalled())
    expect(api.review.mock.calls[0][2][0]).toMatchObject({ kind: 'allocation', assertion_status: 'modeled', value: '110', required_entry_ids: ['entry-3'], required_review_ids: ['review-1'] })
    expect(screen.queryByRole('button', { name: /finalize/i })).not.toBeInTheDocument()
  })

  it('exports the complete server-filtered scope as original bytes, not the visible page', async () => {
    const data = fixture(); data.entries = Array.from({ length: 28 }, (_, index) => entry('recovery_notice', index))
    api.list.mockResolvedValue(data)
    const { user } = renderWithProviders(<RecoveryEvidencePanel />, { route: '/assets?tab=activity&activity=recovery&wallet=wallet-a&recovery_state=conflict&recovery_q=coin' })
    await screen.findByText('2 rounds · 3 asset records · 28 evidence entries')
    expect(screen.getAllByText('recovery notice', { selector: 'strong' })).toHaveLength(25)
    await user.click(screen.getByRole('button', { name: 'Next' }))
    expect(screen.getAllByText('recovery notice', { selector: 'strong' })).toHaveLength(3)
    const create = vi.fn().mockReturnValue('blob:synthetic'); const revoke = vi.fn()
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke }))
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    await user.click(screen.getByRole('button', { name: 'Export CSV' }))
    await waitFor(() => expect(click).toHaveBeenCalled())
    expect(api.export).toHaveBeenCalledWith('workspace-a', { group_id: 'wallet-a', state: 'conflict', q: 'coin' }, 'revision-1', 'csv')
    expect(create).toHaveBeenCalledWith(await api.export.mock.results[0].value)
    expect(revoke).toHaveBeenCalledWith('blob:synthetic')
    click.mockRestore()
  })

  it('shows supported movement navigation in its original source wallet while off-chain receipt stays unapplied', async () => {
    const data = fixture(); data.entries[1].application = { status: 'unapplied', leg_id: 'canonical-leg', application_id: null, reason_codes: [] }; data.entries[1].source_group_id = 'wallet-b'
    api.list.mockResolvedValue(data)
    const { user } = await open()
    await user.click(screen.getByText('receiving receipt', { selector: 'strong' }))
    expect(screen.getByRole('link', { name: 'Review supported quantity movement' })).toHaveAttribute('href', '/assets?tab=activity&activity=transfers&movement=canonical-leg&wallet=wallet-b')
    expect(api.retain).not.toHaveBeenCalled(); expect(api.review).not.toHaveBeenCalled()
  })

  it('guards viewer reads and source errors and distinguishes empty from filtered-empty', async () => {
    api.workspace.mockReturnValue({ current: { id: 'workspace-a', name: 'Investment' }, canWrite: false })
    api.list.mockResolvedValue({ ...fixture(), entries: [] })
    const { user } = renderWithProviders(<RecoveryEvidencePanel />, { route: '/assets?wallet=wallet-a' })
    await screen.findByText('No recovery evidence is saved for this wallet. Add a source or attach retained evidence to start.')
    expect(screen.queryByText('Add evidence or attach a retained source')).not.toBeInTheDocument()
    await user.type(screen.getByLabelText('Search source, asset or reason'), 'absent')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
    await screen.findByText('No evidence matches these filters. Clear filters to inspect other retained sources.')
    expect(screen.getByRole('button', { name: 'Export JSON' })).toBeEnabled()
  })

  it('drops a late export after workspace unmount and a late preview after destination change', async () => {
    let finishExport!: (blob: Blob) => void
    api.export.mockImplementation(() => new Promise((resolve) => { finishExport = resolve }))
    const create = vi.fn(); vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() }))
    const first = await open()
    await first.user.click(screen.getByRole('button', { name: 'Export JSON' }))
    first.unmount()
    await act(async () => finishExport(new Blob(['old workspace'])))
    expect(create).not.toHaveBeenCalled()
    let finishPreview!: (data: RecoveryPackage) => void
    api.preview.mockImplementation(() => new Promise((resolve) => { finishPreview = resolve }))
    const second = await manual()
    await second.user.click(screen.getByRole('button', { name: 'Preview evidence' }))
    await second.user.selectOptions(screen.getByLabelText('Recovery destination wallet / account'), 'wallet-b')
    await act(async () => finishPreview(fixture()))
    expect(screen.queryByRole('button', { name: 'Save evidence' })).not.toBeInTheDocument()
  })

  it('refreshes after a stale save and removes pending confirmation without automatically retrying', async () => {
    const { user } = await manual()
    await user.click(screen.getByRole('button', { name: 'Preview evidence' }))
    await screen.findByRole('button', { name: 'Save evidence' })
    api.retain.mockRejectedValue({ message: 'Review changed; refresh required', response: { status: 409 } })
    await user.click(screen.getByRole('button', { name: 'Save evidence' }))
    await screen.findByText('Review changed; refresh required')
    expect(screen.queryByRole('button', { name: 'Save evidence' })).not.toBeInTheDocument()
    expect(api.retain).toHaveBeenCalledTimes(1)
    expect(api.list.mock.calls.length).toBeGreaterThan(1)
  })

  it('rejects a mismatched workspace response and keeps rows and actions hidden', async () => {
    api.list.mockResolvedValue({ ...fixture(), workspace_id: 'other-workspace' })
    renderWithProviders(<RecoveryEvidencePanel />, { route: '/assets?wallet=wallet-a' })
    await screen.findByText('The response destination changed. Refresh and review the selected wallet again.')
    expect(screen.queryByRole('button', { name: 'Export JSON' })).not.toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })


  it('preserves date-only source precision and clears the date when the source precision becomes unknown', async () => {
    const { user } = await manual('equity_statement')
    await user.selectOptions(screen.getByLabelText('Source date precision'), 'date')
    fireEvent.change(screen.getByLabelText('Reported date'), { target: { value: '2026-01-02' } })
    await user.type(screen.getByLabelText('Source date / time as written'), '2 January 2026')
    await user.click(screen.getByRole('button', { name: 'Preview evidence' }))
    await waitFor(() => expect(api.preview).toHaveBeenCalledTimes(1))
    expect(api.preview.mock.calls[0][2][0].observation).toMatchObject({ event_date: '2026-01-02', event_at: null, observed_at: null, time_precision: 'date', event_time_raw: '2 January 2026' })
    expect(api.preview.mock.calls[0][2][0].details.statement_date).toBeNull()
    await user.selectOptions(screen.getByLabelText('Source date precision'), 'unknown')
    expect(screen.queryByRole('button', { name: 'Save evidence' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Preview evidence' }))
    await waitFor(() => expect(api.preview).toHaveBeenCalledTimes(2))
    expect(api.preview.mock.calls[1][2][0].observation).toMatchObject({ event_date: null, time_precision: 'unknown', event_time_raw: '2 January 2026' })
  })

  it('records an unverified filing assertion as a separate review without accepting basis', async () => {
    const { user } = await openReview('tax workpaper')
    await user.selectOptions(screen.getByLabelText('Review type'), 'assertion')
    await user.selectOptions(screen.getByLabelText('Assertion meaning'), 'filing_assertion')
    expect(screen.queryByLabelText('Assertion status')).not.toBeInTheDocument()
    expect(screen.getByText('Filing assertion remains unverified. Selecting a workpaper or other source does not establish filed-record support or a tax determination.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /finalize/i })).not.toBeInTheDocument()
    await user.type(screen.getByLabelText('Named field / accounting assumption'), 'Filing method needs filed records')
    await user.type(screen.getByLabelText('Review source / document reference'), 'workpaper:note-2')
    await user.type(screen.getByLabelText('Evidence and reason for this review'), 'Model does not establish the filed treatment')
    await user.click(screen.getByRole('button', { name: 'Save separate review' }))
    await waitFor(() => expect(api.review).toHaveBeenCalled())
    expect(api.review.mock.calls[0][2][0]).toMatchObject({ kind: 'assertion', assertion_kind: 'filing_assertion', assertion_status: 'unverified', value: null })
  })

  it('offers a same-workspace cross-account relation target through an explicit wallet selector', async () => {
    const other = entry('disposition', 8); other.id = 'other-entry'; other.source_group_id = 'wallet-b'; other.source_group_name = 'Receiving platform'
    api.list.mockImplementation(async (_workspace, filters) => ({ ...fixture(), group_id: filters.group_id, entries: filters.group_id === 'wallet-b' ? [other] : fixture().entries }))
    const { user } = await openReview('receiving receipt')
    await user.selectOptions(screen.getByLabelText('Wallet containing related evidence / model inputs'), 'wallet-b')
    await screen.findByRole('option', { name: /Receiving platform · disposition/ })
    await user.selectOptions(screen.getByLabelText('Relationship'), 'receipt_disposition')
    await user.selectOptions(screen.getByLabelText('Related evidence'), 'other-entry')
    await user.type(screen.getByLabelText('Review source / document reference'), 'receiving-source:row-8')
    await user.type(screen.getByLabelText('Evidence and reason for this review'), 'Candidate only; transfer is absent')
    await user.click(screen.getByRole('button', { name: 'Save separate review' }))
    await waitFor(() => expect(api.review).toHaveBeenCalled())
    expect(api.review.mock.calls[0][2][0]).toMatchObject({ target_entry_id: 'other-entry', relation_state: 'candidate', owned_transfer_id: null })
  })

  it('excludes related context from filtered entry counts but keeps its original conflicts inspectable', async () => {
    const data = fixture(); const context = entry('disposition', 9); context.reason_codes = ['related_context', 'account_mapping_conflict']; data.entries.push(context)
    api.list.mockResolvedValue(data)
    const { user } = await open()
    expect(screen.getByText('2 rounds · 3 asset records · 4 evidence entries')).toBeInTheDocument()
    await user.click(screen.getByText('Related evidence outside the selected filters', { exact: false, selector: 'summary' }))
    expect(screen.getByText('account mapping conflict')).toBeInTheDocument()
  })


  it('shows actionable source validation errors while retaining the entered source for correction', async () => {
    const { user } = await manual()
    await user.type(screen.getByLabelText('Reported quantity'), 'invalid')
    api.preview.mockRejectedValue({ response: { status: 422, data: { detail: [{ loc: ['body', 'entries', 0, 'observation', 'legs', 0, 'quantity'], msg: 'Financial amounts must be valid decimal strings' }] } } })
    await user.click(screen.getByRole('button', { name: 'Preview evidence' }))
    await screen.findByText('quantity: Financial amounts must be valid decimal strings')
    expect(screen.getByLabelText('Reported quantity')).toHaveValue('invalid')
    expect(screen.queryByRole('button', { name: 'Save evidence' })).not.toBeInTheDocument()
  })

  it('suppresses a pending export as soon as filter controls change', async () => {
    let finish!: (blob: Blob) => void
    api.export.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    const create = vi.fn(); vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() }))
    const { user } = await open()
    await user.click(screen.getByRole('button', { name: 'Export JSON' }))
    fireEvent.change(screen.getByLabelText('Filter round'), { target: { value: 'Round two' } })
    await act(async () => finish(new Blob(['stale filtered export'])))
    expect(create).not.toHaveBeenCalled()
  })
})
