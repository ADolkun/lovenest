import { reportedWallet } from '@/test/balance-fixtures'
import { beforeEach, expect, it, vi } from 'vitest'
import { act, screen, within } from '@testing-library/react'
import PositionsTab from './positions-tab'
import { renderWithProviders } from '@/test/utils'
import { assets } from '@/lib/api'
import type { Asset, AssetGroup, TaxLots, WashSaleExposure } from '@/types'

vi.mock('@/contexts/auth-context', () => ({ useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }) }))

const known = { id: 'known', ticker: 'KNOWN', name: 'Known fund', type: 'stock', currency: 'USD', units: 2, current_value: 125, current_value_primary: 125, gain_loss: 25, gain_loss_primary: 25, group_id: null, sell_date: null, is_archived: false } as Asset
const unknown = { ...known, id: 'unknown', ticker: 'UNKNOWN', name: 'Unpriced fund', current_value: null, current_value_primary: null, gain_loss: null, gain_loss_primary: null }

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(assets, 'income').mockResolvedValue({ holdings: {}, wallets: {} })
  vi.spyOn(assets, 'washSale').mockResolvedValue({ warning: false } as WashSaleExposure)
  vi.spyOn(assets, 'taxLots').mockResolvedValue({ no_wallet: true } as TaxLots)
})

const positions = (holdings: Asset[], wallets: AssetGroup[] = []) => <PositionsTab holdings={holdings} wallets={wallets.map((wallet) => reportedWallet(wallet, holdings))} currency="USD" locale="en-US" dateLocale="en-US" mask={(value) => value} canWrite={false} onClassify={vi.fn()} onOpenHolding={vi.fn()} />

const quantityReport = (overrides: Partial<TaxLots> = {}): TaxLots => ({
  asset_id: 'known', ticker: 'KNOWN', tax_character: true, snapshot: false, no_wallet: false,
  as_of: '2026-01-01', lots: [], sales: [], long_quantity: '0', short_quantity: '0',
  long_cost: '0', short_cost: '0', realised_long: '0', realised_short: '0',
  qualification: { scope: 'latest_reported_vs_recorded', comparison: 'mismatch',
    quantity_supported: false, reported_quantity: '0', stored_quantity: '0',
    replayed_quantity: '12345678901234567890.123456789012345678',
    discrepancy: '12345678901234567890.123456789012345678', tolerance: '0',
    collected_at: '2026-01-01T00:00:00Z', reason_codes: ['lifetime_history_unverified'] },
  ...overrides,
})

it.each([false, true])('shows exact quantity qualification before an empty or snapshot hint: snapshot=%s', async (snapshot) => {
  vi.mocked(assets.taxLots).mockResolvedValue(quantityReport({ snapshot }))
  const { user } = renderWithProviders(positions([known]))
  await user.click(screen.getByRole('button', { name: /^KNOWN/ }))
  expect(await screen.findByText('Recorded lots differ from the reported quantity')).toBeInTheDocument()
  const values = screen.getByText('Reported quantity').parentElement!
  expect(within(values).getByText('0')).toBeInTheDocument()
  expect(screen.getAllByText('12345678901234567890.123456789012345678')).toHaveLength(2)
  expect(screen.getByText(/A match does not establish current ownership/)).toBeInTheDocument()
  expect(screen.getByText(snapshot ? /Imported position with no trades/ : /No lots — no trades/)).toBeInTheDocument()
})

it('keeps unavailable quantity separate from zero and masks exact quantities in privacy mode', async () => {
  const report = quantityReport()
  report.qualification = { ...report.qualification!, comparison: 'not_comparable', reported_quantity: null, discrepancy: null, tolerance: null }
  vi.mocked(assets.taxLots).mockResolvedValue(report)
  const { user } = renderWithProviders(<PositionsTab holdings={[known]} wallets={[]} currency="USD" locale="en-US" dateLocale="en-US" mask={() => '•••••'} canWrite={false} onClassify={vi.fn()} onOpenHolding={vi.fn()} />)
  await user.click(screen.getByRole('button', { name: /^KNOWN/ }))
  expect(await screen.findByText('Reported quantity cannot be compared')).toBeInTheDocument()
  expect(within(screen.getByText('Reported quantity').parentElement!).getByText('Unknown')).toBeInTheDocument()
  expect(within(screen.getByText('Stored holding quantity').parentElement!).getByText('•••••')).toBeInTheDocument()
  expect(screen.queryByText(/12345678901234567890/)).not.toBeInTheDocument()
})

