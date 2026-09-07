import { beforeEach, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import PositionsTab from './positions-tab'
import { renderWithProviders } from '@/test/utils'
import { assets } from '@/lib/api'
import type { Asset, AssetGroup, TaxLots, WashSaleExposure } from '@/types'

const known = { id: 'known', ticker: 'KNOWN', name: 'Known fund', type: 'stock', units: 2, current_value: 125, current_value_primary: 125, gain_loss: 25, gain_loss_primary: 25, group_id: null, sell_date: null, is_archived: false } as Asset
const unknown = { ...known, id: 'unknown', ticker: 'UNKNOWN', name: 'Unpriced fund', current_value: null, current_value_primary: null, gain_loss: null, gain_loss_primary: null }

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(assets, 'income').mockResolvedValue({ holdings: {}, wallets: {} })
  vi.spyOn(assets, 'washSale').mockResolvedValue({ warning: false } as WashSaleExposure)
  vi.spyOn(assets, 'taxLots').mockResolvedValue({ no_wallet: true } as TaxLots)
})

const positions = (holdings: Asset[], wallets: AssetGroup[] = []) => <PositionsTab holdings={holdings} wallets={wallets} currency="USD" locale="en-US" dateLocale="en-US" mask={(value) => value} canWrite={false} onClassify={vi.fn()} onOpenHolding={vi.fn()} />

it('keeps compact value and gain readable, with the remaining position details on expansion', async () => {
  const { user } = renderWithProviders(positions([known]))
  const row = screen.getByRole('button', { name: /^KNOWN/ })
  expect(within(row).getByText('$125.00')).toBeInTheDocument()
  expect(within(row).getAllByText('$25.00')).toHaveLength(2)
  expect(screen.getAllByRole('heading', { name: 'Positions by weight' })).toHaveLength(1)
  await user.click(row)
  expect(row).toHaveAttribute('aria-expanded', 'true')
  expect(within(row.parentElement!).getAllByRole('definition').map((element) => element.textContent)).toEqual(['2', '$50.00', '$100.00', '100.0%', '—'])
})

it('labels partial totals and never reports an unpriced position as zero', () => {
  renderWithProviders(positions([known, unknown]))
  const row = screen.getByRole('button', { name: /^UNKNOWN/ })
  expect(within(row).queryByText('$0.00')).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('Totals include known values only')
  expect(screen.getAllByText('Positions and cash')).toHaveLength(1)
  expect(screen.getAllByText('Known holdings subtotal').length).toBeGreaterThan(0)
})

it('shows unknown totals when none of the positions has a valuation', () => {
  renderWithProviders(positions([unknown]))
  expect(screen.queryByText('$0.00')).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toBeInTheDocument()
})

it('does not hide a partly unpriced position as dust based on its known fraction', () => {
  renderWithProviders(positions([unknown, { ...known, ticker: 'UNKNOWN', current_value: 0.2, current_value_primary: 0.2 }]))
  expect(screen.getByRole('button', { name: /^UNKNOWN/ })).toBeInTheDocument()
  expect(screen.getByRole('status')).toBeInTheDocument()
})

it.each([
  ['manual asset', { ...unknown, ticker: null }, true],
  ['ticker position', unknown, false],
])('only blocks allocation when the unpriced holding is an active ticker position: %s', (_kind, holding, showsAllocation) => {
  renderWithProviders(positions([known, holding]))
  expect(screen.queryByRole('region', { name: 'Allocation by asset class' }) !== null).toBe(showsAllocation)
  expect(screen.queryByRole('region', { name: 'Allocation by account' }) !== null).toBe(showsAllocation)
  expect(screen.queryByRole('region', { name: 'Allocation by account type' })).not.toBeInTheDocument()
  expect(screen.queryByRole('status') !== null).toBe(!showsAllocation)
})


