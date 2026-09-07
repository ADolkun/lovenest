import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Route, Routes, useLocation, useSearchParams } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ModuleRoute } from '@/components/module-route'
import { OnChainRoute } from '@/components/onchain-route'
import TracePage, { OwnedWalletActivity } from '@/pages/trace'
import { renderWithProviders } from '@/test/utils'
import type { OnChainWatchedAddress, TraceResult, TransferCoverage } from '@/types'

const onchain = vi.hoisted(() => ({ chains: vi.fn(), addresses: vi.fn(), trace: vi.fn(), checkpoint: vi.fn() }))
const workspace = vi.hoisted(() => ({ id: 'investment', modules: ['accounts', 'assets'], isLoading: false }))
vi.mock('@/lib/api', () => ({ onchain }))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: () => ({
  current: workspace,
  isLoading: workspace.isLoading,
  hasModule: (module: string) => workspace.modules.includes(module),
}) }))
vi.mock('@/hooks/use-feature-flags', () => ({ useFeatureFlags: () => ({ onchainEnabled: true, isLoading: false }) }))

const first: OnChainWatchedAddress = {
  chain: 'solana', address: 'wallet-one', label: 'Solana wallet-one',
  connection_id: 'connection-one', connection_name: 'My wallet',
}
const second: OnChainWatchedAddress = {
  chain: 'ethereum', address: 'wallet-two', label: 'Ethereum wallet-two',
  connection_id: 'connection-two', connection_name: 'Second wallet',
}
const coverage: TransferCoverage = {
  requested_since: null, requested_until: null, fetched_at: '2025-01-25T00:00:00Z',
  observed_oldest: '2025-01-23T00:00:00Z', observed_newest: '2025-01-24T00:00:00Z',
  examined_oldest: '2025-01-23T23:00:00Z', examined_newest: '2025-01-23T23:00:00Z',
  since_reached: true, until_reached: true, provider_exhausted: true,
  pages_read: 1, rows_read: 2, signatures_read: 2, payloads_requested: 2, payloads_read: 1,
  failed_payloads: 0, pending_payloads: 0,
  missing_timestamps: 0, missing_payloads: 1, unsupported_payloads: 0,
  omitted_signatures: null, omitted_transfers: null, next_cursor: 'synthetic-page-cursor',
  stop_reasons: ['missing_payload'],
}
const traceResult: TraceResult = {
  request: { chain: 'solana', address: 'wallet-one', direction: 'out', max_hops: 3, max_branches: 3 },
  workspace_id: 'investment', started_at: '2026-09-06T11:59:30Z', retrieved_at: '2026-09-06T12:00:00Z',
  continuation: { status: 'not_needed', token: null, expires_at: null, reason: null },
  root: 'solana:wallet-one', direction: 'out', truncated: true, complete: false,
  scope: 'native_coin', root_window: { since: null, until: null },
  nodes: [
    { id: 'solana:wallet-one', chain: 'solana', address: 'wallet-one', depth: 0, symbol: 'SOL', balance: '10', terminal_reason: null,
      balance_observed_at: '2026-09-06T11:59:58Z', window_coverages: [],
      effective_window: { since: null, until: null }, coverage, unfinished_windows: [], stop_reasons: ['missing_payload'], branch_omitted_transfers: 0 },
    { id: 'solana:recipient', chain: 'solana', address: 'recipient', depth: 1, symbol: 'SOL', balance: null, terminal_reason: 'pooled',
      balance_observed_at: null, window_coverages: [],
      effective_window: { since: '2025-01-23T23:00:00Z', until: null }, coverage: null, unfinished_windows: [], stop_reasons: ['high_activity'], branch_omitted_transfers: 0 },
  ],
  edges: [{ source: 'solana:wallet-one', target: 'solana:recipient', chain: 'solana', symbol: 'SOL', amount: '2', reference: 'synthetic-transfer-reference', occurred_at: '2025-01-23T23:00:00Z' }],
}

beforeEach(() => {
  vi.clearAllMocks()
  Object.values(onchain).forEach((mock) => mock.mockReset())
  workspace.id = 'investment'
  workspace.modules = ['accounts', 'assets']
  workspace.isLoading = false
  onchain.chains.mockResolvedValue([
    { key: 'solana', display_name: 'Solana', symbol: 'SOL', kind: 'solana', traceable: true },
    { key: 'ethereum', display_name: 'Ethereum', symbol: 'ETH', kind: 'evm', traceable: true },
  ])
  onchain.addresses.mockImplementation((id: string) => Promise.resolve(id === 'investment' ? [first, second] : []))
  onchain.trace.mockImplementation((request) => Promise.resolve({ ...traceResult, request }))
  onchain.checkpoint.mockResolvedValue(traceResult)
})

function panel(props: Parameters<typeof OwnedWalletActivity>[0] = {}) {
  return <TooltipProvider><OwnedWalletActivity {...props} /></TooltipProvider>
}

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location">{location.pathname}{location.search}</output>
}

function TraceRoutes() {
  return (
    <TooltipProvider>
      <Routes>
        <Route path="/trace" element={<OnChainRoute><ModuleRoute module="accounts"><TracePage /><LocationProbe /></ModuleRoute></OnChainRoute>} />
        <Route path="/assets" element={<LocationProbe />} />
      </Routes>
    </TooltipProvider>
  )
}

