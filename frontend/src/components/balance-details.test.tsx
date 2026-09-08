import { beforeEach, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import { AccountBalanceAmount, AccountBalanceBasis, BalanceDetails } from './balance-details'
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
  const observedAt = '2026-01-01T00:00:00Z'
  const refreshedAt = '2026-01-02T00:00:00Z'
  const broken = { ...wallet, balance_explanation: { ...wallet.balance_explanation!, observed_at: observedAt, refresh_status: 'error', refresh_observed_at: refreshedAt, reason_codes: ['refresh_failed', 'missing_holding_value'], residual_cash: null, reconciliation: 'not_comparable' as const, holdings: [{ ...wallet.balance_explanation!.holdings[0], value: null, observed_at: null }] } }
  const { user } = renderWithProviders(<BalanceDetails workspaceId="workspace" wallets={[broken]} holdingsError />)
  await user.click(screen.getByRole('button', { name: 'Balance details: Wallet A' }))
  expect(screen.getByRole('alert')).toHaveTextContent('holdings request failed')
  expect(screen.getByText('Example fund')).toBeInTheDocument()
  expect(screen.getByText('Quantity: 7')).toBeInTheDocument()
  expect(screen.getByText('Example fund').closest('li')).toHaveTextContent('Observed at: Unknown age')
  expect(screen.getByText('Quote or valuation unavailable')).toBeInTheDocument()
  const observation = screen.getByText('Observed at', { selector: 'dt' }).parentElement!
  expect(within(observation).getByRole('definition').textContent).toBe(new Date(observedAt).toLocaleString('en-US'))
  const refresh = screen.getByText('Latest refresh', { selector: 'dt' }).parentElement!
  expect(within(refresh).getAllByRole('definition').map((item) => item.textContent)).toEqual(['Refresh failed', new Date(refreshedAt).toLocaleString('en-US')])
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

it.each([
  { current_value: 80, current_value_primary: 80, last_price_at: '2026-01-02T00:00:00Z' },
  { current_value: 70, current_value_primary: 70, last_price_at: '2026-01-02T00:00:00Z' },
])('qualifies a successful changed holdings response independently of the retained source snapshot: %s', async (change) => {
  const original = { ...holding, last_price_at: '2026-01-01T00:00:00Z' }
  const source = reportedWallet(wallet, [original])
  source.balance_explanation!.holdings[0].observation_basis = 'quote'
  const { user } = renderWithProviders(<BalanceDetails workspaceId="workspace" wallets={[source]} holdings={[{ ...original, ...change }]} />)
  await user.click(screen.getByRole('button', { name: 'Balance details: Wallet A' }))
  const dialog = within(screen.getByRole('dialog'))
  expect(dialog.getByRole('status')).toHaveTextContent('current holdings differ from the retained source snapshot')
  expect(dialog.queryByRole('alert')).not.toBeInTheDocument()
  expect(dialog.queryByText(/\$50\.00/)).not.toBeInTheDocument()
  expect(dialog.queryByText('Complete for the stated scope')).not.toBeInTheDocument()
  expect(dialog.getByText('Successful refresh')).toBeInTheDocument()
  const row = dialog.getByText('Example fund').closest('li')!
  expect(row).toHaveTextContent(`$${change.current_value.toFixed(2)}`)
  expect(row).toHaveTextContent(new Date(change.last_price_at).toLocaleString('en-US'))
})


it.each([null, 'EUR'])('marks a rejected headline currency unavailable and incomplete: %s', (currency) => {
  const invalid = { ...account, balance_explanation: { ...account.balance_explanation!, currency } }
  renderWithProviders(<><span><AccountBalanceAmount account={invalid} /></span><AccountBalanceBasis account={invalid} /></>)
  expect(screen.getByText('Unavailable')).toBeInTheDocument()
  expect(screen.getByText('Partial or unverified coverage')).toBeInTheDocument()
  expect(screen.queryByText('$120.00')).not.toBeInTheDocument()
})
