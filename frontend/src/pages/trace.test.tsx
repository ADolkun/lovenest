import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Route, Routes, useLocation } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ModuleRoute } from '@/components/module-route'
import { OnChainRoute } from '@/components/onchain-route'
import TracePage, { OwnedWalletActivity } from '@/pages/trace'
import { renderWithProviders } from '@/test/utils'
import type { OnChainWatchedAddress, TraceResult } from '@/types'

const onchain = vi.hoisted(() => ({ chains: vi.fn(), addresses: vi.fn(), trace: vi.fn() }))
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
const traceResult: TraceResult = {
  root: 'solana:wallet-one', direction: 'out', truncated: true,
  nodes: [
    { id: 'solana:wallet-one', chain: 'solana', address: 'wallet-one', depth: 0, symbol: 'SOL', balance: '10', terminal_reason: null },
    { id: 'solana:recipient', chain: 'solana', address: 'recipient', depth: 1, symbol: 'SOL', balance: null, terminal_reason: 'pooled' },
  ],
  edges: [{ source: 'solana:wallet-one', target: 'solana:recipient', chain: 'solana', symbol: 'SOL', amount: '2', reference: 'synthetic-transfer-reference', occurred_at: '2025-01-23T23:00:00Z' }],
}

beforeEach(() => {
  vi.clearAllMocks()
  workspace.id = 'investment'
  workspace.modules = ['accounts', 'assets']
  workspace.isLoading = false
  onchain.chains.mockResolvedValue([
    { key: 'solana', display_name: 'Solana', symbol: 'SOL', kind: 'solana', traceable: true },
    { key: 'ethereum', display_name: 'Ethereum', symbol: 'ETH', kind: 'evm', traceable: true },
  ])
  onchain.addresses.mockImplementation((id: string) => Promise.resolve(id === 'investment' ? [first, second] : []))
  onchain.trace.mockResolvedValue(traceResult)
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
  it('restores an exact incoming trace without fetching and copies a resumable link', async () => {
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
    await user.click(await screen.findByRole('button', { name: 'Copy trace link' }))
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

  it('does not submit a malformed minimum amount restored from a URL', async () => {
    renderWithProviders(panel(), { route: '/assets?chain=solana&address=wallet-one&min_amount=0x10' })
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
      expect(saved.retrieved_at).toBe('2026-09-06T12:00:00.000Z')
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
