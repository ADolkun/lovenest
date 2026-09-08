import type { Account, AssetGroup } from '@/types'
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
  // A present but wrong-scope response must never fall back to its headline.
  return account.balance_explanation ? knownAmount(detail?.amount) : knownAmount(account.current_balance)
}

export function accountBasis(account: Account, workspaceId?: string): BalanceExplanation['basis'] {
  return scopedExplanation(account, workspaceId)?.basis ?? (account.connection_id === null ? 'cash_ledger' : 'unknown')
}

export function accountAggregate(accounts: Account[], currency: string, workspaceId?: string) {
  let total = 0
  let known = 0
  let incomplete = false
  for (const account of accounts) {
    const detail = scopedExplanation(account, workspaceId)
    const native = accountBalance(account, workspaceId)
    const amount = native === null ? null : account.currency === currency ? native : null
    if (amount !== null) { total += amount; known++ }
    if (amount === null || (account.type === 'investment' && (!detail || detail.coverage !== 'complete' || detail.refresh_status !== 'active'))) incomplete = true
  }
  return { amount: known > 0 || accounts.length === 0 ? total : null, incomplete }
}

/** Residuals require server-established scope/time compatibility, then the UI's currency. */
export function walletCash(wallet: AssetGroup, currency?: string): number | null {
  const detail = scopedExplanation(wallet)
  if (!detail || !currency || detail.holdings_currency !== currency || detail.currency !== currency || detail.association !== 'resolved' || detail.basis !== 'reported_account_total' || detail.coverage !== 'complete' || detail.refresh_status !== 'active' || detail.holdings_coverage !== 'complete' || !['residual_derived', 'matched', 'difference'].includes(detail.reconciliation)) return null
  const cash = knownAmount(detail.residual_cash)
  return cash === null ? null : Math.max(0, cash)
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
