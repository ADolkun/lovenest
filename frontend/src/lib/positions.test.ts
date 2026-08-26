import { describe, expect, it } from 'vitest'

import { buildPortfolio, shareOfTotal, CASH_EQUIVALENT_TYPE, UNKNOWN_ACCOUNT_TYPE } from './positions'
import type { Asset, AssetGroup, TaxTreatment } from '@/types'

let seq = 0

function holding(overrides: {
  ticker?: string | null
  name?: string
  type?: string
  units?: number
  value?: number | null
  gain?: number | null
  groupId?: string | null
  sellDate?: string | null
  archived?: boolean
}): Asset {
  const value = overrides.value === undefined ? 0 : overrides.value
  return {
    id: `asset-${++seq}`,
    user_id: 'u1',
    name: overrides.name ?? 'Holding',
    type: overrides.type ?? 'stock',
    currency: 'USD',
    units: overrides.units ?? 0,
    valuation_method: 'market_price',
    purchase_date: null,
    purchase_price: null,
    sell_date: overrides.sellDate ?? null,
    sell_price: null,
    growth_type: null,
    growth_rate: null,
    growth_frequency: null,
    growth_start_date: null,
    is_archived: overrides.archived ?? false,
    position: 0,
    current_value: value,
    current_value_primary: value,
    gain_loss: overrides.gain === undefined ? 0 : overrides.gain,
    gain_loss_primary: overrides.gain === undefined ? 0 : overrides.gain,
    value_count: 1,
    source: 'simplefin',
    connection_id: null,
    isin: null,
    maturity_date: null,
    group_id: overrides.groupId ?? null,
    ticker: overrides.ticker === undefined ? 'VOO' : overrides.ticker,
    ticker_exchange: null,
    last_price: null,
    last_price_at: null,
    value_updated_at: null,
    logo_url: null,
    average_price: null,
    total_invested: null,
    realized_gain: null,
    transaction_count: 0,
  }
}

function wallet(
  id: string,
  accountType: string | null,
  taxTreatment: TaxTreatment = 'taxable',
  accountBalance: number | null = null,
): AssetGroup {
  return {
    id,
    user_id: 'u1',
    name: `Wallet ${id}`,
    icon: 'wallet',
    color: '#0EA5E9',
    position: 0,
    tax_treatment: taxTreatment,
    source: 'simplefin',
    connection_id: null,
    institution_name: null,
    account_type: accountType,
    asset_count: 1,
    current_value: 0,
    current_value_primary: 0,
    account_balance: accountBalance,
    currency: null,
  }
}

describe('consolidation across accounts', () => {
  it('shows a ticker once, summing quantity and value across accounts', () => {
    const { positions } = buildPortfolio(
      [
        holding({ ticker: 'VOO', units: 10, value: 5000, groupId: 'w1' }),
        holding({ ticker: 'voo', units: 4, value: 2000, groupId: 'w2' }),
        holding({ ticker: 'AAPL', units: 3, value: 600, groupId: 'w1' }),
      ],
      [wallet('w1', 'investment'), wallet('w2', 'investment')],
    )

    expect(positions.map((p) => p.ticker)).toEqual(['VOO', 'AAPL'])
    const voo = positions[0]
    expect(voo.quantity).toBe(14)
    expect(voo.value).toBe(7000)
  })

  it('sums quantities without leaking binary floating-point digits', () => {
    const { positions } = buildPortfolio(
      [
        holding({ ticker: 'SPAXX', units: 24183.95, value: 24183.95, groupId: 'w1' }),
        holding({ ticker: 'SPAXX', units: 25252.29, value: 25252.29, groupId: 'w2' }),
      ],
      [wallet('w1', 'investment'), wallet('w2', 'investment')],
    )

    expect(positions[0].quantity).toBe(49436.24)
  })

  it('breaks a position down per account', () => {
    const { positions } = buildPortfolio(
      [
        holding({ ticker: 'VOO', units: 10, value: 5000, groupId: 'w1' }),
        holding({ ticker: 'VOO', units: 4, value: 2000, groupId: 'w2' }),
      ],
      [wallet('w1', 'investment'), wallet('w2', 'investment', 'roth')],
    )

    // Legs are ordered by value, so the heaviest account reads first.
    expect(positions[0].legs.map((l) => [l.walletId, l.quantity, l.value])).toEqual([
      ['w1', 10, 5000],
      ['w2', 4, 2000],
    ])
    expect(positions[0].legs[1].taxTreatment).toBe('roth')
  })

  it('leaves out sold, archived and non-ticker assets', () => {
    const { positions, total } = buildPortfolio(
      [
        holding({ ticker: 'VOO', value: 100 }),
        holding({ ticker: 'SOLD', value: 999, sellDate: '2026-01-01' }),
        holding({ ticker: 'GONE', value: 999, archived: true }),
        holding({ ticker: null, name: 'House', type: 'real_estate', value: 999 }),
      ],
      [],
    )

    expect(positions.map((p) => p.ticker)).toEqual(['VOO'])
    expect(total).toBe(100)
  })

  it('files a holding in no wallet under an unknown account type', () => {
    const { byAccountType } = buildPortfolio([holding({ value: 100, groupId: null })], [])

    expect(byAccountType).toEqual([{ key: UNKNOWN_ACCOUNT_TYPE, value: 100, weight: 1 }])
  })
})

