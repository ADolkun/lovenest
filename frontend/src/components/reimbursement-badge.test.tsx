import { describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'

import { ReimbursementBadge } from '@/components/reimbursement-badge'
import { renderWithProviders, t } from '@/test/utils'
import type { ReimbursementLink, Transaction } from '@/types'

vi.mock('@/hooks/use-display-locale', () => ({ useDisplayLocale: () => 'en-US' }))
vi.mock('@/hooks/use-privacy-mode', () => ({
  usePrivacyMode: () => ({ mask: (value: string) => value }),
}))

const link = (amount: string): ReimbursementLink => ({
  id: `link-${amount}`,
  counterpart_id: 'counterpart',
  counterpart_description: 'Counterpart',
  counterpart_date: '2026-09-03',
  counterpart_category_name: null,
  amount,
  currency: 'USD',
  in_effect: true,
})

const tx = (type: 'credit' | 'debit', links: ReimbursementLink[]) => ({
  id: 'tx',
  type,
  amount: '100.00',
  currency: 'USD',
  reimbursement_links: links,
}) as unknown as Transaction

describe('ReimbursementBadge', () => {
  it('marks a linked credit as a reimbursement and says how many expenses it covers', () => {
    renderWithProviders(<ReimbursementBadge transaction={tx('credit', [link('22.99'), link('23.00')])} />)

    const badge = screen.getByText(t('transactions.reimbursementBadge'))
    expect(badge).toHaveAttribute('title', t('transactions.appliedToExpenses', { count: 2 }))
  })

  it('shows a reimbursed debit how much came back', () => {
    renderWithProviders(<ReimbursementBadge transaction={tx('debit', [link('20.00'), link('25.00')])} />)

    expect(screen.getByText(t('transactions.reimbursedAmount', { amount: '$45.00' }))).toBeInTheDocument()
  })

  it('renders nothing without links', () => {
    const { container } = renderWithProviders(<ReimbursementBadge transaction={tx('debit', [])} />)

    expect(container).toBeEmptyDOMElement()
  })
})
