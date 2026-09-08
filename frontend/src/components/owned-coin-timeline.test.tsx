import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { useLocation, useNavigate } from 'react-router-dom'
import { OwnedCoinTimeline } from './owned-coin-timeline'
import { timeline } from '@/lib/timeline-api'
import { onchain } from '@/lib/api'
import { renderWithProviders } from '@/test/utils'
import type { TimelineAsset, TimelineCoverage, TimelineEvent, TimelineLeg, TimelineRead, TimelineSource, TimelineSourceDetail, TimelineTime } from '@/types/timeline'

vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: () => ({ canWrite: true }) }))

vi.mock('@/contexts/auth-context', () => ({ useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }) }))

const day: TimelineTime = { event_date: '2025-02-03', event_at: null, event_time_raw: '2025-02-03', timezone: null, time_precision: 'date', ordering: 'within_day_unknown' }
const asset: TimelineAsset = { canonical_asset_key: 'chain-a:program-a:token-x', asset_symbol: 'TOKEN-X', chain: 'chain-a', token_address: 'token-x', token_program: 'program-a', identity_status: 'canonical', asset_ids: ['archived-holding'] }
const sibling = { ...asset, canonical_asset_key: 'chain-b:program-b:token-x', chain: 'chain-b', token_program: 'program-b', asset_ids: [] }
const account = { group_id: 'wallet-a', group_name: 'Exchange A', account_id: null, connection_id: 'connection-a' }
const coverage: TimelineCoverage = { coverage_id: 'coverage-a', source: 'Exchange A export', group_id: 'wallet-a', connection_id: 'connection-a', collection_id: null, chain: null, requested: { since: '2025-01-01', time_precision: 'date' }, observed: { until: '2025-02-03' }, last_successful_collection: '2025-03-01T01:02:03Z', inventory: 'unknown', retrieval: 'complete', interpretation: 'partial', settlement: 'unknown', gaps: ['opening_quantity_unknown', 'missing_token_account_history'], streams: {}, source_url: null, history_complete: false }
const source: TimelineSource = { source_id: 'source-a', source: 'Exchange A export', provider: 'synthetic', source_kind: 'primary_activity', source_local_id: 'row-a', source_locator: 'synthetic.csv:row-a', source_reference: 'original-a', observed_at: '2025-03-01T01:02:03Z', time: day, original_type: 'withdrawal', provider_status: 'completed', network_status: 'pending', settlement_status: 'unknown', is_current: true, availability: 'available', unavailable_reason: null, detail_url: '/api/assets/timeline/sources/source-a', collection_id: null, payload_digest: null, decoder_version: null, account }
const receipt: TimelineSource = { ...source, source_id: 'source-b', source: 'Wallet B chain', original_type: 'receipt', provider_status: null, network_status: 'finalized', settlement_status: 'settled', time: { ...day, event_time_raw: '2025-02-03T12:34:56.123456Z', event_at: '2025-02-03T12:34:56.123456Z', time_precision: 'fractional', ordering: 'exact', timezone: 'UTC' } }
const leg: TimelineLeg = { key: 'principal', leg_id: 'leg-a', canonical_asset_key: asset.canonical_asset_key, group_id: 'wallet-a', asset_symbol: 'TOKEN-X', asset_id: 'archived-holding', chain: 'chain-a', token_address: 'token-x', token_program: 'program-a', isin: null, direction: 'out', classification: 'transfer', quantity: '9007199254740993.123456789012345678', unit_price: null, subtotal: null, total: null, fee: null, fee_currency: null, valuation_currency: null, valuation_amount: null, external_funding_amount: null, external_funding_currency: null, acquisition_basis: null, transaction_ref: 'synthetic-ref', leg_ref: 'principal-a', execution_id: null, source_ids: ['source-a', 'source-b'], settlement_status: 'settled', execution_status: 'success', interpretation: 'supported', non_additive: false, is_current: true, reason_codes: [] }
const fee = { ...leg, key: 'fee', leg_id: 'fee-a', classification: 'fee', quantity: '0.000000000000000001', asset_symbol: 'FEE', canonical_asset_key: 'chain-a:native', fee: '0', fee_currency: 'FEE' }
function event(overrides: Partial<TimelineEvent> = {}): TimelineEvent {
  return { event_id: 'event-a', native_trace_url: '/assets?tab=activity&activity=wallets&wallet=wallet-a&chain=chain-a&address=synthetic-owned&since=2025-02-03T00%3A00%3A00Z', kind: 'transfer', status: 'settled', linkage: 'confirmed', time: day, accounts: [account], assets: [asset], legs: [leg, fee], sources: [source, receipt], relationships: [], basis: { state: 'unknown', acquisition_cost: null, known_acquisition_cost: null, unknown_basis_quantity: null, reason_codes: ['acquisition_evidence_unknown'] }, tax_treatment: 'unresolved', reason_codes: [], conflicting_fields: [], coverage: [coverage], transfers: [], recovery: [], incidents: [], ...overrides }
}
function list(overrides: Partial<TimelineRead> = {}): TimelineRead {
  return { workspace_id: 'workspace-a', revision: 'revision-a', events: [event()], assets: [asset, sibling], coverage: [coverage], total: 1, limit: 25, offset: 0, has_more: false, all_available_records_loaded: true, history_complete: false, errors: [], ...overrides }
}
function sourceDetail(): TimelineSourceDetail {
  return { workspace_id: 'workspace-a', source, observation: { quantity: leg.quantity, fee: null }, raw_payload: { encoding: 'json', json: '{"raw_units":18446744073709551615}' }, transaction: null, coverage: [coverage] }
}
function View({ workspaceId = 'workspace-a', groupId = 'wallet-a', collectionId = 'collection-a' }: { workspaceId?: string; groupId?: string | null; collectionId?: string | null }) {
  const navigate = useNavigate()
  const location = useLocation()
  return <><button onClick={() => navigate(-1)}>Browser Back</button><output aria-label="Location">{location.search}</output><OwnedCoinTimeline workspaceId={workspaceId} groupId={groupId} collectionId={collectionId} wallets={[]} nativeTraceEnabled /></>
}
beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.removeItem('privacyMode')
  vi.spyOn(timeline, 'list').mockResolvedValue(list())
  vi.spyOn(timeline, 'event').mockResolvedValue(event())
  vi.spyOn(timeline, 'source').mockResolvedValue(sourceDetail())
})