describe('cost basis and unrealised gain', () => {
  it('sums basis across accounts and averages cost over total quantity', () => {
    const { positions } = buildPortfolio(
      [
        holding({ ticker: 'VOO', units: 10, value: 5000, gain: 1000, groupId: 'w1' }),
        holding({ ticker: 'VOO', units: 10, value: 5000, gain: 3000, groupId: 'w2' }),
      ],
      [wallet('w1', 'investment'), wallet('w2', 'investment')],
    )

    const voo = positions[0]
    expect(voo.costBasis).toBe(6000)
    expect(voo.averageCost).toBe(300)
    expect(voo.gain).toBe(4000)
    expect(voo.gainPct).toBeCloseTo(4000 / 6000)
  })

  it('reports a loss in both dollars and percent', () => {
    const { positions } = buildPortfolio(
      [holding({ ticker: 'ARKK', units: 100, value: 800, gain: -200 })],
      [],
    )

    expect(positions[0].costBasis).toBe(1000)
    expect(positions[0].gain).toBe(-200)
    expect(positions[0].gainPct).toBeCloseTo(-0.2)
  })

  it('reports no basis at all when one account never reported one', () => {
    // A Snapshot Holding with no basis: summing only the known half would
    // report the unknown half's whole value as gain.
    const { positions } = buildPortfolio(
      [
        holding({ ticker: 'VOO', units: 10, value: 5000, gain: 1000, groupId: 'w1' }),
        holding({ ticker: 'VOO', units: 10, value: 5000, gain: null, groupId: 'w2' }),
      ],
      [wallet('w1', 'investment'), wallet('w2', 'investment')],
    )

    expect(positions[0].costBasis).toBeNull()
    expect(positions[0].gain).toBeNull()
    expect(positions[0].gainPct).toBeNull()
    expect(positions[0].averageCost).toBeNull()
    expect(positions[0].legs[1].costBasis).toBeNull()
  })

  it('gives no percentage on a zero cost basis', () => {
    const { positions } = buildPortfolio(
      [holding({ ticker: 'AIR', units: 5, value: 300, gain: 300 })],
      [],
    )

    expect(positions[0].costBasis).toBe(0)
    expect(positions[0].gainPct).toBeNull()
  })
})

