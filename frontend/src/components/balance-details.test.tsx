import { beforeEach, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import { AccountBalanceBasis, BalanceDetails } from './balance-details'
import PositionsTab from './positions-tab'
import { assets } from '@/lib/api'
import { renderWithProviders } from '@/test/utils'
import { reportedWallet } from '@/test/balance-fixtures'
import type { Account, Asset, AssetGroup } from '@/types'

vi.mock('@/contexts/auth-context', () => ({ useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }) }))
const holding = { id: 'holding-a', group_id: 'wallet-a', name: 'Example fund', ticker: 'EXAMPLE', type: 'stock', units: 7, currency: 'USD', current_value: 70, current_value_primary: 70 } as Asset
const privateAsset = { ...holding, id: 'private-a', name: 'Private asset', ticker: null, current_value: 20, current_value_primary: 20 }
const wallet = reportedWallet({ id: 'wallet-a', name: 'Wallet A', account_balance: 120 } as AssetGroup, [holding, privateAsset])
const account = { id: wallet.account_id, name: 'Account A', connection_id: wallet.connection_id, type: 'investment', currency: 'USD', current_balance: 120, balance_explanation: { ...wallet.balance_explanation!, wallet_id: null } } as Account

beforeEach(() => {
  localStorage.removeItem('privacyMode')
  vi.restoreAllMocks()
})

it('shows the same source basis and explanation from account and wallet entrypoints', async () => {
  const { user, rerender } = renderWithProviders(<><AccountBalanceBasis account={account} /><BalanceDetails workspaceId="workspace" account={account} /></>)
  expect(screen.getByText('Reported account total')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Balance details: Account A' }))
  const accountDialog = screen.getByRole('dialog')
  expect(within(accountDialog).getByText(/not independent confirmation/)).toBeInTheDocument()
  expect(within(accountDialog).getByText('$120.00')).toBeInTheDocument()
  await user.keyboard('{Escape}')
  rerender(<BalanceDetails workspaceId="workspace" wallets={[wallet]} />)
  await user.click(screen.getByRole('button', { name: 'Balance details: Wallet A' }))
  const walletDialog = screen.getByRole('dialog')
  expect(within(walletDialog).getByText('Reported account total')).toBeInTheDocument()
  expect(within(walletDialog).getByText(/not independent confirmation/)).toBeInTheDocument()
  expect(within(walletDialog).getByText('Non-ticker assets')).toBeInTheDocument()
  expect(within(walletDialog).getByText(/\$30\.00/)).toBeInTheDocument()
  expect(within(walletDialog).getAllByRole('link', { name: 'Open value details' })[0]).toHaveAttribute('href', '/assets?tab=portfolio&view=wallets&wallet=wallet-a&holding=holding-a')
})

it('names missing holdings while retaining quantity and original observation age after failure', async () => {
  const broken = { ...wallet, balance_explanation: { ...wallet.balance_explanation!, observed_at: null, refresh_status: 'error', refresh_observed_at: '2026-01-02T00:00:00Z', reason_codes: ['refresh_failed', 'missing_holding_value'], residual_cash: null, reconciliation: 'not_comparable' as const, holdings: [{ ...wallet.balance_explanation!.holdings[0], value: null }] } }
  const { user } = renderWithProviders(<BalanceDetails workspaceId="workspace" wallets={[broken]} holdingsError />)
  await user.click(screen.getByRole('button', { name: 'Balance details: Wallet A' }))
  expect(screen.getByRole('alert')).toHaveTextContent('holdings request failed')
  expect(screen.getByText('Example fund')).toBeInTheDocument()
  expect(screen.getByText('Quantity: 7')).toBeInTheDocument()
  expect(screen.getAllByText('Unknown age').length).toBeGreaterThan(0)
  expect(screen.getByText('Quote or valuation unavailable')).toBeInTheDocument()
  expect(screen.getByText(/1\/1\/2026/)).toBeInTheDocument()
  expect(screen.queryByText('$0.00')).not.toBeInTheDocument()
})

it('keeps manual ledger and shared derivation distinct from independent matching', async () => {
  const { user, rerender } = renderWithProviders(<BalanceDetails account={{ ...account, balance_explanation: { ...account.balance_explanation!, basis: 'cash_ledger', reconciliation: 'separate_cash_ledger', residual_cash: null } }} />)
  await user.click(screen.getByRole('button', { name: 'Balance details: Account A' }))
  expect(screen.getByText('Cash ledger')).toBeInTheDocument()
  expect(screen.getByText(/does not authorize subtracting holdings/)).toBeInTheDocument()
  await user.keyboard('{Escape}')
  rerender(<BalanceDetails account={{ ...account, balance_explanation: { ...account.balance_explanation!, basis: 'connector_calculated_subtotal', reconciliation: 'shared_derivation', residual_cash: null } }} />)
  await user.click(screen.getByRole('button', { name: 'Balance details: Account A' }))
  expect(screen.getByText(/not independent reconciliation/)).toBeInTheDocument()
})

it('masks amounts and quantities and respects module permissions for recovery links', async () => {
  localStorage.setItem('privacyMode', 'true')
  const { user } = renderWithProviders(<BalanceDetails account={account} canOpenAccounts={false} canOpenAssets={false} />)
  await user.click(screen.getByRole('button', { name: 'Balance details: Account A' }))
  expect(screen.queryByText('$120.00')).not.toBeInTheDocument()
  expect(screen.queryByText('Quantity: 7')).not.toBeInTheDocument()
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
})

it('closes on workspace change and never opens sibling metadata from a delayed response', async () => {
  const { user, rerender } = renderWithProviders(<BalanceDetails workspaceId="workspace" account={account} />)
  await user.click(screen.getByRole('button', { name: 'Balance details: Account A' }))
  rerender(<BalanceDetails workspaceId="sibling" account={account} />)
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Balance details: Account A' }))
  expect(screen.queryByText('$120.00')).not.toBeInTheDocument()
  expect(screen.queryByText('Example fund')).not.toBeInTheDocument()
})

it('keeps failed cached holdings partial and withholds residual cash in the overview', () => {
  vi.spyOn(assets, 'income').mockResolvedValue({ holdings: {}, wallets: {} })
  renderWithProviders(<PositionsTab holdings={[holding, privateAsset]} wallets={[wallet]} holdingsError currency="USD" locale="en-US" dateLocale="en-US" mask={(value) => value} canWrite={false} onClassify={vi.fn()} onOpenHolding={vi.fn()} />)
  const summary = screen.getByRole('region', { name: 'Balance overview' })
  expect(within(summary).getAllByRole('definition').map((item) => item.textContent)).toEqual(['$70.00', '$70.00', '—'])
  expect(within(summary).getAllByText('Known subtotal')).toHaveLength(2)
})

it('routes viewers to connection status and editors to existing settings without performing a sync', async () => {
  const connected = { ...account, connection_id: 'connection-a', balance_explanation: { ...account.balance_explanation!, connection_id: 'connection-a' } }
  const { user, rerender } = renderWithProviders(<BalanceDetails account={connected} canWrite={false} />)
  await user.click(screen.getByRole('button', { name: 'Balance details: Account A' }))
  expect(screen.getByRole('link', { name: 'Review connection' })).toHaveAttribute('href', '/accounts#connection-connection-a')
  await user.keyboard('{Escape}')
  rerender(<BalanceDetails account={connected} canWrite />)
  await user.click(screen.getByRole('button', { name: 'Balance details: Account A' }))
  expect(screen.getByRole('link', { name: 'Review connection' })).toHaveAttribute('href', '/accounts?review=connection-a')
})