it('shows a qualified event once, preserves all source times, fees and unknown acquisition values', async () => {
  const { user } = renderWithProviders(<View />, { route: '/assets?tab=activity&activity=timeline' })
  const row = await screen.findByRole('button', { name: /transfer · settled/ })
  expect(screen.getAllByRole('button', { name: /transfer · settled/ })).toHaveLength(1)
  expect(row).toHaveTextContent('9007199254740993.123456789012345678')
  expect(row).toHaveTextContent('0.000000000000000001')
  expect(row).toHaveTextContent('Order within day unknown')
  expect(row).not.toHaveTextContent('00:00:00')
  await user.click(row)
  const dialog = within(await screen.findByRole('dialog', { name: 'Event evidence' }))
  expect(await dialog.findByRole('region', { name: 'Movement and fee legs' })).toHaveTextContent('0.000000000000000001')
  expect(dialog.getByRole('region', { name: 'Supporting sources' })).toHaveTextContent('withdrawal')
  expect(dialog.getByRole('region', { name: 'Supporting sources' })).toHaveTextContent('receipt')
  expect(dialog.getByRole('region', { name: 'Supporting sources' })).toHaveTextContent('2025-02-03T12:34:56.123456Z')
  expect(dialog.getByRole('region', { name: 'Acquisition evidence' })).toHaveTextContent('Unknown')
  expect(dialog.queryByText('$0.00')).not.toBeInTheDocument()
  expect(dialog.queryByRole('button', { name: /Edit|Buy|Sell/ })).not.toBeInTheDocument()
  expect(timeline.event).toHaveBeenCalledWith('workspace-a', 'event-a', { collection_id: 'collection-a', group_id: 'wallet-a' }, expect.any(AbortSignal))
})

it('keeps candidate events distinct and displays conflicting source facts and review links', async () => {
  const candidate = event({ linkage: 'candidate', conflicting_fields: ['quantity', 'provider_status'], relationships: [{ kind: 'possible_transfer', state: 'candidate', event_id: 'event-b', source_id: 'source-b', review_id: null, leg_id: 'leg-b', quantity: '2', reason_codes: ['identity_unresolved'], conflicting_fields: ['quantity'], review_url: '/assets?tab=activity&activity=transfers&movement=leg-b' }] })
  vi.mocked(timeline.list).mockResolvedValue(list({ events: [candidate, event({ event_id: 'event-b', linkage: 'unresolved' })], total: 2 }))
  vi.mocked(timeline.event).mockResolvedValue(candidate)
  const { user } = renderWithProviders(<View />)
  expect(await screen.findAllByRole('button', { name: /transfer · settled/ })).toHaveLength(2)
  await user.click(screen.getAllByRole('button', { name: /transfer · settled/ })[0])
  const dialog = within(await screen.findByRole('dialog'))
  expect(await dialog.findByText(/Candidate records remain separate/)).toBeInTheDocument()
  expect(dialog.getAllByText('conflicting quantity').length).toBeGreaterThan(0)
  expect(dialog.getByRole('link', { name: 'Open relationship review' })).toHaveAttribute('href', '/assets?tab=activity&activity=transfers&movement=leg-b')
  expect(dialog.getByRole('region', { name: 'Supporting sources' })).toHaveTextContent('completed')
  expect(dialog.getByRole('region', { name: 'Supporting sources' })).toHaveTextContent('pending')
})

