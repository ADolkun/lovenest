import { BalanceDetails } from '@/components/balance-details'
import { reportedWallet } from '@/test/balance-fixtures'
import { expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import { QueryClient, useQuery } from '@tanstack/react-query'
import AssetsPage from '@/pages/assets'
import { AccountHoldingsSummary } from '@/components/account-holdings'
import { renderWithProviders } from '@/test/utils'
import { accounts, assets, assetGroups, contributions, currencies } from '@/lib/api'
import type { Asset, AssetGroup, Account } from '@/types'
vi.mock('@/contexts/auth-context', () => ({ useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }) }))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: () => ({ current: { id: 'workspace', name: 'Investments' }, canWrite: true, hasModule: () => true }) }))
vi.mock('@/contexts/collection-filter-context', () => ({ useCollectionFilter: () => ({activeWalletIds:null}) }))
vi.mock('@/hooks/use-feature-flags', () => ({ useFeatureFlags: () => ({ onchainEnabled: false }) }))
vi.mock('@/lib/page-chat-context', () => ({ useRegisterPageChatContext: () => undefined }))
it('refreshes Accounts holdings after editing a manual asset value', async () => {
  let amount = 125
  const holding = () => ({id:'held', name:'Private fund', type:'other', source:'manual', currency:'USD', group_id:'wallet-a', current_value:amount, current_value_primary:amount, ticker:null, sell_date:null, is_archived:false, valuation_method:'manual'}) as Asset
  const group = () => ({ id:'wallet-a', name:'Wallet A', source:'manual', account_id:'account-a', current_value:amount, current_value_primary:amount, asset_count:1, unvalued_count:0, tax_treatment:'taxable', color:'#6366f1' }) as AssetGroup
  const groupList=vi.spyOn(assetGroups,'list').mockImplementation(async()=>[group()])
  vi.spyOn(assets,'list').mockImplementation(async()=>[holding()])
  vi.spyOn(assets,'portfolioTrend').mockResolvedValue({ assets: [], trend: [], total:0 })
  vi.spyOn(contributions,'summary').mockResolvedValue([])
  vi.spyOn(currencies,'list').mockResolvedValue([])
  vi.spyOn(assets,'values').mockResolvedValue([])
  vi.spyOn(assets,'valueTrend').mockResolvedValue([])
  vi.spyOn(assets,'transactions').mockResolvedValue([])
  const addValue=vi.spyOn(assets,'addValue').mockImplementation(async(_id, data)=>{amount=data.amount;return {} as never})
  const queryClient=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:300000},mutations:{retry:false}}})
  const {user,unmount}=renderWithProviders(<AssetsPage/>,{route:'/assets?wallet=wallet-a',queryClient})
  await user.click(await screen.findByRole('button',{name:'Private fund'}))
  await user.type(screen.getByRole('spinbutton'),'500')
  await user.click(screen.getByRole('button',{name:'Add Value'}))
  await waitFor(()=>expect(addValue).toHaveBeenCalled())
  await waitFor(()=>expect(queryClient.getQueryData<Asset[]>(['assets'])?.[0].current_value).toBe(500))
  unmount()
  function AccountProbe() { const {data=[]}=useQuery({queryKey:['asset-groups'],queryFn:assetGroups.list});return <AccountHoldingsSummary account={{id:'account-a'} as Account} wallets={data}/> }
  renderWithProviders(<AccountProbe/>,{queryClient})
  expect(await screen.findByRole('link',{name:'Holdings: $500.00'})).toBeInTheDocument()
  expect(groupList).toHaveBeenCalledTimes(2)
})

it('editing holding value invalidates its cached account explanation', async () => {
  let amount = 125
  const holding = () => ({ id: 'held', name: 'Private fund', type: 'other', source: 'manual', currency: 'USD', group_id: 'wallet-a', current_value: amount, current_value_primary: amount, ticker: null, sell_date: null, is_archived: false, valuation_method: 'manual' }) as Asset
  const group = () => reportedWallet({ id: 'wallet-a', name: 'Wallet A', source: 'manual', account_id: 'account-a', current_value: amount, current_value_primary: amount, currency: 'USD', account_balance: null, tax_treatment: 'taxable', color: '#6366f1' } as AssetGroup, [holding()])
  const account = () => ({ id: 'account-a', connection_id: null, name: 'Account A', type: 'investment', currency: 'USD', current_balance: 45, balance_explanation: { ...group().balance_explanation!, wallet_id: null, basis: 'cash_ledger', amount: 45, reconciliation: 'separate_cash_ledger' } }) as Account
  vi.spyOn(assetGroups, 'list').mockImplementation(async () => [group()])
  vi.spyOn(accounts, 'list').mockImplementation(async () => [account()])
  vi.spyOn(assets, 'list').mockImplementation(async () => [holding()])
  vi.spyOn(assets, 'portfolioTrend').mockResolvedValue({ assets: [], trend: [], total: 0 })
  vi.spyOn(contributions, 'summary').mockResolvedValue([])
  vi.spyOn(currencies, 'list').mockResolvedValue([])
  vi.spyOn(assets, 'values').mockResolvedValue([])
  vi.spyOn(assets, 'valueTrend').mockResolvedValue([])
  vi.spyOn(assets, 'transactions').mockResolvedValue([])
  vi.spyOn(assets, 'addValue').mockImplementation(async (_id, data) => { amount = data.amount; return {} as never })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 300000 }, mutations: { retry: false } } })
  queryClient.setQueryData(['accounts'], [account()])
  queryClient.setQueryData(['accounts', 'account-a'], account())
  const { user, unmount } = renderWithProviders(<AssetsPage />, { route: '/assets?wallet=wallet-a', queryClient })
  await user.click(await screen.findByRole('button', { name: 'Private fund' }))
  await user.type(screen.getByRole('spinbutton'), '500')
  await user.click(screen.getByRole('button', { name: 'Add Value' }))
  await waitFor(() => expect(queryClient.getQueryData<AssetGroup[]>(['asset-groups'])?.[0].balance_explanation?.holdings[0].value).toBe(500))
  expect(queryClient.getQueryState(['accounts', 'account-a'])?.isInvalidated).toBe(true)
  unmount()
  function AccountProbe() { const { data = [] } = useQuery({ queryKey: ['accounts'], queryFn: () => accounts.list() }); return data[0] ? <BalanceDetails account={data[0]} workspaceId="workspace" /> : null }
  const next = renderWithProviders(<AccountProbe />, { queryClient })
  await next.user.click(await screen.findByRole('button', { name: 'Balance details: Account A' }))
  const item = within(screen.getByRole('dialog')).getByText('Private fund').closest('li')!
  expect(item).toHaveTextContent('$500.00')
})
