/** Financial values remain decimal strings from the source to the server. */
export interface EvidenceLeg {
  key: string
  asset_symbol: string | null
  asset_id: string | null
  chain: string | null
  token_address: string | null
  isin: string | null
  provider_asset_id?: string | null
  direction: 'in' | 'out' | 'unknown'
  classification: string
  quantity: string | null
  unit_price: string | null
  execution_currency?: string | null
  unit_price_origin?: string | null
  subtotal: string | null
  total: string | null
  fee: string | null
  fee_currency: string | null
  valuation_currency: string | null
  valuation_amount: string | null
  external_funding_amount: string | null
  external_funding_currency: string | null
  acquisition_basis: string | null
  transaction_ref: string | null
  leg_ref: string | null
  execution_id: string | null
  token_program?: string | null
  source_address?: string | null
  destination_address?: string | null
  source_owner?: string | null
  destination_owner?: string | null
  raw_units?: string | null
  decimals?: number | null
  quantity_role?: string | null
  fee_payer?: string | null
  fee_semantics?: 'separate' | 'none' | 'included' | 'unknown'
  derivation?: Record<string, string>
}

export type EvidenceSourceKind = 'primary_activity' | 'balance_snapshot' | 'remaining_lots' | 'tax_workpaper' | 'recovery_notice'

export interface EvidenceObservation {
  reference: string
  source_reference?: string | null
  source: string
  source_kind: EvidenceSourceKind
  provider: string
  source_account_id: string | null
  account_external_id?: string | null
  holding_external_id?: string | null
  source_local_id: string | null
  source_locator: string
  observed_at: string | null
  event_time_raw: string | null
  event_date: string | null
  event_at: string | null
  timezone: string | null
  time_precision: 'unknown' | 'date' | 'minute' | 'second' | 'fractional'
  provider_status: string | null
  network_status: string | null
  settlement_status: 'settled' | 'pending' | 'failed' | 'unknown'
  order_ref: string | null
  historical_workspace_label: string | null
  coverage: string[]
  reason_codes?: string[]
  source_fields?: Record<string, string | null>
  legs: EvidenceLeg[]
}

export interface EvidenceSourceRef {
  observation_ref: string
  source: string
  source_local_id: string | null
  source_locator: string
  leg_key: string
}

export interface EvidenceCandidate {
  leg_id: string
  event_id: string
  asset_symbol: string | null
  direction: string
  classification: string
  quantity: string | null
  event_date: string | null
  source_refs: EvidenceSourceRef[]
}

export interface EvidenceRecord {
  observation_ref: string
  leg_key: string
  match_status: 'linked' | 'candidate' | 'conflicting' | 'unmatched'
  application_status: 'already_applied' | 'eligible' | 'blocked' | 'not_applicable'
  source_refs: EvidenceSourceRef[]
  candidate_legs: EvidenceCandidate[]
  link_ids: string[]
  links?: { link_id: string; leg: EvidenceCandidate; quantity: string | null }[]
  reason_codes: string[]
  conflicting_fields: string[]
  effects: { ledger_rows: number; units_delta: string | null; basis_delta: string | null }
}

export interface EvidencePreview {
  revision: string
  target: { workspace_id: string; workspace_name: string; group_id: string; group_name: string; account_id: string | null }
  observations: EvidenceObservation[]
  records: EvidenceRecord[]
  reconciliation: {
    asset_symbol: string | null
    chain?: string | null
    token_address?: string | null
    provider_asset_id?: string | null
    isin?: string | null
    opening_quantity: string | null
    opening_assumption: string
    opening_as_of: string | null
    snapshot_quantity: string | null
    snapshot_as_of: string | null
    settled_movement_quantity: string
    expected_closing_quantity?: string | null
    discrepancy?: string | null
    missing_coverage: string[]
    unresolved_fee_semantics: boolean
    unresolved_funding_semantics: boolean
    basis_complete: boolean
    history_complete: boolean
  }[]
}

export interface EvidenceDecision {
  observation_ref: string
  leg_key: string
  action: 'retain' | 'apply' | 'link'
  allocations?: { leg_id: string; quantity?: string | null }[]
  reason?: string
  settlement_confirmed?: boolean
}

export interface EvidenceOpeningBoundary {
  as_of: string
  overlap_reviewed: boolean
  assumption: string
}

export interface EvidenceResult {
  import_log_id: string | null
  imported: number
  retained: number
  linked: number
  evidence: EvidencePreview
}

export interface EvidenceImportOptions {
  mode?: 'orders' | 'evidence' | 'opening_lots'
  column_mapping?: Record<string, string>
  date_format?: string
  group_id?: string | null
  allow_unpriced?: boolean
  provider?: string
  source_account_id?: string
  source_kind?: EvidenceSourceKind
  connection_id?: string | null
  opening_boundary?: EvidenceOpeningBoundary
}
