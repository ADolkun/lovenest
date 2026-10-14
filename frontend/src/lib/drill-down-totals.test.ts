import { describe, expect, it } from 'vitest'

import { sumDrillDownTotals } from '@/lib/drill-down-totals'
import type { DrillDownTotalsItem } from '@/lib/drill-down-totals'

const row = (overrides: Partial<DrillDownTotalsItem> = {}): DrillDownTotalsItem => ({
  type: 'debit',
  amount: 100,
  linkedAmount: 0,
  amountPrimary: null,
  currency: 'USD',
  isPending: false,
  isProjected: false,
  ...overrides,
})

describe('sumDrillDownTotals', () => {
  it('splits rows into posted, pending and projected', () => {
    const totals = sumDrillDownTotals([
      row({ amount: 200 }),
      row({ amount: 100, isPending: true }),
      row({ amount: 487.68, isProjected: true }),
    ], 'USD')

    expect(totals.postedTotal).toBeCloseTo(200)
    expect(totals.pendingTotal).toBeCloseTo(100)
    expect(totals.projectedTotal).toBeCloseTo(487.68)
    expect(totals.total).toBeCloseTo(787.68)
  })

  it('keeps a projected row inside the total when the panel also has a pending one', () => {
    // The regression: a projected row is neither posted nor pending, so
    // bucketing it by transaction status dropped it from the footer while
    // the list above still showed it.
    const totals = sumDrillDownTotals([
      row({ amount: 100, isPending: true }),
      row({ amount: 487.68, isProjected: true }),
    ], 'USD')

    expect(totals.postedTotal + totals.pendingTotal + totals.projectedTotal)
      .toBeCloseTo(totals.total)
    expect(totals.total).toBeCloseTo(587.68)
  })

  it('never loses a row: the three buckets always rebuild the total', () => {
    const totals = sumDrillDownTotals([
      row({ amount: 10 }),
      row({ amount: 20, isPending: true }),
      row({ amount: 30, isProjected: true }),
      row({ amount: 40, isPending: true, isProjected: true }),
    ], 'USD')

    expect(totals.postedTotal + totals.pendingTotal + totals.projectedTotal)
      .toBeCloseTo(totals.total)
  })

  it('counts a foreign row through its primary amount', () => {
    expect(sumDrillDownTotals([
      row({ amount: 2500, amountPrimary: 487.68, currency: 'BRL' }),
    ], 'USD')).toMatchObject({ total: 487.68, postedTotal: 487.68 })
  })

  it('skips a foreign row with no conversion rather than counting it raw', () => {
    expect(sumDrillDownTotals([
      row({ amount: 2500, amountPrimary: null, currency: 'BRL' }),
    ], 'USD')).toEqual({
      total: 0,
      postedTotal: 0,
      pendingTotal: 0,
      projectedTotal: 0,
    })
  })

  it('reads amounts as magnitudes, whatever sign they arrive with', () => {
    expect(sumDrillDownTotals([
      row({ amount: -100 }),
      row({ amount: 100 }),
    ], 'USD')).toMatchObject({ total: 200, postedTotal: 200 })
  })

  it('adds every row without a netting side, whatever its type', () => {
    expect(sumDrillDownTotals([
      row({ amount: 100 }),
      row({ type: 'credit', amount: 30 }),
    ], 'USD')).toMatchObject({ total: 130 })
  })

  it('nets rows of the other type against the total, bucket by bucket', () => {
    const totals = sumDrillDownTotals([
      row({ amount: 120 }),
      row({ type: 'credit', amount: 45 }),
      row({ type: 'credit', amount: 10, isPending: true }),
      row({ amount: 25, isProjected: true }),
    ], 'USD', 'debit')

    expect(totals.total).toBeCloseTo(90)
    expect(totals.postedTotal).toBeCloseTo(75)
    expect(totals.pendingTotal).toBeCloseTo(-10)
    expect(totals.projectedTotal).toBeCloseTo(25)
  })

  it('counts a reimbursed expense net of what repaid it', () => {
    expect(sumDrillDownTotals([
      row({ amount: 120, linkedAmount: 45 }),
    ], 'USD', 'debit')).toMatchObject({ total: 75, postedTotal: 75 })
  })

  it('scales a foreign row through the same linked share of its primary amount', () => {
    const totals = sumDrillDownTotals([
      row({ amount: 200, linkedAmount: 50, amountPrimary: 40, currency: 'BRL' }),
    ], 'USD', 'debit')
    expect(totals.total).toBeCloseTo(30)
  })

  it('drops a fully linked credit and keeps only the remainder of a partial one', () => {
    const totals = sumDrillDownTotals([
      row({ amount: 120, linkedAmount: 45 }),
      row({ type: 'credit', amount: 45, linkedAmount: 45 }),
      row({ type: 'credit', amount: 100, linkedAmount: 91.96 }),
    ], 'USD', 'debit')
    expect(totals.total).toBeCloseTo(75 - 8.04)
  })

  it('never lets links push a row below zero', () => {
    expect(sumDrillDownTotals([
      row({ amount: 20, linkedAmount: 25 }),
    ], 'USD', 'debit').total).toBe(0)
  })

  it('returns zeroes for an empty panel', () => {
    expect(sumDrillDownTotals([], 'USD')).toEqual({
      total: 0,
      postedTotal: 0,
      pendingTotal: 0,
      projectedTotal: 0,
    })
  })
})
