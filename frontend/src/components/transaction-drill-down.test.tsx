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

  it('shows an expenses group with its offsetting credits as money in, netted from the total', async () => {
    const row = (overrides: Partial<Transaction>) => ({
      date: '2026-09-04',
      effective_date: '2026-09-04',
      effective_bill_date: null,
      amount_primary: null,
      currency: 'USD',
      category: null,
      status: 'posted',
      attachment_count: 0,
      ...overrides,
    }) as Transaction
    api.transactions.list.mockResolvedValue({
      items: [
        row({ id: 'dinner', description: 'Team dinner', type: 'debit', amount: 120 }),
        row({ id: 'reimbursement', description: 'Employer reimbursement', type: 'credit', amount: 45 }),
      ],
    })
    const projected = {
      account_id: null,
      date: '2026-09-20',
      amount_primary: null,
      currency: 'USD',
      category_id: 'dining',
      category_name: 'Dining',
      category_icon: null,
      category_color: null,
    }
    api.dashboard.projectedTransactions.mockResolvedValue([
      { ...projected, recurring_id: 'r-debit', description: 'Projected dinner', type: 'debit', amount: 25 },
      { ...projected, recurring_id: 'r-credit', description: 'Projected refund', type: 'credit', amount: 300 },
    ])

    renderWithProviders(
      <TooltipProvider>
        <TransactionDrillDown
          filter={{
            title: 'Dining',
            category_id: 'dining',
            pnl_group: 'expenses',
            from: '2026-09-01',
            to: '2026-09-30',
          }}
          onClose={vi.fn()}
        />
      </TooltipProvider>,
    )

    const credit = await screen.findByText('+$45.00')
    expect(credit).toHaveClass('text-emerald-600')
    expect(screen.getByText('-$120.00')).toHaveClass('text-rose-500')
    expect(screen.queryByText('Projected refund')).not.toBeInTheDocument()

    const label = await screen.findByText(t('dashboard.drillDownShownTotal'))
    expect(within(label.parentElement!).getByText('$100.00')).toBeInTheDocument()

    const params = api.transactions.list.mock.calls[0][0]
    expect(params).toMatchObject({ pnl_group: 'expenses', user_pnl_only: true })
    expect(params.type).toBeUndefined()
  })
})