it('renders a failed fee-only event and all swap legs under a canonical coin filter', async () => {
  const swap = event({ kind: 'swap', legs: [leg, { ...leg, leg_id: 'output', canonical_asset_key: sibling.canonical_asset_key, asset_symbol: 'TOKEN-Y', direction: 'in' }, fee] })
  vi.mocked(timeline.list).mockResolvedValue(list({ events: [event({ event_id: 'failed-a', status: 'failed', legs: [fee] }), swap] }))
  vi.mocked(timeline.event).mockResolvedValue(swap)
  const { user } = renderWithProviders(<View />, { route: '/assets?activity=timeline&canonical_asset_key=chain-a%3Aprogram-a%3Atoken-x' })
  const failed = await screen.findByRole('button', { name: /transfer · failed/ })
  expect(failed).toHaveTextContent('fee · out · 0.000000000000000001')
  expect(failed).not.toHaveTextContent(leg.quantity!)
  await user.click(screen.getByRole('button', { name: /swap · settled/ }))
  const details = within(await screen.findByRole('dialog'))
  expect(await details.findByText(/TOKEN-Y/, { selector: 'h4' })).toBeInTheDocument()
  expect(details.getByText(/All supplied event legs/)).toBeInTheDocument()
})

it('retries only a failed supporting source and preserves exact raw source bytes', async () => {
  vi.mocked(timeline.source).mockRejectedValueOnce(new Error('synthetic unavailable')).mockResolvedValue(sourceDetail())
  const { user } = renderWithProviders(<View />, { route: '/assets?activity=timeline&event=event-a&event_source=source-a' })
  const dialog = within(await screen.findByRole('dialog'))
  await dialog.findByText(/This source could not be loaded/)
  expect(dialog.getByRole('region', { name: 'Movement and fee legs' })).toBeInTheDocument()
  await user.click(dialog.getByRole('button', { name: 'Retry' }))
  await user.click(await dialog.findByText('Retained original payload'))
  expect(dialog.getByText('{"raw_units":18446744073709551615}')).toBeInTheDocument()
  expect(timeline.source).toHaveBeenCalledTimes(2)
  expect(timeline.event).toHaveBeenCalledTimes(1)
})

it('scopes archive identities by the server key and promotes a holding for all-account exploration', async () => {
  const { user } = renderWithProviders(<View />, { route: '/assets?activity=timeline&asset_id=archived-holding' })
  await waitFor(() => expect(screen.getByLabelText('Location')).toHaveTextContent('canonical_asset_key=chain-a%3Aprogram-a%3Atoken-x'))
  expect(screen.getByLabelText('Location')).not.toHaveTextContent('asset_id=')
  const select = screen.getByRole('combobox', { name: 'Coin / canonical asset' })
  const options = await within(select).findAllByRole('option', { name: /TOKEN-X/ })
  expect(options).toHaveLength(2)
  expect(options[0]).not.toHaveAttribute('value', options[1].getAttribute('value'))
  await user.selectOptions(select, sibling.canonical_asset_key)
  await waitFor(() => expect(timeline.list).toHaveBeenLastCalledWith('workspace-a', expect.objectContaining({ canonical_asset_key: sibling.canonical_asset_key, collection_id: 'collection-a', group_id: 'wallet-a' }), expect.any(AbortSignal)))
})

