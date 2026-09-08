import { beforeEach, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import { useNavigate, useLocation } from 'react-router-dom'
import AssetsPage from '@/pages/assets'
import { renderWithProviders } from '@/test/utils'
import { reportedWallet } from '@/test/balance-fixtures'
import { assets, assetGroups, contributions, currencies } from '@/lib/api'
import type { Asset, AssetGroup } from '@/types'

vi.mock('@/contexts/auth-context', () => ({ useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }) }))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: () => ({ current: { id: 'workspace', name: 'Synthetic' }, canWrite: false, hasModule: () => true }) }))
vi.mock('@/contexts/collection-filter-context', () => ({ useCollectionFilter: () => ({ activeWalletIds: null }) }))
vi.mock('@/hooks/use-feature-flags', () => ({ useFeatureFlags: () => ({ onchainEnabled: false }) }))
vi.mock('@/lib/page-chat-context', () => ({ useRegisterPageChatContext: () => undefined }))
const stock = { id: 'stock', name: 'Synthetic stock', type: 'stock', ticker: 'STOCK', currency: 'USD', group_id: 'wallet-a', current_value: 70, current_value_primary: 70, units: 1, sell_date: null, is_archived: false, source: 'manual' } as Asset
const coin = { ...stock, id: 'coin', name: 'Synthetic coin', type: 'crypto', ticker: 'COIN', group_id: 'wallet-b', current_value: 30, current_value_primary: 30 }
const wallets = [stock, coin].map((h) => reportedWallet({ id: h.group_id, name: h.group_id, source: 'manual', currency: 'USD', account_balance: h.current_value } as AssetGroup, [h]))
beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(assets, 'list').mockResolvedValue([stock, coin])
  vi.spyOn(assetGroups, 'list').mockResolvedValue(wallets)
  vi.spyOn(assets, 'income').mockResolvedValue({ holdings: {}, wallets: {} })
  vi.spyOn(assets, 'portfolioTrend').mockResolvedValue({ assets: [], trend: [], total: 0 })
  vi.spyOn(assets, 'values').mockResolvedValue([])
  vi.spyOn(assets, 'valueTrend').mockResolvedValue([])
  vi.spyOn(assets, 'transactions').mockResolvedValue([])
  vi.spyOn(contributions, 'summary').mockResolvedValue([])
  vi.spyOn(currencies, 'list').mockResolvedValue([])
})
function Nav() { const navigate = useNavigate(); const location = useLocation(); return <><button onClick={() => navigate(-1)}>Synthetic Back</button><output>{location.pathname + location.search}</output><AssetsPage /></> }
it.each([['Allocation by asset class', /Stock/], ['Allocation by account', /wallet-a/]] as const)('Back from a balance recovery link restores %s', async (region, slice) => {
  const { user } = renderWithProviders(<Nav />, { route: '/assets?view=assets' })
  const chart = await screen.findByRole('region', { name: region })
  await user.click(within(chart).getByRole('button', { name: slice }))
  expect(screen.queryByRole('button', { name: /^COIN/ })).not.toBeInTheDocument()
  const returnLocation = screen.getByRole('status').textContent
  await user.click(screen.getByRole('button', { name: /Balance details:/ }))
  await user.click(screen.getByRole('link', { name: 'Open value details' }))
  expect(await screen.findByRole('button', { name: 'STOCK', expanded: true })).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Synthetic Back' }))
  expect(screen.getByRole('status').textContent).toBe(returnLocation)
  expect(screen.queryByRole('button', { name: /^COIN/ })).not.toBeInTheDocument()
})
