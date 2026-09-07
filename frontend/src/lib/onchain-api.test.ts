import type { AxiosAdapter } from 'axios'
import { afterEach, expect, it, vi } from 'vitest'
import api, { onchain, WORKSPACE_STORAGE_KEY } from '@/lib/api'

const originalAdapter = api.defaults.adapter
afterEach(() => { api.defaults.adapter = originalAdapter })

it('keeps wallet requests in their originating workspace across an interceptor race', async () => {
  const seen: Array<{ url?: string; workspace: unknown; continuation: unknown; data: unknown }> = []
  api.defaults.adapter = vi.fn<AxiosAdapter>(async (config) => {
    seen.push({ url: config.url, workspace: config.headers.get('X-Workspace-Id'), continuation: config.headers.get('X-Trace-Continuation'), data: config.data })
    return { data: [], status: 200, statusText: 'OK', headers: {}, config }
  })
  localStorage.setItem(WORKSPACE_STORAGE_KEY, 'investment')
  const pending = [
    onchain.addresses('investment'),
    onchain.chains('investment'),
    onchain.trace({ chain: 'solana', address: 'test-address' }, 'investment'),
    onchain.checkpoint('synthetic-checkpoint-token', 'investment'),
    onchain.trace({ chain: 'solana', address: 'test-address', continuation_token: 'synthetic-checkpoint-token' }, 'investment'),
  ]
  // Axios interceptors execute asynchronously, after the selection changes.
  localStorage.setItem(WORKSPACE_STORAGE_KEY, 'household')
  await Promise.all(pending)
  expect(seen.map((request) => request.workspace)).toEqual(Array(5).fill('investment'))
  expect(seen[3]).toMatchObject({ url: '/onchain/trace/checkpoint', continuation: 'synthetic-checkpoint-token' })
  expect(JSON.parse(String(seen[4].data))).toMatchObject({ continuation_token: 'synthetic-checkpoint-token' })
  await api.get('/accounts')
  expect(seen.at(-1)?.workspace).toBe('household')
})

it('scopes history requests and exports untouched bytes containing integers beyond JS precision', async () => {
  const raw = '{"raw":9007199254740993,"quantity":"9007199254740993"}'
  const blob = new Blob([raw], { type: 'application/json' })
  const seen: Array<{ url?: string; workspace: unknown; responseType?: string }> = []
  api.defaults.adapter = vi.fn<AxiosAdapter>(async (config) => {
    seen.push({ url: config.url, workspace: config.headers.get('X-Workspace-Id'), responseType: config.responseType })
    return { data: config.responseType === 'blob' ? blob : [], status: 200, statusText: 'OK', headers: {}, config }
  })
  const pending = [
    onchain.collectHistory({ connection_id: 'connection-A', chain: 'solana', address: 'owner-A', ownership_confirmed: true }, 'investment'),
    onchain.histories('investment', 'connection-A', 'owner-A'),
    onchain.history('archive-A', 'investment'),
    onchain.exportHistory('archive-A', 'investment'),
  ]
  localStorage.setItem(WORKSPACE_STORAGE_KEY, 'household')
  const values = await Promise.all(pending)
  expect(seen.map((entry) => entry.workspace)).toEqual(Array(4).fill('investment'))
  expect(seen.at(-1)).toMatchObject({ url: '/onchain/history/archive-A/export', responseType: 'blob' })
  expect(values.at(-1)).toBe(blob)
  const text = await new Promise((resolve) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.readAsText(values.at(-1) as Blob) })
  expect(text).toBe(raw)
})

it('filters saved histories locally without putting private addresses in request URLs', async () => {
  const summaries = ['owner-A', 'owner-B'].map((address) => ({ collection_id: address, request: { address } }))
  api.defaults.adapter = vi.fn<AxiosAdapter>(async (config) => {
    expect(config.url).toBe('/onchain/history')
    expect(config.params).toEqual({ connection_id: 'connection-A' })
    expect(api.getUri(config)).not.toContain('owner-A')
    return { data: summaries, status: 200, statusText: 'OK', headers: {}, config }
  })
  expect(await onchain.histories('investment', 'connection-A', 'owner-A')).toEqual([summaries[0]])
})
