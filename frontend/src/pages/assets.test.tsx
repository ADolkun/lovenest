import { beforeEach, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
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
  vi.spyOn(assets, 'list').mockResolvedValue([{ ...held, units: 3, average_price: 25, last_price: 40, total_invested: 75, gain_loss: 50, realized_gain: 10 }])
  const { user } = renderWithProviders(<AssetsPage />, { route })
  const holding = await screen.findByRole('button', { name: 'Private fund' })
  expect(holding).toHaveAttribute('aria-expanded', 'false')
  expect(within(holding.closest('.grid')!).getByText('$125.00')).toBeInTheDocument()
  expect(screen.queryByRole('definition')).not.toBeInTheDocument()
  await user.click(holding)
  expect(holding).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getAllByRole('definition').map((element) => element.textContent)).toEqual(['3', '$25.00', '$40.00', '+66.7%', '$10.00', '100.0%100.0% invested'])
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

it('opens account wallet links, uses current holdings instead of stale rollups, and clears the filter', async () => {
  const { user } = renderWithProviders(<AssetsPage />, { route: '/assets?wallet=wallet-a' })
  expect(await screen.findByRole('button', { name: 'Private fund' })).toBeInTheDocument()
  expect(screen.getByRole('tab', { name: 'Portfolio' })).toHaveAttribute('aria-selected', 'true')
  expect(screen.queryByRole('button', { name: 'Other account fund' })).not.toBeInTheDocument()
  expect(screen.queryByText('$9,000.00')).not.toBeInTheDocument()
  expect(screen.getByText('Holdings value')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Clear wallet filter' }))
  expect(await screen.findByRole('button', { name: /Wallet B/ })).toBeInTheDocument()
})

it('keeps archived trades within the selected collection on a legacy Activity link', async () => {
  scope.activeWalletIds = ['wallet-a']
  const { user } = renderWithProviders(<AssetsPage />, { route: '/assets?tab=transactions' })
  expect(await screen.findByText('Archived trade')).toBeInTheDocument()
  expect(screen.queryByText('Outside trade')).not.toBeInTheDocument()
  expect(screen.getByRole('tab', { name: 'Activity' })).toHaveAttribute('aria-selected', 'true')
  await user.click(screen.getByRole('tab', { name: 'Portfolio' }))
  await user.click(screen.getByRole('button', { name: 'By wallet' }))
  await waitFor(() => expect(screen.getByRole('button', { name: /Wallet A/ })).toBeInTheDocument())
  expect(screen.queryByText('Wallet B')).not.toBeInTheDocument()
})
