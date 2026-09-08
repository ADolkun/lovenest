import { reportedWallet } from '@/test/balance-fixtures'
import { beforeEach, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import AssetsPage from './assets'
import { renderWithProviders, t } from '@/test/utils'
import { assets, assetGroups, contributions, currencies, onchain } from '@/lib/api'
import type { Asset, AssetGroup, AssetTransaction } from '@/types'

const scope = vi.hoisted(() => ({ activeWalletIds: null as string[] | null, onchainEnabled: false, canWrite: false }))
vi.mock('@/contexts/auth-context', () => ({ useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }) }))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: () => ({ current: { id: 'workspace', name: 'Investments' }, canWrite: scope.canWrite, hasModule: () => true }) }))
vi.mock('@/contexts/collection-filter-context', () => ({ useCollectionFilter: () => scope }))
vi.mock('@/hooks/use-feature-flags', () => ({ useFeatureFlags: () => ({ onchainEnabled: scope.onchainEnabled }) }))
vi.mock('@/lib/page-chat-context', () => ({ useRegisterPageChatContext: () => undefined }))

const held = { id: 'held', name: 'Private fund', type: 'other', source: 'manual', currency: 'USD', group_id: 'wallet-a', current_value: 125, current_value_primary: 125, ticker: null, sell_date: null, is_archived: false } as Asset
const retired = { ...held, id: 'retired', name: 'Closed holding', is_archived: true }
const outside = { ...held, id: 'outside', name: 'Other account fund', group_id: 'wallet-b' }
const wallets = ['a', 'b'].map((name) => ({ id: `wallet-${name}`, name: `Wallet ${name.toUpperCase()}`, color: '#6366f1', position: 0, source: 'manual', tax_treatment: 'taxable', current_value: 9000, current_value_primary: 9000, unvalued_count: 0 })) as AssetGroup[]

it('renders movement and fee quantities without trade price math or ordinary edits', async () => {
  scope.canWrite = true
  vi.spyOn(assets, 'allTransactions').mockResolvedValue((['move_in', 'move_out', 'fee'] as const).map((kind) => ({
    id: kind, asset_id: 'retired', asset_name: `Movement ${kind}`, kind, quantity: kind === 'fee' ? 1e-18 : 9007199254740994, quantity_exact: kind === 'fee' ? '0.000000000000000001' : '9007199254740993.123456789012345678', price: null, fee: 0, currency: 'USD', date: '2026-01-01', source: 'manual',
  })) as AssetTransaction[])
  renderWithProviders(<AssetsPage />, { route: '/assets?tab=activity&wallet=wallet-a' })
  for (const [kind, label] of [['move_in', 'Transfer in'], ['move_out', 'Transfer out'], ['fee', 'Fee units']]) {
    const title = await screen.findByText(`Movement ${kind}`)
    const row = within(title.parentElement!.parentElement!)
    expect(row.getByText(label)).toBeInTheDocument()
    expect(row.getByText(kind === 'fee' ? '0.000000000000000001' : '9007199254740993.123456789012345678', { exact: false })).toBeInTheDocument()
    expect(row.getByText('No sale gain')).toBeInTheDocument()
    expect(row.queryByText(/×|\$0\.00/)).not.toBeInTheDocument()
    expect(row.queryByTitle(t('common.edit'))).not.toBeInTheDocument()
    expect(row.queryByTitle(t('common.delete'))).not.toBeInTheDocument()
  }
})

