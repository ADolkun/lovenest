import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'

import { ConnectionSettingsDialog } from '@/components/connection-settings-dialog'
import { renderWithProviders } from '@/test/utils'
import type { BankConnection, ProviderAccount } from '@/types'

const connectionsApi = vi.hoisted(() => ({
  listProviderAccounts: vi.fn(),
  updateSettings: vi.fn(),
  sync: vi.fn(),
}))
vi.mock('@/lib/api', () => ({ connections: connectionsApi }))
vi.mock('@/contexts/auth-context', () => ({ useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }) }))

const toast = vi.hoisted(() => ({ success: vi.fn(), warning: vi.fn(), error: vi.fn() }))
vi.mock('sonner', () => ({ toast }))

const connection = {
  id: 'conn-1',
  provider: 'simplefin',
  institution_name: 'Synthetic Bank',
  display_name: null,
  status: 'active',
  settings: { account_allowlist: ['acc-checking'] },
} as unknown as BankConnection

const providerAccounts: ProviderAccount[] = [
  { external_id: 'acc-checking', name: 'Checking', balance: '10', currency: 'USD', has_holdings: false, status: 'included' },
  { external_id: 'acc-savings', name: 'Savings', balance: '20', currency: 'USD', has_holdings: false, status: 'pending' },
]

function renderDialog() {
  return renderWithProviders(
    <ConnectionSettingsDialog open onClose={() => {}} onReconnect={() => {}} connection={connection} />,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  connectionsApi.listProviderAccounts.mockResolvedValue(providerAccounts)
  connectionsApi.updateSettings.mockResolvedValue(connection)
})

describe('ConnectionSettingsDialog', () => {
  it('keeps the provider listing when every query is invalidated', async () => {
    // The cross-tab refresh invalidates everything after a save in another
    // tab. Refetching here would spend a provider request and reset the ticks.
    const { queryClient } = renderDialog()
    expect(await screen.findByText('Savings')).toBeInTheDocument()

    await queryClient.invalidateQueries()

    expect(connectionsApi.listProviderAccounts).toHaveBeenCalledTimes(1)
  })

  it('warns instead of confirming when the approval sync was rate-limited', async () => {
    connectionsApi.sync.mockResolvedValue({ ...connection, merged_count: 0, sync_outcome: 'rate_limited' })
    const { user } = renderDialog()

    await user.click(await screen.findByRole('checkbox', { name: /Savings/ }))
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(toast.warning).toHaveBeenCalledWith('Bank daily limit reached, no new data was fetched. Try again later.'))
    expect(connectionsApi.sync).toHaveBeenCalledWith('conn-1')
    expect(toast.success).not.toHaveBeenCalled()
  })

  it('confirms the save when the approval sync refreshed', async () => {
    connectionsApi.sync.mockResolvedValue({ ...connection, merged_count: 0, sync_outcome: 'refreshed' })
    const { user } = renderDialog()

    await user.click(await screen.findByRole('checkbox', { name: /Savings/ }))
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(toast.success).toHaveBeenCalled())
    expect(toast.warning).not.toHaveBeenCalled()
  })
})
