import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import { toast } from 'sonner'

import { TransactionReimbursements } from '@/components/transaction-reimbursements'
import { transactions } from '@/lib/api'
import { renderWithProviders, t } from '@/test/utils'
import type { ReimbursementLink, Transaction } from '@/types'

vi.mock('@/lib/api', () => ({
  transactions: {
    reimbursements: {
      list: vi.fn(),
      unlink: vi.fn(),
      replace: vi.fn(),
      candidates: vi.fn(),
    },
  },
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))
vi.mock('@/hooks/use-privacy-mode', () => ({
  usePrivacyMode: () => ({ mask: (value: string) => value }),
}))

const link = (overrides: Partial<ReimbursementLink> = {}): ReimbursementLink => ({
  id: 'link-1',
  counterpart_id: 'yt-sep',
  counterpart_description: 'YouTube Premium Family',
  counterpart_date: '2026-09-03',
  counterpart_category_name: 'Subscriptions',
  amount: '22.99',
  currency: 'USD',
  in_effect: true,
  ...overrides,
})

const tx = (overrides: Partial<Transaction>): Transaction => ({
  id: 'zelle',
  description: 'Zelle from family',
  type: 'credit',
  amount: '100.00' as unknown as number,
  currency: 'USD',
  reimbursement_links: [],
  ...overrides,
}) as Transaction

beforeEach(() => {
  vi.clearAllMocks()
})

describe('TransactionReimbursements', () => {
  it('lists what a credit repays, what is left of it, and unlinks one', async () => {
    const links = [
      link(),
      link({ id: 'link-2', counterpart_id: 'yt-oct', counterpart_date: '2026-10-03' }),
    ]
    vi.mocked(transactions.reimbursements.list).mockResolvedValue(links)
    vi.mocked(transactions.reimbursements.unlink).mockResolvedValue()

    const { user } = renderWithProviders(<TransactionReimbursements transaction={tx({})} />)

    expect(screen.getByText(t('transactions.reimburses'))).toBeInTheDocument()
    expect(await screen.findAllByText('YouTube Premium Family')).toHaveLength(2)
    expect(screen.getAllByText(/Subscriptions/)).toHaveLength(2)
    expect(screen.getAllByText('$22.99')).toHaveLength(2)
    expect(screen.getByTestId('reimbursement-balance')).toHaveTextContent('$54.02')

    const [first] = screen.getAllByRole('button', {
      name: t('transactions.unlinkReimbursement', { description: 'YouTube Premium Family' }),
    })
    await user.click(first)

    await waitFor(() => expect(transactions.reimbursements.unlink).toHaveBeenCalledWith('zelle', 'link-1'))
    expect(toast.success).toHaveBeenCalledWith(t('transactions.reimbursementUnlinked'))
  })

  it('shows the API reason when an unlink is refused', async () => {
    vi.mocked(transactions.reimbursements.list).mockResolvedValue([link()])
    vi.mocked(transactions.reimbursements.unlink).mockRejectedValue({
      response: { status: 409, data: { detail: 'This link belongs to a closed period.' } },
    })

    const { user } = renderWithProviders(<TransactionReimbursements transaction={tx({})} />)

    await user.click(await screen.findByRole('button', {
      name: t('transactions.unlinkReimbursement', { description: 'YouTube Premium Family' }),
    }))

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('This link belongs to a closed period.'))
  })

  it('shows a reimbursed debit its credits and its net cost', async () => {
    const credit = link({
      id: 'link-ford',
      counterpart_id: 'ford',
      counterpart_description: 'FORD MOTOR COMPA PAYMENTS',
      counterpart_category_name: null,
      amount: '45.00',
    })
    vi.mocked(transactions.reimbursements.list).mockResolvedValue([credit])

    renderWithProviders(
      <TransactionReimbursements
        transaction={tx({
          id: 'dinner',
          description: 'Team dinner',
          type: 'debit',
          amount: '120.00' as unknown as number,
          reimbursement_links: [credit],
        })}
      />,
    )

    const section = screen.getByTestId('transaction-reimbursements')
    expect(within(section).getByText(t('transactions.reimbursedBy'))).toBeInTheDocument()
    expect(within(section).getByText('FORD MOTOR COMPA PAYMENTS')).toBeInTheDocument()
    expect(within(section).getByText(t('transactions.reimbursementNetCost'))).toBeInTheDocument()
    expect(within(section).getByTestId('reimbursement-balance')).toHaveTextContent('$75.00')
    expect(within(section).queryByText(t('transactions.linkExpenses'))).not.toBeInTheDocument()
  })

  it('stays out of the way of a debit nothing has repaid', async () => {
    vi.mocked(transactions.reimbursements.list).mockResolvedValue([])

    renderWithProviders(
      <TransactionReimbursements transaction={tx({ id: 'coffee', type: 'debit', amount: 4 })} />,
    )

    await waitFor(() => expect(transactions.reimbursements.list).toHaveBeenCalledWith('coffee'))
    expect(screen.queryByTestId('transaction-reimbursements')).not.toBeInTheDocument()
  })
})
