import api from '@/lib/api'
import type { AssetGroup } from '@/types'
import type { EvidencePreview } from '@/types/investment-evidence'
import type { RecoveryEntryInput, RecoveryPackage, RecoveryReviewInput, RecoveryRole, RecoveryState, AssertionStatus } from '@/types/recovery-evidence'

export type RecoveryFilters = { group_id: string; case_key?: string; round_key?: string; role?: RecoveryRole; state?: RecoveryState | AssertionStatus; q?: string }
const headers = (workspaceId: string) => ({ 'X-Workspace-Id': workspaceId })
export const recovery = {
  wallets: async (workspaceId: string, signal?: AbortSignal): Promise<AssetGroup[]> => {
    const { data } = await api.get('/asset-groups', { headers: headers(workspaceId), signal })
    return data
  },
  sources: async (workspaceId: string, groupId: string, signal?: AbortSignal): Promise<EvidencePreview> => {
    const { data } = await api.get('/assets/evidence', { headers: headers(workspaceId), params: { group_id: groupId }, signal })
    return data
  },
  list: async (workspaceId: string, filters: RecoveryFilters, signal?: AbortSignal): Promise<RecoveryPackage> => {
    const { data } = await api.get('/assets/recovery', { headers: headers(workspaceId), params: filters, signal })
    return data
  },
  preview: async (workspaceId: string, groupId: string, entries: RecoveryEntryInput[]): Promise<RecoveryPackage> => {
    const { data } = await api.post('/assets/recovery/preview', { group_id: groupId, entries }, { headers: headers(workspaceId) })
    return data
  },
  retain: async (workspaceId: string, groupId: string, entries: RecoveryEntryInput[], revision: string): Promise<RecoveryPackage> => {
    const { data } = await api.post('/assets/recovery/retain', { group_id: groupId, entries, expected_revision: revision }, { headers: headers(workspaceId) })
    return data
  },
  review: async (workspaceId: string, groupId: string, reviews: RecoveryReviewInput[], revision: string): Promise<RecoveryPackage> => {
    const { data } = await api.post('/assets/recovery/reviews', { group_id: groupId, reviews, expected_revision: revision }, { headers: headers(workspaceId) })
    return data
  },
  export: async (workspaceId: string, filters: RecoveryFilters, revision: string, format: 'json' | 'csv'): Promise<Blob> => {
    const { data } = await api.get('/assets/recovery/export', { headers: headers(workspaceId), params: { ...filters, expected_revision: revision, format }, responseType: 'blob' })
    return data
  },
}
