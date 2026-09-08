import { beforeEach, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import PositionsTab from '@/components/positions-tab'
import { renderWithProviders } from '@/test/utils'
import { reportedWallet } from '@/test/balance-fixtures'
import { assets } from '@/lib/api'
import type { Asset, AssetGroup } from '@/types'

vi.mock('@/contexts/auth-context', () => ({ useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }) }))
const held = { id: 'scoped', group_id: 'wallet-a', ticker: 'SCOPED', name: 'Scoped fund', type: 'stock', units: 7, currency: 'USD', current_value: 70, current_value_primary: 70, gain_loss: null, gain_loss_primary: null, is_archived: false, sell_date: null } as Asset
const ungrouped = { ...held, id: 'ungrouped', group_id: null, ticker: 'FREE', name: 'Unassigned fund', current_value: 30, current_value_primary: 30 }
const wallet = reportedWallet({ id: 'wallet-a', name: 'Wallet A', currency: 'USD', account_balance: 70 } as AssetGroup, [held])
const ui = () => <PositionsTab workspaceId="workspace" holdings={[held, ungrouped]} wallets={[wallet]} currency="USD" locale="en-US" dateLocale="en-US" mask={(value) => value} canWrite={false} onClassify={vi.fn()} onOpenHolding={vi.fn()} />
beforeEach(() => { vi.restoreAllMocks(); vi.spyOn(assets, 'income').mockResolvedValue({ holdings: {}, wallets: {} }) })
it('full portfolio details include the unassigned holding counted in the overview', async () => {
  const { user } = renderWithProviders(ui())
  expect(within(screen.getByRole('region', { name: 'Balance overview' })).getAllByRole('definition')[0]).toHaveTextContent('$100.00')
  await user.click(within(screen.getByRole('region', { name: 'Balance overview' })).getByRole('button', { name: /Balance details:/ }))
  expect(within(screen.getByRole('dialog')).getByText('Unassigned fund')).toBeInTheDocument()
})
it('No wallet allocation details exclude holdings from other wallets', async () => {
  const { user } = renderWithProviders(ui())
  const chart = screen.getByRole('region', { name: 'Allocation by account' })
  await user.click(within(chart).getByRole('button', { name: /No wallet/ }))
  expect(within(screen.getByRole('region', { name: 'Balance overview' })).getAllByRole('definition')[0]).toHaveTextContent('$30.00')
  await user.click(screen.getByRole('button', { name: /Balance details:/ }))
  expect(within(screen.getByRole('dialog')).queryByText('Scoped fund')).not.toBeInTheDocument()
})