it('uses inclusive UTC bounds, keeps filters on Back, and never submits native research', async () => {
  const trace = vi.spyOn(onchain, 'trace')
  const { user } = renderWithProviders(<View />, { route: '/assets?tab=activity&activity=timeline&wallet=wallet-a' })
  fireEvent.change(screen.getByLabelText('From (UTC)'), { target: { value: '2025-02-03' } })
  fireEvent.change(screen.getByLabelText('Through (UTC)'), { target: { value: '2025-02-04' } })
  await waitFor(() => expect(timeline.list).toHaveBeenLastCalledWith('workspace-a', expect.objectContaining({ since: '2025-02-03T00:00:00Z', until: '2025-02-04T23:59:59.999999Z' }), expect.any(AbortSignal)))
  await user.click(await screen.findByRole('button', { name: /transfer · settled/ }))
  const dialog = within(await screen.findByRole('dialog'))
  const native = await dialog.findByRole('link', { name: 'Inspect native transfers' })
  expect(native.getAttribute('href')).toContain('activity=wallets')
  expect(native.getAttribute('href')).toContain('since=2025-02-03T00%3A00%3A00Z')
  await user.keyboard('{Escape}')
  expect(screen.getByLabelText('From (UTC)')).toHaveValue('2025-02-03')
  await user.click(screen.getByRole('button', { name: /transfer · settled/ }))
  await user.click(await within(await screen.findByRole('dialog')).findAllByRole('button', { name: 'Open source · Exchange A export' }).then((buttons) => buttons.at(-1)!))
  await user.keyboard('{Escape}')
  await user.click(screen.getByRole('button', { name: 'Browser Back' }))
  expect(await screen.findByRole('dialog')).toBeInTheDocument()
  expect(screen.getByLabelText('Location')).toHaveTextContent('timeline_since=2025-02-03')
  expect(trace).not.toHaveBeenCalled()
})

it('pins continuation to the revision and offers restart when evidence changes', async () => {
  vi.mocked(timeline.list).mockResolvedValueOnce(list({ total: 26, has_more: true, all_available_records_loaded: false })).mockRejectedValueOnce({ response: { status: 409, data: { detail: { code: 'timeline_scope_changed' } } } }).mockResolvedValue(list())
  const { user } = renderWithProviders(<View />)
  await screen.findByRole('button', { name: /transfer · settled/ })
  await user.click(screen.getByRole('button', { name: 'Next' }))
  await screen.findByText(/available evidence changed/)
  expect(timeline.list).toHaveBeenLastCalledWith('workspace-a', expect.objectContaining({ offset: 25, expected_revision: 'revision-a' }), expect.any(AbortSignal))
  await user.click(screen.getByRole('button', { name: 'Restart timeline' }))
  await screen.findByRole('button', { name: /transfer · settled/ })
  expect(screen.getByLabelText('Location')).not.toHaveTextContent('timeline_revision')
})

it('retains useful empty and per-source coverage states without claiming complete history', async () => {
  vi.mocked(timeline.list).mockResolvedValue(list({ events: [], total: 0 }))
  const { user } = renderWithProviders(<View />)
  expect(await screen.findByText(/No events match this available scope/)).toBeInTheDocument()
  expect(screen.getByText(/History completeness remains unresolved/)).toBeInTheDocument()
  expect(screen.getByRole('combobox', { name: 'Coin / canonical asset' })).toHaveTextContent('TOKEN-X')
  await user.click(screen.getByText('Exchange A export', { selector: 'strong' }))
  expect(screen.getByText('opening quantity unknown')).toBeInTheDocument()
  expect(screen.getByText('missing token account history')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Open connection details' })).toHaveAttribute('href', '/accounts#connection-connection-a')
})

it('discards delayed event and source responses after workspace and wallet changes', async () => {
  let finish!: (value: TimelineEvent) => void
  vi.mocked(timeline.event).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve })).mockResolvedValue(event({ accounts: [{ ...account, group_name: 'Workspace B account' }] }))
  vi.mocked(timeline.list).mockImplementation(async (workspace) => list({ workspace_id: workspace, events: workspace === 'workspace-a' ? [event()] : [] }))
  const { rerender } = renderWithProviders(<View />, { route: '/assets?activity=timeline&event=event-a' })
  await waitFor(() => expect(timeline.event).toHaveBeenCalledTimes(1))
  rerender(<View workspaceId="workspace-b" groupId="wallet-b" />)
  await waitFor(() => expect(timeline.event).toHaveBeenLastCalledWith('workspace-b', 'event-a', { collection_id: 'collection-a', group_id: 'wallet-b' }, expect.any(AbortSignal)))
  await act(async () => { finish(event({ accounts: [{ ...account, group_name: 'STALE FOREIGN ACCOUNT' }] })) })
  expect(screen.queryByText('STALE FOREIGN ACCOUNT')).not.toBeInTheDocument()
  expect((await screen.findAllByText('Workspace B account')).length).toBeGreaterThan(0)
})

it('masks quantities, account/source identity, precise dates and original payload in privacy mode', async () => {
  localStorage.setItem('privacyMode', 'true')
  const { user } = renderWithProviders(<View />, { route: '/assets?activity=timeline&event=event-a&event_source=source-a' })
  const dialog = within(await screen.findByRole('dialog'))
  await user.click(await dialog.findByText('Retained original payload'))
  expect(document.body.textContent).not.toContain(leg.quantity)
  expect(dialog.queryByText('Exchange A')).not.toBeInTheDocument()
  expect(dialog.queryByText('synthetic.csv:row-a')).not.toBeInTheDocument()
  expect(dialog.queryByText(/18446744073709551615/)).not.toBeInTheDocument()
  expect(dialog.getByRole('region', { name: 'Acquisition evidence' })).toHaveTextContent('Unknown')
  expect(dialog.getAllByText('•••••').length).toBeGreaterThan(5)
})

