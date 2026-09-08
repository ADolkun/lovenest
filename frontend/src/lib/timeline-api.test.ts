// @vitest-environment jsdom
import type { AxiosAdapter } from 'axios'
import { afterEach, expect, it, vi } from 'vitest'
import api, { WORKSPACE_STORAGE_KEY } from './api'
import { timeline } from './timeline-api'

const originalAdapter = api.defaults.adapter
afterEach(() => { api.defaults.adapter = originalAdapter; localStorage.clear() })

it('uses read-only workspace-pinned endpoints and preserves opaque IDs, filters and exact source bytes', async () => {
  const seen: { url?: string; method?: string; workspace: unknown; params: unknown; signal: unknown }[] = []
  const exact = '{"raw_units":18446744073709551615}'
  api.defaults.adapter = vi.fn<AxiosAdapter>(async (config) => {
    seen.push({ url: config.url, method: config.method, workspace: config.headers.get('X-Workspace-Id'), params: config.params, signal: config.signal })
    return { data: JSON.stringify({ quantity: '9007199254740993.123456789012345678', raw_payload: { encoding: 'json', json: exact } }), status: 200, statusText: 'OK', headers: {}, config }
  })
  const filters = { collection_id: 'collection-a', group_id: 'wallet-a', canonical_asset_key: 'chain:program:mint', source: 'synthetic source', since: '2025-01-01T00:00:00Z', until: '2025-01-02T23:59:59.999999Z', offset: 25, limit: 25, expected_revision: 'revision-a' }
  const signal = new AbortController().signal
  const requests = [timeline.list('workspace-a', filters, signal), timeline.event('workspace-a', 'event:opaque/value', filters, signal), timeline.source('workspace-a', 'source:opaque/value', filters, signal), timeline.wallets('workspace-a', signal)]
  localStorage.setItem(WORKSPACE_STORAGE_KEY, 'workspace-b')
  const responses = await Promise.all(requests)
  expect(seen.map((request) => request.method)).toEqual(['get', 'get', 'get', 'get'])
  expect(seen.map((request) => request.workspace)).toEqual(['workspace-a', 'workspace-a', 'workspace-a', 'workspace-a'])
  expect(seen.map((request) => request.url)).toEqual(['/assets/timeline', '/assets/timeline/event%3Aopaque%2Fvalue', '/assets/timeline/sources/source%3Aopaque%2Fvalue', '/asset-groups'])
  expect(seen.every((request) => request.signal === signal)).toBe(true)
  expect(seen.map((request) => request.params)).toEqual([filters, filters, filters, { include_empty: true }])
  expect(responses[2]).toMatchObject({ quantity: '9007199254740993.123456789012345678', raw_payload: { json: exact } })
})
