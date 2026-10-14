import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { toast } from 'sonner'

import { LinkReimbursementDialog } from '@/components/link-reimbursement-dialog'
import { transactions } from '@/lib/api'
import { renderWithProviders, t } from '@/test/utils'
import type { ReimbursementCandidate, ReimbursementLink, Transaction } from '@/types'

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

const credit = {
  id: 'zelle',
  description: 'Zelle from family',
  type: 'credit',
  amount: '100.00',
  currency: 'USD',
  reimbursement_links: [],
} as unknown as Transaction

const candidate = (
  id: string,
  description: string,
  amount: string,
  remaining: string,
  allocatedHere = '0',
): ReimbursementCandidate => ({
  transaction: {
    id,
    description,
    date: '2026-09-03',
    type: 'debit',
    amount,
    currency: 'USD',
    category: { name: 'Subscriptions' },
    reimbursement_links: [],
  } as unknown as Transaction,
  remaining,
  allocated_here: allocatedHere,
})

const youtubeSep = candidate('yt-sep', 'YouTube September', '22.99', '22.99')
const youtubeOct = candidate('yt-oct', 'YouTube October', '22.99', '22.99')
const groceries = candidate('groceries', 'Groceries', '80.00', '80.00')

const amountInput = (description: string) =>
  screen.getByRole('textbox', { name: t('transactions.reimbursementAmountFor', { description }) })

function renderDialog(links: ReimbursementLink[] = []) {
  const onOpenChange = vi.fn()
  const result = renderWithProviders(
    <LinkReimbursementDialog open onOpenChange={onOpenChange} credit={credit} links={links} />,
  )
  return { ...result, onOpenChange }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(transactions.reimbursements.candidates).mockResolvedValue([youtubeSep, youtubeOct, groceries])
})

