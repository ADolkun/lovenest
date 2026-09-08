import api from './api'
import type { AssetGroup } from '@/types'
import type { TimelineEvent, TimelineRead, TimelineSourceDetail } from '@/types/timeline'

export interface TimelineFilters {
  collection_id?: string
  group_id?: string
  asset_id?: string
  canonical_asset_key?: string
  source?: string
  status?: string
  kind?: string
  direction?: string
  since?: string
  until?: string
  limit?: number
  offset?: number
  expected_revision?: string
}

const headers = (workspaceId: string) => ({ 'X-Workspace-Id': workspaceId })

export const timeline = {
  wallets: async (workspaceId: string, signal?: AbortSignal): Promise<AssetGroup[]> => {
    const { data } = await api.get('/asset-groups', { headers: headers(workspaceId), params: { include_empty: true }, signal })
    return data
  },
  list: async (workspaceId: string, filters: TimelineFilters, signal?: AbortSignal): Promise<TimelineRead> => {
    const { data } = await api.get('/assets/timeline', { headers: headers(workspaceId), params: filters, signal })
    return data
  },
  event: async (workspaceId: string, eventId: string, filters: TimelineFilters, signal?: AbortSignal): Promise<TimelineEvent> => {
    const { data } = await api.get(`/assets/timeline/${encodeURIComponent(eventId)}`, { headers: headers(workspaceId), params: filters, signal })
    return data
  },
  source: async (workspaceId: string, sourceId: string, filters: TimelineFilters, signal?: AbortSignal): Promise<TimelineSourceDetail> => {
    const { data } = await api.get(`/assets/timeline/sources/${encodeURIComponent(sourceId)}`, { headers: headers(workspaceId), params: filters, signal })
    return data
  },
}
