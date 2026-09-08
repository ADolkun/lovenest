/** Source-owned balance facts; null is unknown, including observation age. */
export interface BalanceExplanation {
  workspace_id: string
  wallet_id: string | null
  account_id: string | null
  connection_id: string | null
  association: 'resolved' | 'missing_or_ambiguous'
  basis: 'reported_account_total' | 'connector_calculated_subtotal' | 'cash_ledger' | 'unknown'
  amount: number | null
  currency: string | null
  observation_basis?: string
  value_date?: string | null
  observed_at: string | null
  last_successful_sync_at: string | null
  refresh_status: string
  refresh_observed_at: string | null
  coverage: 'complete' | 'partial' | 'unknown'
  reason_codes: string[]
  unreadable_count: number | null
  omitted_count?: number | null
  holdings: BalanceHolding[]
  holdings_value: number | null
  holdings_currency: string | null
  holdings_coverage: 'complete' | 'partial' | 'unavailable'
  residual_cash: number | null
  reconciliation: 'matched' | 'residual_derived' | 'difference' | 'not_comparable' | 'shared_derivation' | 'separate_cash_ledger'
  difference: number | null
}

export interface BalanceHolding {
  asset_id: string
  wallet_id: string | null
  name: string
  ticker: string | null
  type: string
  quantity: number | null
  value: number | null
  currency: string | null
  observation_basis?: string
  value_date?: string | null
  observed_at: string | null
}
