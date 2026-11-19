import { QueryClient } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'

import { invalidateCategoryQueries, invalidateFinancialQueries } from './invalidate-queries'

it('refreshes account holdings and links after financial mutations', () => {
  const queryClient = new QueryClient()
  queryClient.setQueryData(['accounts'], [{ id: 'account' }])
  queryClient.setQueryData(['asset-groups'], [{ account_id: 'account', current_value: 100 }])

  invalidateFinancialQueries(queryClient)

  expect(queryClient.getQueryState(['accounts'])?.isInvalidated).toBe(true)
  expect(queryClient.getQueryState(['asset-groups'])?.isInvalidated).toBe(true)
})

describe('invalidateCategoryQueries', () => {
  it('invalidates category displays and both category-group key conventions', () => {
    const queryClient = new QueryClient()
    const keys = [
      ['categories'],
      ['categories', 'management'],
      ['categoryGroups'],
      ['categoryGroups', 'management'],
      ['category-groups'],
      ['category-groups', 'management'],
    ] as const

    for (const queryKey of keys) queryClient.setQueryData(queryKey, [])

    invalidateCategoryQueries(queryClient)

    for (const queryKey of keys) {
      expect(queryClient.getQueryState(queryKey)?.isInvalidated).toBe(true)
    }
  })
})

describe('invalidateFinancialQueries', () => {
  it('invalidates every financial prefix including both recurring evidence families', () => {
    const queryClient = new QueryClient()
    const affected = [
      ['transactions', { account: 'a1' }],
      ['dashboard', 'summary'],
      ['accounts', 'a1'],
      ['budgets', 'comparison'],
      ['reports', 'cash-flow'],
      ['drill-down', 'transactions'],
      ['recurring', 'recent-price-changes', 20],
      ['recurring', 'recent-price-changes', 1],
      ['recurring', 'price-history', 'rt-1'],
      ['recurring', 'price-history', 'rt-2'],
    ] as const
    // The recurring list is not derived from transaction evidence: an ordinary
    // financial mutation must not refetch it. Only the two history consumers
    // above depend on linked transaction rows.
    const unrelated = [
      ['recurring'],
      ['categories'],
      ['payees'],
      ['recurring-settings'],
    ] as const
    for (const key of [...affected, ...unrelated]) queryClient.setQueryData(key, [])

    invalidateFinancialQueries(queryClient)

    for (const key of affected) {
      expect(queryClient.getQueryState(key)?.isInvalidated, JSON.stringify(key)).toBe(true)
    }
    for (const key of unrelated) {
      expect(queryClient.getQueryState(key)?.isInvalidated, JSON.stringify(key)).toBe(false)
    }
  })
})