beforeEach(() => {
  vi.restoreAllMocks()
  scope.activeWalletIds = null
  scope.onchainEnabled = false
  scope.canWrite = false
  vi.spyOn(assets, 'list').mockImplementation(async (archived) => archived ? [held, retired, outside] : [held, outside])
  vi.spyOn(assets, 'portfolioTrend').mockResolvedValue({ assets: [], trend: [], total: 0 })
  vi.spyOn(assets, 'values').mockResolvedValue([])
  vi.spyOn(assets, 'valueTrend').mockResolvedValue([])
  vi.spyOn(assetGroups, 'list').mockResolvedValue(wallets)
  vi.spyOn(contributions, 'summary').mockResolvedValue([])
  vi.spyOn(currencies, 'list').mockResolvedValue([])
  vi.spyOn(assets, 'allTransactions').mockResolvedValue([
    { id: 'visible', asset_id: 'retired', asset_name: 'Archived trade', kind: 'buy', quantity: 1, price: 10, fee: 0, currency: 'USD', date: '2026-01-01', source: 'manual' },
    { id: 'hidden', asset_id: 'outside', asset_name: 'Outside trade', kind: 'buy', quantity: 1, price: 20, fee: 0, currency: 'USD', date: '2026-01-01', source: 'manual' },
  ] as AssetTransaction[])
})

it.each(['/assets', '/assets?wallet=wallet-a'])('retains expanded holding details, history, and management in %s', async (route) => {
  scope.canWrite = true
  vi.spyOn(assets, 'list').mockResolvedValue([{ ...held, units: 3, average_price: 25, last_price: 40, total_invested: 75, gain_loss: 50, value_count: 1, realized_gain: 10 }])
  const { user } = renderWithProviders(<AssetsPage />, { route })
  const holding = await screen.findByRole('button', { name: 'Private fund' })
  expect(holding).toHaveAttribute('aria-expanded', 'false')
  expect(within(holding.closest('.grid')!).getByText('$125.00')).toBeInTheDocument()
  expect(within(holding.closest('.grid')!).getByText('+$50.00')).toBeInTheDocument()
  expect(within(holding.closest('.grid')!).getByText('+66.7%')).toBeInTheDocument()
  expect(screen.queryByRole('definition')).not.toBeInTheDocument()
  await user.click(holding)
  expect(holding).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getAllByRole('definition').map((element) => element.textContent)).toEqual(['3', '$25.00', '$40.00', '+$50.00+66.7%', '$10.00', '100.0%100.0% invested'])
  expect(screen.getByText(t('assets.valueHistoryHint'))).toBeInTheDocument()
  expect(screen.getByRole('button', { name: t('common.delete') })).toBeEnabled()
  await user.click(screen.getByRole('button', { name: t('assets.moveToWallet') }))
  expect(screen.getByRole('dialog', { name: t('assets.moveToWallet') })).toBeInTheDocument()
  await user.keyboard('{Escape}')
  await user.click(screen.getByRole('button', { name: t('common.edit') }))
  expect(screen.getByRole('dialog', { name: t('assets.editAsset') })).toBeInTheDocument()
})

it('keeps a saved own address visible after its holding moves into a manual wallet', async () => {
  scope.onchainEnabled = true
  vi.spyOn(assets, 'list').mockResolvedValue([{ ...held, source: 'onchain', external_id: 'solana:owned-address', connection_id: 'saved-connection' }])
  vi.spyOn(onchain, 'chains').mockResolvedValue([{ key: 'solana', display_name: 'Solana', symbol: 'SOL', kind: 'solana', traceable: true }])
  vi.spyOn(onchain, 'addresses').mockResolvedValue([
    { chain: 'solana', address: 'owned-address', label: 'My Solana address', connection_id: 'saved-connection', connection_name: 'My connected wallet' },
    { chain: 'solana', address: 'outside-address', label: 'Outside address', connection_id: 'other-connection', connection_name: 'Another wallet' },
  ])
  const { user } = renderWithProviders(<AssetsPage />, { route: '/assets?tab=activity&activity=wallets&wallet=wallet-a' })
  await user.click(await screen.findByRole('combobox', { name: t('trace.wallet') }))
  expect(await screen.findByRole('option', { name: 'My connected wallet · My Solana address' })).toBeInTheDocument()
  expect(screen.queryByRole('option', { name: /Outside address/ })).not.toBeInTheDocument()
})