it('adopts a scoped canonical event alias and uses readable supplied timestamp precision', async () => {
  vi.mocked(timeline.event).mockResolvedValue(event({ event_id: 'confirmed-transfer', time: { ...day, event_at: '2025-02-03T12:34:00Z', event_time_raw: '1738586040', time_precision: 'minute', ordering: 'exact' } }))
  renderWithProviders(<View />, { route: '/assets?activity=timeline&event=old-evidence-event' })
  await waitFor(() => expect(screen.getByLabelText('Location')).toHaveTextContent('event=confirmed-transfer'))
  const dialog = within(await screen.findByRole('dialog'))
  expect(await dialog.findByText('2025-02-03T12:34Z')).toBeInTheDocument()
  expect(dialog.queryByText('1738586040')).not.toBeInTheDocument()
})

it('offers only wallet-scoped saved-address selection without a supplied native target', async () => {
  vi.mocked(timeline.event).mockResolvedValue(event({ native_trace_url: null }))
  renderWithProviders(<View />, { route: '/assets?activity=timeline&event=event-a&chain=stale-chain&address=stale-address&max_hops=6' })
  const dialog = within(await screen.findByRole('dialog'))
  const link = await dialog.findByRole('link', { name: 'Choose saved address · Exchange A' })
  expect(link).toHaveAttribute('href', '/assets?tab=activity&activity=wallets&wallet=wallet-a')
  expect(link.getAttribute('href')).not.toMatch(/stale|max_hops/)
})

it('never displays a delayed foreign-workspace source while the selected event stays available', async () => {
  let finish!: (value: TimelineSourceDetail) => void
  vi.mocked(timeline.source).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve })).mockResolvedValue({ ...sourceDetail(), workspace_id: 'workspace-b', observation: { source: 'Current B source' } })
  vi.mocked(timeline.list).mockImplementation(async (workspace) => list({ workspace_id: workspace }))
  const { rerender } = renderWithProviders(<View />, { route: '/assets?activity=timeline&event=event-a&event_source=source-a' })
  await waitFor(() => expect(timeline.source).toHaveBeenCalledTimes(1))
  rerender(<View workspaceId="workspace-b" groupId="wallet-b" />)
  await waitFor(() => expect(timeline.source).toHaveBeenLastCalledWith('workspace-b', 'source-a', { collection_id: 'collection-a', group_id: 'wallet-b' }, expect.any(AbortSignal)))
  await act(async () => { finish({ ...sourceDetail(), observation: { source: 'STALE FOREIGN SOURCE' } }) })
  expect(document.body.textContent).not.toContain('STALE FOREIGN SOURCE')
  expect(await screen.findByText(/Current B source/)).toBeInTheDocument()
})

it('uses a readable chain and native unit when no symbol is supplied, keeping internal IDs out of rows', async () => {
  const native = { ...leg, asset_symbol: null, chain: 'solana', token_address: 'native', canonical_asset_key: 'a'.repeat(64) }
  vi.mocked(timeline.list).mockResolvedValue(list({ events: [event({ legs: [native] })] }))
  renderWithProviders(<View />)
  const row = await screen.findByRole('button', { name: /transfer · settled/ })
  expect(row).toHaveTextContent('solana · native')
  expect(row).not.toHaveTextContent('a'.repeat(64))
})

it('offers provisional settlement and wrap mechanics independently of the current page', async () => {
  vi.mocked(timeline.list).mockResolvedValue(list({ events: [], total: 0 }))
  const { user } = renderWithProviders(<View />)
  await screen.findByText(/No events match this available scope/)
  await user.selectOptions(screen.getByRole('combobox', { name: 'Status' }), 'provisional')
  await user.selectOptions(screen.getByRole('combobox', { name: 'Kind' }), 'wrap')
  expect(within(screen.getByRole('combobox', { name: 'Kind' })).getByRole('option', { name: 'unwrap' })).toBeInTheDocument()
  await waitFor(() => expect(timeline.list).toHaveBeenLastCalledWith('workspace-a', expect.objectContaining({ status: 'provisional', kind: 'wrap' }), expect.any(AbortSignal)))
})

