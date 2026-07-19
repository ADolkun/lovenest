import { describe, expect, it } from 'vitest'
import { compareTransactionAmountsDesc } from './transaction-sort'

describe('compareTransactionAmountsDesc', () => {
  it('sorts by primary-currency amount, then newest date', () => {
    const item = (amount: number, amountPrimary: number | null, orderDate: string) => ({
      amount,
      amountPrimary,
      orderDate,
    })
    const items = [
      item(100, 10, '2026-07-03'),
      item(20, null, '2026-07-01'),
      item(5, 20, '2026-07-02'),
    ]

    expect(items.sort(compareTransactionAmountsDesc).map(({ amount }) => amount)).toEqual([5, 20, 100])
  })
})
