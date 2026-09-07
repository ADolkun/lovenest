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
