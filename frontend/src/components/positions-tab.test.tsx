import { beforeEach, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import PositionsTab from './positions-tab'
import { renderWithProviders } from '@/test/utils'
import { assets } from '@/lib/api'
import type { Asset, TaxLots, WashSaleExposure } from '@/types'

const known = { id: 'known', ticker: 'KNOWN', name: 'Known fund', type: 'stock', units: 2, current_value: 125, current_value_primary: 125, gain_loss: 25, gain_loss_primary: 25, group_id: null, sell_date: null, is_archived: false } as Asset
const unknown = { ...known, id: 'unknown', ticker: 'UNKNOWN', name: 'Unpriced fund', current_value: null, current_value_primary: null, gain_loss: null, gain_loss_primary: null }

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(assets, 'income').mockResolvedValue({ holdings: {}, wallets: {} })
  vi.spyOn(assets, 'washSale').mockResolvedValue({ warning: false } as WashSaleExposure)
  vi.spyOn(assets, 'taxLots').mockResolvedValue({ no_wallet: true } as TaxLots)
})

const positions = (holdings: Asset[]) => <PositionsTab holdings={holdings} wallets={[]} currency="USD" locale="en-US" dateLocale="en-US" mask={(value) => value} canWrite={false} onClassify={vi.fn()} onOpenHolding={vi.fn()} />

it('keeps compact value and gain readable, with the remaining position details on expansion', async () => {
  const { user } = renderWithProviders(positions([known]))
  const row = screen.getByRole('button', { name: /^KNOWN/ })
  expect(within(row).getByText('$125.00')).toBeInTheDocument()
  expect(within(row).getAllByText('$25.00')).toHaveLength(2)
  expect(screen.getAllByRole('heading', { name: 'Positions by weight' })).toHaveLength(1)
  await user.click(row)
  expect(row).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getAllByRole('definition').map((element) => element.textContent)).toEqual(['2', '$50.00', '$100.00', '100.0%', '—'])
})

it('labels partial totals and never reports an unpriced position as zero', () => {
  renderWithProviders(positions([known, unknown]))
  const row = screen.getByRole('button', { name: /^UNKNOWN/ })
  expect(within(row).queryByText('$0.00')).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('Totals include known values only')
  expect(screen.getAllByText('Positions and cash')).toHaveLength(2)
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
