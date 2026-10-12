import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import type { ReimbursementLink, Transaction } from '@/types'

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
        reimbursement_links: [],
      } as unknown as Transaction],
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
      reimbursement_links: [],
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

  it('counts linked rows by their unlinked share and says where the money went', async () => {
    const link = (overrides: Partial<ReimbursementLink>): ReimbursementLink => ({
      id: 'link',
      counterpart_id: 'counterpart',
      counterpart_description: 'Counterpart',
      counterpart_date: '2026-09-05',
      counterpart_category_name: 'Dining',
      amount: '45.00',
      currency: 'USD',
      in_effect: true,
      ...overrides,
    })
    const row = (overrides: Partial<Transaction>) => ({
      date: '2026-09-04',
      effective_date: '2026-09-04',
      effective_bill_date: null,
      amount_primary: null,
      currency: 'USD',
      category: null,
      status: 'posted',
      attachment_count: 0,
      reimbursement_links: [],
      ...overrides,
    }) as Transaction
    api.transactions.list.mockResolvedValue({
      items: [
        row({
          id: 'dinner',
          description: 'Team dinner',
          type: 'debit',
          amount: 120,
          reimbursement_links: [link({ id: 'l1', counterpart_id: 'ford' })],
        }),
        row({
          id: 'ford',
          description: 'FORD MOTOR COMPA PAYMENTS',
          type: 'credit',
          amount: 45,
          reimbursement_links: [link({ id: 'l1', counterpart_id: 'dinner' })],
        }),
        row({
          id: 'zelle',
          description: 'Zelle from family',
          type: 'credit',
          amount: 100,
          reimbursement_links: [
            link({ id: 'l2', counterpart_id: 'yt-1', amount: '22.99' }),
            link({ id: 'l3', counterpart_id: 'yt-2', amount: '22.99' }),
          ],
        }),
      ],
    })
    api.dashboard.projectedTransactions.mockResolvedValue([])

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

    expect(await screen.findByText(t('transactions.reimbursedAmount', { amount: '$45.00' }))).toBeInTheDocument()
    expect(screen.getByText(t('transactions.appliedToExpenses', { count: 1 }))).toBeInTheDocument()
    expect(screen.getByText(t('transactions.appliedToExpenses', { count: 2 }))).toBeInTheDocument()

    const fullyLinked = screen.getByText('FORD MOTOR COMPA PAYMENTS').closest('.opacity-60')
    expect(fullyLinked).not.toBeNull()
    expect(screen.getByText('Zelle from family').closest('.opacity-60')).toBeNull()

    // 120 - 45 reimbursed, the Ford credit spent in full, 54.02 of Zelle left over.
    const label = await screen.findByText(t('dashboard.drillDownTotal', { count: 3, total: '$20.98' }))
    expect(label).toBeInTheDocument()
  })

  it('counts a row in full when its links are not in effect', async () => {
    api.transactions.list.mockResolvedValue({
      items: [
        {
          id: 'dinner',
          description: 'Team dinner',
          type: 'debit',
          amount: 120,
          date: '2026-09-04',
          effective_date: '2026-09-04',
          effective_bill_date: null,
          amount_primary: null,
          currency: 'USD',
          category: null,
          status: 'posted',
          attachment_count: 0,
          reimbursement_links: [{
            id: 'l1',
            counterpart_id: 'ford',
            counterpart_description: 'FORD MOTOR COMPA PAYMENTS',
            counterpart_date: '2026-09-05',
            counterpart_category_name: null,
            amount: '45.00',
            currency: 'USD',
            in_effect: false,
          }],
        } as unknown as Transaction,
      ],
    })
    api.dashboard.projectedTransactions.mockResolvedValue([])

    renderWithProviders(
      <TooltipProvider>
        <TransactionDrillDown
          filter={{ title: 'Dining', category_id: 'dining', pnl_group: 'expenses', from: '2026-09-01', to: '2026-09-30' }}
          onClose={vi.fn()}
        />
      </TooltipProvider>,
    )

    expect(await screen.findByText(t('dashboard.drillDownTotal', { count: 1, total: '$120.00' }))).toBeInTheDocument()
    expect(screen.queryByText(t('transactions.reimbursedAmount', { amount: '$45.00' }))).not.toBeInTheDocument()
  })
})
