import api from '@/lib/api'
import type { EvidenceLeg, EvidenceObservation, EvidencePreview } from '@/types/investment-evidence'

export interface SourceSemantics {
  clock_role: 'execution' | 'posted' | 'settled' | 'reported_unknown'
  amount_field: 'unit_price' | 'subtotal' | 'total' | 'valuation_amount' | 'fee' | 'acquisition_basis'
  amount_meaning: 'execution_unit_price' | 'execution_subtotal' | 'fee_inclusive_total' | 'valuation' | 'reported_fee' | 'reported_basis' | 'unknown'
  decimal_places?: number | null
}
export interface SourceReviewRequest {
  group_id: string
  request_key: string
  action: 'associate' | 'revoke' | 'correct' | 'reverse'
  reason: string
  source_leg_id?: string
  target_leg_id?: string
  source_semantics?: SourceSemantics
  target_semantics?: SourceSemantics
  same_execution_reviewed?: boolean
  review_id?: string
}
export interface SourceReviewEffects {
  ledger_rows_added: number
  units_delta: string
  cost_delta: string
  before?: { id: string; quantity: string; price: string; fee: string; date: string; source: string }
  after?: { id: string; quantity: string; price: string; fee: string; date: string; source: string }
  cost_before?: string
  cost_after?: string
  position_quantity_after?: string
  position_cost_after?: string
  original_source_fee?: string | null
  fee_treatment?: string
  reported_quantity?: string | null
  provider_snapshot_preserved?: boolean
  provider_quantity_matches?: boolean | null
}
export interface ReviewedSource {
  observation_id: string
  leg_id: string
  leg_key: string
  observation: EvidenceObservation
  leg: EvidenceLeg
  semantics: SourceSemantics
}
export interface SourceReview {
  id: string
  request_key: string
  supersedes_id: string | null
  created_at: string
  created_by: string | null
  payload: { request: SourceReviewRequest; sources: ReviewedSource[]; effects: SourceReviewEffects }
}
export interface SourceReviewPackage {
  revision: string
  target: EvidencePreview['target']
  legs: { leg_id: string; observation_ref: string; leg_key: string; transaction_id: string | null }[]
  reviews: SourceReview[]
}
export interface SourceReviewPreview {
  revision: string
  preview_digest: string
  target: EvidencePreview['target']
  request: SourceReviewRequest
  supported: boolean
  blockers: string[]
  effects: SourceReviewEffects
  sources: ReviewedSource[]
}
const headers = (workspaceId: string) => ({ 'X-Workspace-Id': workspaceId })
export const sourceReviews = {
  list: async (workspaceId: string, groupId: string, signal?: AbortSignal): Promise<SourceReviewPackage> => {
    const { data } = await api.get('/assets/evidence/source-reviews', { headers: headers(workspaceId), params: { group_id: groupId }, signal })
    return data
  },
  preview: async (workspaceId: string, request: SourceReviewRequest): Promise<SourceReviewPreview> => {
    const { data } = await api.post('/assets/evidence/source-reviews/preview', request, { headers: headers(workspaceId) })
    return data
  },
  confirm: async (workspaceId: string, preview: SourceReviewPreview): Promise<SourceReviewPackage> => {
    const { data } = await api.post('/assets/evidence/source-reviews/confirm', { request: preview.request, expected_revision: preview.revision, preview_digest: preview.preview_digest }, { headers: headers(workspaceId) })
    return data
  },
  export: async (workspaceId: string, groupId: string, revision: string): Promise<Blob> => {
    const { data } = await api.get('/assets/evidence/source-reviews/export', { headers: headers(workspaceId), params: { group_id: groupId, expected_revision: revision }, responseType: 'blob' })
    return data
  },
}
