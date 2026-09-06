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
    fireEvent.change(screen.getByLabelText('From date (UTC)'), { target: { value: '2025-01-23' } })
    fireEvent.change(screen.getByLabelText('Through date (UTC)'), { target: { value: '2025-01-24' } })
    expect(screen.getByText('Advanced trail settings').closest('details')).not.toHaveAttribute('open')
    await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
    await waitFor(() => expect(onchain.trace).toHaveBeenCalledWith(expect.objectContaining({
      since: '2025-01-23T00:00:00Z', until: '2025-01-24T23:59:59.999Z',
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
    fireEvent.change(screen.getByLabelText('From date (UTC)'), { target: { value: '2025-02-01' } })
    fireEvent.change(screen.getByLabelText('Through date (UTC)'), { target: { value: '2025-01-01' } })
    expect(screen.getByRole('button', { name: 'Explore transfers' })).toBeDisabled()
    expect(onchain.trace).not.toHaveBeenCalled()
  })
})
