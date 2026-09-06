import type { AxiosAdapter } from 'axios'
import { afterEach, expect, it, vi } from 'vitest'
import api, { onchain, WORKSPACE_STORAGE_KEY } from '@/lib/api'

const originalAdapter = api.defaults.adapter
afterEach(() => { api.defaults.adapter = originalAdapter })

it('keeps wallet requests in their originating workspace across an interceptor race', async () => {
  const seen: Array<{ url?: string; workspace: unknown }> = []
  api.defaults.adapter = vi.fn<AxiosAdapter>(async (config) => {
    seen.push({ url: config.url, workspace: config.headers.get('X-Workspace-Id') })
    return { data: [], status: 200, statusText: 'OK', headers: {}, config }
  })
  localStorage.setItem(WORKSPACE_STORAGE_KEY, 'investment')
  const pending = [
    onchain.addresses('investment'),
    onchain.chains('investment'),
    onchain.trace({ chain: 'solana', address: 'test-address' }, 'investment'),
  ]
  // Axios interceptors execute asynchronously, after the selection changes.
  localStorage.setItem(WORKSPACE_STORAGE_KEY, 'household')
  await Promise.all(pending)
  expect(seen.map((request) => request.workspace)).toEqual(['investment', 'investment', 'investment'])
  await api.get('/accounts')
  expect(seen.at(-1)?.workspace).toBe('household')
})