it('preserves every rapid filter edit before the router commits its next render', async () => {
  const { user, rerender } = renderWithProviders(<View />, { route: '/assets?tab=activity&activity=timeline&timeline_status=settled' })
  await screen.findByRole('button', { name: /transfer · settled/ })
  const from = screen.getByLabelText('From (UTC)')
  const through = screen.getByLabelText('Through (UTC)')
  const kind = screen.getByRole('combobox', { name: 'Kind' })
  const sourceFilter = screen.getByRole('combobox', { name: 'Source' })
  act(() => {
    fireEvent.click(screen.getByRole('button', { name: 'Clear timeline filters' }))
    fireEvent.change(from, { target: { value: '2030-01-01' } })
    fireEvent.change(through, { target: { value: '2030-01-01' } })
    fireEvent.change(kind, { target: { value: 'transfer' } })
    fireEvent.change(sourceFilter, { target: { value: 'Exchange A export' } })
  })
  await waitFor(() => expect(timeline.list).toHaveBeenLastCalledWith('workspace-a', {
    collection_id: 'collection-a', group_id: 'wallet-a', limit: 25, offset: 0,
    since: '2030-01-01T00:00:00Z', until: '2030-01-01T23:59:59.999999Z', kind: 'transfer', source: 'Exchange A export',
  }, expect.any(AbortSignal)))
  expect(from).toHaveValue('2030-01-01')
  expect(through).toHaveValue('2030-01-01')
  expect(screen.getByLabelText('Location')).not.toHaveTextContent('timeline_status')

  await user.click(screen.getByRole('button', { name: 'Browser Back' }))
  await waitFor(() => expect(sourceFilter).toHaveValue(''))
  fireEvent.change(screen.getByRole('combobox', { name: 'Status' }), { target: { value: 'provisional' } })
  await waitFor(() => expect(timeline.list).toHaveBeenLastCalledWith('workspace-a', expect.objectContaining({ since: '2030-01-01T00:00:00Z', until: '2030-01-01T23:59:59.999999Z', kind: 'transfer', status: 'provisional' }), expect.any(AbortSignal)))
  expect(screen.getByLabelText('Location')).not.toHaveTextContent('timeline_source')

  rerender(<View workspaceId="workspace-b" groupId="wallet-b" />)
  act(() => {
    fireEvent.click(screen.getByRole('button', { name: 'Clear timeline filters' }))
    fireEvent.change(from, { target: { value: '2031-04-05' } })
    fireEvent.change(through, { target: { value: '2031-04-06' } })
  })
  await waitFor(() => expect(timeline.list).toHaveBeenLastCalledWith('workspace-b', {
    collection_id: 'collection-a', group_id: 'wallet-b', limit: 25, offset: 0,
    since: '2031-04-05T00:00:00Z', until: '2031-04-06T23:59:59.999999Z',
  }, expect.any(AbortSignal)))
})

function trail() {
  return {
    workspace_id: 'workspace-a', request: { event_id: 'event-a', leg_id: 'leg-a', direction: 'out' as const, max_hops: 3, max_branches: 3, minimums: {} },
    collection_id: 'retained-a', revision: 'revision-a', events: [event()], history_complete: false as const,
    steps: [{ event_id: 'event-a', leg_id: 'leg-a', asset_key: asset.canonical_asset_key, depth: 0, via: 'selected', effective_window: { since: null, until: null } }],
    frontier: [{ key: 'frontier-a', event_id: 'event-a', leg_id: 'leg-a', chain: 'solana', address: 'external-synthetic', asset_key: asset.canonical_asset_key, direction: 'out' as const, since: null, until: null, depth: 1 }],
    boundaries: [{ code: 'external_ownership_and_allocation_unknown' }],
  }
}

it('previews only on submit and explicitly continues a selected scoped frontier while preserving errors', async () => {
  const preview = vi.spyOn(onchain, 'previewInvestigation').mockResolvedValue(trail())
  const continuation = vi.spyOn(onchain, 'continueInvestigation').mockRejectedValueOnce(new Error('synthetic unavailable')).mockResolvedValue({ ...trail(), revision: 'revision-b' })
  const { user } = renderWithProviders(<View />, { route: '/assets?activity=timeline&event=event-a' })
  const panel = within(await screen.findByRole('region', { name: 'Follow evidence' }))
  expect(preview).not.toHaveBeenCalled()
  expect(continuation).not.toHaveBeenCalled()
  await user.click(panel.getByRole('button', { name: 'Preview saved trail' }))
  await panel.findByText(/external ownership and allocation unknown/)
  expect(preview).toHaveBeenCalledWith(trail().request, 'workspace-a')
  expect(continuation).not.toHaveBeenCalled()
  await user.click(panel.getByRole('button', { name: 'Collect selected continuation' }))
  await panel.findByText(/This investigation request could not finish/)
  expect(panel.getByText('solana · external-synthetic · TOKEN-X')).toBeInTheDocument()
  expect(continuation).toHaveBeenCalledWith({ ...trail().request, collection_id: 'retained-a', expected_revision: 'revision-a', frontier_key: 'frontier-a' }, 'workspace-a')
  await user.click(panel.getByRole('button', { name: 'Retry' }))
  await waitFor(() => expect(continuation).toHaveBeenCalledTimes(2))
  expect(panel.queryByText(/This investigation request could not finish/)).not.toBeInTheDocument()
})