it('retains trace cooldown when the outer wallet scope remounts the result form', async () => {
  scope.onchainEnabled = true
  vi.spyOn(assets, 'list').mockResolvedValue([
    { ...held, source: 'onchain', external_id: 'solana:owned-address' },
    { ...outside, source: 'onchain', external_id: 'solana:outside-address' },
  ])
  vi.spyOn(onchain, 'chains').mockResolvedValue([{ key: 'solana', display_name: 'Solana', symbol: 'SOL', kind: 'solana', traceable: true }])
  vi.spyOn(onchain, 'addresses').mockResolvedValue([
    { chain: 'solana', address: 'owned-address', label: 'My Solana address', connection_id: 'first', connection_name: 'My connected wallet' },
    { chain: 'solana', address: 'outside-address', label: 'Other Solana address', connection_id: 'second', connection_name: 'Other connected wallet' },
  ])
  const trace = vi.spyOn(onchain, 'trace').mockRejectedValue({
    response: { data: { detail: { code: 'trace_admission_limited', retry_after_seconds: 60 } } },
  })
  const { user } = renderWithProviders(<AssetsPage />, {
    route: '/assets?tab=activity&activity=wallets&chain=solana&address=owned-address',
  })
  await screen.findByRole('combobox', { name: 'Your wallet' })
  await user.click(screen.getByRole('button', { name: 'Explore transfers' }))
  await screen.findByText('This server has reached its trace request limit. Wait before retrying.')
  expect(screen.getByRole('button', { name: 'Retry' })).toBeDisabled()
  await user.selectOptions(screen.getByRole('combobox', { name: 'Wallet' }), 'wallet-b')
  await user.click(screen.getByRole('combobox', { name: 'Your wallet' }))
  expect(screen.queryByRole('option', { name: 'My connected wallet · My Solana address' })).not.toBeInTheDocument()
  await user.click(screen.getByRole('option', { name: 'Other connected wallet · Other Solana address' }))
  const submit = screen.getByRole('button', { name: 'Explore transfers' })
  expect(submit).toBeDisabled()
  expect(screen.getByRole('status')).toHaveTextContent(/Retry available in/)
  fireEvent.submit(submit.closest('form')!)
  expect(trace).toHaveBeenCalledOnce()
})

