import type { Account, Asset, AssetGroup } from '@/types'
import type { BalanceExplanation, BalanceHolding } from '@/types/balance-explanation'

export function knownAmount(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** A missing converted value can only use native units in that same currency. */
export function valueInCurrency(value: { current_value: number | null; current_value_primary: number | null; currency: string | null }, currency: string): number | null {
  return knownAmount(value.current_value_primary) ?? (value.currency === currency ? knownAmount(value.current_value) : null)
}

export function scopedExplanation(subject: Account | AssetGroup, workspaceId?: string): BalanceExplanation | null {
  const detail = subject.balance_explanation
  if (!detail || (workspaceId && detail.workspace_id !== workspaceId)) return null
  if ('asset_count' in subject) {
    if (detail.wallet_id !== subject.id || detail.account_id !== (subject.account_id ?? null) || detail.connection_id !== subject.connection_id) return null
  } else if (detail.account_id !== subject.id || detail.connection_id !== subject.connection_id) return null
  return detail
}

export function accountBalance(account: Account, workspaceId?: string): number | null {
  const detail = scopedExplanation(account, workspaceId)
  // Explicit source facts cannot fall back to a legacy scope or currency.
  return account.balance_explanation
    ? detail?.currency && detail.currency === account.currency ? knownAmount(detail.amount) : null
    : knownAmount(account.current_balance)
}

export function accountBasis(account: Account, workspaceId?: string): BalanceExplanation['basis'] {
  return scopedExplanation(account, workspaceId)?.basis ?? (account.connection_id === null ? 'cash_ledger' : 'unknown')
}

/** A card on a shared credit line repeats the whole line's balance, so its row leads with its own open bill, as debt. */
export function sharedCardOpenBill(account: Pick<Account, 'type' | 'shared_balance_group' | 'open_bill_total'>): number | null {
  if (account.type !== 'credit_card' || !account.shared_balance_group) return null
  const bill = knownAmount(account.open_bill_total)
  return bill === null ? null : -bill
}

export function accountAggregate(accounts: Account[], currency: string, workspaceId?: string) {
  let total = 0
  let known = 0
  let incomplete = false
  const sharedGroups = new Set<string>()
  // Loans are long-term debt the net worth already counts, not money on hand.
  const counted = accounts.filter((account) => account.type !== 'loan')
  for (const account of counted) {
    // Cards drawing on one shared credit line each report that line's balance.
    if (account.shared_balance_group) {
      if (sharedGroups.has(account.shared_balance_group)) continue
      sharedGroups.add(account.shared_balance_group)
    }
    const detail = scopedExplanation(account, workspaceId)
    const native = accountBalance(account, workspaceId)
    const amount = native === null ? null : account.currency === currency ? native : null
    if (amount !== null) { total += amount; known++ }
    if (amount === null || (account.type === 'investment' && (!detail || detail.coverage !== 'complete' || detail.refresh_status !== 'active'))) incomplete = true
  }
  return { amount: known > 0 || counted.length === 0 ? total : null, incomplete }
}

/** Residuals require server-established scope/time compatibility, then the UI's currency. */
export function walletCash(wallet: AssetGroup, currency?: string): number | null {
  const detail = scopedExplanation(wallet)
  if (!detail || !currency || detail.holdings_currency !== currency || detail.currency !== currency || detail.association !== 'resolved' || detail.basis !== 'reported_account_total' || detail.coverage !== 'complete' || detail.refresh_status !== 'active' || detail.holdings_coverage !== 'complete' || !['residual_derived', 'matched', 'difference'].includes(detail.reconciliation)) return null
  const cash = knownAmount(detail.residual_cash)
  return cash === null ? null : Math.max(0, cash)
}

/** Compare the actual contributors before applying a retained snapshot to a current view. */
export function holdingsMatchSnapshot(expected: BalanceHolding[], holdings: Asset[]): boolean {
  return expected.length === holdings.length && expected.every((item) => holdings.some((asset) =>
    asset.id === item.asset_id && asset.group_id === item.wallet_id && asset.currency === item.currency &&
    knownAmount(asset.current_value) === knownAmount(item.value) &&
    knownAmount(asset.units) === knownAmount(item.quantity) &&
    (item.observation_basis !== 'quote' || Date.parse(asset.last_price_at ?? '') === Date.parse(item.observed_at ?? '')) &&
    (item.observation_basis !== 'recorded' || Date.parse(asset.value_updated_at ?? '') === Date.parse(item.observed_at ?? '')),
  ))
}

export function holdingComponents(holdings: BalanceHolding[]) {
  const totals = new Map<string, number>()
  const unpriced = new Set(holdings.filter((holding) => knownAmount(holding.value) === null).map((holding) => `${holding.currency}:${holding.ticker?.trim().toUpperCase()}`))
  for (const holding of holdings) {
    if (holding.ticker && knownAmount(holding.value) !== null) {
      const key = `${holding.currency}:${holding.ticker.trim().toUpperCase()}`
      totals.set(key, (totals.get(key) ?? 0) + holding.value!)
    }
  }
  const groups = new Map<string, { kind: string; currency: string | null; amount: number | null; partial: boolean }>()
  for (const holding of holdings) {
    const ticker = holding.ticker?.trim().toUpperCase()
    const kind = !ticker ? 'non_ticker_assets' : !unpriced.has(`${holding.currency}:${ticker}`) && (totals.get(`${holding.currency}:${ticker}`) ?? Infinity) < 1 ? 'dust' : holding.type === 'cash_equivalent' ? 'cash_equivalents' : 'positions'
    const key = `${kind}:${holding.currency}`
    const group = groups.get(key) ?? { kind, currency: holding.currency, amount: null, partial: false }
    const value = knownAmount(holding.value)
    if (value === null) group.partial = true
    else group.amount = (group.amount ?? 0) + value
    groups.set(key, group)
  }
  return [...groups.values()]
}
