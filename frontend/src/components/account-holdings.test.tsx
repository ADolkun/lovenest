import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { AccountHoldingsSummary, UnlinkedWallets } from './account-holdings'
import { renderWithProviders } from '@/test/utils'
import type { Account, AssetGroup } from '@/types'

const { update, workspace } = vi.hoisted(() => ({ update: vi.fn(), workspace: vi.fn() }))
vi.mock('@/lib/api', () => ({ assetGroups: { update } }))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: workspace }))
vi.mock('@/contexts/auth-context', () => ({ useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }) }))

const account = { id: 'account-a', name: 'Exchange', display_name: null, type: 'investment', connection_id: null, is_closed: false, current_balance: 0 } as Account
const wallet = { id: 'wallet-a', name: 'Exchange', source: 'manual', account_id: null, asset_count: 3, unvalued_count: 0, current_value_primary: 125, currency: 'USD' } as AssetGroup

beforeEach(() => {
  update.mockReset().mockResolvedValue({})
  workspace.mockReturnValue({ current: { id: 'investment' }, canWrite: true })
})

describe('account holdings', () => {
  it('shows an unlinked value and requires choosing an eligible account before linking', async () => {
    const { user } = renderWithProviders(<UnlinkedWallets wallets={[wallet]} accounts={[account, { ...account, id: 'bank', connection_id: 'provider' }, { ...account, id: 'closed', is_closed: true }]} />)
    expect(screen.getByText('$125.00')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Link account' })).toBeDisabled()
    expect(screen.getAllByRole('option')).toHaveLength(2)
    expect(update).not.toHaveBeenCalled()
    await user.selectOptions(screen.getByRole('combobox'), account.id)
    await user.click(screen.getByRole('button', { name: 'Link account' }))
    await waitFor(() => expect(update).toHaveBeenCalledWith(wallet.id, { account_id: account.id }))
  })

  it('links by persisted identity, never by matching display names or cash balance', () => {
    const { rerender } = renderWithProviders(<AccountHoldingsSummary account={account} wallets={[wallet]} />)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    rerender(<AccountHoldingsSummary account={account} wallets={[{ ...wallet, account_id: account.id }]} />)
    expect(screen.getByRole('link')).toHaveAttribute('href', '/assets?wallet=wallet-a')
    expect(screen.getByText('$125.00')).toBeInTheDocument()
  })

  it('identifies partial values and does not turn entirely unpriced holdings into zero', () => {
    renderWithProviders(<UnlinkedWallets wallets={[{ ...wallet, unvalued_count: 1 }, { ...wallet, id: 'unknown', name: 'Unknown', asset_count: 1, unvalued_count: 1, current_value_primary: 0 }]} accounts={[]} />)
    expect(screen.getAllByText('Partial · 1 unpriced')).toHaveLength(2)
    expect(screen.getByText('Value unavailable')).toBeInTheDocument()
    expect(screen.queryByText('$0.00')).not.toBeInTheDocument()
    expect(screen.getByText(/No unlinked manual investment accounts are available/)).toBeInTheDocument()
  })

  it('keeps valuation visible for viewers and removes linking controls', () => {
    workspace.mockReturnValue({ current: { id: 'investment' }, canWrite: false })
    renderWithProviders(<UnlinkedWallets wallets={[wallet]} accounts={[account]} />)
    expect(screen.getByText('$125.00')).toBeInTheDocument()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(screen.queryByText(/Choose an investment account/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Use Add Account/)).not.toBeInTheDocument()
  })

  it('unlinks only manual associations and masks monetary values', async () => {
    localStorage.setItem('privacyMode', 'true')
    const { user } = renderWithProviders(<AccountHoldingsSummary account={account} wallets={[{ ...wallet, account_id: account.id }, { ...wallet, id: 'provider-wallet', source: 'coinbase', account_id: account.id }]} />)
    expect(screen.queryByText('$125.00')).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Unlink' })).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'More actions: Holdings · Exchange' })).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'More actions: Holdings · Exchange' }))
    await user.click(screen.getByRole('menuitem', { name: 'Unlink' }))
    await waitFor(() => expect(update).toHaveBeenCalledWith(wallet.id, { account_id: null }))
  })

  it('keeps persisted holdings visible on closed or retyped accounts without viewer actions', () => {
    workspace.mockReturnValue({ current: { id: 'investment' }, canWrite: false })
    renderWithProviders(<AccountHoldingsSummary account={{ ...account, type: 'checking', is_closed: true }} wallets={[{ ...wallet, account_id: account.id }]} size="large" />)
    expect(screen.getByRole('link', { name: 'Holdings: $125.00' })).toHaveAttribute('href', '/assets?wallet=wallet-a')
    expect(screen.getByRole('button', { name: 'Balance details: Exchange' })).toBeInTheDocument()
  })
})

it.each(['link', 'unlink'])('invalidates inactive account list and detail after a manual %s', async (action) => {
  const { QueryClient } = await import('@tanstack/react-query')
  const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 300000, retry: false } } })
  let linked = action === 'unlink'
  const read = async () => ({ ...account, balance_explanation: { holdings: linked ? [{ asset_id: 'synthetic-held', value: 125 }] : [] } })
  const keys = [['accounts'], ['accounts', account.id]]
  for (const queryKey of keys) await queryClient.fetchQuery({ queryKey, queryFn: read })
  update.mockImplementation(async (_id, data) => { linked = data.account_id !== null; return {} })
  const { user } = renderWithProviders(action === 'link'
    ? <UnlinkedWallets wallets={[wallet]} accounts={[account]} />
    : <AccountHoldingsSummary account={account} wallets={[{ ...wallet, account_id: account.id }]} />, { queryClient })
  if (action === 'link') {
    await user.selectOptions(screen.getByRole('combobox'), account.id)
    await user.click(screen.getByRole('button', { name: 'Link account' }))
  } else {
    await user.click(screen.getByRole('button', { name: 'More actions: Holdings · Exchange' }))
    await user.click(screen.getByRole('menuitem', { name: 'Unlink' }))
  }
  await waitFor(() => expect(update).toHaveBeenCalledWith(wallet.id, { account_id: action === 'link' ? account.id : null }))
  for (const queryKey of keys) {
    expect(queryClient.getQueryState(queryKey)?.isInvalidated).toBe(true)
    const current = await queryClient.fetchQuery({ queryKey, queryFn: read })
    expect(current.balance_explanation.holdings).toHaveLength(action === 'link' ? 1 : 0)
  }
})