describe('legacy trace route', () => {
  it('opens wallet activity in Assets while preserving the saved wallet and other filters', async () => {
    renderWithProviders(<TraceRoutes />, { route: '/trace?chain=solana&address=wallet-one&wallet=group-one&tab=positions' })
    await waitFor(() => expect(screen.getByTestId('location').textContent).toMatch(/^\/assets\?/))
    const destination = new URL(screen.getByTestId('location').textContent!, 'https://test.invalid')
    expect(Object.fromEntries(destination.searchParams)).toEqual({
      chain: 'solana', address: 'wallet-one', wallet: 'group-one', tab: 'activity', activity: 'wallets',
    })
    expect(onchain.addresses).not.toHaveBeenCalled()
  })

  it('waits for workspace modules before choosing the destination', async () => {
    workspace.isLoading = true
    const { rerender } = renderWithProviders(<TraceRoutes />, { route: '/trace' })
    expect(screen.queryByTestId('location')).not.toBeInTheDocument()
    expect(onchain.addresses).not.toHaveBeenCalled()
    workspace.isLoading = false
    rerender(<TraceRoutes />)
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/assets?tab=activity&activity=wallets'))
  })

  it('keeps the Accounts wallet entry usable without the Assets module', async () => {
    workspace.modules = ['accounts']
    renderWithProviders(<TraceRoutes />, { route: '/trace' })
    expect(await screen.findByRole('combobox', { name: 'Your wallet' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Wallet activity' })).toBeInTheDocument()
    expect(screen.getByText('Accounts')).toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/trace$/)
    expect(screen.getByRole('link', { name: 'Manage wallets' })).toHaveAttribute('href', '/accounts')
  })
})

describe('owned wallet activity', () => {
  it('shows measured coverage for every node without turning unknown counts into zero', async () => {
    const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await user.click(await screen.findByRole('button', { name: 'Explore transfers' }))
    const region = await screen.findByRole('region', { name: 'Native transfer coverage' })
    expect(within(region).getByText(/Native history is incomplete/)).toBeInTheDocument()
    const root = within(region).getByText(/Starting address · wallet-one/).closest('details')!
    await user.click(root.querySelector('summary')!)
    expect(root).toHaveAttribute('open')
    for (const [label, value] of [
      ['History pages read', '1'], ['Solana signatures read', '2'], ['Non-null payloads received', '1'],
      ['Missing timestamps', '0'], ['Missing payloads', '1'], ['Signatures not examined', 'Unknown / not measured'],
      ['Decoded transfers omitted', 'Unknown / not measured'],
      ['Observed history-row dates', '2025-01-23 00:00:00 UTC → 2025-01-24 00:00:00 UTC'],
      ['Examined payload dates', '2025-01-23 23:00:00 UTC → 2025-01-23 23:00:00 UTC'],
    ]) expect(within(root).getByText(label).nextElementSibling).toHaveTextContent(value)
    expect(within(root).getByText('synthetic-page-cursor')).toBeInTheDocument()
    expect(within(root).getByText(/Continues the signature list only/)).toBeInTheDocument()
    const child = within(region).getByText(/Hop 1 · recipient/).closest('details')!
    await user.click(child.querySelector('summary')!)
    expect(within(child).getByText(/Provider coverage is unknown/)).toBeInTheDocument()
    expect(within(child).getByText(/High activity stopped further reading; it does not identify/)).toBeInTheDocument()
    expect(within(root).getByText('Provider history exhausted').nextElementSibling).toHaveTextContent('Yes')
    expect(within(region).queryByText(/^Complete for/)).not.toBeInTheDocument()
  })

  it.each(['out', 'in'] as const)('explains the %s root window and keeps a converging unfinished child window visible', async (direction) => {
    const childWindow = direction === 'out'
      ? { since: '2025-01-25T12:00:00.125Z', until: null }
      : { since: null, until: '2025-01-22T12:00:00.125Z' }
    onchain.trace.mockResolvedValue({
      ...traceResult, direction, root_window: { since: '2025-01-23T00:00:00Z', until: '2025-01-24T00:00:00Z' },
      nodes: [traceResult.nodes[0], { ...traceResult.nodes[1], effective_window: childWindow,
        unfinished_windows: [{ ...childWindow, reason: 'unexpanded_window' }],
        stop_reasons: ['unexpanded_window', 'branch_limit'], branch_omitted_transfers: 2,
      }],
    })
    const { user } = renderWithProviders(panel(), { route: `/assets?chain=solana&address=wallet-one&direction=${direction}&since=2025-01-23T00:00:00Z&until=2025-01-24T00:00:00Z` })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    expect(screen.getByText(direction === 'out' ? /no inherited root end date/ : /no inherited root start date/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    const region = await screen.findByRole('region', { name: 'Native transfer coverage' })
    const child = within(region).getByText(/Hop 1 · recipient/).closest('details')!
    await user.click(child.querySelector('summary')!)
    expect(within(child).getByRole('heading', { name: 'Unfinished windows' })).toBeInTheDocument()
    expect(within(child).getByText('Eligible transfers omitted by the branch limit: 2')).toBeInTheDocument()
    expect(within(child).getAllByText(/Another path requires a window that was not examined/)).toHaveLength(2)
    expect(child).toHaveTextContent(direction === 'out' ? '2025-01-25 12:00:00.125 UTC → No upper bound' : 'No lower bound → 2025-01-22 12:00:00.125 UTC')
  })

  it('limits complete empty history to the native window and keeps absent observed dates unknown', async () => {
    onchain.trace.mockResolvedValue({
      ...traceResult, complete: true, truncated: false, edges: [], nodes: [{
        ...traceResult.nodes[0], terminal_reason: 'no_movement', stop_reasons: [],
        coverage: { ...coverage, observed_oldest: null, observed_newest: null, examined_oldest: null, examined_newest: null,
          rows_read: 0, signatures_read: 0, payloads_requested: 0, payloads_read: 0, missing_payloads: 0,
          omitted_signatures: 0, omitted_transfers: 0, next_cursor: null, stop_reasons: [] },
      }],
    })
    const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await user.click(await screen.findByRole('button', { name: 'Explore transfers' }))
    const region = await screen.findByRole('region', { name: 'Native transfer coverage' })
    expect(within(region).getByText('Complete for the declared native-coin windows at the time of this read.')).toBeInTheDocument()
    expect(screen.getByText('No matching native transfers in the examined complete window.')).toBeInTheDocument()
    expect(within(region).getByText(/Tokens, fees, swaps, bridges, exchange activity and tax basis remain outside/)).toBeInTheDocument()
    await user.click(within(region).getByText(/Starting address · wallet-one/))
    expect(within(region).getByText('Observed history-row dates').nextElementSibling).toHaveTextContent('Unknown / not measured → Unknown / not measured')
    expect(within(region).queryByText('Next signature-page cursor')).not.toBeInTheDocument()
  })

  it('restricts root selection to saved addresses and the supplied wallet scope', async () => {
    const { user } = renderWithProviders(panel({ connectionIds: ['connection-one'] }), {
      route: '/assets?chain=solana&address=someone-else',
    })
    const wallet = await screen.findByRole('combobox', { name: 'Your wallet' })
    expect(screen.queryByRole('textbox', { name: 'Address' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Explore transfers' })).toBeDisabled()
    await user.click(wallet)
    expect(screen.getByRole('option', { name: 'My wallet · Solana wallet-one' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /Second wallet/ })).not.toBeInTheDocument()
    await user.click(screen.getByRole('option', { name: 'My wallet · Solana wallet-one' }))
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    await waitFor(() => expect(onchain.trace).toHaveBeenCalledWith(expect.objectContaining({ chain: 'solana', address: 'wallet-one' }), 'investment'))
  })

  it('sends inclusive UTC dates and retains coverage warnings and transaction evidence', async () => {
    const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    fireEvent.change(screen.getByLabelText('From (UTC)'), { target: { value: '2025-01-23T00:00' } })
    fireEvent.change(screen.getByLabelText('Through (UTC)'), { target: { value: '2025-01-24T23:59:59.999' } })
    expect(screen.getByText('Advanced trail settings').closest('details')).not.toHaveAttribute('open')
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    await waitFor(() => expect(onchain.trace).toHaveBeenCalledWith(expect.objectContaining({
      since: '2025-01-23T00:00:00.000Z', until: '2025-01-24T23:59:59.999Z',
    }), 'investment'))
    expect(await screen.findByText('The trail stops at a high-activity address.')).toBeInTheDocument()
    expect(screen.getByText(/Only part of the trail is shown/)).toBeInTheDocument()
    expect(screen.getByText(/Native-coin transfers only/)).toBeInTheDocument()
    expect(screen.getByTitle('synthetic-transfer-reference')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Copy transaction reference' }))
    expect(await navigator.clipboard.readText()).toBe('synthetic-transfer-reference')
  })

  it('discards an in-flight result when the selected address changes', async () => {
    let resolve!: (result: TraceResult) => void
    onchain.trace.mockReturnValue(new Promise<TraceResult>((done) => { resolve = done }))
    const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    await user.click(screen.getByRole('combobox', { name: 'Your wallet' }))
    await user.click(screen.getByRole('option', { name: 'Second wallet · Ethereum wallet-two' }))
    await act(async () => { resolve(traceResult) })
    expect(screen.queryByTitle('synthetic-transfer-reference')).not.toBeInTheDocument()
    expect(screen.queryByText('The trail stops at a high-activity address.')).not.toBeInTheDocument()
  })

  it('removes saved addresses and late results when switching workspaces', async () => {
    let resolve!: (result: TraceResult) => void
    onchain.trace.mockReturnValue(new Promise<TraceResult>((done) => { resolve = done }))
    const { user, rerender } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    workspace.id = 'household'
    rerender(panel())
    await screen.findByText(/No saved wallet addresses match this view/)
    await act(async () => { resolve(traceResult) })
    expect(screen.queryByTitle('synthetic-transfer-reference')).not.toBeInTheDocument()
    expect(screen.queryByText('My wallet · Solana wallet-one')).not.toBeInTheDocument()
    expect(onchain.addresses).toHaveBeenLastCalledWith('household', expect.any(AbortSignal))
  })

  it('distinguishes an address-fetch failure from an empty wallet list', async () => {
    onchain.addresses.mockRejectedValue(new Error('Offline'))
    renderWithProviders(panel())
    expect(await screen.findByText('Could not load your saved wallet addresses.')).toBeInTheDocument()
    expect(screen.queryByText(/No saved wallet addresses match this view/)).not.toBeInTheDocument()
    expect(onchain.trace).not.toHaveBeenCalled()
  })

  it('clears results when an asset filter excludes the selected wallet address', async () => {
    const { user, rerender } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    await screen.findByTitle('synthetic-transfer-reference')
    rerender(panel({ addressKeys: [] }))
    expect(screen.queryByTitle('synthetic-transfer-reference')).not.toBeInTheDocument()
    expect(await screen.findByText(/No saved wallet addresses match this view/)).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Your wallet' })).not.toBeInTheDocument()
  })

  it('rejects a reversed date window before contacting the chain', async () => {
    renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    fireEvent.change(screen.getByLabelText('From (UTC)'), { target: { value: '2025-02-01T00:00' } })
    fireEvent.change(screen.getByLabelText('Through (UTC)'), { target: { value: '2025-01-01T00:00' } })
    expect(screen.getByRole('button', { name: 'Explore transfers' })).toBeDisabled()
    expect(onchain.trace).not.toHaveBeenCalled()
  })
  it('restores exact incoming settings without fetching and copies a settings link', async () => {
    const params = new URLSearchParams({
      tab: 'activity', activity: 'wallets', wallet: 'group-one', chain: 'solana', address: 'wallet-one',
      direction: 'in', max_hops: '5', max_branches: '2', min_amount: '0.123456789',
      since: '2025-01-23T13:00:00-08:00', until: '2025-01-24T02:18:00.125Z',
    })
    const { user } = renderWithProviders(panel(), { route: `/assets?${params}` })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    expect(onchain.trace).not.toHaveBeenCalled()
    expect(screen.getByLabelText('From (UTC)')).toHaveValue('2025-01-23T21:00')
    expect(screen.getByLabelText('Through (UTC)')).toHaveValue('2025-01-24T02:18:00.125')
    expect(screen.getByText('Advanced trail settings').closest('details')).toHaveAttribute('open')
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    const expected = {
      chain: 'solana', address: 'wallet-one', direction: 'in', max_hops: 5, max_branches: 2,
      min_amount: '0.123456789', since: '2025-01-23T21:00:00.000Z', until: '2025-01-24T02:18:00.125Z',
    }
    await waitFor(() => expect(onchain.trace).toHaveBeenCalledWith(expected, 'investment'))
    await user.click(await screen.findByRole('button', { name: 'Copy settings link' }))
    const copied = new URL(await navigator.clipboard.readText())
    expect(copied.pathname).toBe('/assets')
    expect(Object.fromEntries(copied.searchParams)).toEqual({
      tab: 'activity', activity: 'wallets', wallet: 'group-one', ...expected, max_hops: '5', max_branches: '2',
    })
    expect(screen.getByText(/Jan 23, 2025.*UTC/)).toBeInTheDocument()
    const hop = screen.getByText('Hop 1').closest('details')!
    await user.click(hop.querySelector('summary')!)
    expect(hop).not.toHaveAttribute('open')
    fireEvent.change(screen.getByLabelText('Through (UTC)'), { target: { value: '2025-01-23T21:00' } })
    expect(screen.getByRole('button', { name: 'Explore transfers' })).toBeEnabled()
  })

  it('keeps old day-only trace links inclusive and rejects malformed limits', async () => {
    const { user } = renderWithProviders(panel(), {
      route: '/assets?chain=solana&address=wallet-one&since=2025-01-23&until=2025-01-24&max_hops=99&max_branches=-1',
    })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    await waitFor(() => expect(onchain.trace).toHaveBeenCalledWith(expect.objectContaining({
      since: '2025-01-23T00:00:00.000Z', until: '2025-01-24T23:59:59.999Z', max_hops: 3, max_branches: 3,
    }), 'investment'))
  })

  it('ignores invalid URL dates without running or crashing', async () => {
    renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one&since=2025-02-30&until=2025' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    expect(screen.getByLabelText('From (UTC)')).toHaveValue('')
    expect(screen.getByLabelText('Through (UTC)')).toHaveValue('')
    expect(onchain.trace).not.toHaveBeenCalled()
  })

  it.each([
    ['1.', '1'],
    [' 1 ', '1'],
    [' 12345678901234567890.123456789012345678 ', '12345678901234567890.123456789012345678'],
    ['1.e-3', '1e-3'],
    ['.123456789012345678E+2', '.123456789012345678E+2'],
  ])('keeps URL minimum %s visible and submits its exact normalized value', async (value, expected) => {
    const params = new URLSearchParams({ chain: 'solana', address: 'wallet-one', min_amount: value })
    const { user } = renderWithProviders(panel(), { route: `/assets?${params}` })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    const amount = screen.getByRole('spinbutton', { name: 'Minimum amount (SOL)' }) as HTMLInputElement
    expect(amount.value).toBe(expected)
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    await waitFor(() => expect(onchain.trace).toHaveBeenCalledWith(expect.objectContaining({ min_amount: expected }), 'investment'))
  })

  it.each([
    '0000-01-01',
    '0001-01-01T00:00:00+01:00',
    '9999-12-31T23:59:59-01:00',
    '+010000-01-01T00:00:00Z',
  ])('does not retain the invisible date bound %s', async (value) => {
    const params = new URLSearchParams({ chain: 'solana', address: 'wallet-one', since: value, until: value })
    const { user } = renderWithProviders(panel(), { route: `/assets?${params}` })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    expect(screen.getByLabelText('From (UTC)')).toHaveValue('')
    expect(screen.getByLabelText('Through (UTC)')).toHaveValue('')
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    await waitFor(() => expect(onchain.trace).toHaveBeenCalledOnce())
    expect(onchain.trace.mock.calls[0][0]).not.toHaveProperty('since')
    expect(onchain.trace.mock.calls[0][0]).not.toHaveProperty('until')
  })

  it.each(['0x10', '1..', '1..e3', '1e1.'])('does not submit malformed URL minimum %s', async (value) => {
    const params = new URLSearchParams({ chain: 'solana', address: 'wallet-one', min_amount: value })
    renderWithProviders(panel(), { route: `/assets?${params}` })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    expect(screen.getByRole('button', { name: 'Explore transfers' })).toBeDisabled()
    expect(screen.getByText('Enter a nonnegative minimum amount.')).toBeInTheDocument()
    expect(onchain.trace).not.toHaveBeenCalled()
  })

  it('downloads the submitted request and unmodified evidence with its completion time and limits', async () => {
    const blobs: Blob[] = []
    const createObjectURL = vi.fn((blob: Blob) => { blobs.push(blob); return 'blob:trace-download' })
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = createObjectURL
      static revokeObjectURL = revokeObjectURL
    })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-09-06T12:00:00Z'))
    try {
      const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
      await screen.findByRole('combobox', { name: 'Your wallet' })
      await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
      const download = await screen.findByRole('button', { name: 'Download result' })
      vi.setSystemTime(new Date('2026-09-07T00:00:00Z'))
      await user.click(download)
      expect(click).toHaveBeenCalledOnce()
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:trace-download')
      const contents = await new Promise<string>((resolve) => {
        const reader = new FileReader()
        reader.onload = () => resolve(String(reader.result))
        reader.readAsText(blobs[0])
      })
      const saved = JSON.parse(contents)
      expect(saved.request).toEqual(onchain.trace.mock.calls[0][0])
      expect(saved.result).toEqual(traceResult)
      expect(saved.retrieved_at).toBe(traceResult.retrieved_at)
      expect(saved.coverage).toContain('Bounded native-coin transfers only')
      expect(saved.coverage).toContain('cost basis are not reconstructed')
      expect(saved.result.nodes[1].terminal_reason).toBe('pooled')
      expect(saved.result.truncated).toBe(true)
    } finally {
      click.mockRestore()
      vi.useRealTimers()
      vi.unstubAllGlobals()
    }
  })

})

function ChangeTraceDates() {
  const [, setParams] = useSearchParams()
  return <button onClick={() => setParams({ chain: 'solana', address: 'wallet-one', since: '2025-01-23' })}>Change trace dates</button>
}

const resumable: TraceResult = {
  ...traceResult,
  continuation: { status: 'available', token: 'synthetic-checkpoint-token', expires_at: '2099-01-01T00:00:00Z', reason: null },
}

function savedFile(result = resumable) {
  return new File([JSON.stringify({ version: 1, workspace_id: result.workspace_id, result })], 'trace.json', { type: 'application/json' })
}

describe('saved trace continuation', () => {
  it('names pending reads, deadline and retention gaps without raw provider codes', async () => {
    onchain.trace.mockResolvedValue({ ...resumable, nodes: [{ ...resumable.nodes[0], stop_reasons: ['pending_payload', 'deadline_exceeded', 'retention_limit'] }] })
    const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await user.click(await screen.findByRole('button', { name: 'Explore transfers' }))
    const region = await screen.findByRole('region', { name: 'Native transfer coverage' })
    await user.click(within(region).getByText(/Starting address · wallet-one/))
    for (const message of ['Some transaction payloads are still pending', 'The time limit interrupted the reads', 'This trace reached its saved-data limit. Restart with narrower settings.']) {
      expect(within(region).getByText(message)).toBeInTheDocument()
    }
  })

  it.each(['upstream_rate_limited', 'trace_admission_limited', 'trace_checkpoint_unavailable', 'trace_restart_required'])('keeps prior evidence and download when continuing fails with %s', async (code) => {
    onchain.trace.mockResolvedValueOnce(resumable).mockRejectedValueOnce({ response: { data: { detail: {
      code, retry_after_seconds: 10, message: 'https://secret.invalid/private-key',
    } } } })
    const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await user.click(await screen.findByRole('button', { name: 'Explore transfers' }))
    await user.click(await screen.findByRole('button', { name: 'Continue remaining work' }))
    await waitFor(() => expect(onchain.trace).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Restart trace' })).toBeInTheDocument())
    expect(onchain.trace).toHaveBeenLastCalledWith({ ...resumable.request, continuation_token: resumable.continuation.token }, 'investment')
    expect(screen.getByTitle('synthetic-transfer-reference')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download result' })).toBeEnabled()
    expect(screen.queryByText(/private-key/)).not.toBeInTheDocument()
    if (code.endsWith('limited') || code === 'trace_restart_required') expect(screen.getByRole('button', { name: 'Continue remaining work' })).toBeDisabled()
    if (code.endsWith('limited')) expect(screen.getByRole('button', { name: 'Restart trace' })).toBeDisabled()
  })

  it('continues the exact request, renders each retained window once, and restarts without a token', async () => {
    const laterCoverage = { ...coverage, requested_since: '2025-01-20T00:00:00Z', failed_payloads: 2, pending_payloads: 3 }
    const continued = {
      ...resumable, retrieved_at: '2026-09-07T00:00:00Z',
      continuation: { ...resumable.continuation, status: 'not_needed' as const, token: 'synthetic-next-checkpoint-token' },
      nodes: [{ ...resumable.nodes[0], window_coverages: [coverage, laterCoverage] }, resumable.nodes[1]],
      edges: [...resumable.edges, { ...resumable.edges[0], reference: 'synthetic-second-transfer' }],
    }
    onchain.trace.mockResolvedValueOnce(resumable).mockResolvedValueOnce(continued).mockResolvedValueOnce(traceResult)
    const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one&continuation_token=must-not-copy' })
    await user.click(await screen.findByRole('button', { name: 'Explore transfers' }))
    await user.click(await screen.findByRole('button', { name: 'Continue remaining work' }))
    await screen.findByTitle('synthetic-second-transfer')
    expect(screen.getAllByTitle('synthetic-transfer-reference')).toHaveLength(1)
    expect(screen.getByText(/No remaining retryable work for these settings/)).toBeInTheDocument()
    expect(screen.getByText(/Native history is incomplete/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Continue remaining work' })).not.toBeInTheDocument()
    const region = screen.getByRole('region', { name: 'Native transfer coverage' })
    const root = within(region).getByText(/Starting address · wallet-one/).closest('details')!
    await user.click(root.querySelector('summary')!)
    expect(within(root).getAllByText('Requested provider window')).toHaveLength(2)
    expect(within(root).getAllByText('Failed payload reads').map((label) => label.nextElementSibling?.textContent)).toEqual(['0', '2'])
    expect(within(root).getAllByText('Payload reads still pending').map((label) => label.nextElementSibling?.textContent)).toEqual(['0', '3'])
    await user.click(screen.getByRole('button', { name: 'Copy settings link' }))
    expect(new URL(await navigator.clipboard.readText()).searchParams.has('continuation_token')).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Restart trace' }))
    await waitFor(() => expect(onchain.trace).toHaveBeenCalledTimes(3))
    expect(onchain.trace.mock.calls[2][0]).toEqual(resumable.request)
  })

  it('downloads, reloads settings, reopens the canonical snapshot, and continues only on explicit click', async () => {
    let blob!: Blob
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL(value: Blob) { blob = value; return 'blob:trace' }
      static revokeObjectURL() {}
    })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    onchain.trace.mockResolvedValue(resumable)
    onchain.checkpoint.mockResolvedValue(resumable)
    try {
      const firstPanel = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
      await firstPanel.user.click(await screen.findByRole('button', { name: 'Explore transfers' }))
      await firstPanel.user.click(await screen.findByRole('button', { name: 'Download result' }))
      const contents = await new Promise<string>((resolve) => {
        const reader = new FileReader()
        reader.onload = () => resolve(String(reader.result))
        reader.readAsText(blob)
      })
      const exported = JSON.parse(contents)
      expect(exported).toMatchObject({ version: 1, workspace_id: 'investment', request: resumable.request, retrieved_at: resumable.retrieved_at, result: resumable })
      firstPanel.unmount()
      const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
      await screen.findByRole('combobox', { name: 'Your wallet' })
      expect(onchain.trace).toHaveBeenCalledOnce()
      expect(onchain.checkpoint).not.toHaveBeenCalled()
      // A tampered evidence body is never rendered: only server data is trusted.
      exported.result.edges[0].reference = 'tampered-file-evidence'
      await user.upload(screen.getByLabelText('Reopen saved result'), new File([JSON.stringify(exported)], 'saved.json', { type: 'application/json' }))
      expect(await screen.findByTitle('synthetic-transfer-reference')).toBeInTheDocument()
      expect(screen.queryByTitle('tampered-file-evidence')).not.toBeInTheDocument()
      expect(onchain.checkpoint).toHaveBeenCalledWith(resumable.continuation.token, 'investment')
      expect(onchain.trace).toHaveBeenCalledOnce()
      expect(screen.getByText(/Balances are snapshots at their observation times/)).toBeInTheDocument()
      expect(screen.getAllByText(/observed.*UTC/).length).toBeGreaterThan(0)
      await user.click(screen.getByRole('button', { name: 'Continue remaining work' }))
      await waitFor(() => expect(onchain.trace).toHaveBeenCalledTimes(2))
    } finally {
      click.mockRestore()
      vi.unstubAllGlobals()
    }
  })

  it('restores canonical settings and does not revive an old throttle cooldown', async () => {
    onchain.checkpoint.mockResolvedValue({
      ...resumable, request: { ...resumable.request, direction: 'in', max_hops: 5, max_branches: 2, min_amount: '0.123456789012345678', since: '2025-01-23T13:00:00.125Z' },
      interruption: { code: 'upstream_rate_limited', phase: 'history', retry_after_seconds: 120 },
    })
    const { user } = renderWithProviders(panel())
    await screen.findByRole('combobox', { name: 'Your wallet' })
    await user.upload(screen.getByLabelText('Reopen saved result'), savedFile())
    expect(await screen.findByRole('button', { name: 'Continue remaining work' })).toBeEnabled()
    expect(screen.getByLabelText('From (UTC)')).toHaveValue('2025-01-23T13:00:00.125')
    expect((screen.getByRole('spinbutton', { name: 'Minimum amount (SOL)' }) as HTMLInputElement).value).toBe('0.123456789012345678')
    expect(screen.getByRole('combobox', { name: 'Maximum hops' })).toHaveTextContent('5')
    expect(screen.getByRole('combobox', { name: 'Direction' })).toHaveTextContent('Where the funds came from')
    expect(screen.queryByText(/Retry available in/)).not.toBeInTheDocument()
    expect(onchain.trace).not.toHaveBeenCalled()
  })

  it.each([
    ['not json', 'Choose a valid Lovenest trace download'],
    [JSON.stringify({ version: 2, workspace_id: 'investment' }), 'Choose a valid Lovenest trace download'],
    [JSON.stringify({ version: 1, workspace_id: 'household', result: resumable }), 'This saved result belongs to another workspace'],
    [JSON.stringify({ version: 1, workspace_id: 'investment', result: { continuation: { token: 'header\r\ninjection' } } }), 'Choose a valid Lovenest trace download'],
    [JSON.stringify({ version: 1, workspace_id: 'investment', result: { continuation: { token: 'x'.repeat(257) } } }), 'Choose a valid Lovenest trace download'],
  ])('rejects malformed or foreign file metadata without a lookup (%s)', async (contents, message) => {
    const { user } = renderWithProviders(panel())
    await screen.findByRole('combobox', { name: 'Your wallet' })
    await user.upload(screen.getByLabelText('Reopen saved result'), new File([contents], 'trace.json', { type: 'application/json' }))
    expect(await screen.findByText(new RegExp(message))).toBeInTheDocument()
    expect(onchain.checkpoint).not.toHaveBeenCalled()
    expect(onchain.trace).not.toHaveBeenCalled()
  })

  it('rejects oversized files before reading or looking up their metadata', async () => {
    const { user } = renderWithProviders(panel())
    await screen.findByRole('combobox', { name: 'Your wallet' })
    const file = savedFile()
    Object.defineProperty(file, 'size', { value: 8 * 1024 * 1024 + 1 })
    await user.upload(screen.getByLabelText('Reopen saved result'), file)
    await screen.findByText(/Choose a valid Lovenest trace download/)
    expect(onchain.checkpoint).not.toHaveBeenCalled()
  })

  it.each(['trace_restart_required', 'trace_checkpoint_unavailable'])('shows an explicit %s restore failure without trusting the file or restarting', async (code) => {
    onchain.checkpoint.mockRejectedValue({ response: { data: { detail: { code } } } })
    const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    await user.upload(screen.getByLabelText('Reopen saved result'), savedFile())
    await screen.findByText(code === 'trace_restart_required' ? /This saved trace is expired/ : /Saved traces are temporarily unavailable/)
    expect(screen.queryByTitle('synthetic-transfer-reference')).not.toBeInTheDocument()
    expect(onchain.trace).not.toHaveBeenCalled()
  })

  it('does not display a canonical snapshot excluded by the current asset filter', async () => {
    onchain.checkpoint.mockResolvedValue(resumable)
    const { user } = renderWithProviders(panel({ addressKeys: ['ethereum:wallet-two'] }))
    await screen.findByRole('combobox', { name: 'Your wallet' })
    await user.upload(screen.getByLabelText('Reopen saved result'), savedFile())
    await screen.findByText(/The saved trace starts from a wallet outside this view/)
    expect(screen.queryByTitle('synthetic-transfer-reference')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Continue remaining work' })).not.toBeInTheDocument()
  })

  it.each(['From (UTC)', 'Through (UTC)', 'Minimum amount (SOL)', 'Direction', 'Maximum hops', 'Branches per hop', 'Your wallet'])('invalidates continuation when %s changes', async (name) => {
    onchain.trace.mockResolvedValue(resumable)
    const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await user.click(await screen.findByRole('button', { name: 'Explore transfers' }))
    await screen.findByRole('button', { name: 'Continue remaining work' })
    const value = { Direction: 'Where the funds came from', 'Maximum hops': '5', 'Branches per hop': '2', 'Your wallet': 'Second wallet · Ethereum wallet-two' }[name]
    if (value) {
      if (name === 'Maximum hops' || name === 'Branches per hop') await user.click(screen.getByText('Advanced trail settings'))
      await user.click(screen.getByRole('combobox', { name }))
      await user.click(screen.getByRole('option', { name: value }))
    } else {
      fireEvent.change(screen.getByLabelText(name), { target: { value: name.startsWith('Minimum') ? '0.1' : '2025-01-22T12:00' } })
    }
    expect(screen.queryByRole('button', { name: 'Continue remaining work' })).not.toBeInTheDocument()
    expect(screen.queryByTitle('synthetic-transfer-reference')).not.toBeInTheDocument()
    expect(onchain.trace).toHaveBeenCalledOnce()
  })

  it.each(['reopen', 'continue'] as const)('isolates a pending %s response on workspace switch', async (kind) => {
    let resolve!: (result: TraceResult) => void
    const pending = new Promise<TraceResult>((done) => { resolve = done })
    onchain.trace.mockResolvedValueOnce(resumable)
    onchain.checkpoint.mockReturnValue(pending)
    const { user, rerender } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    if (kind === 'continue') {
      await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
      onchain.trace.mockReturnValueOnce(pending)
      await user.click(await screen.findByRole('button', { name: 'Continue remaining work' }))
    } else {
      await user.upload(screen.getByLabelText('Reopen saved result'), savedFile())
      await waitFor(() => expect(onchain.checkpoint).toHaveBeenCalledOnce())
    }
    workspace.id = 'household'
    rerender(panel())
    await screen.findByText(/No saved wallet addresses match this view/)
    await act(async () => resolve(resumable))
    expect(screen.queryByTitle('synthetic-transfer-reference')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Download result' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Continue remaining work' })).not.toBeInTheDocument()
  })

  it('discards a delayed restore when settings change before the response arrives', async () => {
    let resolve!: (result: TraceResult) => void
    onchain.checkpoint.mockReturnValue(new Promise<TraceResult>((done) => { resolve = done }))
    const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    await user.upload(screen.getByLabelText('Reopen saved result'), savedFile())
    await waitFor(() => expect(onchain.checkpoint).toHaveBeenCalledOnce())
    fireEvent.change(screen.getByLabelText('From (UTC)'), { target: { value: '2025-02-01T00:00' } })
    await act(async () => resolve(resumable))
    expect(screen.getByLabelText('From (UTC)')).toHaveValue('2025-02-01T00:00')
    expect(screen.queryByRole('button', { name: 'Continue remaining work' })).not.toBeInTheDocument()
    expect(screen.queryByTitle('synthetic-transfer-reference')).not.toBeInTheDocument()
  })

  it('expires continuation without an automatic trace while retaining the result', async () => {
    onchain.trace.mockResolvedValue({ ...resumable, continuation: { ...resumable.continuation, expires_at: new Date(Date.now() + 60_000).toISOString() } })
    renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    vi.useFakeTimers()
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Explore transfers' }))
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      expect(screen.getByRole('button', { name: 'Continue remaining work' })).toBeEnabled()
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
      expect(screen.getByRole('button', { name: 'Continue remaining work' })).toBeDisabled()
      expect(screen.getByText(/This saved trace can no longer be continued/)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Download result' })).toBeEnabled()
      expect(onchain.trace).toHaveBeenCalledOnce()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('trace interruptions and retry guidance', () => {
  const failure = (code: string, retry_after_seconds: unknown = 10) => ({
    response: { data: { detail: { code, retry_after_seconds, message: 'https://secret.invalid/private-provider-key' } } },
  })

  it.each([
    ['upstream_rate_limited', 'The chain provider limited this trace. Wait before retrying.'],
    ['trace_admission_limited', 'This server has reached its trace request limit. Wait before retrying.'],
    ['history_unavailable', 'Transfer history is not configured for this chain on this server.'],
    ['unrecognized_upstream_code', 'The trace could not be completed'],
  ])('renders safe guidance for %s without exposing upstream detail', async (code, message) => {
    onchain.trace.mockRejectedValue(failure(code))
    const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(screen.queryByText(/private-provider-key/)).not.toBeInTheDocument()
    if (code.endsWith('limited')) {
      expect(screen.getByRole('button', { name: 'Retry' })).toBeDisabled()
      expect(screen.getByRole('status')).toHaveTextContent('Retry available in 10s.')
    } else {
      expect(screen.queryByText(/Retry available/)).not.toBeInTheDocument()
    }
    expect(Boolean(screen.queryByText(/A server operator can configure/))).toBe(code === 'upstream_rate_limited')
    expect(onchain.trace).toHaveBeenCalledOnce()
  })

  it('keeps cooldown across form and URL edits and only retries on submission after it expires', async () => {
    onchain.trace.mockRejectedValueOnce(failure('upstream_rate_limited'))
    renderWithProviders(<>{panel()}<ChangeTraceDates /></>, { route: '/assets?chain=solana&address=wallet-one' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    vi.useFakeTimers()
    try {
      fireEvent.submit(screen.getByRole('button', { name: 'Explore transfers' }).closest('form')!)
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      expect(screen.getByRole('button', { name: 'Retry' })).toBeDisabled()
      fireEvent.change(screen.getByLabelText('From (UTC)'), { target: { value: '2025-01-22T00:00' } })
      fireEvent.click(screen.getByRole('button', { name: 'Change trace dates' }))
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      const submit = screen.getByRole('button', { name: 'Explore transfers' })
      expect(submit).toBeDisabled()
      fireEvent.submit(submit.closest('form')!)
      expect(onchain.trace).toHaveBeenCalledOnce()
      await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
      expect(submit).toBeEnabled()
      expect(onchain.trace).toHaveBeenCalledOnce()
      fireEvent.submit(submit.closest('form')!)
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      expect(onchain.trace).toHaveBeenCalledTimes(2)
      expect(onchain.trace.mock.calls[1][0].since).toBe('2025-01-23T00:00:00.000Z')
    } finally {
      vi.useRealTimers()
    }
  })

  it.each([null, -1, '10', Infinity])('uses a short manual retry floor for invalid guidance %s', async (delay) => {
    onchain.trace.mockRejectedValue(failure('upstream_rate_limited', delay))
    const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Retry available in 5s.')
    expect(onchain.trace).toHaveBeenCalledOnce()
  })

  it('keeps completed transfers visible with partial throttle cooldown', async () => {
    onchain.trace.mockResolvedValue({ ...traceResult, interruption: { code: 'upstream_rate_limited', phase: 'history', retry_after_seconds: 120 } })
    const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    expect(await screen.findByTitle('synthetic-transfer-reference')).toBeInTheDocument()
    expect(screen.getByText(/Only part of the trail is shown/)).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Retry available in 120s.')
    expect(screen.getByRole('button', { name: 'Restart trace' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Download result' })).toBeEnabled()
  })

  it('qualifies balance-only expiry without inventing incomplete transfer coverage', async () => {
    onchain.trace.mockResolvedValue({
      ...traceResult, truncated: false, complete: true,
      nodes: traceResult.nodes.map((node) => ({ ...node, balance: null })),
      interruption: { code: 'deadline_exceeded', phase: 'balances', retry_after_seconds: null },
    })
    const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    expect(await screen.findByText(/Some current balances could not be read/)).toBeInTheDocument()
    expect(screen.getByTitle('synthetic-transfer-reference')).toBeInTheDocument()
    expect(screen.queryByText(/Only part of the trail is shown/)).not.toBeInTheDocument()
    expect(screen.queryByText(/balance 0 SOL/)).not.toBeInTheDocument()
  })

  it('makes zero-edge deadline expiry explicit rather than claiming empty history', async () => {
    onchain.trace.mockResolvedValue({
      ...traceResult, edges: [],
      nodes: [{ ...traceResult.nodes[0], balance: null, terminal_reason: 'budget' }],
      interruption: { code: 'deadline_exceeded', phase: 'history', retry_after_seconds: null },
    })
    const { user } = renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one' })
    await screen.findByRole('combobox', { name: 'Your wallet' })
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    expect(await screen.findByText(/The time limit interrupted the history read/)).toBeInTheDocument()
    expect(screen.getByText(/Only part of the trail is shown/)).toBeInTheDocument()
    expect(screen.queryByText(/No transfers were returned/)).not.toBeInTheDocument()
    expect(screen.queryByText(/No native-coin transfers were found/)).not.toBeInTheDocument()
  })
})