describe('weight and allocation', () => {
  it('weighs positions against the invested total, dust excluded', () => {
    const { positions, total, investedTotal, dustTotal } = buildPortfolio(
      [
        holding({ ticker: 'VOO', value: 750 }),
        holding({ ticker: 'AAPL', value: 250 }),
        holding({ ticker: 'SHIB', value: 0.4 }),
      ],
      [],
    )

    const byTicker = Object.fromEntries(positions.map((p) => [p.ticker, p]))
    expect(byTicker.VOO.weight).toBe(0.75)
    expect(byTicker.AAPL.weight).toBe(0.25)
    expect(byTicker.SHIB.isDust).toBe(true)
    expect(byTicker.SHIB.weight).toBeNull()
    expect(investedTotal).toBe(1000)
    expect(dustTotal).toBe(0.4)
    expect(total).toBe(1000.4)
  })

  it('does not mistake an unpriced holding for dust', () => {
    const { positions, dustTotal } = buildPortfolio(
      [
        holding({ ticker: 'VOO', value: 1000 }),
        holding({ ticker: 'ILLIQ', units: 5, value: null, gain: null }),
      ],
      [],
    )

    const illiquid = positions.find((p) => p.ticker === 'ILLIQ')!
    expect(illiquid.isDust).toBe(false)
    expect(dustTotal).toBe(0)
  })

  it('counts a cash equivalent in the total but never in allocation', () => {
    const { total, investedTotal, cashEquivalentTotal, byAssetClass, positions } = buildPortfolio(
      [
        holding({ ticker: 'VOO', value: 1000, type: 'etf' }),
        holding({ ticker: 'SPAXX', value: 49000, type: CASH_EQUIVALENT_TYPE }),
      ],
      [],
    )

    expect(total).toBe(50000)
    expect(investedTotal).toBe(1000)
    expect(cashEquivalentTotal).toBe(49000)
    expect(byAssetClass).toEqual([{ key: 'etf', value: 1000, weight: 1 }])
    expect(positions.find((p) => p.ticker === 'SPAXX')?.weight).toBeNull()
  })

  it('allocates by asset class and by account type, ranked by value', () => {
    const { byAssetClass, byAccountType } = buildPortfolio(
      [
        holding({ ticker: 'VOO', type: 'etf', value: 600, groupId: 'w1' }),
        holding({ ticker: 'AAPL', type: 'stock', value: 300, groupId: 'w2' }),
        holding({ ticker: 'BTC', type: 'crypto', value: 100, groupId: 'w2' }),
      ],
      [wallet('w1', 'investment'), wallet('w2', 'cash')],
    )

    expect(byAssetClass).toEqual([
      { key: 'etf', value: 600, weight: 0.6 },
      { key: 'stock', value: 300, weight: 0.3 },
      { key: 'crypto', value: 100, weight: 0.1 },
    ])
    expect(byAccountType).toEqual([
      { key: 'investment', value: 600, weight: 0.6 },
      { key: 'cash', value: 400, weight: 0.4 },
    ])
  })

  it('splits one ticker held in two account types across both buckets', () => {
    const { byAccountType, byAssetClass } = buildPortfolio(
      [
        holding({ ticker: 'VOO', type: 'etf', value: 700, groupId: 'w1' }),
        holding({ ticker: 'VOO', type: 'etf', value: 300, groupId: 'w2' }),
      ],
      [wallet('w1', 'investment'), wallet('w2', 'savings')],
    )

    expect(byAssetClass).toEqual([{ key: 'etf', value: 1000, weight: 1 }])
    expect(byAccountType).toEqual([
      { key: 'investment', value: 700, weight: 0.7 },
      { key: 'savings', value: 300, weight: 0.3 },
    ])
  })

  it('holds up on an empty portfolio', () => {
    const portfolio = buildPortfolio([], [])

    expect(portfolio.positions).toEqual([])
    expect(portfolio.total).toBe(0)
    expect(portfolio.investedTotal).toBe(0)
    expect(portfolio.byAssetClass).toEqual([])
  })
})

