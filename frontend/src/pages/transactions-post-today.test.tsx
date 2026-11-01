/**
 * "Post today" in the transactions list is offered only on rows the
 * viewer can write. A row shared from another owner's workspace is listed
 * read-only, and posting it would 404, so it gets no button.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'

import TransactionsPage from '@/pages/transactions'
import { renderWithProviders, t } from '@/test/utils'
import type { Transaction } from '@/types'

const api = vi.hoisted(() => ({
  transactions: { list: vi.fn(), update: vi.fn(), delete: vi.fn(), create: vi.fn(), postToday: vi.fn() },
  categories: { list: vi.fn(), listIncludingHidden: vi.fn() },
  categoryGroups: { list: vi.fn() },
  accounts: {
    get: vi.fn(), list: vi.fn(), bills: vi.fn(), summary: vi.fn(),
    update: vi.fn(), runningBalance: vi.fn(),
  },
  recurring: { list: vi.fn() },
  payees: { list: vi.fn() },
  admin: { accountingMode: vi.fn() },
  groups: { get: vi.fn(), list: vi.fn() },
  rules: { create: vi.fn() },
  reconciliation: { suggestions: vi.fn() },
  currencies: { list: vi.fn() },
  settings: { attachments: vi.fn() },
}))

vi.mock('@/lib/api', () => ({
  transactions: api.transactions,
  categories: api.categories,
  categoryGroups: api.categoryGroups,
  accounts: api.accounts,
  recurring: api.recurring,
  payees: api.payees,
  admin: api.admin,
  groups: api.groups,
  rules: api.rules,
  reconciliation: api.reconciliation,
  currencies: api.currencies,
  settings: api.settings,
}))
vi.mock('@/hooks/use-mobile', () => ({ useIsMobile: () => false }))
vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))
vi.mock('@/hooks/use-privacy-mode', () => ({
  usePrivacyMode: () => ({ mask: (value: string) => value, privacyMode: false, MASK: '***' }),
}))
vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { id: 'user-1', preferences: { currency_display: 'BRL' } } }),
}))
vi.mock('@/contexts/workspace-context', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/contexts/workspace-context')>()
  return { ...actual, useWorkspace: () => ({ canWrite: true, hasModule: () => true }) }
})
vi.mock('@/contexts/collection-filter-context', () => ({
  useCollectionFilter: () => ({
    collections: [], activeCollectionId: null, activeCollection: null,
    setActiveCollectionId: () => {}, activeAccountIds: null, activeWalletIds: null,
  }),
}))

function pendingTx(over: Partial<Transaction> & { id: string; description: string }): Transaction {
  return {
    user_id: 'user-1', account_id: 'acc-1', category_id: null, category: null,
    external_id: null, original_description: null, amount: 1, currency: 'BRL',
    date: '2026-10-08', type: 'debit', source: 'manual', status: 'pending',
    payee: null, payee_id: null, payee_name: null, notes: null,
    transfer_pair_id: null, transfer_counterpart: null, amount_primary: null,
    fx_rate_used: null, fx_fallback: false, installment_number: null,
    total_installments: null, installment_total_amount: null,
    installment_purchase_date: null, installment_series_id: null, bill_id: null,
    effective_bill_date: null, splits: [], is_ignored: false, reimbursement_links: [],
    effective_date: '2026-10-08',
    ...over,
  }
}

const rows: Transaction[] = [
  pendingTx({ id: 'tx-own', description: 'Own pending' }),
  pendingTx({
    id: 'tx-shared', description: 'Shared pending', user_id: 'user-2',
    is_shared: true, parent_owner_name: 'Alex', group_id: 'group-1',
  }),
]

beforeEach(() => {
  vi.clearAllMocks()
  vi.setSystemTime(new Date('2026-10-20T12:00:00'))
  api.transactions.list.mockResolvedValue({ items: rows, total: rows.length, page: 1, limit: 50 })
  api.categories.list.mockResolvedValue([])
  api.categories.listIncludingHidden.mockResolvedValue([])
  api.categoryGroups.list.mockResolvedValue([])
  api.accounts.list.mockResolvedValue([])
  api.recurring.list.mockResolvedValue([])
  api.payees.list.mockResolvedValue([])
  api.admin.accountingMode.mockResolvedValue(false)
  api.groups.list.mockResolvedValue([])
  api.reconciliation.suggestions.mockResolvedValue([])
  api.currencies.list.mockResolvedValue([])
  api.settings.attachments.mockResolvedValue(false)
})

describe('post today in the transactions list', () => {
  it('is offered on an own pending row and not on a shared one', async () => {
    renderWithProviders(<TransactionsPage />, { route: '/transactions', path: '/transactions' })

    const label = t('transactions.postToday')
    const ownRow = (await screen.findByText('Own pending')).closest('div[class*="flex"]')!
    const sharedRow = (await screen.findByText('Shared pending')).closest('div[class*="flex"]')!
    expect(ownRow.querySelector(`button[aria-label="${label}"]`)).not.toBeNull()
    expect(sharedRow.querySelector(`button[aria-label="${label}"]`)).toBeNull()
  })
})