it('keeps a delayed tax-lot response in its originating workspace and supports retry', async () => {
  let finish!: (value: TaxLots) => void
  vi.mocked(assets.taxLots).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    .mockRejectedValueOnce(new Error('Synthetic request failure')).mockResolvedValue(quantityReport())
  const view = (workspaceId: string) => <PositionsTab workspaceId={workspaceId} holdings={[known]} wallets={[]} currency="USD" locale="en-US" dateLocale="en-US" mask={value => value} canWrite={false} onClassify={vi.fn()} onOpenHolding={vi.fn()} />
  const { user, rerender, queryClient } = renderWithProviders(view('workspace-a'))
  await user.click(screen.getByRole('button', { name: /^KNOWN/ }))
  rerender(view('workspace-b'))
  await act(async () => finish(quantityReport({ ticker: 'PRIVATE-OLD' })))
  expect(await screen.findByRole('button', { name: 'Retry' })).toBeInTheDocument()
  expect(screen.queryByText('Recorded lots differ from the reported quantity')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Retry' }))
  expect(await screen.findByText('Recorded lots differ from the reported quantity')).toBeInTheDocument()
  expect(queryClient.getQueryData(['asset-tax-lots', 'workspace-b', 'known'])).toEqual(quantityReport())
})

it('shows nullable transferred lot dates and costs beside a supported zero in the holding currency without fabricating gains', async () => {
  vi.mocked(assets.taxLots).mockResolvedValue({
    asset_id: 'known', ticker: 'KNOWN', tax_character: true, snapshot: false, no_wallet: false, as_of: '2026-01-01',
    lots: [
      { lot_id: 'unknown-lineage', acquired: null, quantity: '1.000000000000000001', unit_price: null, cost: null, holding_days: null, long_term: null, days_until_long_term: null, lineage: ['transfer-a'], missing_links: ['acquisition_missing'] },
      { lot_id: 'explicit-zero', acquired: '2024-01-01', quantity: '1', unit_price: '0', cost: '0', holding_days: 731, long_term: true, days_until_long_term: 0 },
    ],
    long_quantity: '1', short_quantity: '0', long_cost: '0', short_cost: '0', sales: [{ date: '2025-01-01', quantity: '1', gain: null, long_quantity: '0', short_quantity: '0', long_gain: null, short_gain: null }], realised_long: null, realised_short: null,
    basis_complete: false, settlement_complete: false, known_acquisition_cost: '0', unknown_basis_quantity: '1.000000000000000001', unknown_disposition_quantity: '1', missing_links: ['transfer_evidence_invalidated'],
  })
  const { user } = renderWithProviders(positions([{ ...known, currency: 'EUR' }]))
  await user.click(screen.getByRole('button', { name: /^KNOWN/ }))
  const unknownDate = await screen.findByText('Acquisition date unknown')
  const lot = within(unknownDate.parentElement!)
  expect(lot.getByText('Holding period unknown')).toBeInTheDocument()
  expect(lot.getAllByText('Unknown')).toHaveLength(2)
  expect(lot.getByText('1.000000000000000001')).toBeInTheDocument()
  expect(lot.queryByText(/^Short/)).not.toBeInTheDocument()
  expect(screen.getAllByText('0 EUR').length).toBeGreaterThan(0)
  expect(screen.getByText('Long-term Unknown')).toBeInTheDocument()
  expect(screen.getByText('Short-term Unknown')).toBeInTheDocument()
  expect(screen.getByText('Movement settlement unresolved')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Open transfer' })).toHaveAttribute('href', expect.stringContaining('transfer=transfer-a'))
  expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument()
})

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
  const { user } = renderWithProviders(<PositionsTab holdings={[held, cash, dust, manual]} wallets={[reportedWallet(wallet, [held, cash, dust, manual])]} currency="USD" locale="en-US" dateLocale="en-US" mask={(value) => value} canWrite={false} onClassify={vi.fn()} onOpenHolding={vi.fn()} />)
  const summary = screen.getByRole('region', { name: 'Balance overview' })
  expect(within(summary).getAllByRole('definition').map((element) => element.textContent)).toEqual(['$250.00', '$125.00', '$124.50'])
  const allocation = screen.getByRole('region', { name: 'Allocation breakdown' })
  expect(summary.compareDocumentPosition(allocation) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(await within(summary).findByText('$12.00', { selector: 'p span' })).toBeInTheDocument()
  const disclosure = within(summary).getByText('Portfolio breakdown').closest('details')!
  expect(disclosure).not.toHaveAttribute('open')
  await user.click(within(summary).getByText('Portfolio breakdown'))
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
  await user.click(within(summary).getByText('Portfolio breakdown'))
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
