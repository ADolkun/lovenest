import { describe, expect, it } from 'vitest'
import { transactionOrderDate } from './transaction-order-date'

describe('transactionOrderDate', () => {
  it('uses manual bill override first', () => {
    expect(transactionOrderDate({
      date: '2026-06-02',
      effective_date: '2026-07-10',
      effective_bill_date: '2026-08-10',
    }, true)).toBe('2026-08-10')
  })

  it('uses purchase date in cash mode', () => {
    expect(transactionOrderDate({
      date: '2026-06-02',
      effective_date: '2026-07-10',
    }, false)).toBe('2026-06-02')
  })

  it('uses effective date in accrual mode', () => {
    expect(transactionOrderDate({
      date: '2026-06-02',
      effective_date: '2026-07-10',
    }, true)).toBe('2026-07-10')
  })
})