describe('liquid cash', () => {
  it('derives settled cash as the account balance less what the wallet holds', () => {
    const { liquidCashTotal, total } = buildPortfolio(
      [holding({ ticker: 'AMC', value: 137.96, groupId: 'w1' })],
      [wallet('w1', 'investment', 'taxable', 535.26)],
    )

    expect(liquidCashTotal).toBe(397.3)
    expect(total).toBe(535.26)
  })

  it('floors at zero when the provider reports no balance against real holdings', () => {
    // Coinbase prices its own positions and leaves the account balance at 0.
    // Without the floor this wallet would report −52980.02 and eat the other's cash.
    const { liquidCashTotal } = buildPortfolio(
      [
        holding({ ticker: 'BTC', value: 52980.02, groupId: 'w1' }),
        holding({ ticker: 'AMC', value: 137.96, groupId: 'w2' }),
      ],
      [
        wallet('w1', 'crypto', 'taxable', 0),
        wallet('w2', 'investment', 'taxable', 535.26),
      ],
    )

    expect(liquidCashTotal).toBe(397.3)
  })

  it('treats a whole balance as cash when the wallet holds nothing', () => {
    const { liquidCashTotal, investedTotal, total } = buildPortfolio(
      [],
      [wallet('w1', 'investment', 'taxable', 500)],
    )

    expect(liquidCashTotal).toBe(500)
    expect(investedTotal).toBe(0)
    expect(total).toBe(500)
  })

  it('derives nothing when the holdings exactly match the balance', () => {
    const { liquidCashTotal } = buildPortfolio(
      [
        holding({ ticker: 'VOO', value: 24183.95, groupId: 'w1' }),
        holding({ ticker: 'AAPL', value: 45791.1, groupId: 'w1' }),
      ],
      [wallet('w1', 'investment', 'taxable', 69975.05)],
    )

    expect(liquidCashTotal).toBe(0)
  })

  it('derives no cash from a wallet whose account reported no balance', () => {
    // Null is not zero: a manual wallet has no account behind it to subtract from.
    const { liquidCashTotal, total } = buildPortfolio(
      [holding({ ticker: 'VOO', value: 1000, groupId: 'w1' })],
      [wallet('w1', null)],
    )

    expect(liquidCashTotal).toBe(0)
    expect(total).toBe(1000)
  })

  it('keeps liquid cash out of allocation and the invested total', () => {
    const { investedTotal, byAssetClass, byAccountType } = buildPortfolio(
      [holding({ ticker: 'VOO', type: 'etf', value: 1000, groupId: 'w1' })],
      [wallet('w1', 'investment', 'taxable', 1500)],
    )

    expect(investedTotal).toBe(1000)
    expect(byAssetClass).toEqual([{ key: 'etf', value: 1000, weight: 1 }])
    expect(byAccountType).toEqual([{ key: 'investment', value: 1000, weight: 1 }])
  })

  it('adds the equivalents that behave as cash to the cash total', () => {
    const { cashTotal, cashEquivalentTotal, liquidCashTotal } = buildPortfolio(
      [
        holding({ ticker: 'SPAXX', type: CASH_EQUIVALENT_TYPE, value: 400, groupId: 'w1' }),
        holding({ ticker: 'VOO', type: 'etf', value: 600, groupId: 'w1' }),
      ],
      [wallet('w1', 'investment', 'taxable', 1100)],
    )

    expect(cashEquivalentTotal).toBe(400)
    expect(liquidCashTotal).toBe(100)
    expect(cashTotal).toBe(500)
  })
})

describe('shareOfTotal', () => {
  it('reads cash as a percentage of the whole portfolio', () => {
    const { cashTotal, total } = buildPortfolio(
      [
        holding({ ticker: 'SPAXX', type: CASH_EQUIVALENT_TYPE, value: 100, groupId: 'w1' }),
        holding({ ticker: 'VOO', type: 'etf', value: 700, groupId: 'w1' }),
      ],
      [wallet('w1', 'investment', 'taxable', 1000)],
    )

    expect(total).toBe(1000)
    expect(cashTotal).toBe(300)
    expect(shareOfTotal(cashTotal, total)).toBe(0.3)
  })

  it('gives no share of an empty portfolio rather than NaN', () => {
    expect(shareOfTotal(0, 0)).toBe(0)
  })
})
