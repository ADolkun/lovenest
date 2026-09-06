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
    expect(screen.getAllByRole('button', { name: 'Unlink' })).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'Unlink' }))
    await waitFor(() => expect(update).toHaveBeenCalledWith(wallet.id, { account_id: null }))
  })
})