it('opens account wallet links, uses current holdings instead of stale rollups, and clears the filter', async () => {
  const { user } = renderWithProviders(<AssetsPage />, { route: '/assets?wallet=wallet-a' })
  expect(await screen.findByRole('button', { name: 'Private fund' })).toBeInTheDocument()
  expect(screen.getByRole('tab', { name: 'Portfolio' })).toHaveAttribute('aria-selected', 'true')
  expect(screen.queryByRole('button', { name: 'Other account fund' })).not.toBeInTheDocument()
  expect(screen.queryByText('$9,000.00')).not.toBeInTheDocument()
  expect(screen.getByText('Holdings value')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Clear wallet filter' }))
  expect(await screen.findByRole('button', { name: /^Wallet B/ })).toBeInTheDocument()
})

it('keeps archived trades within the selected collection on a legacy Activity link', async () => {
  scope.activeWalletIds = ['wallet-a']
  const { user } = renderWithProviders(<AssetsPage />, { route: '/assets?tab=transactions' })
  expect(await screen.findByText('Archived trade')).toBeInTheDocument()
  expect(screen.queryByText('Outside trade')).not.toBeInTheDocument()
  expect(screen.getByRole('tab', { name: 'Activity' })).toHaveAttribute('aria-selected', 'true')
  await user.click(screen.getByRole('tab', { name: 'Portfolio' }))
  await user.click(screen.getByRole('button', { name: 'By wallet' }))
  await waitFor(() => expect(screen.getByRole('button', { name: /^Wallet A/ })).toBeInTheDocument())
  expect(screen.queryByText('Wallet B')).not.toBeInTheDocument()
})

it('keeps allocation above holdings across groupings and routes chart selections into the matching positions', async () => {
  const stock = { ...held, id: 'stock', name: 'Example stock', ticker: 'STOCK', type: 'stock', units: 2, gain_loss: 25, gain_loss_primary: 25 }
  const coin = { ...held, id: 'coin', name: 'Example coin', ticker: 'COIN', type: 'crypto', units: 1, current_value: 75, current_value_primary: 75, gain_loss: 5, gain_loss_primary: 5, group_id: 'wallet-b' }
  const dust = { ...coin, id: 'dust', name: 'Small coin balance', ticker: 'SMALL', current_value: 0.1, current_value_primary: 0.1 }
  const closed = { ...stock, id: 'closed', name: 'Sold example', ticker: 'SOLD', sell_date: '2026-01-01' }
  vi.spyOn(assets, 'list').mockResolvedValue([stock, coin, held, dust, closed])
  vi.spyOn(assets, 'income').mockResolvedValue({ holdings: {}, wallets: {} })
  const { user } = renderWithProviders(<AssetsPage />)
  const allocation = await screen.findByRole('region', { name: t('assets.allocationBreakdown') })
  const holdingsHeading = screen.getByRole('heading', { name: t('accountHoldings.holdings') })
  expect(allocation.compareDocumentPosition(holdingsHeading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(screen.getByRole('button', { name: /^STOCK/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Private fund' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /^SMALL/ })).not.toBeInTheDocument()

  const accountChart = screen.getByRole('region', { name: t('assets.posAllocationByAccount') })
  await user.click(within(accountChart).getByRole('button', { name: /^Wallet A/ }))
  expect(screen.getByRole('button', { name: /^STOCK/ })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /^COIN/ })).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'By wallet' }))
  expect(screen.getByRole('region', { name: t('assets.allocationBreakdown') })).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: t('assets.posRanking') })).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: /^Wallet B/, expanded: false }))
  expect(screen.getByRole('button', { name: 'SMALL' })).toBeInTheDocument()

  await user.click(within(accountChart).getByRole('button', { name: /^Wallet B/ }))
  expect(screen.getByRole('button', { name: 'By asset' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByRole('button', { name: /^COIN/ })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /^STOCK/ })).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: /Clear filter/ }))
  expect(screen.getByRole('button', { name: /^STOCK/ })).toBeInTheDocument()
  await user.click(screen.getByText(`${t('assets.soldAssets')} (1)`))
  expect(screen.getByRole('button', { name: 'SOLD' })).toBeInTheDocument()
})

it('keeps native wallet and activity selectors connected to their scoped views', async () => {
  scope.onchainEnabled = true
  vi.spyOn(onchain, 'chains').mockResolvedValue([])
  vi.spyOn(onchain, 'addresses').mockResolvedValue([])
  const { user } = renderWithProviders(<AssetsPage />, { route: '/assets?tab=activity&activity=wallets' })
  const activity = await screen.findByRole('combobox', { name: t('assets.activityType') })
  await user.selectOptions(activity, 'trades')
  expect(await screen.findByText('Archived trade')).toBeInTheDocument()
  const wallet = screen.getByRole('combobox', { name: t('assets.wallet') })
  await user.selectOptions(wallet, 'wallet-b')
  expect(wallet).toHaveValue('wallet-b')
  expect(activity).toHaveValue('trades')
  expect(screen.getByText('Outside trade')).toBeInTheDocument()
  expect(screen.queryByText('Archived trade')).not.toBeInTheDocument()
})

