import type { TimelineEvent } from './timeline'

export interface InvestigationRequest {
  event_id: string
  leg_id: string
  direction: 'in' | 'out'
  since?: string | null
  until?: string | null
  max_hops: number
  max_branches: number
  minimums: Record<string, string>
}

export interface InvestigationRead {
  workspace_id: string
  request: InvestigationRequest
  collection_id?: string | null
  revision?: string | null
  events: TimelineEvent[]
  steps: {
    event_id: string
    leg_id: string
    asset_key: string
    depth: number
    via: string
    effective_window: { since: string | null; until: string | null }
  }[]
  frontier: {
    key: string
    event_id: string
    leg_id: string
    chain: string
    address: string
    asset_key: string
    direction: 'in' | 'out'
    since: string | null
    until: string | null
    depth: number
    resumable?: boolean
  }[]
  boundaries: { code: string; event_id?: string; leg_id?: string }[]
  evidence?: { limits?: Record<string, unknown>; coverage?: Record<string, string>; gaps?: string[] } | null
  history_complete: false
}

export interface InvestigationContinueRequest extends InvestigationRequest {
  collection_id: string
  expected_revision: string
  frontier_key: string
}

export interface BridgeCandidate {
  source_event_id: string
  source_leg_id: string
  destination_event_id: string
  destination_leg_id: string
  source_id: string
  destination_source_id: string
  protocol: string
  message_id: string
  status: 'eligible' | 'unresolved' | 'confirmed'
  reason_codes: string[]
  collection_id: string | null
  revision: string | null
  source_summary: Record<string, unknown>
  destination_summary: Record<string, unknown>
  omitted_candidates?: number
}

export interface BridgeReviewRequest {
  collection_id: string
  expected_revision: string
  source_event_id: string
  source_leg_id: string
  destination_event_id: string
  destination_leg_id: string
  source_id: string
  destination_source_id: string
  reviewed: true
}