describe('LinkReimbursementDialog', () => {
  it('gives each checked expense what it can take of what the credit has left, in check order', async () => {
    const { user } = renderDialog()

    await user.click(await screen.findByRole('checkbox', { name: 'Groceries' }))
    expect(amountInput('Groceries')).toHaveValue('80.00')
    await user.click(screen.getByRole('checkbox', { name: 'YouTube September' }))
    expect(amountInput('YouTube September')).toHaveValue('20.00')
    expect(screen.getByTestId('reimbursement-unallocated')).toHaveTextContent('$0.00')

    await user.click(screen.getByRole('checkbox', { name: 'Groceries' }))
    await user.click(screen.getByRole('checkbox', { name: 'YouTube October' }))
    expect(amountInput('YouTube October')).toHaveValue('22.99')
    expect(screen.getByTestId('reimbursement-unallocated')).toHaveTextContent('$57.01')
  })

  it('blocks a save that hands out more than the credit or more than an expense has left', async () => {
    const { user } = renderDialog()
    const save = () => screen.getByRole('button', { name: t('common.save') })

    await user.click(await screen.findByRole('checkbox', { name: 'Groceries' }))
    await user.click(screen.getByRole('checkbox', { name: 'YouTube September' }))
    await user.clear(amountInput('YouTube September'))
    await user.type(amountInput('YouTube September'), '22.99')

    expect(screen.getByRole('alert')).toHaveTextContent(
      t('transactions.reimbursementOverCredit', { amount: '$2.99' }),
    )
    expect(screen.getByTestId('reimbursement-unallocated')).toHaveTextContent('-$2.99')
    expect(save()).toBeDisabled()

    await user.clear(amountInput('YouTube September'))
    await user.type(amountInput('YouTube September'), '30')
    expect(screen.getByRole('alert')).toHaveTextContent(t('transactions.reimbursementOverExpense'))
    expect(amountInput('YouTube September')).toHaveAttribute('aria-invalid', 'true')
    expect(save()).toBeDisabled()

    await user.clear(amountInput('YouTube September'))
    await user.type(amountInput('YouTube September'), '20')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(save()).toBeEnabled()
    expect(transactions.reimbursements.replace).not.toHaveBeenCalled()
  })

  it('keeps existing links, even ones the search does not return, and replaces the full set', async () => {
    vi.mocked(transactions.reimbursements.candidates).mockResolvedValue([
      candidate('yt-sep', 'YouTube September', '22.99', '22.99', '22.99'),
      groceries,
    ])
    vi.mocked(transactions.reimbursements.replace).mockResolvedValue([])
    const links: ReimbursementLink[] = [
      {
        id: 'link-1',
        counterpart_id: 'yt-sep',
        counterpart_description: 'YouTube September',
        counterpart_date: '2026-09-03',
        counterpart_category_name: 'Subscriptions',
        amount: '22.99',
        currency: 'USD',
        in_effect: true,
      },
      {
        id: 'link-2',
        counterpart_id: 'pharmacy',
        counterpart_description: 'Pharmacy',
        counterpart_date: '2026-03-14',
        counterpart_category_name: 'Health',
        amount: '10.00',
        currency: 'USD',
        in_effect: false,
      },
    ]
    const { user, onOpenChange } = renderDialog(links)

    const groceriesBox = await screen.findByRole('checkbox', { name: 'Groceries' })
    expect(screen.getByRole('checkbox', { name: 'YouTube September' })).toBeChecked()
    expect(amountInput('YouTube September')).toHaveValue('22.99')
    expect(screen.getByRole('checkbox', { name: 'Pharmacy' })).toBeChecked()
    expect(amountInput('Pharmacy')).toHaveValue('10.00')

    await user.click(groceriesBox)
    expect(amountInput('Groceries')).toHaveValue('67.01')
    await user.click(screen.getByRole('button', { name: t('common.save') }))

    await waitFor(() => expect(transactions.reimbursements.replace).toHaveBeenCalledWith('zelle', [
      { expense_id: 'yt-sep', amount: '22.99' },
      { expense_id: 'pharmacy', amount: '10.00' },
      { expense_id: 'groceries', amount: '67.01' },
    ]))
    expect(toast.success).toHaveBeenCalledWith(t('transactions.reimbursementsSaved'))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('prefills amounts with their cents', async () => {
    vi.mocked(transactions.reimbursements.candidates).mockResolvedValue([
      candidate('gas', 'Gas', '10.60', '10.60'),
      candidate('yt-sep', 'YouTube September', '40.00', '40.00', '31.70'),
    ])
    const { user } = renderDialog([
      {
        id: 'link-1',
        counterpart_id: 'yt-sep',
        counterpart_description: 'YouTube September',
        counterpart_date: '2026-09-03',
        counterpart_category_name: 'Subscriptions',
        amount: 31.7,
        currency: 'USD',
        in_effect: true,
      },
    ])

    await user.click(await screen.findByRole('checkbox', { name: 'Gas' }))
    expect(amountInput('Gas')).toHaveValue('10.60')
    expect(amountInput('YouTube September')).toHaveValue('31.70')
  })

  it('searches candidates once typing pauses', async () => {
    const { user } = renderDialog()
    await screen.findByRole('checkbox', { name: 'Groceries' })

    await user.type(screen.getByRole('textbox', { name: t('transactions.reimbursementSearchPlaceholder') }), 'you')

    await waitFor(() => expect(transactions.reimbursements.candidates).toHaveBeenLastCalledWith(
      'zelle',
      { q: 'you', limit: 50 },
    ))
    expect(transactions.reimbursements.candidates).not.toHaveBeenCalledWith('zelle', { q: 'y', limit: 50 })
  })

  it('shows why the server refused the links and stays open', async () => {
    vi.mocked(transactions.reimbursements.replace).mockRejectedValue({
      response: { status: 422, data: { detail: 'Groceries is already reimbursed in full.' } },
    })
    const { user, onOpenChange } = renderDialog()

    await user.click(await screen.findByRole('checkbox', { name: 'Groceries' }))
    await user.click(screen.getByRole('button', { name: t('common.save') }))

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Groceries is already reimbursed in full.'))
    expect(onOpenChange).not.toHaveBeenCalled()
  })
})
