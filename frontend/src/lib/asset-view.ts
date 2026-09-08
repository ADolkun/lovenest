import type { Asset, AssetTransaction } from '@/types'

/** Keep shared links to the former four tabs meaningful after consolidation. */
export function readAssetView(params: URLSearchParams) {
  const tab = params.get('tab')
  const activity = params.get('activity')
  const view = params.get('view')
  return {
    tab: tab === 'activity' || tab === 'transactions' || tab === 'contributions'
      ? 'activity' as const : 'portfolio' as const,
    view: view === 'wallets' || tab === 'holdings' || (!view && !!params.get('wallet'))
      ? 'wallets' as const : 'assets' as const,
    activity: tab === 'contributions' || activity === 'contributions'
      ? 'contributions' as const : activity === 'wallets' ? 'wallets' as const : activity === 'transfers' ? 'transfers' as const : activity === 'recovery' ? 'recovery' as const : 'trades' as const,
  }
}

/** The ledger endpoint is workspace-wide; the visible holdings define its slice. */
export function transactionsForHoldings(rows: AssetTransaction[], holdings: Asset[]): AssetTransaction[] {
  const ids = new Set(holdings.map((holding) => holding.id))
  return rows.filter((row) => ids.has(row.asset_id))
}