it('keeps one scoped balance above allocation, with cash details and income outside the total', async () => {
  const wallet: AssetGroup = { id: 'wallet', user_id: 'user', name: 'Brokerage', icon: 'wallet', color: '#6366f1', position: 0, tax_treatment: 'taxable', source: 'manual', connection_id: null, institution_name: null, account_type: 'investment', asset_count: 4, current_value: 200.5, current_value_primary: 200.5, account_balance: 300, currency: 'USD' }
  const held = { ...known, group_id: wallet.id }
  const cash = { ...held, id: 'cash', ticker: 'CASH', type: 'cash_equivalent', current_value: 25, current_value_primary: 25 }
  const dust = { ...held, id: 'dust', ticker: 'SMALL', current_value: 0.5, current_value_primary: 0.5 }
  const manual = { ...held, id: 'manual', ticker: null, current_value: 50, current_value_primary: 50 }
  vi.mocked(assets.income).mockResolvedValue({ holdings: {}, wallets: { wallet: { total: 12, payouts: 12, cadence: 'monthly', run_rate: 15.6, last_date: null, last_amount: null, currency: 'USD' } } })
  const { user } = renderWithProviders(<PositionsTab holdings={[held, cash, dust, manual]} wallets={[wallet]} currency="USD" locale="en-US" dateLocale="en-US" mask={(value) => value} canWrite={false} onClassify={vi.fn()} onOpenHolding={vi.fn()} />)
  const summary = screen.getByRole('region', { name: 'Balance overview' })
  expect(within(summary).getAllByRole('definition').map((element) => element.textContent)).toEqual(['$250.00', '$125.00', '$124.50'])
  const allocation = screen.getByRole('region', { name: 'Allocation breakdown' })
  expect(summary.compareDocumentPosition(allocation) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(await within(summary).findByText('$12.00', { selector: 'p span' })).toBeInTheDocument()
  const disclosure = within(summary).getByText('Balance details').closest('details')!
  expect(disclosure).not.toHaveAttribute('open')
  await user.click(within(summary).getByText('Balance details'))
  expect(disclosure).toHaveAttribute('open')
  expect(within(disclosure).getByText('$25.00')).toBeInTheDocument()
  expect(within(disclosure).getByText('$99.50')).toBeInTheDocument()
  expect(within(disclosure).getByText('$0.50')).toBeInTheDocument()
  expect(within(disclosure).getByText(/15.60\/yr/)).toBeInTheDocument()
  await user.click(within(screen.getByRole('region', { name: 'Allocation by asset class' })).getByRole('button', { name: /Stock/ }))
  expect(within(summary).getAllByRole('definition').map((element) => element.textContent)).toEqual(['$125.50', '$125.00', '$0.00'])
  expect(within(summary).queryByText('Income received (12m)')).not.toBeInTheDocument()
})

it.each([
  ['unpriced manual asset', 1000, true, 0, ['$125.00', '$125.00', '—']],
  ['missing account balance', null, false, 0, ['$125.00', '$125.00', '—']],
  ['known equivalent and unpriced manual asset', 1000, true, 25, ['$150.00', '$125.00', '$25.00']],
] as const)('labels incomplete balance and cash without hiding priced allocation: %s', (_name, balance, unpricedManual, equivalent, expected) => {
  const wallet = { id: 'wallet', name: 'Brokerage', account_balance: balance } as AssetGroup
  const held = { ...known, group_id: wallet.id }
  const holdings = [held]
  if (unpricedManual) holdings.push({ ...unknown, ticker: null, group_id: wallet.id })
  if (equivalent) holdings.push({ ...held, id: 'cash', ticker: 'CASH', type: 'cash_equivalent', current_value: equivalent, current_value_primary: equivalent })
  renderWithProviders(positions(holdings, [wallet]))

  const summary = screen.getByRole('region', { name: 'Balance overview' })
  expect(within(summary).getAllByRole('definition').map((element) => element.textContent)).toEqual(expected)
  expect(within(summary).getAllByText('Known subtotal')).toHaveLength(2)
  expect(within(summary).getByRole('status')).toHaveTextContent('Some wallet cash balances are unavailable')
  expect(screen.getByRole('region', { name: 'Allocation by asset class' })).toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'Allocation by account' })).toBeInTheDocument()
})

it('marks positive partial cash and clears its uncertainty when narrowing to a complete wallet or asset class', async () => {
  const partial = { id: 'partial', name: 'Partial account', account_balance: 1000 } as AssetGroup
  const complete = { id: 'complete', name: 'Complete account', account_balance: 575 } as AssetGroup
  const { user } = renderWithProviders(positions([
    { ...known, group_id: partial.id },
    { ...unknown, ticker: null, group_id: partial.id },
    { ...known, id: 'second', ticker: 'SECOND', group_id: complete.id, current_value: 75, current_value_primary: 75 },
  ], [partial, complete]))
  const summary = screen.getByRole('region', { name: 'Balance overview' })
  const values = () => within(summary).getAllByRole('definition').map((element) => element.textContent)
  expect(values()).toEqual(['$700.00', '$200.00', '$500.00'])
  expect(within(summary.querySelector('dl')!).getAllByText('Known subtotal')).toHaveLength(2)
  await user.click(within(summary).getByText('Balance details'))
  expect(within(summary).getAllByText('Known subtotal')).toHaveLength(3)
  expect(within(summary).queryByText(/%/)).not.toBeInTheDocument()

  await user.click(within(screen.getByRole('region', { name: 'Allocation by account' })).getByRole('button', { name: /Complete account/ }))
  expect(values()).toEqual(['$575.00', '$75.00', '$500.00'])
  expect(within(summary).queryByRole('status')).not.toBeInTheDocument()
  expect(within(summary).queryByText('Known subtotal')).not.toBeInTheDocument()
  await user.click(within(screen.getByRole('region', { name: 'Allocation by asset class' })).getByRole('button', { name: /Stock/ }))
  expect(values()).toEqual(['$200.00', '$200.00', '$0.00'])
  expect(within(summary).queryByRole('status')).not.toBeInTheDocument()
})

it.each([500, 0, null])('keeps cash-only balances distinct from unavailable cash: %s', (balance) => {
  renderWithProviders(positions([], [{ id: 'wallet', account_balance: balance } as AssetGroup]))
  const summary = screen.getByRole('region', { name: 'Balance overview' })
  const cash = balance === null ? '—' : `$${balance.toFixed(2)}`
  expect(within(summary).getAllByRole('definition').map((element) => element.textContent)).toEqual([cash, '$0.00', cash])
  expect(within(summary).queryByRole('status') !== null).toBe(balance === null)
  expect(screen.queryByRole('region', { name: 'Allocation breakdown' })).not.toBeInTheDocument()
  expect(screen.queryByRole('region', { name: 'Positions by weight' })).not.toBeInTheDocument()
})
