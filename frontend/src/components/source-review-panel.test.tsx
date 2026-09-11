import { act, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { SourceReviewPanel } from './source-review-panel'
import { createTestQueryClient, renderWithProviders } from '@/test/utils'
import type { EvidencePreview } from '@/types/investment-evidence'
import type { SourceReviewPackage, SourceReviewPreview, SourceReviewRequest } from '@/lib/source-review-api'

const mocks = vi.hoisted(() => ({ list: vi.fn(), preview: vi.fn(), confirm: vi.fn(), export: vi.fn(), workspace: vi.fn(), privacy: vi.fn() }))
vi.mock('@/lib/source-review-api', () => ({ sourceReviews: mocks }))
vi.mock('@/lib/api', () => ({ assetErrorMessage: (error: { response?: { data?: { detail?: string } } }, fallback: string) => error.response?.data?.detail ?? fallback }))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: mocks.workspace }))
vi.mock('@/hooks/use-privacy-mode', () => ({ usePrivacyMode: mocks.privacy }))

const target = { workspace_id: 'workspace-A', workspace_name: 'Synthetic Investment', group_id: 'wallet-A', group_name: 'Synthetic wallet', account_id: null }
const evidence = { revision: 'evidence-1', target, observations: [], records: [], reconciliation: [] } as EvidencePreview
const associationRequest: SourceReviewRequest = { group_id: 'wallet-A', request_key: 'association', action: 'associate', reason: 'Synthetic source documents inclusive total', source_leg_id: 'source-A', target_leg_id: 'source-B', same_execution_reviewed: true, source_semantics: { clock_role: 'posted', amount_field: 'total', amount_meaning: 'fee_inclusive_total' }, target_semantics: { clock_role: 'execution', amount_field: 'unit_price', amount_meaning: 'valuation' } }
const image = { id: 'transaction-A', quantity: '12.000000000000000000', price: '7.000000000000000000', fee: '0.00', date: '2025-02-03', source: 'coinbase' }
function packageFixture(): SourceReviewPackage {
  return { revision: 'review-1', target, legs: [{ leg_id: 'source-A', observation_ref: 'csv-source', leg_key: 'amount', transaction_id: null }, { leg_id: 'source-B', observation_ref: 'api-source', leg_key: 'amount', transaction_id: 'transaction-A' }], reviews: [{ id: 'association-A', request_key: 'association', supersedes_id: null, created_at: '2026-09-10T12:00:00Z', created_by: 'synthetic-reviewer', payload: { request: associationRequest, sources: [], effects: { ledger_rows_added: 0, units_delta: '0', cost_delta: '0' } } }] }
}
function previewFixture(request: SourceReviewRequest): SourceReviewPreview {
  return { revision: 'review-1', preview_digest: 'digest-1', target, request, supported: true, blockers: [], sources: [], effects: { ledger_rows_added: 0, units_delta: '0', cost_delta: '36', before: image, after: { ...image, price: '10.000000000000000000' }, cost_before: '84', cost_after: '120', original_source_fee: null, fee_treatment: 'included_no_additional_fee' } }
}
beforeEach(() => {
  vi.clearAllMocks()
  mocks.workspace.mockReturnValue({ canWrite: true })
  mocks.privacy.mockReturnValue({ privacyMode: false })
  mocks.list.mockResolvedValue(packageFixture())
  mocks.preview.mockImplementation(async (_workspace, request: SourceReviewRequest) => previewFixture(request))
  mocks.confirm.mockResolvedValue({ ...packageFixture(), revision: 'review-2' })
})
async function open() {
  const rendered = renderWithProviders(<SourceReviewPanel workspaceId="workspace-A" groupId="wallet-A" evidence={evidence} disabled={false} />)
  await rendered.user.click(screen.getByText('Source meanings and acquisition corrections'))
  await rendered.user.click(await screen.findByText(/associate · 2026-09-10/))
  return rendered
}
describe('source correction review', () => {
  it.each(['correct', 'reverse'] as const)('invalidates actual lot, value and timeline detail caches after %s', async action => {
    const client = createTestQueryClient()
    client.setDefaultOptions({ queries: { retry: false, gcTime: Infinity, staleTime: Infinity } })
    const keys = [['asset-tax-lots', 'asset-A'], ['asset-tax-lots', 'asset-A', 'workspace-A'],
      ['investment-timeline', 'workspace-A'], ['investment-timeline-event', 'workspace-A', 'event-A'],
      ['investment-timeline-source', 'workspace-A', 'source-A'], ['asset-values', 'asset-A'], ['asset-trend', 'asset-A'], ['portfolio-trend']]
    for (const key of [...keys, ['transactions']]) client.setQueryData(key, { retained: true })
    const data = packageFixture()
    if (action === 'reverse') data.reviews[0].payload.request = { ...associationRequest, action: 'correct' }
    mocks.list.mockResolvedValue(data)
    const { user } = renderWithProviders(<SourceReviewPanel workspaceId="workspace-A" groupId="wallet-A" evidence={evidence} disabled={false} />, { queryClient: client })
    await user.click(screen.getByText('Source meanings and acquisition corrections'))
    await user.click(await screen.findByText(new RegExp(`${action === 'correct' ? 'associate' : 'correct'} · 2026-09-10`)))
    await user.click(screen.getByRole('button', { name: action === 'correct' ? 'Preview acquisition correction' : 'Preview correction reversal' }))
    await user.click(await screen.findByLabelText('I reviewed the source meanings, destination and exact effects above.'))
    await user.click(screen.getByRole('button', { name: action === 'correct' ? 'Apply acquisition correction' : 'Apply correction reversal' }))
    await waitFor(() => expect(client.getQueryState(keys[0])?.isInvalidated).toBe(true))
    for (const key of keys) expect(client.getQueryState(key)?.isInvalidated).toBe(true)
    expect(client.getQueryState(['transactions'])?.isInvalidated).toBe(false)
  })
  it('previews exact effects, retains unknown fee, cancels, then explicitly applies', async () => {
    const { user } = await open()
    await user.click(screen.getByRole('button', { name: 'Preview acquisition correction' }))
    const preview = await screen.findByRole('region', { name: 'Source review preview' })
    expect(within(preview).getByText('10.000000000000000000')).toBeInTheDocument()
    expect(within(preview).getByText(/Original source fee.*Unknown/)).toBeInTheDocument()
    expect(within(preview).getByRole('button', { name: 'Apply acquisition correction' })).toBeDisabled()
    await user.click(within(preview).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('region', { name: 'Source review preview' })).not.toBeInTheDocument()
    expect(mocks.confirm).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Preview acquisition correction' }))
    await user.click(await screen.findByLabelText('I reviewed the source meanings, destination and exact effects above.'))
    await user.click(screen.getByRole('button', { name: 'Apply acquisition correction' }))
    await waitFor(() => expect(mocks.confirm).toHaveBeenCalledTimes(1))
    expect(mocks.confirm).toHaveBeenCalledWith('workspace-A', expect.objectContaining({ revision: 'review-1', preview_digest: 'digest-1' }))
  })

  it('clears a financial preview when association inputs change and blocks unsupported previews', async () => {
    const { user } = await open()
    await user.click(screen.getByRole('button', { name: 'Preview acquisition correction' }))
    await screen.findByRole('region', { name: 'Source review preview' })
    await user.type(screen.getByLabelText('Source evidence and reason'), 'A new source interpretation')
    expect(screen.queryByRole('region', { name: 'Source review preview' })).not.toBeInTheDocument()
    mocks.preview.mockImplementation(async (_workspace, request) => ({ ...previewFixture(request), supported: false, blockers: ['dependent_disposal_or_movement'] }))
    await user.click(screen.getByRole('button', { name: 'Preview acquisition correction' }))
    expect(await screen.findByText('dependent disposal or movement')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Apply acquisition correction' })).toBeDisabled()
  })

  it('revalidates after stale rejection and never applies a stale preview twice', async () => {
    mocks.confirm.mockRejectedValue({ response: { data: { detail: 'Source review changed; preview again before applying' } } })
    const { user } = await open()
    await user.click(screen.getByRole('button', { name: 'Preview acquisition correction' }))
    await user.click(await screen.findByLabelText('I reviewed the source meanings, destination and exact effects above.'))
    await user.click(screen.getByRole('button', { name: 'Apply acquisition correction' }))
    expect(await screen.findByText('Source review changed; preview again before applying')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Source review preview' })).not.toBeInTheDocument()
    expect(mocks.confirm).toHaveBeenCalledTimes(1)
  })

  it('hides review inputs for viewers and clears pending results on role or privacy changes', async () => {
    let resolve: (value: SourceReviewPreview) => void = () => {}
    mocks.preview.mockImplementation((_workspace, request) => new Promise<SourceReviewPreview>(done => { resolve = value => done({ ...value, request }) }))
    const rendered = await open()
    await rendered.user.click(screen.getByRole('button', { name: 'Preview acquisition correction' }))
    mocks.workspace.mockReturnValue({ canWrite: false })
    rendered.rerender(<SourceReviewPanel workspaceId="workspace-A" groupId="wallet-A" evidence={evidence} disabled={false} />)
    await act(async () => resolve(previewFixture({ ...associationRequest, action: 'correct' })))
    expect(screen.queryByRole('region', { name: 'Source review preview' })).not.toBeInTheDocument()
    expect(screen.queryByText('Document a sourced association')).not.toBeInTheDocument()
    mocks.privacy.mockReturnValue({ privacyMode: true })
    rendered.rerender(<SourceReviewPanel workspaceId="workspace-A" groupId="wallet-A" evidence={evidence} disabled={false} />)
    expect(screen.queryByText('Source meanings and acquisition corrections')).not.toBeInTheDocument()
    expect(mocks.confirm).not.toHaveBeenCalled()
  })
})