it('keeps exact per-asset minimums and inclusive backward bounds, and reuses event detail with Back navigation', async () => {
  const preview = vi.spyOn(onchain, 'previewInvestigation').mockResolvedValue(trail())
  const { user } = renderWithProviders(<View />, { route: '/assets?activity=timeline&event=event-a&canonical_asset_key=chain-a%3Aprogram-a%3Atoken-x' })
  const panel = within(await screen.findByRole('region', { name: 'Follow evidence' }))
  await user.selectOptions(panel.getByLabelText('Follow direction'), 'in')
  await user.click(panel.getByText('Investigation bounds'))
  await user.type(panel.getByLabelText('Minimum quantity in the starting asset'), '9007199254740993.123456789')
  fireEvent.change(panel.getByLabelText('Trail from (UTC)'), { target: { value: '2025-01-01T00:00:00.001' } })
  fireEvent.change(panel.getByLabelText('Trail through (UTC)'), { target: { value: '2025-02-04T23:59:59.999' } })
  await user.click(panel.getByRole('button', { name: 'Preview saved trail' }))
  await panel.findByText(/external ownership and allocation unknown/)
  expect(preview).toHaveBeenCalledWith({ ...trail().request, direction: 'in', since: '2025-01-01T00:00:00.001Z', until: '2025-02-04T23:59:59.999Z', minimums: { [asset.canonical_asset_key]: '9007199254740993.123456789' } }, 'workspace-a')
  await user.click(panel.getByRole('button', { name: 'Inspect event and all legs' }))
  expect(panel.getByRole('region', { name: 'Movement and fee legs' })).toHaveTextContent('0.000000000000000001')
  expect(panel.getByRole('region', { name: 'Acquisition evidence' })).toHaveTextContent('Unknown')
  await user.click(panel.getAllByRole('button', { name: 'Open source · Exchange A export' })[0])
  await waitFor(() => expect(timeline.source).toHaveBeenLastCalledWith('workspace-a', 'source-a', {}, expect.any(AbortSignal)))
  // Browser chrome can navigate Back while Radix makes the background inert.
  fireEvent.click(screen.getByText('Browser Back'))
  expect(panel.queryByRole('button', { name: 'Back to trail' })).not.toBeInTheDocument()
  expect(screen.getByRole('dialog')).toBeInTheDocument()
})

it('shows already retained evidence and disables a completed continuation after previewing again', async () => {
  const completed = { ...trail(), frontier: trail().frontier.map((item) => ({ ...item, resumable: false })) }
  vi.spyOn(onchain, 'previewInvestigation').mockResolvedValueOnce(trail()).mockResolvedValue(completed)
  const continuation = vi.spyOn(onchain, 'continueInvestigation').mockRejectedValue({ response: { data: { detail: { code: 'investigation_complete' } } } })
  const { user } = renderWithProviders(<View />, { route: '/assets?activity=timeline&event=event-a' })
  const panel = within(await screen.findByRole('region', { name: 'Follow evidence' }))
  await user.click(panel.getByRole('button', { name: 'Preview saved trail' }))
  await user.click(await panel.findByRole('button', { name: 'Collect selected continuation' }))
  await panel.findByText('No remaining pages in this declared continuation. Coverage gaps may remain.')
  await user.click(panel.getByRole('button', { name: 'Preview saved trail' }))
  await waitFor(() => expect(panel.getByRole('button', { name: 'Collect selected continuation' })).toBeDisabled())
  expect(continuation).toHaveBeenCalledTimes(1)
  expect(panel.getByText('solana · external-synthetic · TOKEN-X')).toBeInTheDocument()
})

