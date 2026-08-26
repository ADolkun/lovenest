import { describe, expect, it } from 'vitest'

import {
  annualRows,
  draftError,
  draftFromContribution,
  draftPayload,
  emptyDraft,
  isPriorYearEntry,
  rowsByWallet,
  summariesByWallet,
  type ContributionDraft,
} from './contributions'
import type { AssetContribution, ContributionSummary } from '@/types'

let seq = 0

function contribution(overrides: Partial<AssetContribution> = {}): AssetContribution {
  return {
    id: `c-${++seq}`,
    group_id: 'w1',
    kind: 'contribution',
    party: 'self',
    amount: 100,
    date: '2026-03-01',
    tax_year: 2026,
    vested_on: null,
    is_vested: true,
    source: 'manual',
    notes: null,
    ...overrides,
  }
}

function summary(overrides: Partial<ContributionSummary> = {}): ContributionSummary {
  return {
    group_id: 'w1',
    own_contributions: 0,
    employer_contributions: 0,
    employer_vested: 0,
    employer_unvested: 0,
    distributions: 0,
    net: 0,
    return_net_of_contributions: null,
    current_value: null,
    years: [],
    ...overrides,
  }
}

function draft(overrides: Partial<ContributionDraft> = {}): ContributionDraft {
  return { ...emptyDraft('w1', '2026-08-25'), amount: '500', ...overrides }
}

describe('emptyDraft', () => {
  it('counts a new row against the year of the date it is dated', () => {
    expect(emptyDraft('w1', '2026-08-25')).toEqual({
      groupId: 'w1',
      kind: 'contribution',
      party: 'self',
      amount: '',
      date: '2026-08-25',
      taxYear: '2026',
      vestedOn: '',
      notes: '',
    })
  })
})

describe('draftFromContribution', () => {
  it('round-trips a row back through the payload it came from', () => {
    const row = contribution({
      kind: 'contribution',
      party: 'employer',
      amount: 1200.5,
      date: '2026-02-10',
      tax_year: 2025,
      vested_on: '2028-02-10',
      notes: 'match',
    })

    expect(draftPayload(draftFromContribution(row))).toEqual({
      group_id: 'w1',
      kind: 'contribution',
      party: 'employer',
      amount: 1200.5,
      date: '2026-02-10',
      tax_year: 2025,
      vested_on: '2028-02-10',
      notes: 'match',
    })
  })
})

describe('isPriorYearEntry', () => {
  it('is false when the tax year is the date year', () => {
    expect(isPriorYearEntry(draft({ date: '2026-08-25', taxYear: '2026' }))).toBe(false)
  })

  it('is true for a January-to-April contribution designated for last year', () => {
    expect(isPriorYearEntry(draft({ date: '2026-03-01', taxYear: '2025' }))).toBe(true)
  })

  it('is true for a hand-typed total predating provider coverage', () => {
    expect(isPriorYearEntry(draft({ date: '2026-08-25', taxYear: '2019' }))).toBe(true)
  })

  it('says nothing about a draft with no usable year', () => {
    expect(isPriorYearEntry(draft({ taxYear: '' }))).toBe(false)
    expect(isPriorYearEntry(draft({ date: '' }))).toBe(false)
  })
})

describe('draftError', () => {
  it('accepts a plain contribution', () => {
    expect(draftError(draft())).toBeNull()
  })

  it('demands a wallet', () => {
    expect(draftError(draft({ groupId: '' }))).toBe('assets.contribErrWallet')
  })

  it('rejects an empty, zero, negative or unreadable amount', () => {
    expect(draftError(draft({ amount: '' }))).toBe('assets.contribErrAmount')
    expect(draftError(draft({ amount: '0' }))).toBe('assets.contribErrAmount')
    expect(draftError(draft({ amount: '-5' }))).toBe('assets.contribErrAmount')
    expect(draftError(draft({ amount: 'abc' }))).toBe('assets.contribErrAmount')
  })

  it('demands a date', () => {
    expect(draftError(draft({ date: '' }))).toBe('assets.contribErrDate')
  })

  it('rejects a tax year outside the range the server accepts', () => {
    expect(draftError(draft({ taxYear: '' }))).toBe('assets.contribErrTaxYear')
    expect(draftError(draft({ taxYear: '26' }))).toBe('assets.contribErrTaxYear')
    expect(draftError(draft({ taxYear: '2201' }))).toBe('assets.contribErrTaxYear')
    expect(draftError(draft({ taxYear: '2026.5' }))).toBe('assets.contribErrTaxYear')
  })

  it('mirrors the server: an employer cannot take a distribution', () => {
    expect(draftError(draft({ party: 'employer', kind: 'distribution' }))).toBe(
      'assets.contribErrEmployerDistribution',
    )
  })

  it('mirrors the server: only employer money vests', () => {
    expect(draftError(draft({ party: 'self', vestedOn: '2028-01-01' }))).toBe(
      'assets.contribErrVestingOwnMoney',
    )
  })
})

