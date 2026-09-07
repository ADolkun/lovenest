import { expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import { ProviderAccountPicker } from './provider-account-picker'
import { renderWithProviders } from '@/test/utils'

vi.mock('@/contexts/auth-context', () => ({ useAuth: () => ({ user: null }) }))

it('keeps an unavailable balance selectable and preserves an explicitly reported zero', async () => {
  const onToggle = vi.fn()
  const { user } = renderWithProviders(
    <ProviderAccountPicker
      accounts={[
        { external_id: 'unknown', name: 'Incomplete wallet', balance: null, currency: 'USD', has_holdings: true, status: 'included' },
        { external_id: 'empty', name: 'Empty wallet', balance: '0', currency: 'USD', has_holdings: true, status: 'included' },
      ]}
      selected={new Set(['unknown', 'empty'])}
      allSelected
      isLoading={false}
      isError={false}
      onToggle={onToggle}
      onToggleAll={vi.fn()}
      onRetry={vi.fn()}
    />,
  )
  const unknown = screen.getByRole('checkbox', { name: /Incomplete wallet/ })
  expect(within(unknown.closest('label')!).getByText(/Value unavailable/)).toBeInTheDocument()
  expect(within(unknown.closest('label')!).queryByText(/\$0\.00/)).not.toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: /Empty wallet \$0\.00/ })).toBeChecked()
  await user.click(unknown)
  expect(onToggle).toHaveBeenCalledWith('unknown')
})
