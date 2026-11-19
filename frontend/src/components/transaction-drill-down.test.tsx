import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import type { ReimbursementLink, Transaction } from '@/types'

import { TransactionDrillDown } from '@/components/transaction-drill-down'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { TooltipProvider } from '@/components/ui/tooltip'
import { renderWithProviders, t } from '@/test/utils'

const api = vi.hoisted(() => ({
  transactions: { list: vi.fn() },
  dashboard: { projectedTransactions: vi.fn() },
  admin: { accountingMode: vi.fn() },
}))

vi.mock('@/lib/api', () => api)
vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { id: 'me', preferences: { currency_display: 'USD' } } }),
}))
vi.mock('@/hooks/use-privacy-mode', () => ({
  usePrivacyMode: () => ({ mask: (value: string) => value }),
}))
vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

const outsideClickFilter = { title: 'Uncategorized', uncategorized: true }

function renderPanel({ dialogOpen }: { dialogOpen: boolean }) {
  const onClose = vi.fn()
  const result = renderWithProviders(
    <>
      <TransactionDrillDown filter={outsideClickFilter} onClose={onClose} />
      <Dialog open={dialogOpen}>
        <DialogContent>
          <DialogTitle>Edit transaction</DialogTitle>
          <button type="button">Category</button>
        </DialogContent>
      </Dialog>
    </>,
  )
  return { ...result, onClose }
}

// The outside-click listener is attached after a short delay so the click
// that opened the panel does not close it.
async function waitForOutsideClickListener() {
  await new Promise(resolve => setTimeout(resolve, 150))
}

beforeEach(() => {
  vi.clearAllMocks()
  api.admin.accountingMode.mockResolvedValue({ mode: 'cash' })
  api.transactions.list.mockResolvedValue({ items: [], total: 0 })
  api.dashboard.projectedTransactions.mockResolvedValue([])
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
    expect(credit.parentElement).toHaveClass('text-emerald-600')
    expect(screen.getByText('-$120.00').parentElement).toHaveClass('text-rose-500')
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
          reimbursement_links: [link({ id: 'l1', counterpart_id: 'acme' })],
        }),
        row({
          id: 'acme',
          description: 'ACME CORP PAYMENTS',
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

    const fullyLinked = screen.getByText('ACME CORP PAYMENTS').closest('.opacity-60')
    expect(fullyLinked).not.toBeNull()
    expect(screen.getByText('Zelle from family').closest('.opacity-60')).toBeNull()

    // 120 - 45 reimbursed, the Acme credit spent in full, 54.02 of Zelle left over.
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
            counterpart_id: 'acme',
            counterpart_description: 'ACME CORP PAYMENTS',
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

  it('preserves the expense share of recurring category splits in the Overview panel', async () => {
    api.dashboard.projectedTransactions.mockResolvedValue([{
      recurring_id: 'split-bill', account_id: 'account-1', description: 'Split recurring bill',
      amount: 100, amount_primary: 200, currency: 'EUR', type: 'debit', date: '2026-11-10',
      category_id: null, has_category_lines: true, category_lines: [
        { category_id: 'food', category_name: 'Food', category_icon: null, category_color: null, amount: 80, treat_as_transfer: false },
        { category_id: 'investment', category_name: 'Investment', category_icon: null, category_color: null, amount: 20, treat_as_transfer: true },
      ],
    }])
    renderWithProviders(<TransactionDrillDown filter={{ title: 'Expenses', type: 'debit', from: '2026-11-01', to: '2026-11-30' }} onClose={vi.fn()} />)
    expect(await screen.findByText('Split recurring bill')).toBeInTheDocument()
    expect(screen.getByText('-€80.00')).toBeInTheDocument()
    expect(screen.getAllByText('$160.00').length).toBeGreaterThan(0)
    expect(screen.queryByText('$200.00')).not.toBeInTheDocument()
    expect(screen.getByText(/part of €100.00/)).toBeInTheDocument()
  })

  it('closes on a click outside the panel', async () => {
    const { onClose } = renderPanel({ dialogOpen: false })
    await waitForOutsideClickListener()

    fireEvent.mouseDown(document.body)

    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('stays open while the user clicks inside a dialog opened from it', async () => {
    const { onClose } = renderPanel({ dialogOpen: true })
    await waitForOutsideClickListener()

    fireEvent.mouseDown(await screen.findByRole('button', { name: 'Category' }))

    expect(onClose).not.toHaveBeenCalled()
  })

  it('leaves Escape to the open dialog', async () => {
    const { onClose } = renderPanel({ dialogOpen: true })
    await screen.findByRole('dialog')

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(onClose).not.toHaveBeenCalled()
  })

  it('closes on Escape when no dialog is open', async () => {
    const { onClose } = renderPanel({ dialogOpen: false })

    fireEvent.keyDown(document, { key: 'Escape' })

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('totals only the user share of a split expense and asks for posted rows', async () => {
    const tx = (overrides: Record<string, unknown>) => ({
      id: 'tx', description: 'Dinner', date: '2026-09-05', effective_date: '2026-09-05', effective_bill_date: null,
      type: 'debit', amount: 100, amount_primary: 100, currency: 'USD', status: 'posted', category: null,
      attachment_count: 0, reimbursement_links: [], splits: [], viewer_share: null, is_shared: false, user_id: 'me', ...overrides,
    })
    api.transactions.list.mockResolvedValue({
      items: [
        // Owner of a 50/50 split keeps their half.
        tx({ id: 'owned', splits: [{ share_amount: 50 }, { share_amount: 50 }], viewer_share: 50 }),
        // A member sees only their own share of someone else's expense.
        tx({ id: 'shared', amount: 80, amount_primary: 80, is_shared: true, viewer_share: 20 }),
        tx({ id: 'refund', type: 'credit', amount: 10, amount_primary: 10 }),
        // Another member's split the user is not part of counts in full.
        tx({ id: 'other', amount: 30, amount_primary: 30, user_id: 'other', splits: [{ share_amount: 15 }] }),
      ],
      total: 4,
    })

    renderWithProviders(
      <TransactionDrillDown
        filter={{ title: 'Food', from: '2026-09-01', to: '2026-09-12', realizedOnly: true, ownShare: true, netTotal: true }}
        onClose={vi.fn()}
      />,
    )

    expect((await screen.findAllByText('-$90.00')).length).toBeGreaterThan(0)
    expect(api.transactions.list).toHaveBeenCalledWith(expect.objectContaining({ status: 'posted', to: '2026-09-12' }))
  })
})