it('discards a delayed investigation result after switching workspace', async () => {
  let finish!: (value: ReturnType<typeof trail>) => void
  vi.spyOn(onchain, 'previewInvestigation').mockReturnValue(new Promise((resolve) => { finish = resolve }))
  const { user, rerender } = renderWithProviders(<View />, { route: '/assets?activity=timeline&event=event-a' })
  await user.click(await screen.findByRole('button', { name: 'Preview saved trail' }))
  vi.mocked(timeline.list).mockResolvedValue(list({ workspace_id: 'foreign', events: [] }))
  vi.mocked(timeline.event).mockRejectedValue(new Error('not available'))
  rerender(<View workspaceId="foreign" groupId={null} collectionId={null} />)
  await act(async () => finish(trail()))
  expect(screen.queryByText(/external-synthetic/)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Collect selected continuation' })).not.toBeInTheDocument()
})

it('masks investigation endpoint and asset references and keeps no-collection preview read-only', async () => {
  localStorage.setItem('privacyMode', 'true')
  vi.spyOn(onchain, 'previewInvestigation').mockResolvedValue({ ...trail(), collection_id: undefined, revision: undefined })
  const { user } = renderWithProviders(<View />, { route: '/assets?activity=timeline&event=event-a' })
  await user.click(await screen.findByRole('button', { name: 'Preview saved trail' }))
  const panel = await screen.findByRole('region', { name: 'Follow evidence' })
  await within(panel).findByText(/external ownership and allocation unknown/)
  expect(panel).not.toHaveTextContent('external-synthetic')
  expect(panel).not.toHaveTextContent(asset.canonical_asset_key)
  expect(within(panel).getByRole('button', { name: 'Collect selected continuation' })).toBeDisabled()
})

it('opens retained bridge evidence without collection and reviews only a complete eligible pair', async () => {
  const candidate = { source_event_id: 'event-a', source_leg_id: 'leg-a', destination_event_id: 'event-b', destination_leg_id: 'leg-b', source_id: 'source-a', destination_source_id: 'source-b', protocol: 'synthetic-bridge', message_id: 'synthetic-message', status: 'eligible' as const, reason_codes: [], collection_id: 'retained-a', revision: 'revision-a', source_summary: { chain: 'ethereum', quantity: '10', fee: '0', transaction_ref: 'synthetic-send' }, destination_summary: { chain: 'base', quantity: '10', fee: '0', transaction_ref: 'synthetic-receipt' } }
  const candidates = vi.spyOn(onchain, 'bridgeCandidates').mockResolvedValue([candidate, { ...candidate, destination_leg_id: 'pending-leg', status: 'unresolved', reason_codes: ['destination_execution_unsettled'] }])
  const review = vi.spyOn(onchain, 'reviewBridge').mockResolvedValue({ ...candidate, status: 'confirmed', revision: 'revision-b' })
  const preview = vi.spyOn(onchain, 'previewInvestigation')
  const continuation = vi.spyOn(onchain, 'continueInvestigation')
  const { user } = renderWithProviders(<View />, { route: '/assets?activity=timeline&event=event-a' })
  const panel = within(await screen.findByRole('region', { name: 'Bridge endpoint review' }))
  expect(candidates).not.toHaveBeenCalled()
  await user.click(panel.getByRole('button', { name: 'Bridge endpoint review' }))
  await panel.findByText('destination execution unsettled')
  expect(panel.getAllByText('synthetic-send')).toHaveLength(2)
  expect(panel.getAllByText('synthetic-receipt')).toHaveLength(2)
  const buttons = panel.getAllByRole('button', { name: 'Confirm reviewed bridge endpoints' })
  expect(buttons[1]).toBeDisabled()
  await user.click(buttons[0])
  await panel.findByText(/Reviewed relationship saved/)
  expect(review).toHaveBeenCalledWith({ collection_id: 'retained-a', expected_revision: 'revision-a', source_event_id: 'event-a', source_leg_id: 'leg-a', destination_event_id: 'event-b', destination_leg_id: 'leg-b', source_id: 'source-a', destination_source_id: 'source-b', reviewed: true }, 'workspace-a')
  expect(preview).not.toHaveBeenCalled()
  expect(continuation).not.toHaveBeenCalled()
})


it.each(['supported', 'unresolved'])('shows separate Token-2022 atomic facts with %s quantity interpretation', async (interpretation) => {
  vi.mocked(timeline.event).mockResolvedValue(event({ legs: [{ ...leg, quantity: interpretation === 'supported' ? '9.9' : null, interpretation, sender_debit_raw_units: '10000000', receiver_credit_raw_units: '9900000', withheld_fee_raw_units: '100000' }] }))
  const { user } = renderWithProviders(<View />, { route: '/assets?activity=timeline&event=event-a' })
  const details = within(await screen.findByRole('dialog'))
  await user.click(await details.findByText('Endpoints, exact units and derivation'))
  for (const [label, amount] of [['Sender debit (atomic units)', '10000000'], ['Recipient credit (atomic units)', '9900000'], ['Withheld fee (atomic units)', '100000']]) expect(details.getByText(label).nextElementSibling).toHaveTextContent(amount)
  if (interpretation === 'unresolved') expect(details.getByRole('heading', { level: 4, name: /transfer/ })).toHaveTextContent('Unknown')
})
