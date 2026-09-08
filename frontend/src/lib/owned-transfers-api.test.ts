// @vitest-environment jsdom
import type { AxiosAdapter } from 'axios'
import { afterEach, expect, it, vi } from 'vitest'
import api, { ownedTransfers, WORKSPACE_STORAGE_KEY } from './api'
import type { MovementRequest, TransferRequest } from '@/types/owned-transfers'

const originalAdapter = api.defaults.adapter
afterEach(() => { api.defaults.adapter = originalAdapter; localStorage.clear() })

it('pins reads and financial decisions to their originating workspace and preserves exact strings', async () => {
  const seen: Array<{ url?: string; method?: string; workspace: unknown; data: unknown; params: unknown }> = []
  api.defaults.adapter = vi.fn<AxiosAdapter>(async (config) => {
    seen.push({ url: config.url, method: config.method, workspace: config.headers.get('X-Workspace-Id'), data: typeof config.data === 'string' ? JSON.parse(config.data) : config.data, params: config.params })
    return { data: { revision: 'r' }, status: 200, statusText: 'OK', headers: {}, config }
  })
  const allocation = { lot_id: 'source-fragment', quantity: '9007199254740993.000000000000000001' }
  const movement: MovementRequest = { leg_id: 'out', asset_id: 'source', ownership_id: 'assertion-a', allocations: [allocation], reason: 'Reviewed source', ordering_reviewed: true }
  const transfer: TransferRequest = { out_leg_id: 'out', in_leg_id: 'in', source_asset_id: 'source', destination_asset_id: 'destination', source_ownership_id: 'assertion-a', destination_ownership_id: 'assertion-b', allocations: [allocation], fees: [], reason: 'Reviewed source', ordering_reviewed: true }
  const ownership = { group_id: 'wallet', beneficial_owner: 'same-owner', chain: 'solana', address: 'invented-address', source_account_id: null, valid_from: null, valid_until: null, reason: 'Reviewed', evidence_observation_ids: [] }
  const incident = { leg_id: 'out', allegation: 'reported_scam' as const, source_status: 'user_reported' as const, note: 'Unresolved allegation', evidence_observation_ids: [], related_fee_leg_ids: [] }
  const pending = [
    ownedTransfers.index('investment'), ownedTransfers.lots('investment', 'source', 'out'),
    ownedTransfers.createOwnership('investment', ownership), ownedTransfers.revokeOwnership('investment', 'assertion-a', 'r'),
    ownedTransfers.preview('investment', transfer), ownedTransfers.confirm('investment', { ...transfer, expected_revision: 'r' }),
    ownedTransfers.detail('investment', 'transfer'), ownedTransfers.reverse('investment', 'transfer', 'r'),
    ownedTransfers.previewMovement('investment', movement), ownedTransfers.applyMovement('investment', { ...movement, expected_revision: 'r' }),
    ownedTransfers.reverseMovement('investment', 'application', 'r'), ownedTransfers.annotate('investment', incident), ownedTransfers.annotate('investment', incident, 'incident'),
  ]
  localStorage.setItem(WORKSPACE_STORAGE_KEY, 'other-workspace')
  await Promise.all(pending)
  expect(seen.map((request) => request.workspace)).toEqual(Array(13).fill('investment'))
  expect(seen.find((request) => request.url === '/assets/evidence/transfers' && request.method === 'post')?.data).toEqual({ ...transfer, expected_revision: 'r' })
  expect(seen.find((request) => request.url === '/assets/evidence/movements' && request.method === 'post')?.data).toEqual({ ...movement, expected_revision: 'r' })
  expect(seen[1].params).toEqual({ asset_id: 'source', before_leg_id: 'out' })
  expect(seen[7]).toMatchObject({ url: '/assets/evidence/transfers/transfer', method: 'delete', params: { expected_revision: 'r' } })
  expect(seen[12]).toMatchObject({ url: '/assets/evidence/incidents/incident', method: 'put' })
  expect(seen.some((request) => request.url?.includes('/onchain/'))).toBe(false)
})