it.each([500, 0])('shows a cash-only wallet balance through the selector, both groupings, and direct account links: %s', async (balance) => {
  const stock = { ...held, ticker: 'STOCK', type: 'stock', units: 2, gain_loss: 25, gain_loss_primary: 25 }
  vi.spyOn(assets, 'list').mockResolvedValue([stock])
  vi.spyOn(assets, 'income').mockResolvedValue({ holdings: {}, wallets: {} })
  vi.mocked(assetGroups.list).mockResolvedValue([
    reportedWallet({ ...wallets[0], account_balance: 125 }, [stock]),
    reportedWallet({ ...wallets[1], account_balance: balance, current_value: 0, current_value_primary: 0, asset_count: 0 }),
  ])
  const { user, unmount } = renderWithProviders(<AssetsPage />)
  await screen.findByRole('region', { name: 'Balance overview' })
  await user.selectOptions(screen.getByRole('combobox', { name: 'Wallet' }), 'wallet-b')
  const expected = [`$${balance.toFixed(2)}`, '$0.00', `$${balance.toFixed(2)}`]
  const summary = screen.getByRole('region', { name: 'Balance overview' })
  expect(within(summary).getAllByRole('definition').map((element) => element.textContent)).toEqual(expected)
  expect(screen.queryByRole('region', { name: 'Allocation breakdown' })).not.toBeInTheDocument()
  expect(screen.queryByText(t('assets.noAssets'))).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'By wallet' }))
  expect(within(summary).getAllByRole('definition').map((element) => element.textContent)).toEqual(expected)
  expect(screen.getByRole('button', { name: /^Wallet B/ })).toBeInTheDocument()
  unmount()

  renderWithProviders(<AssetsPage />, { route: '/assets?wallet=wallet-b' })
  const linkedSummary = await screen.findByRole('region', { name: 'Balance overview' })
  expect(within(linkedSummary).getAllByRole('definition').map((element) => element.textContent)).toEqual(expected)
})

it('does not treat a failed holdings request as an empty cash-only wallet', async () => {
  vi.mocked(assets.list).mockRejectedValue(new Error('Holdings unavailable'))
  vi.mocked(assetGroups.list).mockResolvedValue([{ ...wallets[0], account_balance: 1000 }])
  renderWithProviders(<AssetsPage />, { route: '/assets?wallet=wallet-a' })
  expect(await screen.findByText(t('assets.loadError'))).toBeInTheDocument()
  expect(screen.queryByRole('region', { name: 'Balance overview' })).not.toBeInTheDocument()
})

it.each([125, null])('keeps manual assets outside the cash balance and visible in both groupings: %s', async (value) => {
  vi.mocked(assets.list).mockResolvedValue([{ ...held, current_value: value, current_value_primary: value }])
  vi.spyOn(assets, 'income').mockResolvedValue({ holdings: {}, wallets: {} })
  vi.mocked(assetGroups.list).mockResolvedValue([reportedWallet({ ...wallets[0], account_balance: 500 }, [{ ...held, current_value: value, current_value_primary: value }])])
  const { user } = renderWithProviders(<AssetsPage />, { route: '/assets?wallet=wallet-a&view=assets' })
  const summary = await screen.findByRole('region', { name: 'Balance overview' })
  const cash = value === null ? '—' : '$375.00'
  expect(within(summary).getAllByRole('definition').map((element) => element.textContent)).toEqual([cash, '$0.00', cash])
  expect(within(summary).queryByRole('status') !== null).toBe(value === null)
  expect(screen.getByRole('button', { name: 'Private fund' })).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'By wallet' }))
  expect(within(summary).getAllByRole('definition').map((element) => element.textContent)).toEqual([cash, '$0.00', cash])
  expect(screen.getByRole('button', { name: 'Private fund' })).toBeInTheDocument()
})

it('keeps the wallet header complete for native same-currency values without a converted field', async () => {
  vi.mocked(assets.list).mockResolvedValue([{ ...held, current_value: 70, current_value_primary: null }])
  renderWithProviders(<AssetsPage />, { route: '/assets?wallet=wallet-a&view=wallets&holding=held' })
  const header = await screen.findByRole('button', { name: /^Wallet A/ })
  const section = within(header.parentElement!)
  expect(section.getByText('$70.00')).toBeInTheDocument()
  expect(section.getByText('Holdings value')).toBeInTheDocument()
  expect(section.queryByText('Known holdings subtotal')).not.toBeInTheDocument()
})
