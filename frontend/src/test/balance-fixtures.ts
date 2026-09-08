import type { Asset, AssetGroup } from '@/types'
import type { BalanceExplanation } from '@/types/balance-explanation'

/** Invented source snapshot for the existing allocation fixtures. */
export function reportedWallet(wallet: AssetGroup, assets: Asset[] = []): AssetGroup {
  const holdings = assets.filter((asset) => asset.group_id === wallet.id && !asset.is_archived && !asset.sell_date).map((asset) => ({
    asset_id: asset.id, wallet_id: wallet.id, name: asset.name, ticker: asset.ticker, type: asset.type,
    quantity: asset.units ?? null, value: asset.current_value ?? asset.current_value_primary ?? null,
    currency: asset.currency ?? 'USD', observed_at: '2026-01-01T00:00:00Z',
  }))
  const amount = wallet.account_balance ?? null
  const complete = holdings.every((holding) => holding.value !== null)
  const total = holdings.reduce((sum, holding) => sum + (holding.value ?? 0), 0)
  const accountId = wallet.account_id ?? `account-${wallet.id}`
  const explanation: BalanceExplanation = {
    workspace_id: 'workspace', wallet_id: wallet.id, account_id: accountId, connection_id: wallet.connection_id ?? null,
    association: 'resolved', basis: 'reported_account_total', amount, currency: 'USD', observed_at: '2026-01-01T00:00:00Z',
    last_successful_sync_at: '2026-01-01T00:00:00Z', refresh_status: 'active', refresh_observed_at: '2026-01-01T00:00:00Z', coverage: 'complete', reason_codes: [], unreadable_count: 0,
    holdings, holdings_value: complete ? total : null, holdings_currency: 'USD', holdings_coverage: complete ? 'complete' : 'partial',
    residual_cash: amount === null || !complete ? null : Math.max(0, Math.round((amount - total) * 100) / 100),
    reconciliation: amount === null || !complete ? 'not_comparable' : amount < total ? 'difference' : 'residual_derived', difference: amount === null || !complete ? null : amount - total,
  }
  return { ...wallet, asset_count: holdings.length, account_id: accountId, connection_id: wallet.connection_id ?? null, balance_explanation: explanation }
}
