import { describe, expect, it } from 'vitest'
import { accountAggregate, accountBalance, holdingComponents, knownAmount, scopedExplanation, valueInCurrency, walletCash } from './balance-explanation'
import { buildPortfolio } from './positions'
import { reportedWallet } from '@/test/balance-fixtures'
import type { Account, Asset, AssetGroup } from '@/types'

const asset = { id: 'holding-a', group_id: 'wallet-a', name: 'Example fund', ticker: 'EXAMPLE', type: 'stock', currency: 'USD', units: 7, current_value: 70, current_value_primary: 70, is_archived: false, sell_date: null } as Asset
const wallet = reportedWallet({ id: 'wallet-a', name: 'Example wallet', account_balance: 120, currency: 'USD' } as AssetGroup, [asset])
const account = { id: wallet.account_id, connection_id: wallet.connection_id, type: 'investment', current_balance: 120, currency: 'USD', balance_explanation: { ...wallet.balance_explanation!, wallet_id: null } } as Account

describe('balance explanation contract', () => {
  it('preserves numeric zero and rejects unusable amounts', () => {
    expect(knownAmount(0)).toBe(0)
    for (const value of [null, undefined, '', '0', NaN, Infinity]) expect(knownAmount(value)).toBeNull()
  })

  it('rejects another workspace, account or connection even when display names agree', () => {
    expect(scopedExplanation(account, 'workspace')).not.toBeNull()
    expect(accountBalance(account, 'other-workspace')).toBeNull()
    expect(scopedExplanation({ ...wallet, account_id: 'other-account' })).toBeNull()
    expect(scopedExplanation({ ...wallet, connection_id: 'other-connection' })).toBeNull()
  })

  it('retains positive account subtotals and excludes unverified foreign conversions', () => {
    const foreign = { ...account, id: 'foreign', currency: 'EUR', balance_primary: 999, balance_explanation: undefined }
    const unknown = { ...account, id: 'unknown', current_balance: null, balance_explanation: undefined } as unknown as Account
    expect(accountAggregate([account, foreign, unknown], 'USD', 'workspace')).toEqual({ amount: 120, incomplete: true })
    expect(accountAggregate([unknown], 'USD')).toEqual({ amount: null, incomplete: true })
    expect(accountAggregate([{ ...account, balance_explanation: { ...account.balance_explanation!, amount: 0 } }], 'USD')).toEqual({ amount: 0, incomplete: false })
    expect(accountAggregate([{ ...account, balance_explanation: { ...account.balance_explanation!, coverage: 'partial' } }], 'USD')).toEqual({ amount: 120, incomplete: true })
  })

  it('only consumes compatible explicit cash and preserves the nonnegative floor', () => {
    expect(walletCash(wallet, 'USD')).toBe(50)
    expect(walletCash(wallet, 'EUR')).toBeNull()
    expect(walletCash({ ...wallet, balance_explanation: undefined }, 'USD')).toBeNull()
    for (const reconciliation of ['not_comparable', 'shared_derivation', 'separate_cash_ledger'] as const) {
      expect(walletCash({ ...wallet, balance_explanation: { ...wallet.balance_explanation!, reconciliation } }, 'USD')).toBeNull()
    }
    expect(walletCash({ ...wallet, balance_explanation: { ...wallet.balance_explanation!, residual_cash: -10, reconciliation: 'difference' } }, 'USD')).toBe(0)
  })

  it('does not turn absent holdings, unpriced holdings or legacy metadata into cash', () => {
    expect(buildPortfolio([asset], [wallet], 'USD').liquidCashTotal).toBe(50)
    for (const holdings of [[], [{ ...asset, current_value: null, current_value_primary: null }], [{ ...asset, current_value: 80, current_value_primary: 80 }]]) {
      expect(buildPortfolio(holdings, [wallet], 'USD').unknownCashWalletIds).toEqual(['wallet-a'])
    }
    expect(buildPortfolio([asset], [{ ...wallet, balance_explanation: undefined }], 'USD').unknownCashWalletIds).toEqual(['wallet-a'])
  })

  it('keeps the excluded non-ticker component named without adding it twice', () => {
    const nonTicker = { ...asset, id: 'private', name: 'Private asset', ticker: null, current_value: 20, current_value_primary: 20 }
    const scoped = reportedWallet(wallet, [asset, nonTicker])
    expect(buildPortfolio([asset, nonTicker], [scoped], 'USD').total).toBe(100)
    expect(holdingComponents(scoped.balance_explanation!.holdings)).toEqual([
      { kind: 'positions', currency: 'USD', amount: 70, partial: false },
      { kind: 'non_ticker_assets', currency: 'USD', amount: 20, partial: false },
    ])
  })
})

it('uses same-currency native values when the converted field is absent, never foreign units', () => {
  expect(valueInCurrency({ current_value: 70, current_value_primary: null, currency: 'USD' }, 'USD')).toBe(70)
  expect(valueInCurrency({ current_value: 70, current_value_primary: null, currency: 'EUR' }, 'USD')).toBeNull()
  expect(valueInCurrency({ current_value: 70, current_value_primary: 0, currency: 'USD' }, 'USD')).toBe(0)
})

it.each([null, 'EUR'])('withholds explicit missing or different source currency: %s', (currency) => {
  const invalid = { ...account, balance_explanation: { ...account.balance_explanation!, currency } }
  expect(accountBalance(invalid, 'workspace')).toBeNull()
  expect(accountAggregate([invalid], 'USD', 'workspace')).toEqual({ amount: null, incomplete: true })
  expect(accountAggregate([account, invalid], 'USD', 'workspace')).toEqual({ amount: 120, incomplete: true })
})

it('rejects changed quote age, quantity, native currency and value without rejecting supported zero', () => {
  const quoted = { ...asset, last_price_at: '2026-01-01T00:00:00Z' }
  const snapshot = { ...wallet, balance_explanation: { ...wallet.balance_explanation!, holdings: wallet.balance_explanation!.holdings.map((holding) => ({ ...holding, observation_basis: 'quote' })) } }
  expect(buildPortfolio([quoted], [snapshot], 'USD').liquidCashTotal).toBe(50)
  for (const changed of [{ ...quoted, last_price_at: '2026-01-02T00:00:00Z' }, { ...quoted, units: 8 }, { ...quoted, currency: 'EUR' }, { ...quoted, current_value: 80 }]) {
    expect(buildPortfolio([changed], [snapshot], 'USD').unknownCashWalletIds).toEqual(['wallet-a'])
  }
  const zero = { ...quoted, current_value: 0, current_value_primary: 0 }
  expect(buildPortfolio([zero], [reportedWallet({ ...wallet, account_balance: 0 }, [zero])], 'USD').unknownCashWalletIds).toEqual([])
})
