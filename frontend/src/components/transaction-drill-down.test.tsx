import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import type { Transaction } from '@/types'

import { TransactionDrillDown } from '@/components/transaction-drill-down'
import { TooltipProvider } from '@/components/ui/tooltip'
import { renderWithProviders, t } from '@/test/utils'

const api = vi.hoisted(() => ({
  transactions: { list: vi.fn() },
  dashboard: { projectedTransactions: vi.fn() },
  admin: { accountingMode: vi.fn() },
}))

vi.mock('@/lib/api', () => api)
vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }),
}))
vi.mock('@/hooks/use-privacy-mode', () => ({
  usePrivacyMode: () => ({ mask: (value: string) => value }),
}))
vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

beforeEach(() => {
  vi.clearAllMocks()
  api.admin.accountingMode.mockResolvedValue({ mode: 'cash' })
})

describe('TransactionDrillDown', () => {
  it('includes projected rows in the shown total when pending rows exist', async () => {
    api.transactions.list.mockResolvedValue({
      items: [{
        id: 'pending-1',
        description: 'Pending charge',
        date: '2026-09-04',
        effective_date: '2026-09-04',
        effective_bill_date: null,
        type: 'debit',
        amount: 10,
        amount_primary: null,
        currency: 'USD',
        category: null,
        status: 'pending',
        attachment_count: 0,
      } as Transaction],
    })
    api.dashboard.projectedTransactions.mockResolvedValue([{
      recurring_id: 'recurring-1',
      account_id: null,
      description: 'Projected charge',
      date: '2026-09-10',
      type: 'debit',
      amount: 25,
      amount_primary: null,
      currency: 'USD',
      category_id: null,
      category_name: null,
      category_icon: null,
      category_color: null,
    }])

    renderWithProviders(
      <TooltipProvider>
        <TransactionDrillDown
          filter={{
            title: 'Expenses',
            type: 'debit',
            from: '2026-09-01',
            to: '2026-09-30',
          }}
          onClose={vi.fn()}
        />
      </TooltipProvider>,
    )

    const label = await screen.findByText(t('dashboard.drillDownShownTotal'))
    expect(within(label.parentElement!).getByText('$35.00')).toBeInTheDocument()
  })
})
