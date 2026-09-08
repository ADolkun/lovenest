import type { EvidenceLeg } from './investment-evidence'

export interface TimelineTime {
  event_at: string | null
  event_date: string | null
  event_time_raw: string | null
  timezone: string | null
  time_precision: string
  ordering: 'exact' | 'within_day_unknown' | 'unknown' | 'conflicting'
}

export interface TimelineAsset {
  canonical_asset_key: string
  asset_symbol: string | null
  chain: string | null
  token_address: string | null
  token_program: string | null
  identity_status: 'canonical' | 'holding' | 'unresolved'
  asset_ids: string[]
}

export interface TimelineAccount {
  group_id: string | null
  group_name: string | null
  account_id: string | null
  connection_id: string | null
}

export interface TimelineSource {
  source_id: string
  source: string
  provider: string
  source_kind: string
  source_local_id: string | null
  source_locator: string | null
  source_reference: string | null
  observed_at: string | null
  time: TimelineTime
  original_type: string | null
  provider_status: string | null
  network_status: string | null
  settlement_status: string
  is_current: boolean
  availability: 'available' | 'unavailable'
  unavailable_reason: string | null
  detail_url: string
  collection_id: string | null
  payload_digest: string | null
  decoder_version: string | null
  account: TimelineAccount
}

export interface TimelineLeg extends EvidenceLeg {
  leg_id: string
  canonical_asset_key: string
  group_id: string | null
  source_ids: string[]
  settlement_status: string
  execution_status: string | null
  interpretation: string | null
  non_additive: boolean
  is_current: boolean
  reason_codes: string[]
}

export interface TimelineRelationship {
  kind: string
  state: string
  event_id: string | null
  source_id: string | null
  review_id: string | null
  leg_id: string | null
  quantity: string | null
  reason_codes: string[]
  conflicting_fields: string[]
  review_url: string | null
}

export interface TimelineCoverage {
  coverage_id: string
  source: string
  group_id: string | null
  connection_id: string | null
  collection_id: string | null
  chain: string | null
  requested: Record<string, unknown>
  observed: Record<string, unknown>
  last_successful_collection: string | null
  inventory: string
  retrieval: string
  interpretation: string
  settlement: string
  gaps: string[]
  streams: Record<string, unknown>
  source_url: string | null
  history_complete: false
}

export interface TimelineEvent {
  event_id: string
  native_trace_url: string | null
  kind: string
  status: string
  linkage: 'confirmed' | 'candidate' | 'conflicting' | 'unresolved'
  time: TimelineTime
  accounts: TimelineAccount[]
  assets: TimelineAsset[]
  legs: TimelineLeg[]
  sources: TimelineSource[]
  relationships: TimelineRelationship[]
  basis: {
    state: 'known' | 'partial' | 'unknown'
    acquisition_cost: string | null
    known_acquisition_cost: string | null
    unknown_basis_quantity: string | null
    reason_codes: string[]
  }
  tax_treatment: 'unresolved'
  reason_codes: string[]
  conflicting_fields: string[]
  coverage: TimelineCoverage[]
  transfers: Record<string, unknown>[]
  recovery: Record<string, unknown>[]
  incidents: Record<string, unknown>[]
}

export interface TimelineRead {
  workspace_id: string
  revision: string
  events: TimelineEvent[]
  assets: TimelineAsset[]
  coverage: TimelineCoverage[]
  total: number
  limit: number
  offset: number
  has_more: boolean
  all_available_records_loaded: boolean
  history_complete: false
  errors: Record<string, unknown>[]
}

export interface TimelineSourceDetail {
  workspace_id: string
  source: TimelineSource
  observation: Record<string, unknown> | null
  raw_payload: { encoding: 'json'; json: string } | null
  transaction: { encoding: 'json'; json: string } | null
  coverage: TimelineCoverage[]
}