describe('draftPayload', () => {
  it('drops a vesting date the party no longer justifies', () => {
    const payload = draftPayload(draft({ party: 'self', vestedOn: '2028-01-01' }))
    expect(payload.vested_on).toBeNull()
  })

  it('keeps the vesting date on employer money', () => {
    const payload = draftPayload(draft({ party: 'employer', vestedOn: '2028-01-01' }))
    expect(payload.vested_on).toBe('2028-01-01')
  })

  it('sends blank notes as null rather than an empty string', () => {
    expect(draftPayload(draft({ notes: '   ' })).notes).toBeNull()
  })
})

describe('summariesByWallet', () => {
  it('keys each summary by its wallet', () => {
    const map = summariesByWallet([summary({ group_id: 'w1', net: 10 }), summary({ group_id: 'w2', net: 20 })])
    expect(map.get('w2')?.net).toBe(20)
    expect(map.has('w3')).toBe(false)
  })
})

describe('annualRows', () => {
  it('orders years newest first and grosses up own plus employer', () => {
    const rows = annualRows(
      summary({
        years: [
          { tax_year: 2024, own: 6000, employer: 3000, distributions: 0, net: 9000 },
          { tax_year: 2026, own: 7000, employer: 3500, distributions: 500, net: 10000 },
          { tax_year: 2025, own: 6500, employer: 0, distributions: 0, net: 6500 },
        ],
      }),
    )

    expect(rows.map((r) => r.tax_year)).toEqual([2026, 2025, 2024])
    expect(rows.map((r) => r.gross)).toEqual([10500, 6500, 9000])
  })

  it('leaves the source summary untouched', () => {
    const source = summary({
      years: [
        { tax_year: 2024, own: 1, employer: 0, distributions: 0, net: 1 },
        { tax_year: 2026, own: 2, employer: 0, distributions: 0, net: 2 },
      ],
    })
    annualRows(source)
    expect(source.years.map((y) => y.tax_year)).toEqual([2024, 2026])
  })

  it('holds up on a wallet with no summary at all', () => {
    expect(annualRows(undefined)).toEqual([])
  })
})

describe('rowsByWallet', () => {
  it('groups by wallet and puts the newest movement first', () => {
    const map = rowsByWallet([
      contribution({ id: 'a', group_id: 'w1', date: '2026-01-05' }),
      contribution({ id: 'b', group_id: 'w2', date: '2026-06-01' }),
      contribution({ id: 'c', group_id: 'w1', date: '2026-07-30' }),
    ])

    expect(map.get('w1')?.map((r) => r.id)).toEqual(['c', 'a'])
    expect(map.get('w2')?.map((r) => r.id)).toEqual(['b'])
  })

  it('breaks a same-day tie on tax year then id, so a refetch cannot reshuffle', () => {
    const map = rowsByWallet([
      contribution({ id: 'b', date: '2026-03-01', tax_year: 2025 }),
      contribution({ id: 'a', date: '2026-03-01', tax_year: 2025 }),
      contribution({ id: 'c', date: '2026-03-01', tax_year: 2026 }),
    ])

    expect(map.get('w1')?.map((r) => r.id)).toEqual(['c', 'a', 'b'])
  })

  it('holds up on no rows', () => {
    expect(rowsByWallet([]).size).toBe(0)
  })
})
