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
